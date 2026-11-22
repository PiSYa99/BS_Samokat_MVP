"""Candidate generation and deployable variants for the basket experiments."""
from collections import defaultdict
from dataclasses import replace
from itertools import islice
import math
import re
from statistics import mean, median, pstdev

from basket import BasketEngine, EMPTY, Prior, Ranker

EXTRA_GROUPS = {
    "bread_bakery": {"Багеты": r"^(?:мини-)?багет\b", "Батоны": r"^батон\b",
                     "Чиабатта": r"^(?:мини-)?чиабатт[аы]\b", "Бриошь": r"^(?:черная )?бриошь\b"},
    "ready_meals": {"Готовые пирожки": r"^(?:карельский )?пирож[ое]к\b|^пирожки\b",
                    "Готовый плов": r"^плов\b", "Готовые киши": r"^(?:черный )?киш\b",
                    "Готовые котлеты с гарниром": r"\bкотлет[аы]\b.*\b(?:с пюре|с картофельным пюре|с рисом|с гречкой)\b"},
    "produce": {"Мандарины": r"\bмандарины\b", "Груши": r"^груш[аи]\b", "Грейпфруты": r"^грейпфрут[ы]?\b",
                "Лимоны": r"^лимон[ы]?\b", "Киви": r"^киви\b", "Помидоры": r"^(?:оранжевые |розовые )?томаты\b",
                "Виноград": r"^виноград\b", "Голубика": r"^голубика\b", "Укроп": r"^укроп\b"},
    "pantry": {"Гречневая крупа": r"\bгречневая крупа\b|^крупа.*гречневая\b",
               "Пшено": r"^пшено\b", "Горох сухой": r"^горох\b", "Мюсли": r"^мюсли\b",
               "Гранола": r"^гранола\b", "Сухие макароны": r"^(?:яичные макароны|фунчоза|лапша)\b"},
    "dairy_eggs_cheese": {"Йогурт греческий": r"^греческий йогурт\b",
                           "Масло сливочное": r"^(?:сладко|кисло)[ -]?сливочное масло\b",
                           "Творог": r"^(?:мягкий|рассыпчатый) творог\b",
                           "Ряженка": r"^ряженка\b", "Моцарелла": r"^(?:сыр )?моцарелла\b",
                           "Сулугуни": r"^(?:сыр )?сулугуни\b", "Йогурт питьевой": r"^(?:питьевой йогурт|питьевой биойогурт|биойогурт питьевой)\b"},
    "tea_coffee": {"Чёрный чай": r"^черный чай\b", "Зелёный чай": r"^зеленый чай\b",
                    "Кофе растворимый": r"^(?:растворимый кофе|кофе растворимый)\b",
                    "Кофе в зёрнах": r"^кофе\b.*\bв зернах\b", "Кофе молотый": r"^кофе\b.*\bмолотый\b"},
    "frozen": {"Замороженная брокколи": r"^брокколи\b", "Замороженная цветная капуста": r"^цветная капуста\b",
               "Замороженный картофель фри": r"^картофель фри\b",
               "Замороженное слоёное тесто": r"^слоеное тесто\b"},
    "canned": {"Томатная паста": r"^(?:томатная паста|паста томатная)\b",
               "Тунец консервированный": r"^(?:филе .{0,20}тунца|тунец)\b",
               "Горошек консервированный": r"^(?:зеленый )?горошек\b",
               "Печень трески": r"^печень трески\b"},
    "meat_fish": {"Фарш индейки": r"^фарш\b.*\bиндейк[аи]\b", "Свиной фарш": r"^(?:свиной фарш|фарш свиной)\b",
                   "Свино-говяжий фарш": r"^свино-говяжий фарш\b", "Куриное филе": r"^филе (?:куриной грудки|грудки цыпленка)\b"},
    "deli": {"Варёная колбаса": r"^(?:вареная|докторская) колбаса\b", "Сыровяленая колбаса": r"^сыровяленая колбас(?:а|ка)\b"},
    "drinks": {"Лимонад": r"^лимонад\b", "Холодный зелёный чай": r"^холодный зеленый чай\b",
               "Апельсиновый сок": r"^(?:апельсиновый сок|сок апельсиновый)\b"},
    "sauces_oil_spices": {"Кетчуп": r"^кетчуп\b", "Горчица": r"^горчица\b", "Оливковое масло": r"^(?:оливковое масло|масло оливковое)\b"},
}
RULES = {category: [(group, re.compile(pattern)) for group, pattern in rules.items()]
         for category, rules in EXTRA_GROUPS.items()}


def enrich_catalog(catalog):
    enriched = {}
    for sku, product in catalog.items():
        name = re.sub(r"^из [^:]+:\s*", "", product.name.lower().replace("ё", "е"))
        group = product.group or next(
            (group for group, pattern in RULES.get(product.category, ()) if pattern.search(name)), ""
        )
        enriched[sku] = replace(product, group=group) if group != product.group else product
    return enriched


def target_features(history, at):
    sizes, amounts = history.sizes, history.amounts
    days = (at - history.last.at).total_seconds() / 86400
    return [1, median(sizes), mean(sizes), mean(sizes[-3:]), pstdev(sizes), median(amounts),
            mean(amounts), mean(amounts[-3:]), pstdev(amounts), math.log1p(history.orders),
            math.log1p(days)]


class EnhancedEngine(BasketEngine):
    def __init__(self, catalog, prior, ranker, neighbors, group_ranker=None, targets=None, settings=None, legacy_ranker=None):
        super().__init__(catalog, prior, ranker)
        self.embedding_neighbors = neighbors
        self.group_ranker = group_ranker
        self.legacy_ranker = legacy_ranker
        self.target_model = targets or {}
        self.settings = settings or {}
        self.group_category = {
            group: products[0].category for group, products in self.by_group.items()
        }

    def candidate_sources(self, history, at):
        original = {p.id for p in super().candidates(history, at)}
        popular = set()
        for category, _ in history.categories.most_common():
            popular.update(p.id for p in self.by_category[category][:12])
        near = set()
        seeds = sorted(history.sku, key=lambda sku: (
            -history.sku[sku].count, history.sku[sku].age_days(at), sku))[:8]
        for sku in seeds:
            near.update(self.embedding_neighbors.get(sku, ())[:6])
        return original, popular, near

    def candidates(self, history, at, allowed=None, source=None):
        source = self.settings.get("source", "both") if source is None else source
        if source == "original":
            return super().candidates(history, at, allowed)
        selected, popular, near = self.candidate_sources(history, at)
        if source in ("popular", "both"):
            selected.update(popular)
        if source in ("embedding", "both"):
            selected.update(near)
        return [self.catalog[sku] for sku in sorted(selected) if allowed is None or sku in allowed]

    def features(self, history, at, products):
        values = super().features(history, at, products)
        return [(*row, math.log1p(history.sku.get(p.id, EMPTY).count) *
                 math.log1p(history.categories[p.category]),
                 history.categories[p.category] / history.orders * math.log1p(self.prior.sku[p.id]),
                 float(p.group in history.groups and not history.sku.get(p.id)))
                for row, p in zip(values, products)]

    def group_features(self, history, at, groups):
        rows = []
        strongest = sorted(history.groups, key=lambda g: (-history.groups[g].count, g))[:5]
        for group in groups:
            bought = history.groups.get(group, EMPTY)
            age = bought.age_days(at)
            gap = bought.gap_days()
            cat_share = history.categories[self.group_category[group]] / history.orders
            rows.append((bought.count, bought.count / history.orders, age if bought.count else float("nan"),
                         age / gap if gap else float("nan"), len(bought.products), cat_share,
                         self.prior.groups[group], max((self.prior.pair_strength(group, g, history)
                                                       for g in strongest if g != group), default=0)))
        return rows

    def score_parts(self, history, at, products):
        scores = self.ranker.scores(self.features(history, at, products))
        marked = sorted({p.group for p in products if p.group})
        group_scores = self.group_ranker.scores(self.group_features(history, at, marked)) if self.group_ranker else []
        # Compare groups to a shared reference. Ungrouped SKUs are left at zero.
        reference = median(group_scores) if group_scores else 0
        adjustments = {g: max(-2, min(2, v - reference)) for g, v in zip(marked, group_scores)}
        return [(p, score, adjustments.get(p.group, 0)) for p, score in zip(products, scores)]

    @staticmethod
    def combine(parts, weight):
        return sorted(((p, sku + weight * group) for p, sku, group in parts),
                      key=lambda t: (-t[1], t[0].id))

    def ranked(self, history, at, allowed=None, unavailable=()):
        blocked = set(unavailable)
        if allowed is not None or blocked:
            allowed = set(self.catalog if allowed is None else allowed) - blocked
        products = self.candidates(history, at, allowed)
        weight = self.settings.get("legacy_weight", 0)
        if weight == 1:
            scores = self.legacy_ranker.scores(super().features(history, at, products))
            return sorted(zip(products, scores), key=lambda t: (-t[1], t[0].id))
        parts = self.score_parts(history, at, products)
        if weight:
            historical = self.legacy_ranker.scores(super().features(history, at, products))
            parts = [(p, (1-weight)*score + weight*old_score, group)
                     for (p, score, group), old_score in zip(parts, historical)]
        return self.combine(parts, self.settings.get("group_weight", 0))

    def estimates(self, history, at, strategy=None):
        strategy = self.settings.get("target_strategy", "median") if strategy is None else strategy
        size, spend = history.target()
        if strategy != "median" and self.target_model:
            values = target_features(history, at)
            for key, blend in (("size", strategy[0]), ("spend", strategy[1])):
                if blend:
                    fitted = sum(coeff * value for coeff, value in zip(self.target_model[key], values))
                    if key == "size":
                        size = max(1, round((1 - blend) * size + blend * fitted))
                    else:
                        spend = max(0, round((1 - blend) * spend + blend * fitted))
        return size, spend

    def assemble(self, history, ranked, budget=None, size=None, pairs=True, *, target=None, pair_weight=None, requested_amounts=None, category_bonus=None):
        return super().assemble(history, ranked, budget, size, pairs,
                                target=target, pair_weight=self.settings.get("pair_weight", .30)
                                if pair_weight is None else pair_weight,
                                requested_amounts=requested_amounts, category_bonus=category_bonus)

    def predict(self, history, at, budget=None, size=None, allowed=None, unavailable=()):
        return super().predict(history, at, budget, size, allowed, unavailable,
                               target=self.estimates(history, at))

    def resolve_availability(self, history, intended, available, *, budget=None, size=None, target=None, expand=None):
        if expand is None:
            expand = self.settings.get("expand_replacements", False)
        return super().resolve_availability(history, intended, available, budget=budget,
                                            size=size, target=target, expand=expand)


def load_upgrade(catalog, raw):
    if raw["settings"].get("enriched"):
        catalog = enrich_catalog(catalog)
    return EnhancedEngine(catalog, Prior.load(raw["prior"]), Ranker(**raw["ranker"]),
                          raw["neighbors"], Ranker(**raw["group_ranker"]),
                          raw["targets"], raw["settings"],
                          Ranker(**raw["legacy_ranker"]) if "legacy_ranker" in raw else None)
