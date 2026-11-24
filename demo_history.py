"""Synthetic shopping routines over the last calendar year, in Moscow local time."""
from datetime import datetime, time, timedelta, timezone
from random import Random

MOSCOW = timezone(timedelta(hours=3))


def annual_orders(uid, end, products, catalog):
    end = end.astimezone(MOSCOW).date() - timedelta(days=1)
    try:
        start = end.replace(year=end.year - 1) + timedelta(days=1)
    except ValueError:  # February 29
        start = end.replace(year=end.year - 1, day=28) + timedelta(days=1)
    groups = {}
    for sku in catalog:
        product = products[sku]
        if product["available"]:
            groups.setdefault(product["group"], []).append(sku)
    for pool in groups.values():
        pool.sort(key=lambda sku: (products[sku]["price"], sku))

    pools = {
        "кофе": ["ax-c25bd657628cbc15", "ax-fe778e9bd64cde89", "ax-27809efc3c464d3b"],
        "Молоко коровье": ["ax-06491e8110dffddd", "ax-64eb235fcb07e0f8", "ax-a89408042b9a6ee2"],
        "Яйца куриные": ["ax-eee364ab91d62663", "ax-26d2eb5d227c26d1", "ax-bcfeb25855e84ca2"],
        "лапша": ["ax-1416b14c0e7444f4", "ax-5bf2fd36d43f15fd",
                  "iz-tailanda-lapsha-mama-so-vkusom-krevetki-i-tom-yam-bystrogo-prigotovleniya-60-g"],
        "чипсы": ["chipsy-lay-s-syr-140-g", "ax-cd077f048f45add0", "ax-4d67700f05d482fb"],
        "попкорн": ["popkorn-holy-corn-dlya-svch-nezhnyy-syr-70-g", "popkorn-holy-corn-syr-60-g"],
        "батончик": ["shokoladnyy-batonchik-snickers-super-80-g"],
    }
    day = start
    while day <= end:
        rng = Random(f"annual-v1/{uid}/{day}")
        week = day.weekday()
        items = {}

        def add(group, quantity=1):
            pool = pools[group] if group in pools else groups[group]
            # Regular favourites, with occasional alternatives from the same shelf.
            choices = pool[:3] if rng.random() < .85 else pool[:8]
            sku = rng.choice(choices)
            items[sku] = items.get(sku, 0) + quantity

        if uid == "katya":
            weekend = week in (5, 6)
            probability = (.12, .55, .16, .50, .12, .83, .22)[week]
            hour = rng.choice((10, 11, 12)) if weekend else rng.choice((17, 18, 19))
            if rng.random() < probability:
                add("Молоко коровье", rng.choice((1, 2, 2)))
                add(rng.choice(("Хлеб", "Яйца куриные", "Овсяные хлопья")))
                add(rng.choice(("Бананы", "Яблоки", "Йогурт")), rng.choice((1, 1, 2)))
                if weekend or rng.random() < .3:
                    for group in rng.sample(["Картофель", "Куриное филе", "Морковь", "Творог",
                                             "Сухие макароны", "Огурцы", "Масло сливочное"], rng.randint(2, 4)):
                        add(group)
                if day.month in (11, 12, 1) and rng.random() < .25:
                    add("Мандарины")
        elif uid == "ev":
            hour = rng.choice((12, 12, 13, 14))
            vacation = (day.month == 1 and day.day <= 8) or (day.month == 8 and 10 <= day.day <= 23)
            if not vacation and rng.random() < (.42, .72, .52, .67, .34, .01, .01)[week]:
                if rng.random() < .18:
                    hour = rng.choice((9, 10))
                    add("Круассаны")
                    add("кофе")
                else:
                    add(rng.choice(("Готовые супы", "Готовая паста и лазанья", "Бургеры и сэндвичи")))
                    add("кофе" if rng.random() < .72 else "Вода без газа")
                    if rng.random() < .35:
                        add("Готовые салаты" if rng.random() < .65 else "Круассаны")
        else:
            movie = week in (4, 5)
            hour = rng.choice((19, 20, 21, 22)) if movie else rng.choice((15, 16, 17, 18))
            probability = (.10, .48, .22, .46, .54, .29, .10)[week]
            if day.month in (7, 8) and not movie:
                probability *= .35
            if rng.random() < probability:
                if movie:
                    add(rng.choice(("попкорн", "чипсы")))
                    add("Кола")
                    if rng.random() < .55:
                        add("Замороженная пицца")
                else:
                    add("лапша" if rng.random() < .72 else "Замороженные пельмени", rng.choice((1, 1, 1, 2)))
                    if rng.random() < .83:
                        add(rng.choice(("батончик", "Кола", "Вода без газа", "чипсы")))
        if items:
            moment = datetime.combine(day, time(hour, rng.randrange(60)), MOSCOW)
            yield moment.astimezone(timezone.utc), items
        day += timedelta(days=1)
