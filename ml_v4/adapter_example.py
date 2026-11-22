"""Local integration example. Run from the extracted bundle with Python 3.10+."""
import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from basket import History, Order, read_dataset, timestamp
from personalization import load_personalized


def build_engine(root, catalog):
    return load_personalized(
        catalog, root / "model.json", root / "discovery-model.json",
        root / "personal-policy.json", root / "product-semantic-embedding.json",
    )


def recommend(engine, catalog, rows, user_id, as_of, zone,
              *, allowed, budget=None, size=None, unavailable=()):
    local_zone = ZoneInfo(zone)
    query_at = timestamp(as_of).astimezone(local_zone)
    orders = []
    for row in rows:
        if row["user_id"] != user_id:
            raise ValueError("Order belongs to another user")
        at = timestamp(row["ordered_at"]).astimezone(local_zone)
        if at >= query_at:
            continue
        items = dict(row["items"])
        if any(type(qty) is not int or qty < 1 for qty in items.values()):
            raise ValueError("Order must have positive integer pack counts")
        if not items or set(items) - catalog.keys():
            raise ValueError("Historical order is empty or contains unknown SKU")
        orders.append(Order(row["order_id"], user_id, at, items, row["total_rub"]))
    history = History()
    for order in sorted(orders, key=lambda order: (order.at, order.id)):
        history.add(order, catalog)
    if not history.orders:
        return None
    result = engine.predict(
        history, query_at, budget=budget, size=size,
        allowed=set(allowed), unavailable=unavailable,
    )
    return {"user_id": user_id, "as_of": query_at.isoformat(),
            "history_orders": history.orders, **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="demo_11")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--budget", type=int)
    parser.add_argument("--size", type=int)
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    catalog, _ = read_dataset(root / "catalog.zip", include_orders=False)
    engine = build_engine(root, catalog)
    rows = json.loads((root / "orders-demo-11.json").read_text(encoding="utf-8"))
    if args.user != "demo_11":
        rows = []
    result = recommend(
        engine, catalog, rows, args.user, args.as_of, "Europe/Moscow",
        allowed=catalog.keys(), budget=args.budget, size=args.size,
        unavailable=args.exclude,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
