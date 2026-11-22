from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from basket import History, Order, Prior, Product, Ranker
from novelty import Discovery
from personalization import PersonalizedBasket, Policy, PriceTiers
from upgrade import EnhancedEngine


class PersonalizationTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,11,1,tzinfo=timezone.utc)
        self.catalog={
            str(i): Product(str(i),str(i),"drinks","",price,"ml",500,500,"500 мл","fixed",frozenset())
            for i,price in enumerate((39,69,99,149,199,249,299))
        }
        prior=Prior(Counter({sku:1 for sku in self.catalog}),Counter(),Counter(),7)
        base=EnhancedEngine(self.catalog,prior,Ranker(0,[]),{},settings={"source":"original"})
        self.model=PersonalizedBasket(Discovery(base,Ranker(0,[])),PriceTiers(self.catalog,{}),Policy("mean_all",1.1,0))
        self.history=History()
        for i in range(11):
            items={sku:1 for sku in (("2","3","4","5","6") if i<2 else ("2","3"))}
            self.history.add(Order(str(i),"u",self.start+timedelta(days=i),items,
                                   sum(self.catalog[sku].price for sku in items)),self.catalog)
        self.when=self.start+timedelta(days=11)

    def test_mean_uses_full_history(self):
        self.assertAlmostEqual(self.model.average_positions(self.history),28/11)
        self.assertEqual(self.model.target(self.history,self.when,Policy("mean_all"))[0],3)
        self.assertEqual(self.model.target(self.history,self.when,Policy("mean_recent"))[0],2)

    def test_budget_keeps_low_price_options_available(self):
        result=self.model.predict(self.history,self.when,size=1,budget=39,allowed={"0"})
        self.assertEqual(result["status"],"complete")
        self.assertEqual(result["total_rub"],39)
        self.assertEqual(result["items"][0]["product_id"],"0")

    def test_prediction_stays_within_personal_target(self):
        result = self.model.predict(self.history, self.when)
        self.assertEqual(result["target_positions"], 3)
        self.assertLessEqual(result["positions"], 3)

    def test_price_priority_prefers_expensive_product_at_equal_relevance(self):
        ranked = [(self.catalog[sku], 0.0, 0.0) for sku in ("2", "3")]
        signals = ranked, {}
        neutral = Policy(price_weight=0)
        priority = replace(neutral, price_priority=1.5)
        history = replace(self.history, amounts=[99])
        before = self.model.assemble(history, self.when, signals, neutral, size=1)
        after = self.model.assemble(history, self.when, signals, priority, size=1)
        limited = self.model.assemble(history, self.when, signals, priority, size=1, budget=99)
        self.assertEqual(before["total_rub"], 99)
        self.assertEqual(after["total_rub"], 149)
        self.assertEqual(after["positions"], 1)
        self.assertEqual(limited["total_rub"], 99)

    def test_tiers_compare_unit_price_in_same_type(self):
        options=dict(self.catalog)
        options["larger"]=replace(options["2"],id="larger",price=198,low=1000,high=1000)
        within_type=PriceTiers(options,{sku:"Вода" for sku in options})
        self.assertEqual(within_type.tier["2"],within_type.tier["larger"])
        self.assertLess(within_type.tier["0"],within_type.tier["3"])
        targets=self.model.tiers.personal_targets(self.history,self.catalog)
        self.assertGreater(self.model.tiers.preference(self.catalog["3"],self.history,targets),
                           self.model.tiers.preference(self.catalog["0"],self.history,targets))

    def test_periodic_pair_reappears_only_near_its_window(self):
        self.model.policy = replace(self.model.policy, periodic_orders=True)
        for days in (7, 14):
            with self.subTest(interval=days):
                history = History()
                for i in range(3):
                    at = self.start + timedelta(days=i * days, hours=19)
                    history.add(Order(f"r{i}", "u", at, {"2": 1, "3": 1}, 248), self.catalog)
                    history.add(Order(f"b{i}", "u", at + timedelta(days=i + 1), {"0": 1, str(i + 4): 1},
                                      39 + self.catalog[str(i + 4)].price), self.catalog)
                expected = self.start + timedelta(days=3 * days, hours=19)
                due = self.model.predict(history, expected)
                self.assertEqual(due["mode"], "periodic")
                self.assertEqual(due["routines_due"][0]["interval_days"], days)
                self.assertEqual({row["product_id"] for row in due["items"]}, {"2", "3"})
                for off_at in (expected - timedelta(days=3), expected - timedelta(minutes=90),
                               expected + timedelta(minutes=90)):
                    outside = self.model.predict(history, off_at)
                    self.assertEqual(outside["mode"], "irregular")
                    self.assertFalse({row["product_id"] for row in outside["items"]} & {"2", "3"})
                limited = self.model.predict(history, expected, budget=130)
                self.assertEqual(limited["routines_due"], [])
                self.assertLessEqual(limited["total_rub"], 130)

    def test_calendar_tolerates_one_outlier_and_one_missed_week(self):
        self.model.policy = replace(self.model.policy, periodic_orders=True)
        cases = (
            ([(0, 19), (7, 19), (14, 14), (21, 19), (28, 19)], 35, 19, 4),
            ([(0, 19), (7, 19), (21, 19), (28, 19)], 35, 19, 4),
            ([(0, 19), (7, 19), (15, 19), (21, 19), (28, 19)], 35, 19, 4),
            ([(0, 19), (7, 19), (15, 14), (23, 14), (28, 19)], 35, 19, 0),
            ([(0, 19), (7, 19), (14, 19), (21, 14), (28, 14), (35, 14)], 42, 14, 3),
        )
        for times, day, hour, support in cases:
            with self.subTest(times=times):
                history = History()
                for index, (day_offset, order_hour) in enumerate(times):
                    at = self.start + timedelta(days=day_offset, hours=order_hour)
                    history.add(Order(str(index), "u", at, {"2": 1, "3": 1}, 248), self.catalog)
                expected = self.start + timedelta(days=day, hours=hour)
                result = self.model.predict(history, expected)
                outside = self.model.predict(history, expected + timedelta(hours=3))
                self.assertEqual(outside["routines_due"], [])
                if support:
                    self.assertEqual(result["mode"], "periodic")
                    self.assertEqual(result["routines_due"][0]["interval_days"], 7)
                    self.assertEqual(result["routines_due"][0]["occurrences"], support)
                    self.assertEqual({row["product_id"] for row in result["items"]}, {"2", "3"})
                else:
                    self.assertEqual(result["routines_detected"], 0)

    def test_periodic_size_and_quantities_come_from_the_periodic_orders(self):
        self.model.policy = replace(self.model.policy, periodic_orders=True)
        history = History()
        stock_up = {"2": 2, "3": 1, "4": 1, "5": 1}
        stock_spend = sum(self.catalog[sku].price * qty for sku, qty in stock_up.items())
        for week in range(3):
            at = self.start + timedelta(days=7 * week, hours=19)
            history.add(Order(f"r{week}", "u", at, stock_up, stock_spend), self.catalog)
            for day in (1, 3, 4):
                history.add(Order(f"b{week}-{day}", "u", at + timedelta(days=day), {"2": 1}, 99), self.catalog)
        at = self.start + timedelta(days=21, hours=19)
        result = self.model.predict(history, at)
        self.assertEqual(self.model.target(history, at)[0], 2)
        self.assertEqual(result["target_positions"], 4)
        self.assertEqual(result["target_spend_rub"], stock_spend)
        self.assertEqual({row["product_id"]: row["quantity"] for row in result["items"]}, stock_up)
        for constrained in (self.model.predict(history, at, size=2),
                            self.model.predict(history, at, budget=200)):
            self.assertEqual(constrained["routines_due"], [])
        self.assertLessEqual(self.model.predict(history, at, budget=200)["total_rub"], 200)

    def test_off_window_size_and_check_use_whole_irregular_orders(self):
        self.model.policy = replace(self.model.policy, periodic_orders=True, spend_uplift=.04)
        history = History()
        for week, day in enumerate((1, 2, 4)):
            at = self.start + timedelta(days=7 * week, hours=19)
            regular = {sku: 1 for sku in ("2", "3", "4", "5")}
            if week < 2:
                regular["6"] = 1  # A regular-order extra may inform ranking but not the target.
            history.add(Order(f"r{week}", "u", at, regular,
                              sum(self.catalog[sku].price for sku in regular)), self.catalog)
            history.add(Order(f"i{week}", "u", at + timedelta(days=day), {"0": 1, "1": 1}, 108),
                        self.catalog)
        result = self.model.predict(history, self.start + timedelta(days=20, hours=19))
        self.assertEqual(result["mode"], "irregular")
        self.assertEqual(result["target_positions"], 2)
        self.assertEqual(result["target_spend_rub"], 112)
        self.assertEqual(result["positions"], 2)

    def test_clock_window_follows_a_gradual_shift_with_a_noisy_last_order(self):
        self.model.policy = replace(self.model.policy, periodic_orders=True)
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                history = History()
                for week, offset in enumerate((0, 5, -8, 0, 3, 62, 51)):
                    at = self.start + timedelta(days=7 * week, hours=10, minutes=direction * offset)
                    history.add(Order(str(week), "u", at, {"2": 1, "3": 1}, 248), self.catalog)
                old_time = self.start + timedelta(days=49, hours=10)
                new_time = old_time + timedelta(minutes=direction * 100)
                self.assertEqual(self.model.predict(history, old_time)["routines_due"], [])
                result = self.model.predict(history, new_time)
                self.assertEqual(result["mode"], "periodic")
                self.assertEqual(result["routines_due"][0]["interval_days"], 7)
                self.assertLessEqual(result["routines_due"][0]["window_minutes"], 120)

    def test_width_grows_for_recurring_variation_but_not_a_single_outlier(self):
        self.model.policy = replace(self.model.policy, periodic_orders=True)
        for hours, width in (((15, 15, 15, 15, 15), 60),
                             ((15, 15, 15, 11, 15), 60),
                             ((13, 16, 14, 18, 15), 360)):
            with self.subTest(hours=hours):
                history = History()
                for week, hour in enumerate(hours):
                    at = self.start + timedelta(days=7 * week, hours=hour)
                    history.add(Order(str(week), "u", at, {"2": 1, "3": 1}, 248), self.catalog)
                at = self.start + timedelta(days=35)
                due = self.model.predict(history, at + timedelta(hours=15))
                self.assertEqual(due["routines_due"][0]["window_minutes"], width)
                self.assertEqual(bool(self.model.predict(history, at + timedelta(hours=18))["routines_due"]),
                                 width > 120)
                self.assertFalse(self.model.predict(history, at + timedelta(days=1, hours=15))["routines_due"])

    def test_weekly_trend_ignores_a_late_last_purchase(self):
        self.model.policy = replace(self.model.policy, periodic_orders=True)
        for final_clock in (436, 660):  # 07:16 or an isolated purchase at 11:00.
            with self.subTest(final_clock=final_clock):
                history = History()
                for week, minutes in enumerate((740, 689, 626, 588, 531, 472, final_clock)):
                    at = self.start + timedelta(days=7 * week, minutes=minutes)
                    history.add(Order(str(week), "u", at, {"2": 1, "3": 1}, 248), self.catalog)
                date = self.start + timedelta(days=49)
                result = self.model.predict(history, date + timedelta(hours=6, minutes=20))
                self.assertEqual(result["mode"], "periodic")
                self.assertEqual(result["routines_due"][0]["interval_days"], 7)
                self.assertFalse(self.model.predict(history, date + timedelta(hours=11))["routines_due"])


if __name__ == "__main__":
    unittest.main()
