"""Local shop data and the adapter to the supplied v4 predictor."""
from demo_history import annual_orders

import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from itertools import combinations, product
from math import log1p
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
MODEL = ROOT / "ml_v4"
sys.path.insert(0, str(MODEL))
from adapter_example import build_engine, read_dataset
from basket import History, Order

CATALOG, _ = read_dataset(MODEL / "catalog.zip", include_orders=False)
ENGINE = build_engine(MODEL, CATALOG)


def load_products():
    featured = {p["id"]: p for p in json.loads((ROOT / "data/assortment.json").read_text())}
    photos = json.loads((ROOT / "data/catalog-photos.json").read_text())
    labels = {"Хлеб и выпечка": "Выпечка", "Молоко, яйца и сыр": "Молочное",
              "Вода и напитки": "Напитки", "Снеки": "Перекусы"}
    products = {}
    with ZipFile(MODEL / "catalog.zip") as archive, io.TextIOWrapper(
        archive.open("catalog.csv"), encoding="utf-8-sig"
    ) as stream:
        for row in csv.DictReader(stream):
            sku = row["product_id"]
            department = row["product_category"]
            sections = row["site_sections"].split(" | ")
            available = bool(row["price_rub"]) and row["listed_as_orderable"] == "True"
            products[sku] = {
                "id": sku, "name": row["name"], "unit": row["pack_label"],
                "price": int(row["price_rub"] or row["observed_price_rub"]),
                "available": available, "old_price": int(row["old_price_rub"] or 0) or None,
                "category": labels.get(department, department), "department": department,
                "group": row["product_group"], "sections": sections,
                "vegetable": department == "Овощи и фрукты" and (
                    "Овощи, грибы и зелень" in sections or row["product_group"] == "Помидоры"),
                "new": "Новинки и хиты" in sections, **featured.get(sku, {}),
            }
            if sku in photos:
                products[sku]["image"] = "catalog/" + photos[sku]["file"]
                products[sku]["illustrative_photo"] = photos[sku]["illustrative"]
    return {**{sku: products[sku] for sku in featured}, **products}


PRODUCTS = load_products()
PROFILES = json.loads((ROOT / "data/profiles.json").read_text())
DB = Path(os.environ.get("SAMOKAT_DB", ROOT / "runtime/shop.sqlite3"))


def now():
    return datetime.now(timezone.utc)


def pack(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


@contextmanager
def database(write=False):
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield con
        con.commit()
    finally:
        con.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT, occupation TEXT, habit TEXT);
CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, name TEXT, price INTEGER);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), mode TEXT, started_at TEXT,
 cart TEXT DEFAULT '{}', revision INTEGER DEFAULT 0, closed INTEGER DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS active_session ON sessions(user_id,mode) WHERE closed=0;
CREATE TABLE IF NOT EXISTS visits (
 session_id TEXT REFERENCES sessions(id), visit_id TEXT, start_ms INTEGER, end_ms INTEGER,
 PRIMARY KEY(session_id,visit_id));
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), ordered_at TEXT, total_rub INTEGER,
 items TEXT, prices TEXT, session_id TEXT UNIQUE REFERENCES sessions(id));
CREATE INDEX IF NOT EXISTS user_history ON orders(user_id,ordered_at);
CREATE TABLE IF NOT EXISTS offers (
 id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), name TEXT, items TEXT);
CREATE TABLE IF NOT EXISTS recommendation_pages (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), latest_order TEXT,
 position INTEGER NOT NULL, pages TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, session_id TEXT REFERENCES sessions(id), at TEXT,
 action TEXT, payload TEXT);
"""


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with database(write=True) as db:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        db.executemany("INSERT OR REPLACE INTO products VALUES (?,?,?)",
                       [(p["id"], p["name"], p["price"]) for p in PRODUCTS.values()])
        if db.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        end = now()
        for user in PROFILES:
            uid = user["id"]
            db.execute("INSERT OR IGNORE INTO users VALUES (?,?,?,?)",
                       tuple(user[k] for k in ("id", "name", "occupation", "habit")))
            db.executemany("DELETE FROM orders WHERE id=? AND user_id=? AND session_id IS NULL",
                           [(f"{uid}-{i:02d}", uid) for i in range(1, 11)])
            for at, items in annual_orders(uid, end, PRODUCTS, CATALOG):
                save_order(db, f"{uid}-year-{at.date()}", uid, at, items, None)
        db.execute("DELETE FROM recommendation_pages")
        db.execute("PRAGMA user_version=1")


def needs_history_update():
    with database() as db:
        return db.execute("PRAGMA user_version").fetchone()[0] < 1


def total(items):
    return sum(PRODUCTS[sku]["price"] * qty for sku, qty in items.items())


def save_order(db, oid, uid, at, items, session):
    prices = {sku: PRODUCTS[sku]["price"] for sku in items}
    db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
               (oid, uid, at.isoformat(), total(items), pack(items), pack(prices), session))


def history(db, uid):
    rows = db.execute("SELECT * FROM orders WHERE user_id=? ORDER BY ordered_at DESC,id", (uid,))
    return [dict(order_id=r["id"], user_id=uid, ordered_at=r["ordered_at"],
                 total_rub=r["total_rub"], items=json.loads(r["items"]),
                 prices=json.loads(r["prices"]), demo_seed=r["session_id"] is None) for r in rows]


# Each slot describes a different use in a meal. Product IDs and profiles are
# deliberately absent: the shopper's own orders supply the candidate products.
MEALS = {
    "Каша с молоком и фруктами": ("cereal", "milk", "fruit"),
    "К семейному завтраку": ("bread", "eggs", "dairy"),
    "На завтрак с фруктами": ("breakfast", "dairy", "fruit"),
    "Для домашнего ужина": ("protein", "starch", "vegetable"),
    "Рыба с яйцом и зеленью": ("fish", "eggs", "vegetable"),
    "На обед": ("lunch", "side", "drink"),
    "На обед с выпечкой": ("lunch", "pastry", "drink"),
    "К кино": ("pizza", "movie_snack", "cold_drink"),
    "На быстрый перекус": ("quick_meal", "snack", "cold_drink"),
}

GROUP_ROLES = {
    "Молоко коровье": ("milk", "dairy"), "Йогурт": ("dairy",), "Творог": ("dairy",),
    "Хлеб": ("bread", "breakfast"), "Яйца куриные": ("eggs", "breakfast"),
    "Овсяные хлопья": ("cereal", "breakfast"), "Готовые сырники": ("breakfast",),
    "Яблоки": ("fruit",), "Бананы": ("fruit",), "Мандарины": ("fruit",),
    "Куриное филе": ("protein",), "Сухие макароны": ("starch",), "Картофель": ("starch",),
    "Огурцы": ("vegetable",), "Морковь": ("vegetable",), "Помидоры": ("vegetable",),
    "Готовые супы": ("lunch",), "Готовая паста и лазанья": ("lunch",),
    "Бургеры и сэндвичи": ("lunch",), "Готовые салаты": ("side",),
    "Круассаны": ("pastry",), "Вода без газа": ("drink", "cold_drink"),
    "Кола": ("cold_drink",), "Замороженная пицца": ("pizza",),
    "Замороженные пельмени": ("quick_meal",),
}


def meal_roles(sku):
    item = PRODUCTS[sku]
    name = item["name"].lower()
    if "лапша" in name and "быстрого приготовления" in name:
        return ("quick_meal",)
    if "кофейный напиток" in name:
        return ("drink",)
    if "попкорн" in name:
        return ("movie_snack",)
    if "чипсы" in name:
        return ("movie_snack", "snack")
    if "шоколадный батончик" in name:
        return ("snack",)
    if "салатный микс" in name:
        return ("vegetable",)
    if "форель" in name or "лосось" in name:
        if item["department"] == "Мясо и рыба":
            return ("fish", "protein")
    if item["group"] == "Готовые салаты" and ("смесь для" in name or "набор для" in name):
        return ()
    return GROUP_ROLES.get(item["group"], ())


def model_orders(rows):
    result = []
    for row in rows:
        items = {sku: qty for sku, qty in row["items"].items() if sku in CATALOG}
        if items:
            amount = sum(row["prices"][sku] * qty for sku, qty in items.items())
            result.append({**row, "items": items, "total_rub": amount})
    return result


def build_pages(uid, rows, excluded=()):
    rows = model_orders(rows)
    if not rows:
        return [[]]
    zone = ZoneInfo("Europe/Moscow")
    at = now().astimezone(zone)
    history = History()
    supports = Counter()
    recent = Counter()
    bought_together = Counter()
    role_pairs = Counter()
    for row in sorted(rows, key=lambda r: (r["ordered_at"], r["order_id"])):
        ordered = datetime.fromisoformat(row["ordered_at"]).astimezone(zone)
        if ordered >= at:
            continue
        history.add(Order(row["order_id"], uid, ordered, row["items"], row["total_rub"]), CATALOG)
        bought_together.update(combinations(sorted(row["items"]), 2))
        roles = {role for sku in row["items"] for role in meal_roles(sku)}
        role_pairs.update(combinations(sorted(roles), 2))
        age = (at - ordered).total_seconds() / 86400
        for title, slots in MEALS.items():
            if set(slots) <= roles:
                supports[title] += 1
                recent[title] += 1 / (1 + age / 7)
    if not history.orders:
        return [[]]

    available = set(history.sku).difference(excluded)
    ranked = ENGINE.base.ranked(history, at, allowed=available)
    prediction = ENGINE.predict(history, at)
    preferred = {row["product_id"] for row in prediction["items"]}
    by_role = defaultdict(list)
    scores = {}
    for rank, (item, _) in enumerate(ranked):
        sku = item.id
        scores[sku] = 1 / (1 + rank / 8) + (sku in preferred)
        for role in meal_roles(sku):
            by_role[role].append(sku)

    candidates = {}
    for title, slots in MEALS.items():
        # A shopper may buy a main with a drink one day and with a snack another.
        anchor_support = min(role_pairs[tuple(sorted((slots[0], side)))] for side in slots[1:])
        if not supports[title] and anchor_support < 2:
            continue
        pools = [sorted(by_role[role], key=scores.get, reverse=True)[:8] for role in slots]
        for skus in product(*pools):
            key = frozenset(skus)
            if len(key) != len(slots):
                continue
            support = [bought_together[pair] for pair in combinations(sorted(skus), 2)]
            if not any(support):
                continue
            score = (.35 * sum(log1p(count) for count in support) + sum(scores[k] for k in skus) / len(skus)
                     + log1p(recent[title]) + .15 * log1p(supports[title]))
            if key not in candidates or candidates[key][0] < score:
                candidates[key] = (score, title, skus)

    ranked_sets = sorted(candidates, key=lambda k: (-candidates[k][0], candidates[k][2]))
    pages = []
    exposure = Counter()
    while len(ranked_sets) >= 2 and len(pages) < 32:
        ranked_sets.sort(key=lambda k: sum(exposure[i] for i in k) * .4 - candidates[k][0])
        for index, left in enumerate(ranked_sets):
            choices = [k for k in ranked_sets[index+1:] if k.isdisjoint(left)]
            if choices:
                right = next((k for k in choices if candidates[k][1] != candidates[left][1]), choices[0])
                break
        else:
            break
        page = []
        for key in (left, right):
            _, name, skus = candidates[key]
            items = {k: ENGINE.base.quantity(CATALOG[k], history, 3) for k in skus}
            ident = hashlib.sha256(pack([uid, name, items]).encode()).hexdigest()[:24]
            page.append(dict(id=ident, name=name, items=items, total=total(items),
                             history_orders=history.orders, model_mode=prediction.get("mode", "history")))
            exposure.update(skus)
            ranked_sets.remove(key)
        pages.append(page)
    return pages or [[]]


def generate(uid, rows, excluded=()):
    return build_pages(uid, rows, excluded)[0]
