"""Optional personalized discovery of SKUs absent from a buyer's history."""
from __future__ import annotations

import math
from collections import defaultdict
from itertools import islice
from pathlib import Path
import json

from basket import EMPTY, Ranker, load_engine


class Discovery:
    def __init__(self, base, ranker, feature_categories=None, suggestion_mode="model"):
        self.base, self.ranker = base, ranker
        self.suggestion_mode = suggestion_mode
        categories = defaultdict(list)
        for product in base.catalog.values():
            categories[product.category].append(product)
        self.popular = {category: sorted(items, key=lambda p: (-base.prior.sku[p.id], p.id))
                        for category, items in categories.items()}
        self.feature_categories = sorted(self.popular) if feature_categories is None else feature_categories
        self.embedding = {sku: {neighbor: 1/(i+1) for i, neighbor in enumerate(links)}
                          for sku, links in base.embedding_neighbors.items()}

    def features(self, h, at):
        candidates = {}
        from_popularity, from_group = set(), set()
        # Include every category even if absent so far, with more candidates for familiar categories.
        favorite_categories = {key for key, _ in h.categories.most_common(5)}
        for category, items in self.popular.items():
            limit = 20 if category in favorite_categories else 8
            new = (p for p in items if p.id not in h.sku)
            for product in islice(new, limit):
                candidates[product.id] = product
                from_popularity.add(product.id)
        neighbors = {}
        anchors = sorted(h.sku, key=lambda sku: (-h.sku[sku].count, h.sku[sku].age_days(at), sku))[:10]
        for sku in anchors:
            for neighbor, weight in self.embedding.get(sku, {}).items():
                if neighbor in h.sku:
                    continue
                candidates[neighbor] = self.base.catalog[neighbor]
                neighbors[neighbor] = max(weight, neighbors.get(neighbor, 0))
        for p in self.base.candidates(h, at, source="original"):
            if p.id not in h.sku:
                candidates[p.id] = p
                if p.group in h.groups:
                    from_group.add(p.id)
        count, spend = self.base.estimates(h, at)
        rows = []
        products = [candidates[key] for key in sorted(candidates)]
        familiar_groups = sorted(h.groups, key=lambda g: (-h.groups[g].count, g))[:5]
        signals = {}
        for p in products:
            group = h.groups.get(p.group, EMPTY)
            c = h.categories[p.category]
            typical = group.typical_amount(p.unit)
            price = p.price * count / max(1, spend)
            popularity = self.base.prior.sku[p.id]
            if p.group and p.group not in signals:
                signals[p.group] = max((self.base.prior.pair_strength(p.group, anchor, h)
                                        for anchor in familiar_groups if anchor != p.group), default=0)
            rows.append((c, c/h.orders, popularity, p.price, price, group.count,
                         float(p.id in from_popularity), float(p.id in from_group), neighbors.get(p.id, 0),
                         len(group.products), abs(math.log(p.amount/typical)) if typical else float("nan"),
                         h.orders, math.log1p(popularity)*(c+1)/(h.orders+2), signals.get(p.group, 0),
                         float(p.category in favorite_categories), *[float(p.category==cat) for cat in self.feature_categories]))
        return products, rows

    def score(self, h, at):
        products, rows = self.features(h, at)
        return sorted(zip(products, self.ranker.scores(rows)), key=lambda x: (-x[1], x[0].id))

    @staticmethod
    def combine(old, fresh, mode, size, seen):
        if mode == "v2":
            return old
        if mode.startswith("offset:"):
            # Preserve scores for repeats, assign a separately calibrated score to new SKUs.
            offset = float(mode.split(":")[1])
            adjustment = {p.id: (p, score+offset) for p, score in fresh}
            rows = {p.id: (p, score) for p, score in old}
            rows.update(adjustment)
            return sorted(rows.values(), key=lambda x: (-x[1], x[0].id))
        if mode != "slot":
            raise ValueError(f"Неизвестный режим корзины: {mode}.")
        if size < 3:
            return old
        # Place one scored new option after the strongest repeats. Keep the rest as fallbacks.
        repeats = [(p, score) for p, score in old if p.id in seen]
        if not fresh or not repeats:
            return old
        cut = max(0, min(size-1, len(repeats)))
        anchor_score = repeats[cut-1][1] if cut else repeats[0][1]
        if cut < len(repeats):
            anchor_score = (anchor_score+repeats[cut][1])/2
        p, _ = fresh[0]
        selected = [(item, score) for item, score in old if item.id != p.id]
        return sorted([*selected, (p, anchor_score+.4)], key=lambda x: (-x[1], x[0].id))

    def predict(self, h, at, budget=None, size=None, allowed=None, unavailable=(), *, explore=False, model=None):
        mode = "slot" if explore else model or "v2"
        if mode == "v2":
            return self.base.predict(h, at, budget, size, allowed, unavailable)
        target = self.base.estimates(h, at)
        old = self.base.ranked(h, at)
        blocked = set(unavailable)
        available = set(self.base.catalog if allowed is None else allowed) - blocked
        fresh = [(p, score) for p, score in self.score(h, at) if p.id in available]
        ranked = self.combine(old, fresh, mode, size or target[0], h.sku)
        intended = self.base.assemble(h, ranked, budget, size, target=target)
        if allowed is None and not blocked:
            return intended
        return self.base.resolve_availability(h, intended, available, budget=budget, size=size, target=target)

    def suggestions_ranked(self, h, at, method=None):
        method = method or self.suggestion_mode
        ranked = self.score(h, at)
        if method == "model":
            return [product for product, _ in ranked]
        if method not in {"category", "popular"}:
            raise ValueError(f"Неизвестный способ подбора новинок: {method}.")
        return sorted((p for p, _ in ranked), key=lambda p: (
            -self.base.prior.sku[p.id] * ((h.categories[p.category]+2)/(h.orders+3)
                                         if method == "category" else 1), p.id))

    def suggest(self, h, at, limit=3, *, available=None, unavailable=(), basket=()):
        if limit < 0:
            raise ValueError("Количество дополнительных рекомендаций должно быть неотрицательным.")
        if not limit:
            return []
        if not h.last or at <= h.last.at:
            raise ValueError("Дата рекомендаций должна быть позже последнего заказа.")
        blocked = set(unavailable) | {row["product_id"] for row in basket}
        eligible = self.base.catalog.keys() if available is None else set(available)
        products = (p for p in self.suggestions_ranked(h, at) if p.id in eligible and p.id not in blocked)
        return [{"product_id": p.id, "name": p.name, "pack": p.pack_label, "unit_price_rub": p.price,
                 "category": p.category, "group": p.group or None} for p in islice(products, limit)]


def load_discovery(catalog, baseline, novelty):
    base = load_engine(catalog, baseline)
    raw = json.loads(Path(novelty).read_text(encoding="utf-8"))
    return Discovery(base, Ranker(**raw["ranker"]), raw.get("categories"), raw.get("suggestion_mode", "model")), raw["mode"]
