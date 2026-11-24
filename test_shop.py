"""Regression checks for the v4 adapter, persistent cart and experiment log."""
from itertools import permutations
import csv
import io
import tempfile
from datetime import datetime, timedelta, timezone
from time import monotonic
from zipfile import ZipFile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from shop_service import ShopError
from pydantic import ValidationError
import app
import store


class ShopTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = patch.object(store, "DB", Path(tmp.name) / "shop.db")
        path.start()
        self.addCleanup(path.stop)
        # The trained predictor uses weekday and order time when sizing baskets.
        # Keep the seed history reproducible for tests with minimum page counts.
        anchor = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
        started = monotonic()
        self.enterContext(patch.object(store, "now", side_effect=lambda:
            anchor + timedelta(seconds=monotonic() - started)))
        store.initialize()
        self.counts = {u["id"]:len(app.orders(u["id"])) for u in store.PROFILES}
        self.assertTrue(all(n > 50 for n in self.counts.values()))

    def test_annual_history_and_upgrade_preserve_checkout(self):
        for profile in store.PROFILES:
            rows = app.orders(profile["id"])
            dates = [datetime.fromisoformat(row["ordered_at"]) for row in rows]
            self.assertGreaterEqual((dates[0] - dates[-1]).days, 345)
            self.assertTrue(all(at < store.now() for at in dates))
            self.assertTrue(all(row["total_rub"] == store.total(row["items"]) for row in rows))
            self.assertGreater(len({frozenset(row["items"]) for row in rows}), 20)

        cart = self.start(mode="ordinary")
        cart = self.change(cart, {next(iter(store.CATALOG)): 1})
        receipt = app.checkout(cart["id"], app.Checkout(revision=cart["revision"]))
        with store.database(True) as db:
            db.execute("DELETE FROM orders WHERE session_id IS NULL")
            store.save_order(db, "katya-01", "katya", store.now(), cart["items"], None)
            db.execute("PRAGMA user_version=0")
        store.initialize()
        after = app.orders("katya")
        self.assertIn(receipt["id"], [row["order_id"] for row in after])
        self.assertNotIn("katya-01", [row["order_id"] for row in after])
        self.assertEqual(len(after), self.counts["katya"] + 1)
        store.initialize()
        self.assertEqual(app.orders("katya"), after)

    def start(self, uid="katya", mode="personal"):
        return app.begin(uid, app.Start(mode=mode))

    def change(self, s, items, offer=None):
        return app.change_cart(s["id"], app.Change(revision=s["revision"], items=items, offer=offer))

    def test_personalizations_and_sku_contract(self):
        signatures = set()
        for u in app.users():
            s = self.start(u["id"])
            bundles = app.recommendations(s["id"])
            self.assertEqual(len(bundles), 2)
            self.assertFalse(bundles[0]["items"].keys() & bundles[1]["items"].keys())
            signatures.add(tuple(sorted(bundles[0]["items"])))
            for b in bundles:
                self.assertEqual(b["history_orders"], self.counts[u["id"]])
                self.assertEqual(b["total"], store.total(b["items"]))
                for sku in b["items"]:
                    self.assertEqual(store.PRODUCTS[sku]["price"], store.CATALOG[sku].price)
        self.assertEqual(len(signatures), 3)
        for p in app.catalog():
            self.assertTrue(p["image"])
            self.assertTrue((store.ROOT / "docs/assets" / (p["image"]+".jpg")).is_file())

    def test_full_csv_and_mixed_checkout(self):
        with ZipFile(store.MODEL / "catalog.zip") as archive, io.TextIOWrapper(
            archive.open("catalog.csv"), encoding="utf-8-sig"
        ) as source:
            rows = list(csv.DictReader(source))
        catalog = {p["id"]: p for p in app.catalog()}
        self.assertEqual(set(catalog), {r["product_id"] for r in rows})
        self.assertEqual(len(catalog), len(rows))
        with store.database() as db:
            self.assertEqual(set(catalog), {r[0] for r in db.execute("SELECT id FROM products")})
        carrot = next(p for p in catalog.values() if p["name"] == "Морковь")
        self.assertTrue(carrot["vegetable"])
        self.assertTrue(any(p["vegetable"] and p["group"] == "Огурцы" for p in catalog.values()))
        household = next(p for p in catalog.values() if p["department"] == "Для себя и дома" and p["available"])
        s = self.start()
        unavailable = next(p for p in catalog.values() if not p["available"])
        with self.assertRaises(ShopError):
            self.change(s, {unavailable["id"]: 1})
        items = {carrot["id"]: 2, household["id"]: 1}
        cart = self.change(s, items)
        app.checkout(s["id"], app.Checkout(revision=cart["revision"]))
        order = app.orders("katya")[0]
        self.assertEqual(order["items"], items)
        self.assertEqual(order["total_rub"], carrot["price"] * 2 + household["price"])
        model_order = store.model_orders([order])[0]
        self.assertEqual(model_order["items"], {carrot["id"]: 2})
        self.assertEqual(model_order["total_rub"], carrot["price"] * 2)
        self.assertEqual(app.recommendations(self.start()["id"])[0]["history_orders"], self.counts["katya"] + 1)

    def test_refresh_offers_new_products_without_changing_cart_or_history(self):
        for user in app.users():
            with self.subTest(user=user["id"]):
                sid = self.start(user["id"])["id"]
                first = app.recommendations(sid)
                previous = {sku for b in first for sku in b["items"]}
                cart = self.change(app.cart(sid), {next(iter(previous)): 1})
                refreshed = app.recommendations(sid, list(previous))
                self.assertEqual(len(refreshed), 2)
                for b in refreshed:
                    self.assertFalse(previous.intersection(b["items"]))
                    self.assertEqual(b["history_orders"], self.counts[user["id"]])
                self.assertEqual(cart, app.cart(sid))
                self.change(cart, refreshed[0]["items"], refreshed[0]["id"])
        ordinary = self.start(mode="ordinary")
        self.assertEqual(app.recommendations(ordinary["id"], []), [])

    def test_multiple_refreshes_never_repeat_a_composition(self):
        for user in app.users():
            with self.subTest(user=user["id"]):
                sid = self.start(user["id"])["id"]
                cart = app.cart(sid)
                page = app.current_recommendations(sid)
                seen = set()
                count = 0
                while True:
                    count += 1
                    self.assertEqual(len(page["bundles"]), 2)
                    left, right = page["bundles"]
                    self.assertTrue(left["items"].keys().isdisjoint(right["items"]))
                    for bundle in (left, right):
                        self.assertEqual(bundle["total"], store.total(bundle["items"]))
                    for bundle in page["bundles"]:
                        self.assertGreaterEqual(len(bundle["items"]), 3)
                        slots = store.MEALS[bundle["name"]]
                        self.assertEqual(len(slots), len(bundle["items"]))
                        self.assertTrue(any(all(role in store.meal_roles(sku)
                                                for role, sku in zip(slots, order))
                                            for order in permutations(bundle["items"])))
                        composition = frozenset(bundle["items"])
                        self.assertNotIn(composition, seen)
                        seen.add(composition)
                    self.assertEqual(app.current_recommendations(sid), page)
                    request = app.Refresh(version=page["version"])
                    if not page["has_more"]:
                        break
                    page = app.refresh_recommendations(sid, request)
                    self.assertEqual(app.refresh_recommendations(sid, request), page)
                self.assertGreaterEqual(count, 8)
                store.initialize()
                self.assertEqual(app.current_recommendations(sid), page)
                self.assertEqual(app.refresh_recommendations(sid, request), page)
                self.assertEqual(app.cart(sid), cart)
                bundle = page["bundles"][0]
                self.change(cart, bundle["items"], bundle["id"])

    def test_checkout_records_edits_and_feedback(self):
        s = self.start()
        b = app.recommendations(s["id"])[0]
        sku = next(iter(b["items"]))
        s = self.change(s, {sku: 1})
        edited = dict(b["items"])
        edited[sku] += 1
        app.log_edit(s["id"], app.Edit(offer=b["id"], before=b["items"], after=edited, action="quantity"))
        s = self.change(s, edited, b["id"])
        self.assertEqual(s["items"][sku], edited[sku]+1)
        app.record_visit(s["id"], app.Visit(visit_id=uuid4(), elapsed_ms=5))
        receipt = app.checkout(s["id"], app.Checkout(revision=s["revision"]))
        self.assertEqual(receipt, app.checkout(s["id"], app.Checkout(revision=s["revision"])))
        order = app.orders("katya")[0]
        self.assertEqual((order["items"],order["total_rub"]), (s["items"],s["total"]))
        self.assertEqual(len(app.orders("katya")), self.counts["katya"] + 1)
        nxt = self.start()
        self.assertFalse(nxt["items"])
        self.assertEqual(app.recommendations(nxt["id"])[0]["history_orders"], self.counts["katya"] + 1)
        events = [e for e in app.metrics() if e["session_id"]==s["id"]]
        self.assertEqual([e["action"] for e in events],
                         ["shopping_started","cart_changed","bundle_edited","bundle_added","checkout"])
        self.assertTrue(events[-2]["payload"]["edited"])
        self.assertGreaterEqual(events[-1]["payload"]["active_build_seconds"], 0)
        self.assertEqual(events[-1]["payload"]["total_rub"], receipt["total_rub"])

    def test_isolation_persistence_and_ordinary_checkout(self):
        s = self.start(mode="ordinary")
        self.assertEqual(app.recommendations(s["id"]), [])
        sku=next(iter(store.PRODUCTS))
        s=self.change(s,{sku:2})
        store.initialize()
        self.assertEqual(self.start(mode="ordinary"),s)
        self.assertEqual(self.start()["items"],{})
        self.assertEqual(self.start("ev","ordinary")["items"],{})
        app.checkout(s["id"],app.Checkout(revision=s["revision"]))
        self.assertEqual(app.recommendations(self.start()["id"])[0]["history_orders"], self.counts["katya"] + 1)
        self.assertEqual(len(app.orders("ev")),self.counts["ev"])

    def test_stale_edits_and_invalid_cart_are_atomic(self):
        s=self.start()
        sku=next(iter(store.PRODUCTS))
        updated=self.change(s,{sku:2})
        with self.assertRaises(ShopError) as e:
            self.change(s,{sku:1})
        self.assertEqual(e.exception.status_code,409)
        with self.assertRaises(ShopError):
            self.change(updated,{"unknown":1})
        self.assertEqual(app.cart(s["id"]),updated)
        for invalid in (0, -1, 1.5, True, 1000):
            with self.assertRaises(ValidationError):
                app.Change(revision=1,items={sku:invalid})
        empty=self.change(updated,{})
        with self.assertRaises(ShopError):
            app.checkout(s["id"],app.Checkout(revision=empty["revision"]))
        self.assertEqual(len(app.orders("katya")),self.counts["katya"])

    def test_new_purchases_change_predicted_products(self):
        user = "dima"
        personal = self.start(user)
        new_items = {sku: 2 for sku, p in store.PRODUCTS.items()
                     if p["image"] in ("greens", "salmon", "eggs")}
        previous = app.recommendations(personal["id"])[0]["items"]
        self.assertFalse(previous.keys() & new_items.keys())
        for _ in range(4):
            cart = self.change(self.start(user, "ordinary"), new_items)
            app.checkout(cart["id"], app.Checkout(revision=cart["revision"]))
        updated = app.recommendations(personal["id"])[0]
        self.assertEqual(updated["history_orders"], self.counts[user] + 4)
        self.assertTrue(updated["items"].keys() & new_items.keys())

    def test_offer_is_bound_to_user_and_mode(self):
        s=self.start()
        b=app.recommendations(s["id"])[0]
        for other in (self.start("ev"),self.start(mode="ordinary")):
            with self.assertRaises(ShopError):
                self.change(other,b["items"],b["id"])
            self.assertFalse(app.cart(other["id"])["items"])

    def test_active_time_merges_overlapping_tabs(self):
        s=self.start()
        with store.database(True) as db:
            db.executemany("INSERT INTO visits VALUES (?,?,?,?)", [
                (s["id"], "a", 0, 1000), (s["id"], "b", 500, 1700),
                (s["id"], "c", 17000, 19000)])
            self.assertEqual(app.service.active_seconds(db,s["id"]),3.7)
