"""Basket size and price tiers derived from the customer's order history."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
import json
from pathlib import Path
from statistics import median

from basket import History
from novelty import load_discovery
from periodicity import find_routines, irregular_history_and_orders, product_key, rank_by_time


@dataclass(frozen=True)
class Policy:
    size_mode: str = "mean_all"
    price_weight: float = .5
    variety_weight: float = 0
    price_priority: float = 0
    spend_uplift: float = 0
    periodic_orders: bool = False


class PriceTiers:
    def __init__(self, catalog, type_by_sku):
        categories, groups = defaultdict(list), defaultdict(list)
        for sku, p in catalog.items():
            categories[p.category].append(p.price)
            label = type_by_sku.get(sku)
            if label:
                groups[label, p.unit].append(p.price / p.amount)
        for pool in (*categories.values(), *groups.values()):
            pool.sort()
        self.tier = {}
        for sku, p in catalog.items():
            label = type_by_sku.get(sku)
            peers = groups.get((label, p.unit), ())
            # Compare equivalent volumes within product type when the cluster is large enough.
            if len(peers) >= 5:
                pool, value = peers, p.price / p.amount
            else:
                pool, value = categories[p.category], p.price
            l, r = bisect_left(pool, value), bisect_right(pool, value)
            self.tier[sku] = (l + r) / (2 * len(pool))

    def personal_targets(self, history, catalog):
        sums, weights = defaultdict(float), defaultdict(int)
        for sku, purchase in history.sku.items():
            cat = catalog[sku].category
            sums[cat] += self.tier[sku] * purchase.count
            weights[cat] += purchase.count
        total = sum(weights.values())
        central = sum(sums.values()) / max(1, total)
        # Prefer the middle of the assortment, with individual prices taking over gradually.
        return {
            category: min(.78, max(.45, (sums[category] + 4 * (.60 * .55 + .40 * central)) /
                                  (weights[category] + 4)))
            for category in weights
        }

    def preference(self, product, history, targets):
        percentile = self.tier[product.id]
        target = targets.get(product.category, .55)
        # Penalize the cheapest tail gently. Preserve a familiar product if it is a real habit.
        recurring = product.id in history.sku and history.sku[product.id].count >= 3
        cheap_penalty = max(0, .25 - percentile) * (1 if recurring else 2)
        expensive_penalty = max(0, percentile - .9)
        return 1 - 1.8 * abs(percentile - target) - cheap_penalty - expensive_penalty


class PersonalizedBasket:
    def __init__(self, discovery, tiers, policy):
        self.discovery = discovery
        self.base = discovery.base
        self.tiers = tiers
        self.policy = policy

    @staticmethod
    def average_positions(history):
        if not history.orders:
            raise ValueError("Для прогноза нужен хотя бы один прошлый заказ.")
        return history.total_positions / history.orders

    def target(self, history, at, policy=None):
        policy = policy or self.policy
        size, spend = self.base.estimates(history, at)
        if policy.size_mode == "mean_all":
            size = max(1, int(self.average_positions(history) + .5))
        elif policy.size_mode == "mean_recent":
            size = max(1, int(sum(history.sizes) / len(history.sizes) + .5))
        elif policy.size_mode != "v3":
            raise ValueError(f"Неизвестный способ прогноза размера: {policy.size_mode}.")
        return size, round(spend * (1 + policy.spend_uplift))

    def signals(self, history, ranked):
        targets = self.tiers.personal_targets(history, self.base.catalog)
        enriched = [(p, score, self.tiers.preference(p, history, targets)) for p, score in ranked]
        variety = {key: min(1, (.25 + count / history.orders))
                   for key, count in history.categories.items()}
        return enriched, variety

    def assemble(self, history, at, signals, policy=None, *, budget=None, size=None, targets=None):
        policy = policy or self.policy
        enriched, variety = signals
        targets = targets or self.target(history, at, policy)
        planned_size = size if size is not None and size > 0 else targets[0]
        reference_price = max(1, targets[1] / planned_size)
        ranked = [(p, score + policy.price_weight * value
                   + policy.price_priority * p.price / (p.price + reference_price))
                  for p, score, value in enriched]
        category_bonus = {key: policy.variety_weight * value for key, value in variety.items()}
        return self.base.assemble(history, ranked, budget=budget, size=size, target=targets,
                                  category_bonus=category_bonus)

    def routine_orders(self, routine, routines):
        catalog = self.base.catalog
        other_keys = defaultdict(set)
        for other in routines:
            if other is not routine:
                for order in other.occurrences:
                    other_keys[order.id].update(other.keys - routine.keys)
        return [replace(order, items={sku: qty for sku, qty in order.items.items()
                                      if product_key(catalog[sku]) not in other_keys[order.id]})
                for order in routine.occurrences]

    def periodic_basket(self, history, at, routines, available, budget, size):
        catalog = self.base.catalog
        background, whole_orders = irregular_history_and_orders(history, catalog, routines)
        scheduled = set().union(*(routine.keys for routine in routines))
        independent = {product_key(catalog[sku]) for sku in background.sku}
        background_ranked = [(product, score) for product, score in
                             (self.base.ranked(background, at, allowed=available) if background.orders else ())
                             if product_key(product) not in scheduled - independent]
        background_ranked = rank_by_time(background, at, catalog, background_ranked)
        if (size is not None and size < 1) or (budget is not None and budget < 0):
            raise ValueError("Число позиций должно быть положительным, бюджет неотрицательным.")
        routine_keys, due, routine_items = set(), [], defaultdict(set)
        combined_orders, selected_routines = {}, []
        extra_slots, cluster_spend = 0, 0
        for routine in routines:
            if not routine.due(at) or (size is not None and len(routine_keys | routine.keys) > size):
                continue
            orders = self.routine_orders(routine, routines)
            cluster = History()
            for order in orders:
                cluster.add(replace(order, amount=sum(catalog[sku].price * qty for sku, qty in order.items.items())),
                            catalog)
            options = defaultdict(set)
            for sku in cluster.sku:
                product = catalog[sku]
                key = product_key(product)
                if available is None or sku in available:
                    options[key].add(sku)
                elif key in routine.keys:
                    options[key].update(p.id for p in self.base._alternative(product, cluster, available))
            if any(not options.get(key) for key in routine.keys):
                continue
            core_options = {key: values for key, values in options.items() if key in routine.keys}
            if budget is not None:
                combined = {key: routine_items.get(key, set()) | core_options.get(key, set())
                            for key in routine_keys | routine.keys}
                if sum(min(catalog[sku].price for sku in values) for values in combined.values()) > budget:
                    continue
            for key, values in options.items():
                routine_items[key].update(values)
            for order in orders:
                _, items = combined_orders.setdefault(order.id, (order, {}))
                items.update(order.items)
            cluster_size = int(median(len({product_key(catalog[sku]) for sku in order.items})
                                    for order in orders) + .5)
            shared = routine_keys & routine.keys
            cluster_spend += median(sum(catalog[sku].price * qty for sku, qty in order.items.items()
                                        if product_key(catalog[sku]) not in shared) for order in orders)
            extra_slots += max(0, cluster_size - len(routine.keys))
            routine_keys.update(routine.keys)
            selected_routines.append(routine)
            due.append({"interval_days": routine.days, "expected_at": routine.next_at(at).isoformat(),
                        "window_minutes": 2 * routine.half_window,
                        "occurrences": len(routine.occurrences),
                        "cluster_positions": cluster_size,
                        "product_ids": sorted(sku for choices in core_options.values() for sku in choices)})
        if due:
            profile = History()
            for order, items in sorted(combined_orders.values(), key=lambda entry: entry[0].at):
                profile.add(replace(order, items=items, amount=sum(catalog[sku].price * qty for sku, qty in items.items())),
                            catalog)
            if len(selected_routines) > 1:
                joint_ids = set.intersection(*({order.id for order in routine.occurrences} for routine in selected_routines))
                if joint_ids:
                    joint = [combined_orders[oid][1] for oid in joint_ids]
                    extra_slots = max(0, int(median(len({product_key(catalog[sku]) for sku in items})
                                                  for items in joint) + .5) - len(routine_keys))
                    cluster_spend = median(sum(catalog[sku].price * qty for sku, qty in items.items()) for items in joint)
            ids = set().union(*routine_items.values())
            scores = {p.id: score for p, score in self.base.ranked(profile, at, allowed=ids)}
            low = min(scores.values(), default=0)
            bonus = max(scores.values(), default=low) - low + 5
            ranked = [(catalog[sku], scores.get(sku, low) + (bonus if key in routine_keys else 0))
                      for key, skus in routine_items.items() for sku in sorted(skus)]
            cluster_targets = len(routine_keys) + extra_slots, round(cluster_spend * (1 + self.policy.spend_uplift))
            result = self.assemble(profile, at, self.signals(profile, ranked), budget=budget, size=size,
                                   targets=cluster_targets)
            covered = {product_key(catalog[row["product_id"]]) for row in result["items"]}
            if routine_keys - covered:
                due = []
        if not due:
            profile = background if background.orders else history
            if whole_orders:
                targets = (max(1, int(median(len(order.items) for order in whole_orders) + .5)),
                           round(median(order.amount for order in whole_orders) * (1 + self.policy.spend_uplift)))
            else:
                targets = self.target(history, at)
            result = self.assemble(profile, at, self.signals(profile, background_ranked),
                                   budget=budget, size=size, targets=targets)
        result.update(mode="periodic" if due else "irregular", routines_due=due,
                      routines_detected=len(routines))
        return result

    def predict(self, history, at, budget=None, size=None, allowed=None, unavailable=()):
        exclusions = set(unavailable)
        available = (set(self.base.catalog if allowed is None else allowed) - exclusions
                     if allowed is not None or exclusions else None)
        if history.last is None or at <= history.last.at:
            raise ValueError("Прогноз должен быть позже последнего известного заказа.")
        routines = find_routines(history, self.base.catalog) if self.policy.periodic_orders else []
        if routines:
            chosen = self.periodic_basket(history, at, routines, available, budget, size)
        else:
            ranked = self.base.ranked(history, at)
            if self.policy.periodic_orders:
                ranked = rank_by_time(history, at, self.base.catalog, ranked)
            chosen = self.assemble(history, at, self.signals(history, ranked), budget=budget, size=size)
            if available is not None:
                chosen = self.base.resolve_availability(history, chosen, available, budget=budget, size=size,
                                                        target=self.target(history, at))
            if self.policy.periodic_orders:
                chosen.update(mode="history", routines_due=[], routines_detected=0)
        chosen["historical_mean_positions"] = round(self.average_positions(history), 3)
        return chosen


def load_personalized(catalog, baseline, discovery, policy_path, semantic_path):
    model, _ = load_discovery(catalog, baseline, discovery)
    raw = json.loads(Path(policy_path).read_text(encoding="utf-8"))
    products = json.loads(Path(semantic_path).read_text(encoding="utf-8"))["products"]
    indexed = {sku: item["type"] for sku, item in products.items()}
    return PersonalizedBasket(model, PriceTiers(catalog, indexed), Policy(**raw))
