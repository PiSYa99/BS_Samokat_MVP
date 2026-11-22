"""Identify repeated combinations of product groups from timestamped orders."""
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import combinations
from statistics import median, quantiles

from basket import History


def product_key(product):
    return ("group", product.group) if product.group else ("sku", product.id)


def minute_of_day(value):
    return value.hour * 60 + value.minute


def clock_distance(left, right):
    return abs((left - right + 720) % 1440 - 720)


@dataclass(frozen=True)
class Routine:
    keys: frozenset
    occurrences: tuple
    days: int
    minute: int
    half_window: int
    previous_occurrences: tuple = ()
    drift_minutes: float = 0
    calendar_anchor: datetime | None = None

    def next_at(self, at):
        last = self.calendar_anchor or self.occurrences[-1].at
        days = (at.astimezone(last.tzinfo).date() - last.date()).days
        cycle = min(2, max(1, round(days / self.days)))
        midnight = last.replace(hour=0, minute=0, second=0, microsecond=0)
        minute = round(self.minute + self.drift_minutes * cycle) % 1440
        return midnight + timedelta(days=self.days * cycle, minutes=minute)

    def due(self, at):
        expected = self.next_at(at)
        local = at.astimezone(expected.tzinfo)
        return local.date() == expected.date() and abs(local - expected) <= timedelta(minutes=self.half_window)


def fit_schedule(keys, orders, zone, max_spread=120):
    local = [order.at.astimezone(zone) for order in orders]
    dates = [value.date().toordinal() for value in local]
    minutes = [minute_of_day(value) for value in local]
    required = max(3, (3 * len(orders) + 3) // 4)
    periods = sorted({right - left for left, right in combinations(dates, 2)
                      if 3 <= right - left <= 45})
    best = None
    best_quality = None
    for period in periods:
        phases = defaultdict(list)
        for index, date in enumerate(dates):
            phases[date % period].append(index)
        for phase in phases.values():
            if len(phase) < required:
                continue
            starts = {minutes[index] for index in phase} if max_spread < 1439 else (0,)
            for start in starts:
                by_day = {}
                for index in phase:
                    offset = (minutes[index] - start) % 1440
                    if offset <= max_spread:
                        by_day.setdefault(dates[index], (index, offset))
                if len(by_day) < required:
                    continue
                days = list(by_day)
                gaps = [(right - left) // period for left, right in zip(days, days[1:])]
                missed = sum(gap - 1 for gap in gaps)
                if (1 not in gaps or max(gaps) > 2 or missed > max(1, (len(days) - 1) // 4)
                        or dates[-1] - days[-1] > 2 * period):
                    continue
                picked, offsets = zip(*by_day.values())
                low, high = min(offsets), max(offsets)
                quality = (len(picked), -missed, -(high - low), days[-1], -period)
                if best_quality is None or quality > best_quality:
                    best_quality = quality
                    anchor = offsets[-1]
                    center = (start + anchor + round((low + high) / 2 - anchor)) % 1440
                    best = Routine(frozenset(keys), tuple(orders[index] for index in picked), period,
                                   center, max(30, (high - low + 1) // 2))
    return best


def robust_clock_line(samples):
    center = median(value for _, value in samples)
    fixed_error = median(abs(value - center) for _, value in samples)
    minimum = max(4, (3 * len(samples) + 3) // 4)
    best = None
    quality = None
    for (day_a, time_a), (day_b, time_b) in combinations(samples, 2):
        slope = (time_b - time_a) / (day_b - day_a)
        if not 15 <= abs(slope) <= 65:
            continue
        intercept = median(value - cycle * slope for cycle, value in samples)
        residuals = [abs(value - intercept - cycle * slope) for cycle, value in samples]
        keep = [i for i, error in enumerate(residuals) if error <= 35]
        typical_error = median(residuals)
        if (len(keep) < minimum or samples[keep[-1]][0] < -1
                or fixed_error - typical_error < max(10, .35 * fixed_error)):
            continue
        score = (len(keep), -typical_error, -sum(residuals[i] for i in keep))
        if quality is None or score > quality:
            quality = score
            best = intercept, slope, residuals, keep
    return best


def follow_time_drift(routine, zone):
    recent = routine.occurrences[-7:]
    last = recent[-1].at.astimezone(zone)
    clock = minute_of_day(last)
    local = [order.at.astimezone(zone) for order in recent]
    samples = [((value.date() - last.date()).days // routine.days,
                (minute_of_day(value) - clock + 720) % 1440 - 720) for value in local]
    for start in range(len(samples) - 3):
        fitted = robust_clock_line(samples[start:])
        if fitted is None:
            continue
        intercept, slope, residuals, keep = fitted
        inliers = tuple(recent[start + i] for i in keep)
        older = routine.occurrences[:len(routine.occurrences) - len(recent) + start]
        return replace(routine, occurrences=inliers, previous_occurrences=older,
                       minute=round(clock + intercept) % 1440, drift_minutes=slope,
                       half_window=max(30, round(max(residuals[i] for i in keep) + 15)), calendar_anchor=last)
    return None


def learn_width(routine, orders, zone):
    anchor = routine.occurrences[-1].at.astimezone(zone).date()
    first_seen = routine.occurrences[0].at
    by_cycle = {}
    for order in orders:
        local = order.at.astimezone(zone)
        days = (local.date() - anchor).days
        if order.at >= first_seen and days % routine.days == 0:
            predicted = routine.minute + days // routine.days * routine.drift_minutes
            offset = (minute_of_day(local) - predicted + 720) % 1440 - 720
            if days not in by_cycle or abs(offset) < abs(by_cycle[days]):
                by_cycle[days] = offset
    observations = list(by_cycle.values())[-8:]
    if len(observations) < 4:
        return None
    low_quartile, _, high_quartile = quantiles(observations, n=4, method="inclusive")
    fence = max(60, 1.5 * (high_quartile - low_quartile))
    inliers = [value for value in observations if low_quartile - fence <= value <= high_quartile + fence]
    low, high = min(inliers), max(inliers)
    if len(inliers) < 4 or high - low <= 120:
        return None
    center = round(routine.minute + (low + high) / 2) % 1440
    return replace(routine, minute=center, half_window=min(210, round((high - low) / 2 + 30)))


def learn_schedule(keys, orders, zone):
    calendar = fit_schedule(keys, orders, zone, max_spread=1439) if len(orders) >= 4 else None
    if calendar:
        moving = follow_time_drift(calendar, zone)
        if moving:
            return moving
    # A new rhythm needs three consecutive observations and a confirmed earlier rhythm.
    if len(orders) >= 6:
        recent = fit_schedule(keys, orders[-3:], zone)
        earlier = fit_schedule(keys, orders[:-3], zone) if recent else None
        if earlier:
            anchor = earlier.occurrences[-1].at.astimezone(zone).date()
            recent_dates = [order.at.astimezone(zone) for order in recent.occurrences]
            same_phase = any((value.date() - anchor).days % earlier.days == 0 and
                             clock_distance(minute_of_day(value), earlier.minute) <= earlier.half_window
                             for value in recent_dates)
            if recent.days != earlier.days or not same_phase:
                return replace(recent, previous_occurrences=earlier.occurrences)
    routine = fit_schedule(keys, orders, zone)
    if routine is not None:
        return learn_width(routine, orders, zone) or routine
    if len(orders) >= 4:
        dispersed = fit_schedule(keys, orders, zone, max_spread=420)
        if dispersed is not None:
            return learn_width(dispersed, orders, zone)
    return None


def find_routines(history, catalog):
    if len(history.recent_orders) < 3:
        return []
    zone = history.last.at.tzinfo
    events = defaultdict(list)
    for order in history.recent_orders:
        keys = sorted({product_key(catalog[sku]) for sku in order.items})
        for pair in combinations(keys, 2):
            events[pair].append(order)
    cores = defaultdict(set)
    by_id = {order.id: order for order in history.recent_orders}
    for pair, orders in events.items():
        if len(orders) >= 3:
            cores[tuple(o.id for o in orders)].update(pair)
    candidates = []
    for order_ids, keys in cores.items():
        orders = tuple(by_id[oid] for oid in order_ids)
        routine = learn_schedule(keys, orders, zone)
        if routine:
            candidates.append(routine)
    chosen = []
    # Evaluate the best-supported patterns first. Suppress subsets of a stronger pattern.
    for routine in sorted(candidates, key=lambda r: (-len(r.occurrences), -len(r.keys), sorted(r.keys))):
        if any(routine.keys <= prior.keys for prior in chosen):
            continue
        chosen.append(routine)
    return chosen


def rank_by_time(history, at, catalog, ranked):
    matches = defaultdict(float)
    support = defaultdict(int)
    total = defaultdict(int)
    for order in history.recent_orders:
        local = order.at.astimezone(at.tzinfo)
        distance = clock_distance(minute_of_day(local), minute_of_day(at))
        closeness = max(0, 1 - distance / 120)
        weight = closeness * (1 if local.weekday() == at.weekday() else .65)
        for key in {product_key(catalog[sku]) for sku in order.items}:
            total[key] += 1
            if weight:
                matches[key] += weight
                support[key] += 1
    bonuses = {key: 2.0 * weight / (total[key] + 1) for key, weight in matches.items() if support[key] >= 2}
    return [(product, score + bonuses.get(product_key(product), 0)) for product, score in ranked]


def irregular_history_and_orders(history, catalog, routines):
    recurring = defaultdict(set)
    for routine in routines:
        for order in (*routine.previous_occurrences, *routine.occurrences):
            recurring[order.id].update(routine.keys)
    background = History()
    whole_orders = []
    for order in history.recent_orders:
        if order.id not in recurring:
            background.add(order, catalog)
            whole_orders.append(order)
            continue
        items = {sku: qty for sku, qty in order.items.items() if product_key(catalog[sku]) not in recurring[order.id]}
        if items:
            amount = sum(catalog[sku].price * qty for sku, qty in items.items())
            background.add(replace(order, items=items, amount=amount), catalog)
    return background, whole_orders
