"""Regression tests for constraints and historical quantity matching."""
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

from basket import BasketEngine, History, Order, Prior, Product, Ranker


def item(sku, group, price, grams, claims=()):
    return Product(sku, sku, 'food', group, price, 'g', grams, grams, str(grams), 'fixed', frozenset(claims))


class BasketTest(unittest.TestCase):
    def setUp(self):
        self.products = {
            product.id: product for product in [
                item("apple1000", "Яблоки", 210, 1000),
                item("apple500", "Яблоки", 100, 500),
                item("milk", "Молоко", 110, 500, ["без лактозы"]),
                item("milk_alt", "Молоко", 120, 500),
                item("cereal", "Хлопья", 250, 400),
                item("oats", "Хлопья", 60, 400),
            ]
        }
        self.at = datetime(2026, 11, 20, tzinfo=timezone.utc)
        self.history = History()
        self.order = Order('o1', 'u1', self.at, {'apple1000': 1, 'milk': 1, 'cereal': 1}, 570)
        self.history.add(self.order, self.products)
        self.engine = BasketEngine(self.products, Prior(Counter(), Counter(), Counter(), 0), Ranker(0, []))

    def test_replace_with_two_packs_of_half_size(self):
        history = History()
        history.add(Order('o1', 'u1', self.at, {'apple1000': 1}, 210), self.products)
        engine = BasketEngine({sku: self.products[sku] for sku in ('apple1000', 'apple500')},
                              Prior(Counter(), Counter(), Counter(), 0), Ranker(0, []))
        result = engine.predict(history, self.at + timedelta(days=7),
                                allowed={"apple1000", "apple500"}, unavailable=["apple1000"], size=1)
        self.assertEqual(result["items"][0]["product_id"], "apple500")
        self.assertEqual(result["items"][0]["quantity"], 2)
        self.assertEqual(result["items"][0]["physical_min"], 1000)
        self.assertEqual(result["items"][0]["replaces_product_id"], "apple1000")

    def test_budget_reserves_enough_to_keep_three_distinct_groups(self):
        ranked = [(self.products[sku], score) for sku, score in [
            ("cereal", 10), ("apple1000", 8), ("milk", 3), ("apple500", 2), ("oats", 1)]]
        result = self.engine.assemble(self.history, ranked, budget=270, size=3)
        self.assertEqual(result["status"], "complete")
        self.assertEqual({line["product_id"] for line in result["items"]}, {"oats", "apple500", "milk"})
        self.assertLessEqual(result["total_rub"], 270)

    def test_expanded_replacements_can_keep_group_under_budget(self):
        source = item("original", "Яблоки", 100, 500)
        choices = [item(str(price), "Яблоки", price, 500) for price in (111, 112, 113, 60)]
        catalog = {p.id: p for p in (source, *choices)}
        history = History()
        history.add(Order("o", "u", self.at, {"original": 1}, 100), catalog)
        engine = BasketEngine(catalog, Prior(Counter(), Counter(), Counter(), 0), Ranker(0, []))
        intended = engine.assemble(history, [(source, 5)], budget=110)
        available = {p.id for p in choices}
        closest = engine.resolve_availability(history, intended, available, budget=110)
        expanded = engine.resolve_availability(history, intended, available, budget=110, expand=True)
        self.assertEqual(closest["status"], "partial")
        self.assertEqual(expanded["status"], "complete")
        self.assertEqual(expanded["items"][0]["product_id"], "60")
        self.assertEqual(expanded["items"][0]["replaces_product_id"], "original")

    def test_cannot_see_order_at_forecast_time(self):
        with self.assertRaises(ValueError):
            self.engine.predict(self.history, self.at)

    def test_substitution_preserves_explicit_claim(self):
        self.assertEqual(self.engine._alternative(self.products["milk"], self.history, None), [])

    def test_empty_availability_returns_partial(self):
        result = self.engine.predict(self.history, self.at + timedelta(days=7), allowed=set())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["total_rub"], 0)


if __name__ == '__main__':
    unittest.main()
