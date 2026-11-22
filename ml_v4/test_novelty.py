"""Regression checks for separate new-item suggestions."""
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

from basket import History, Order, Prior, Product, Ranker
from novelty import Discovery
from upgrade import EnhancedEngine


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {sku: Product(sku, sku, "drinks", "", 100, "ml", 500, 500, "500 мл",
                                     "fixed", frozenset())
                        for sku in ("seen", "unavailable", "in_basket", "recommendation")}
        prior = Prior(Counter({"seen": 30, "unavailable": 25, "in_basket": 20, "recommendation": 15}),
                      Counter(), Counter(), 40)
        base = EnhancedEngine(self.catalog, prior, Ranker(0, []), {},
                              settings={"source": "original"})
        self.recommender = Discovery(base, Ranker(0, []), suggestion_mode="category")
        self.history = History()
        first = datetime(2026, 11, 2, tzinfo=timezone.utc)
        self.history.add(Order("o1", "u1", first, {"seen": 1}, 100), self.catalog)
        self.at = first+timedelta(days=7)

    def test_suggestions_exclude_history_unavailable_and_selected_cart(self):
        rows = self.recommender.suggest(self.history, self.at, 3, unavailable=["unavailable"],
                                        basket=[{"product_id": "in_basket"}])
        self.assertEqual([row["product_id"] for row in rows], ["recommendation"])

    def test_explicit_empty_inventory_returns_no_suggestion(self):
        self.assertEqual(self.recommender.suggest(self.history, self.at, 3, available=[]), [])

    def test_mode_v2_keeps_original_output_and_budget(self):
        baseline = self.recommender.base.predict(self.history, self.at, size=1, budget=100)
        result = self.recommender.predict(self.history, self.at, size=1, budget=100, model="v2")
        self.assertEqual(baseline, result)
        self.assertLessEqual(result["total_rub"], 100)


if __name__ == "__main__":
    unittest.main()
