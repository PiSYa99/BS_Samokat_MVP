"""Local shop data and the adapter to the supplied v4 predictor."""
import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
MODEL = ROOT / "ml_v4"
sys.path.insert(0, str(MODEL))
from adapter_example import build_engine, read_dataset, recommend

CATALOG, _ = read_dataset(MODEL / "catalog.zip", include_orders=False)
ENGINE = build_engine(MODEL, CATALOG)


def load_products():
    featured = {p["id"]: p for p in json.loads((ROOT / "data/assortment.json").read_text())}
    photos = json.loads((ROOT / "data/catalog-photos.json").read_text())
    labels = {"Хлеб и выпечка": "Выпечка", "Молоко, яйца и сыр": "Молочное",
              "Вода и напитки": "Напитки", "Снеки": "Перекусы"}
    products = {}
    with ZipFile(MODEL / "catalog.zip") as archive, io.TextIOWrapper(
        archive.open("catalog.csv"), encoding="utf-8-sig"
    ) as stream:
        for row in csv.DictReader(stream):
            sku = row["product_id"]
            department = row["product_category"]
            sections = row["site_sections"].split(" | ")
            available = bool(row["price_rub"]) and row["listed_as_orderable"] == "True"
            products[sku] = {
                "id": sku, "name": row["name"], "unit": row["pack_label"],
                "price": int(row["price_rub"] or row["observed_price_rub"]),
                "available": available, "old_price": int(row["old_price_rub"] or 0) or None,
                "category": labels.get(department, department), "department": department,
                "group": row["product_group"], "sections": sections,
                "vegetable": department == "Овощи и фрукты" and (
                    "Овощи, грибы и зелень" in sections or row["product_group"] == "Помидоры"),
                "new": "Новинки и хиты" in sections, **featured.get(sku, {}),
            }
            if sku in photos:
                products[sku]["image"] = "catalog/" + photos[sku]["file"]
                products[sku]["illustrative_photo"] = photos[sku]["illustrative"]
    return {**{sku: products[sku] for sku in featured}, **products}


PRODUCTS = load_products()
PROFILES = json.loads((ROOT / "data/profiles.json").read_text())
DB = Path(os.environ.get("SAMOKAT_DB", ROOT / "runtime/shop.sqlite3"))


def now():
    return datetime.now(timezone.utc)


def pack(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


@contextmanager
def database(write=False):
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield con
        con.commit()
    finally:
        con.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT, occupation TEXT, habit TEXT);
CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, name TEXT, price INTEGER);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), mode TEXT, started_at TEXT,
 cart TEXT DEFAULT '{}', revision INTEGER DEFAULT 0, closed INTEGER DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS active_session ON sessions(user_id,mode) WHERE closed=0;
CREATE TABLE IF NOT EXISTS visits (
 session_id TEXT REFERENCES sessions(id), visit_id TEXT, start_ms INTEGER, end_ms INTEGER,
 PRIMARY KEY(session_id,visit_id));
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), ordered_at TEXT, total_rub INTEGER,
 items TEXT, prices TEXT, session_id TEXT UNIQUE REFERENCES sessions(id));
CREATE INDEX IF NOT EXISTS user_history ON orders(user_id,ordered_at);
CREATE TABLE IF NOT EXISTS offers (
 id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), name TEXT, items TEXT);
CREATE TABLE IF NOT EXISTS recommendation_pages (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), latest_order TEXT,
 position INTEGER NOT NULL, pages TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, session_id TEXT REFERENCES sessions(id), at TEXT,
 action TEXT, payload TEXT);
"""


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with database(write=True) as db:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        db.executemany("INSERT OR REPLACE INTO products VALUES (?,?,?)",
                       [(p["id"], p["name"], p["price"]) for p in PRODUCTS.values()])
        for user in PROFILES:
            if db.execute("SELECT 1 FROM users WHERE id=?", (user["id"],)).fetchone():
                continue
            db.execute("INSERT INTO users VALUES (?,?,?,?)",
                       tuple(user[k] for k in ("id", "name", "occupation", "habit")))
            for i, items in enumerate(user["orders"]):
                at = now() - timedelta(days=(len(user["orders"])-i)*2, hours=i%3)
                save_order(db, f'{user["id"]}-{i+1:02d}', user["id"], at, items, None)


def total(items):
    return sum(PRODUCTS[sku]["price"] * qty for sku, qty in items.items())


def save_order(db, oid, uid, at, items, session):
    prices = {sku: PRODUCTS[sku]["price"] for sku in items}
    db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
               (oid, uid, at.isoformat(), total(items), pack(items), pack(prices), session))


def history(db, uid):
    rows = db.execute("SELECT * FROM orders WHERE user_id=? ORDER BY ordered_at DESC,id", (uid,))
    return [dict(order_id=r["id"], user_id=uid, ordered_at=r["ordered_at"],
                 total_rub=r["total_rub"], items=json.loads(r["items"]),
                 prices=json.loads(r["prices"]), demo_seed=r["session_id"] is None) for r in rows]


THEMES = {
    "К завтраку": "milk syrniki yogurt porridge banana eggs croissant apples",
    "На обед": "sandwich pizza greens coffee bread salmon",
    "К кино": "cola chips popcorn pizza snickers nuts",
    "На быстрый перекус": "noodles sandwich nuts juice snickers banana",
    "Для домашнего ужина": "potatoes salmon salt cream greens eggs bread",
}


def bundle_name(items, taken):
    tags = Counter(PRODUCTS[sku]["image"] for sku in items)
    scores = {name: sum(tags[k] for k in keys.split()) for name, keys in THEMES.items()}
    best = max(scores, key=scores.get)
    if best not in taken and scores[best]:
        return best
    alternatives = {"К завтраку": "Для неспешного утра", "На обед": "В обеденный перерыв",
                    "К кино": "На вечер с фильмом", "На быстрый перекус": "Перекус с собой",
                    "Для домашнего ужина": "К ужину дома"}
    return alternatives[best] if scores[best] else "Из ваших покупок"


def model_orders(rows):
    result = []
    for row in rows:
        items = {sku: qty for sku, qty in row["items"].items() if sku in CATALOG}
        if items:
            amount = sum(row["prices"][sku] * qty for sku, qty in items.items())
            result.append({**row, "items": items, "total_rub": amount})
    return result


def predict_bundle(uid, rows, available, at, names):
    if not available:
        return None
    prediction = recommend(ENGINE, CATALOG, rows, uid, at, "Europe/Moscow", allowed=available)
    if not prediction or not prediction["items"]:
        return None
    items = {r["product_id"]: r["quantity"] for r in prediction["items"]}
    if not items.keys() <= available:
        raise RuntimeError("Модель вернула SKU за пределами витрины")
    name = bundle_name(items, names)
    key = hashlib.sha256(pack([uid, name, items]).encode()).hexdigest()[:24]
    return dict(id=key, name=name, items=items, total=total(items),
                model_mode=prediction["mode"], history_orders=prediction["history_orders"])


def generate(uid, rows, excluded=()):
    at = now().isoformat()
    rows = model_orders(rows)
    result = []
    available = set(CATALOG).difference(excluded)
    for _ in range(2):
        bundle = predict_bundle(uid, rows, available, at, {b["name"] for b in result})
        if not bundle:
            break
        result.append(bundle)
        available.difference_update(bundle["items"])
    return result


def build_pages(uid, rows):
    first = generate(uid, rows)
    pages = [first]
    if not first:
        return pages
    rows = model_orders(rows)
    seen = {frozenset(b["items"]) for b in first}
    candidates = []
    at = now().isoformat()
    # The same predictor selects items and quantities in each shelf. Removing
    # one of its choices at a time yields its next-best alternatives.
    for tags in THEMES.values():
        shelf = frozenset(sku for sku, p in PRODUCTS.items() if p["image"] in tags.split())
        queue = deque([shelf])
        visited = {shelf}
        while queue:
            available = queue.popleft()
            bundle = predict_bundle(uid, rows, available, at, set())
            if not bundle:
                continue
            contents = frozenset(bundle["items"])
            if len(contents) >= 2 and contents not in seen:
                candidates.append((contents, bundle))
                seen.add(contents)
            if len(shelf) - len(available) < 3:
                for sku in sorted(contents):
                    subset = available - {sku}
                    if len(subset) >= 2 and subset not in visited:
                        visited.add(subset)
                        queue.append(subset)

    # Prefer familiar products initially, then spread exposure across products.
    # Compositions are removed from the pool as they are dealt.
    purchases = Counter(sku for order in rows for sku in order["items"])
    exposure = Counter(sku for b in first for sku in b["items"])
    while candidates:
        previous = {sku for b in pages[-1] for sku in b["items"]}
        page = []
        selected = set()
        names = set()
        for _ in range(2):
            def priority(candidate):
                items, bundle = candidate
                return (len(items & selected), len(items & previous), bundle["name"] in names,
                        sum(exposure[s] for s in items) / len(items),
                        -sum(purchases[s] for s in items) / len(items))
            choice = min(candidates, key=priority)
            items, bundle = choice
            if selected.intersection(items):
                break
            candidates.remove(choice)
            page.append(bundle)
            selected.update(items)
            names.add(bundle["name"])
            if not candidates:
                break
        pages.append(page)
        exposure.update(selected)
    return pages
