"""Dispatch requests from the static storefront to the same shop operations as FastAPI."""
import json
from types import SimpleNamespace

import shop_service as shop


READ = {
    "orders": shop.orders,
    "cart": shop.cart,
    "recommendations/page": shop.current_recommendations,
}
WRITE = {
    ("POST", "sessions"): shop.begin,
    ("POST", "recommendations/refresh"): shop.refresh_recommendations,
    ("PATCH", "cart"): shop.change_cart,
    ("POST", "edits"): shop.log_edit,
    ("POST", "visits"): shop.record_visit,
    ("POST", "checkout"): shop.checkout,
}


def dispatch(method, path, payload):
    try:
        if path in ("/api/users", "/api/catalog", "/api/metrics") and method == "GET":
            value = {"/api/users": shop.users, "/api/catalog": shop.catalog,
                     "/api/metrics": shop.metrics}[path]()
        else:
            _, api, resource, key, operation = path.split("/", 4)
            if api != "api" or resource not in ("users", "sessions"):
                raise shop.ShopError(404, "Страница не найдена")
            if method == "GET":
                value = READ[operation](key)
            else:
                body = SimpleNamespace(**json.loads(payload))
                value = WRITE[method, operation](key, body)
        return json.dumps({"value": value}, ensure_ascii=False)
    except shop.ShopError as error:
        return json.dumps({"error": error.detail, "status": error.status_code}, ensure_ascii=False)
