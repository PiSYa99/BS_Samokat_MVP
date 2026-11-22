"""Local next-basket prediction. Inference uses only the Python standard library."""
from __future__ import annotations

import csv
import io
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations, islice
from pathlib import Path
from statistics import median
from zipfile import ZipFile


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    category: str
    group: str
    price: int
    unit: str
    low: float
    high: float
    pack_label: str
    pack_kind: str
    claims: frozenset[str]

    @property
    def amount(self):
        return (self.low + self.high) / 2


@dataclass
class Order:
    id: str
    user: str
    at: datetime
    items: dict[str, int]
    amount: int
    split: str = ""


def timestamp(value):
    at = datetime.fromisoformat(value)
    if at.utcoffset() is None:
        raise ValueError("Дата должна включать часовой пояс.")
    return at


def read_dataset(path: Path, include_orders=True):
    with ZipFile(path) as archive:
        def rows(name):
            with io.TextIOWrapper(archive.open(name + ".csv"), encoding="utf-8-sig") as stream:
                yield from csv.DictReader(stream)

        catalog = {
            row["product_id"]: Product(
                row["product_id"], row["name"], row["calibration_category"],
                row["product_group"], int(row["price_rub"]), row["pack_unit"],
                float(row["pack_min"]), float(row["pack_max"]), row["pack_label"],
                row["pack_kind"], frozenset(filter(None, row["dietary_claims_in_name"].split(" | "))),
            )
            for row in rows("catalog") if row["eligible_for_simulation"] == "True"
        }
        orders = []
        if include_orders:
            detail = defaultdict(dict)
            for row in rows("order_items"):
                detail[row["order_id"]][row["product_id"]] = int(row["packs"])
            for row in rows("orders"):
                orders.append(Order(row["order_id"], row["user_id"], timestamp(row["ordered_at_msk"]),
                                    detail[row["order_id"]], int(row["total_rub"]), row["split"]))
        return catalog, sorted(orders, key=lambda o: (o.at, o.id))


@dataclass
class Purchase:
    count: int = 0
    times: list[datetime] = field(default_factory=list)
    packs: list[int] = field(default_factory=list)
    volumes: list[tuple[str, float]] = field(default_factory=list)
    products: Counter = field(default_factory=Counter)

    def add(self, at, items):
        self.count += 1
        self.times = (self.times + [at])[-8:]
        self.packs = (self.packs + [sum(q for _, q in items)])[-8:]
        units = {p.unit for p, _ in items}
        if len(units) == 1:
            self.volumes = (self.volumes + [
                (items[0][0].unit, sum(p.amount * q for p, q in items)),
            ])[-8:]
        self.products.update(p.id for p, _ in items)

    def gap_days(self):
        return median([(b - a).total_seconds() / 86400 for a, b in zip(self.times, self.times[1:])]) if len(self.times) > 1 else 0

    def age_days(self, at):
        return max(0, (at - self.times[-1]).total_seconds() / 86400) if self.times else 0

    def typical_amount(self, unit):
        comparable = [value for measure, value in self.volumes if measure == unit]
        return median(comparable) if comparable else 0


EMPTY = Purchase()


@dataclass
class History:
    orders: int = 0
    last: Order | None = None
    sizes: list[int] = field(default_factory=list)
    amounts: list[int] = field(default_factory=list)
    sku: dict[str, Purchase] = field(default_factory=dict)
    groups: dict[str, Purchase] = field(default_factory=dict)
    categories: Counter = field(default_factory=Counter)
    pairs: Counter = field(default_factory=Counter)
    total_positions: int = 0
    recent_orders: list[Order] = field(default_factory=list)

    def add(self, order, catalog):
        if self.last and order.at <= self.last.at:
            raise ValueError("Заказы одного покупателя должны иметь строго возрастающие даты.")
        products = [(catalog[sku], q) for sku, q in order.items.items()]
        groups = defaultdict(list)
        for product, quantity in products:
            self.sku.setdefault(product.id, Purchase()).add(order.at, [(product, quantity)])
            if product.group:
                groups[product.group].append((product, quantity))
        for key, selections in groups.items():
            self.groups.setdefault(key, Purchase()).add(order.at, selections)
        self.categories.update({p.category for p, _ in products})
        self.pairs.update(combinations(sorted(groups), 2))
        self.sizes = (self.sizes + [len(order.items)])[-9:]
        self.amounts = (self.amounts + [order.amount])[-9:]
        self.orders += 1
        self.total_positions += len(order.items)
        self.last = order
        self.recent_orders.append(order)
        if len(self.recent_orders) > 80:
            del self.recent_orders[0]

    def target(self):
        if not self.orders:
            raise ValueError("Нужен хотя бы один предыдущий заказ.")
        return max(1, round(median(self.sizes))), round(median(self.amounts))


@dataclass
class Prior:
    sku: Counter
    groups: Counter
    pairs: Counter
    order_count: int

    @classmethod
    def build(cls, orders, catalog):
        sku, groups, pairs, count = Counter(), Counter(), Counter(), 0
        for order in orders:
            sku.update(order.items.keys())
            present = sorted({catalog[x].group for x in order.items if catalog[x].group})
            groups.update(present)
            pairs.update(combinations(present, 2))
            count += 1
        return cls(sku, groups, pairs, count)

    def pair_strength(self, left, right, history):
        key = tuple(sorted((left, right)))
        together = history.pairs[key]
        local = (together - 1) / max(1, min(history.groups.get(left, EMPTY).count,
                                           history.groups.get(right, EMPTY).count)) if together >= 2 else 0
        n = self.pairs[key]
        expected = self.groups[left] * self.groups[right] / max(1, self.order_count)
        global_signal = max(0, math.log((n + 8) / (expected + 8))) * n / (n + 12)
        return min(1, local + 0.25 * global_signal)

    def serialize(self):
        return {"sku": dict(self.sku), "groups": dict(self.groups),
                "pairs": [[a, b, n] for (a, b), n in self.pairs.items()], "order_count": self.order_count}

    @classmethod
    def load(cls, raw):
        return cls(Counter(raw["sku"]), Counter(raw["groups"]),
                   Counter({(a, b): n for a, b, n in raw["pairs"]}), raw["order_count"])


FEATURES = (
    "sku_count", "sku_share", "sku_age", "sku_interval_ratio", "in_previous_order",
    "group_count", "group_age", "group_variants", "group_interval_ratio",
    "category_share", "catalog_popularity", "price_rub", "price_vs_basket_item",
    "quantity_match", "orders_in_history", "days_since_order", "pair_affinity",
)


@dataclass
class Ranker:
    bias: float
    stumps: list

    def scores(self, rows):
        return [
            self.bias + sum(missing if math.isnan(row[index]) else left if row[index] <= cut else right
                            for index, cut, left, right, missing in self.stumps)
            for row in rows
        ]


class BasketEngine:
    def __init__(self, catalog, prior, ranker):
        self.catalog, self.prior, self.ranker = catalog, prior, ranker
        self.by_group = defaultdict(list)
        self.by_category = defaultdict(list)
        self.neighbors = defaultdict(list)
        for product in catalog.values():
            if product.group:
                self.by_group[product.group].append(product)
            self.by_category[product.category].append(product)
        for category in self.by_category.values():
            category.sort(key=lambda p: (-prior.sku[p.id], p.id))
        for (a, b), count in prior.pairs.items():
            self.neighbors[a].append((count, b))
            self.neighbors[b].append((count, a))
        for key, links in self.neighbors.items():
            self.neighbors[key] = [name for _, name in sorted(links, reverse=True)[:6]]

    def _similar(self, source, target, wanted=0):
        ratio = abs(math.log(target.amount / (wanted or source.amount))) if source.unit == target.unit else 10
        return ratio + 0.25 * abs(math.log(target.price / source.price))

    def _compatible_alternatives(self, source, history, allowed):
        group = history.groups.get(source.group, EMPTY)
        wanted = group.typical_amount(source.unit)
        choices = (p for p in self.by_group.get(source.group, [])
                   if p.id != source.id and (allowed is None or p.id in allowed)
                   and p.unit == source.unit and source.claims <= p.claims
                   and 0.5 <= p.amount / source.amount <= 2)
        return sorted(choices, key=lambda p: (self._similar(source, p, wanted), p.id))

    def _alternative(self, source, history, allowed):
        return self._compatible_alternatives(source, history, allowed)[:3]

    def candidates(self, history, at, allowed=None):
        if not history.last or at <= history.last.at:
            raise ValueError("Прогноз должен быть позже последнего известного заказа.")
        # Keep exact repeats, plus catalog alternatives for a few well-supported groups.
        ranked = sorted(history.sku, key=lambda sku: (
            -(history.sku[sku].count + 0.4 / (1 + history.sku[sku].age_days(at) / 10)), sku))
        found = dict.fromkeys(ranked[:60])
        found.update(dict.fromkeys(history.last.items))
        strongest = sorted(history.groups, key=lambda g: (-history.groups[g].count, g))[:8]
        for group in strongest:
            source = self.catalog[history.groups[group].products.most_common(1)[0][0]]
            found.update(dict.fromkeys(p.id for p in self._alternative(source, history, allowed)))
        neighbor_groups = dict.fromkeys(g for group in strongest[:3] for g in self.neighbors[group][:2])
        for group in neighbor_groups:
            choices = sorted(self.by_group[group], key=lambda p: (-self.prior.sku[p.id], p.id))
            eligible = (p.id for p in choices if allowed is None or p.id in allowed)
            found.update(dict.fromkeys(islice(eligible, 2)))
        for category, _ in history.categories.most_common():
            popular = (p.id for p in self.by_category[category] if allowed is None or p.id in allowed)
            found.update(dict.fromkeys(islice(popular, 3)))
        return [self.catalog[sku] for sku in found if allowed is None or sku in allowed]

    def features(self, history, at, products):
        count, total = history.target()
        days = (at - history.last.at).total_seconds() / 86400
        strongest = sorted(history.groups, key=lambda g: (-history.groups[g].count, g))[:5]
        pair_strengths = {}
        nan = float("nan")
        rows = []
        for product in products:
            sku = history.sku.get(product.id, EMPTY)
            group = history.groups.get(product.group, EMPTY)
            want = group.typical_amount(product.unit)
            if product.group and product.group not in pair_strengths:
                pair_strengths[product.group] = max(
                    (self.prior.pair_strength(product.group, anchor, history)
                     for anchor in strongest if anchor != product.group), default=0,
                )
            sku_gap, group_gap = sku.gap_days(), group.gap_days()
            rows.append((
                sku.count, sku.count / history.orders, sku.age_days(at) if sku.count else nan,
                sku.age_days(at) / sku_gap if sku_gap else nan,
                float(product.id in history.last.items),
                group.count, group.age_days(at) if group.count else nan, len(group.products),
                group.age_days(at) / group_gap if group_gap else nan,
                history.categories[product.category] / history.orders, self.prior.sku[product.id],
                product.price, product.price * count / max(1, total),
                abs(math.log(product.amount / want)) if want else nan,
                history.orders, days, pair_strengths.get(product.group, 0),
            ))
        return rows

    def ranked(self, history, at, allowed=None, unavailable=()):
        unavailable = set(unavailable)
        if unavailable or allowed is not None:
            allowed = set(self.catalog if allowed is None else allowed) - unavailable
        candidates = self.candidates(history, at, allowed)
        rows = self.features(history, at, candidates)
        scores = self.ranker.scores(rows)
        return sorted(zip(candidates, scores), key=lambda pair: (-pair[1], pair[0].id))

    def quantity(self, product, history, max_packs):
        sku = history.sku.get(product.id, EMPTY)
        group = history.groups.get(product.group, EMPTY)
        wanted = group.typical_amount(product.unit)
        if wanted:
            return min(range(1, max_packs + 1), key=lambda n: (abs(n * product.amount - wanted), n))
        return max(1, min(max_packs, round(median(sku.packs)) if sku.packs else 1))

    def assemble(self, history, ranked, budget=None, size=None, pairs=True, *, target=None, pair_weight=.30, requested_amounts=None, category_bonus=None):
        target_count, target_spend = history.target() if target is None else target
        target_count = target_count if size is None else size
        if target_count < 1 or (budget is not None and budget < 0):
            raise ValueError("Число позиций должно быть положительным, бюджет неотрицательным.")
        if budget is not None:
            target_spend = min(target_spend, budget)
        choices, groups, ids = [], set(), set()
        used_categories = set()
        total = 0
        while len(choices) < target_count:
            slots = target_count - len(choices)
            target_line = max(0, target_spend - total) / slots
            best = None
            remaining = self._minimum_costs(ranked, ids, groups) if budget is not None else []
            affordable_slots = 0
            if budget is not None:
                minimum = 0
                for _, price in remaining[:slots]:
                    minimum += price
                    if minimum > budget - total:
                        break
                    affordable_slots += 1
                if not affordable_slots:
                    break
            for product, score in ranked:
                if product.id in ids or (product.group and product.group in groups):
                    continue
                max_packs = 1 if product.group == "Кола" or product.pack_kind == "multipack" or (
                    product.unit in {"g", "ml"} and product.high > 1000
                ) else 2
                if budget is not None:
                    key = product.group or product.id
                    reserve = sum(islice((price for group, price in remaining if group != key),
                                         affordable_slots - 1))
                    max_packs = min(max_packs, (budget - total - reserve) // product.price)
                if max_packs <= 0:
                    continue
                wanted = requested_amounts.get(product.id) if requested_amounts else None
                quantity = (min(range(1, max_packs + 1), key=lambda n: (abs(n * product.amount - wanted), n))
                            if wanted is not None else self.quantity(product, history, max_packs))
                price = product.price * quantity
                affinity = max((self.prior.pair_strength(product.group, group, history)
                                for group in groups), default=0) if pairs and pair_weight and product.group else 0
                penalty = min(2, abs(price - target_line) / max(120, target_line))
                variety = (category_bonus.get(product.category, 0)
                           if category_bonus and product.category not in used_categories else 0)
                utility = score + pair_weight * affinity - 0.35 * penalty + variety
                if best is None or utility > best[0]:
                    best = utility, product, quantity, price
            if best is None:
                break
            _, product, quantity, price = best
            ids.add(product.id)
            used_categories.add(product.category)
            if product.group:
                groups.add(product.group)
            membership = ("repeat_sku" if product.id in history.sku else
                          "known_group" if product.group in history.groups else "new_candidate")
            choices.append({
                "product_id": product.id, "name": product.name, "group": product.group or None,
                "pack": product.pack_label, "quantity": quantity, "unit_price_rub": product.price,
                "line_total_rub": price, "physical_unit": product.unit,
                "physical_min": round(quantity * product.low, 3), "physical_max": round(quantity * product.high, 3),
                "reason": membership,
            })
            total += price
        return {
            "target_positions": target_count, "target_spend_rub": target_spend, "budget_rub": budget,
            "positions": len(choices), "packs": sum(row["quantity"] for row in choices),
            "total_rub": total, "items": choices,
            "status": "complete" if len(choices) == target_count else "partial",
        }

    @staticmethod
    def _minimum_costs(ranked, ids, groups):
        costs = {}
        for product, _ in ranked:
            if product.id in ids or (product.group and product.group in groups):
                continue
            key = product.group or product.id
            costs[key] = min(costs.get(key, product.price), product.price)
        return sorted(costs.items(), key=lambda item: item[1])

    def predict(self, history, at, budget=None, size=None, allowed=None, unavailable=(), *, target=None):
        unavailable = set(unavailable)
        ranked = self.ranked(history, at)
        intended = self.assemble(history, ranked, budget, size, target=target)
        if allowed is None and not unavailable:
            return intended
        available = set(self.catalog if allowed is None else allowed) - unavailable
        return self.resolve_availability(history, intended, available, budget=budget, size=size, target=target)

    def resolve_availability(self, history, intended, available, *, budget=None, size=None, target=None, expand=False):
        if all(row["product_id"] in available for row in intended["items"]):
            return intended

        # Resolve availability after choosing the intended groups, so another group
        # cannot outrank a compatible replacement for something the basket needs.
        candidates = []
        replacements = {}
        requested_amounts = {}
        for row in intended["items"]:
            source = self.catalog[row["product_id"]]
            if source.id in available:
                candidates.append((source, 3.0))
                continue
            compatible = self._compatible_alternatives(source, history, available)
            alternatives = compatible[:3]
            if expand:
                closest = {product.id for product in alternatives}
                alternatives += sorted((product for product in compatible if product.id not in closest),
                                       key=lambda product: (product.price, product.id))[:3]
            for rank, product in enumerate(alternatives):
                candidates.append((product, 2.0 - .45 * rank))
                replacements[product.id] = source.id
                if expand:
                    requested_amounts[product.id] = source.amount * row["quantity"]
        result = self.assemble(history, candidates, budget, size, target=target,
                               requested_amounts=requested_amounts)
        for row in result["items"]:
            if row["product_id"] in replacements:
                row.update(reason="availability_replacement",
                           replaces_product_id=replacements[row["product_id"]])
        filled = {row.get("replaces_product_id", row["product_id"]) for row in result["items"]}
        result["unfilled_product_ids"] = [
            row["product_id"] for row in intended["items"] if row["product_id"] not in filled
        ]
        return result


def load_engine(catalog, model_path):
    raw = json.loads(Path(model_path).read_text(encoding="utf-8"))
    if raw.get("schema") == 2:
        from upgrade import load_upgrade
        return load_upgrade(catalog, raw)
    if raw["features"] != list(FEATURES):
        raise ValueError("Версия признаков не соответствует модели.")
    return BasketEngine(catalog, Prior.load(raw["prior"]), Ranker(**raw["ranker"]))
