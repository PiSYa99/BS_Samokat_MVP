"""Shop operations shared by the Python server and the GitHub Pages worker."""
import json
from datetime import datetime
from uuid import uuid4

import store


class ShopError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def one(db, sql, params, message):
    row = db.execute(sql, params).fetchone()
    if row is None:
        raise ShopError(404, message)
    return row


def user(db, uid):
    return one(db, "SELECT * FROM users WHERE id=?", (uid,), "Профиль не найден")


def session(db, sid, *, active=True):
    row = one(db, "SELECT * FROM sessions WHERE id=?", (sid,), "Сессия не найдена")
    if active and row["closed"]:
        raise ShopError(409, "Этот заказ уже оформлен")
    return row


def offer(db, s, key):
    if s["mode"] != "personal":
        raise ShopError(409, "Наборы доступны в персональном режиме")
    return one(db, "SELECT * FROM offers WHERE id=? AND user_id=?", (key, s["user_id"]),
               "Набор не найден у этого пользователя")


def snapshot(s):
    items = json.loads(s["cart"])
    return dict(id=s["id"], user_id=s["user_id"], mode=s["mode"], revision=s["revision"],
                items=items, total=store.total(items))


def check_revision(s, rev):
    if s["revision"] != rev:
        raise ShopError(409, "Корзина изменилась. Проверьте её состав и повторите действие.")


def valid(items):
    if items.keys() - store.PRODUCTS.keys():
        raise ShopError(422, "Неизвестный товар")
    if any(not store.PRODUCTS[sku]["available"] for sku in items):
        raise ShopError(422, "Цена товара уточняется")


def event(db, sid, action, payload):
    db.execute("INSERT INTO events(session_id,at,action,payload) VALUES(?,?,?,?)",
               (sid, store.now().isoformat(), action, store.pack(payload)))


def catalog():
    return list(store.PRODUCTS.values())


def users():
    with store.database() as db:
        return [dict(r) for r in db.execute("SELECT * FROM users ORDER BY rowid")]


def orders(uid: str):
    with store.database() as db:
        user(db, uid)
        return store.history(db, uid)


def begin(uid: str, body):
    with store.database(True) as db:
        user(db, uid)
        s = db.execute("SELECT * FROM sessions WHERE user_id=? AND mode=? AND closed=0",
                       (uid, body.mode)).fetchone()
        if not s:
            sid = uuid4().hex
            db.execute("INSERT INTO sessions(id,user_id,mode,started_at) VALUES(?,?,?,?)",
                       (sid, uid, body.mode, store.now().isoformat()))
            event(db, sid, "shopping_started", {})
            s = session(db, sid)
        return snapshot(s)


def register_offers(db, uid, bundles):
    db.executemany("INSERT OR IGNORE INTO offers VALUES(?,?,?,?)",
                   [(b["id"], uid, b["name"], store.pack(b["items"])) for b in bundles])


def recommendation_page(sid, advance_from=None):
    with store.database(True) as db:
        s = session(db, sid)
        if s["mode"] == "ordinary":
            return dict(bundles=[], version="", has_more=False)
        uid = s["user_id"]
        orders = store.history(db, uid)
        latest_order = orders[0]["order_id"] if orders else ""
        latest = f"pairs-v3/{latest_order}"
        saved = db.execute("SELECT * FROM recommendation_pages WHERE session_id=?", (sid,)).fetchone()
        if saved and saved["latest_order"] == latest:
            pages = json.loads(saved["pages"])
            position = saved["position"]
        else:
            pages = store.build_pages(uid, orders)
            position = 0
            db.execute("INSERT OR REPLACE INTO recommendation_pages VALUES(?,?,?,?)",
                       (sid, latest, position, store.pack(pages)))
        # A retry or a second tab submitting the same version gets the current page.
        if advance_from == f"{latest}/{position}" and position + 1 < len(pages):
            position += 1
            db.execute("UPDATE recommendation_pages SET position=? WHERE session_id=?", (position,sid))
        # Keep one full page ahead, including when reopening an exhausted session.
        if position == len(pages) - 1 and len(pages[position]) == 2:
            shown = {sku for page in pages for bundle in page for sku in bundle["items"]}
            next_pair = store.generate(uid, orders, shown)
            if len(next_pair) == 2:
                pages.append(next_pair)
                db.execute("UPDATE recommendation_pages SET pages=? WHERE session_id=?",
                           (store.pack(pages), sid))
        bundles = pages[position]
        register_offers(db, uid, bundles)
        return dict(bundles=bundles, version=f"{latest}/{position}", has_more=position+1 < len(pages))


def current_recommendations(sid: str):
    return recommendation_page(sid)


def refresh_recommendations(sid: str, body):
    return recommendation_page(sid, body.version)


def recommendations(sid: str, exclude=None):
    if not exclude:
        return recommendation_page(sid)["bundles"]
    with store.database(True) as db:
        s = session(db, sid)
        if s["mode"] == "ordinary":
            return []
        excluded = set(exclude)
        if excluded - store.PRODUCTS.keys():
            raise ShopError(422, "Неизвестный товар")
        uid = s["user_id"]
        bundles = store.generate(uid, store.history(db, uid), excluded)
        register_offers(db, uid, bundles)
        return bundles


def cart(sid: str):
    with store.database() as db:
        return snapshot(session(db, sid))


def change_cart(sid: str, body):
    with store.database(True) as db:
        s = session(db, sid)
        check_revision(s, body.revision)
        valid(body.items)
        items = body.items.copy()
        if body.offer:
            original = json.loads(offer(db, s, body.offer)["items"])
            if not items:
                raise ShopError(422, "Набор пуст")
            previous = json.loads(s["cart"])
            for sku, qty in previous.items():
                items[sku] = items.get(sku, 0) + qty
            if max(items.values()) > 999:
                raise ShopError(422, "Максимум 999 упаковок одного товара")
            event(db, sid, "bundle_added", dict(offer=body.offer, original=original,
                  added=body.items, edited=original != body.items, value=store.total(body.items)))
        else:
            event(db, sid, "cart_changed", dict(before=json.loads(s["cart"]), after=items))
        db.execute("UPDATE sessions SET cart=?,revision=revision+1 WHERE id=?", (store.pack(items), sid))
        return snapshot(session(db, sid))


def log_edit(sid: str, body):
    with store.database(True) as db:
        s = session(db, sid)
        offer(db, s, body.offer)
        valid(body.before)
        valid(body.after)
        event(db, sid, "bundle_edited", dict(offer=body.offer, before=body.before, after=body.after, action=body.action))
    return {"saved": True}


def record_visit(sid: str, body):
    with store.database(True) as db:
        s = session(db, sid, active=False)
        if not s["closed"]:
            end = round(store.now().timestamp() * 1000)
            opened = round(datetime.fromisoformat(s["started_at"]).timestamp() * 1000)
            start = max(opened, end - body.elapsed_ms)
            db.execute("""INSERT INTO visits VALUES (?,?,?,?)
                          ON CONFLICT(session_id,visit_id) DO UPDATE SET
                          end_ms=MAX(visits.end_ms,MIN(?,visits.start_ms+?))""",
                       (sid, str(body.visit_id), start, end, end, body.elapsed_ms))
    return {"saved": True}


def active_seconds(db, sid):
    intervals = db.execute("SELECT start_ms,end_ms FROM visits WHERE session_id=? ORDER BY start_ms",
                           (sid,)).fetchall()
    if not intervals:
        return None
    elapsed, previous = 0, 0
    for start, end in intervals:
        elapsed += max(0, end-max(previous, start))
        previous = max(previous, end)
    return round(elapsed/1000, 3)


def checkout(sid: str, body):
    with store.database(True) as db:
        s = session(db, sid, active=False)
        if s["closed"]:
            row = one(db, "SELECT id,total_rub FROM orders WHERE session_id=?", (sid,), "Заказ не найден")
            return dict(row)
        check_revision(s, body.revision)
        items = json.loads(s["cart"])
        if not items:
            raise ShopError(422, "Корзина пуста")
        at = store.now()
        oid = uuid4().hex
        value = store.total(items)
        store.save_order(db, oid, s["user_id"], at, items, sid)
        elapsed = (at-datetime.fromisoformat(s["started_at"])).total_seconds()
        event(db, sid, "checkout", dict(order_id=oid, duration_seconds=round(elapsed, 3),
                                        active_build_seconds=active_seconds(db, sid), total_rub=value))
        db.execute("UPDATE sessions SET closed=1,cart='{}',revision=revision+1 WHERE id=?", (sid,))
        return dict(id=oid, total_rub=value)


def metrics():
    with store.database() as db:
        rows = db.execute("""SELECT e.id,e.at,e.session_id,s.user_id,s.mode,e.action,e.payload
                             FROM events e JOIN sessions s ON s.id=e.session_id ORDER BY e.id""")
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

