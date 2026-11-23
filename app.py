from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from fastapi.responses import HTMLResponse, JSONResponse

import store
import shop_service as service

Mode = Literal["ordinary", "personal"]
Quantity = Annotated[int, Field(strict=True, ge=1, le=999)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(Input):
    mode: Mode


class Change(Input):
    revision: Annotated[int, Field(strict=True, ge=0)]
    items: dict[str, Quantity]
    offer: str | None = None


class Edit(Input):
    offer: str
    before: dict[str, Quantity]
    after: dict[str, Quantity]
    action: Literal["add", "remove", "replace", "quantity"]


class Visit(Input):
    visit_id: UUID
    elapsed_ms: Annotated[int, Field(strict=True, ge=0)]


class Refresh(Input):
    version: str


class Checkout(Input):
    revision: int


@asynccontextmanager
async def lifespan(app):
    store.initialize()
    yield


app = FastAPI(title="Самокат · демонстрационный магазин", lifespan=lifespan)


@app.get("/api/catalog")
def catalog():
    return service.catalog()


@app.get("/api/users")
def users():
    return service.users()


@app.get("/api/users/{uid}/orders")
def orders(uid: str):
    return service.orders(uid)


@app.post("/api/users/{uid}/sessions")
def begin(uid: str, body: Start):
    return service.begin(uid, body)


@app.get("/api/sessions/{sid}/recommendations/page")
def current_recommendations(sid: str):
    return service.current_recommendations(sid)


@app.post("/api/sessions/{sid}/recommendations/refresh")
def refresh_recommendations(sid: str, body: Refresh):
    return service.refresh_recommendations(sid, body)


@app.get("/api/sessions/{sid}/recommendations")
def recommendations(sid: str, exclude: Annotated[list[str] | None, Query()] = None):
    return service.recommendations(sid, exclude)


@app.get("/api/sessions/{sid}/cart")
def cart(sid: str):
    return service.cart(sid)


@app.patch("/api/sessions/{sid}/cart")
def change_cart(sid: str, body: Change):
    return service.change_cart(sid, body)


@app.post("/api/sessions/{sid}/edits")
def log_edit(sid: str, body: Edit):
    return service.log_edit(sid, body)


@app.post("/api/sessions/{sid}/visits")
def record_visit(sid: str, body: Visit):
    return service.record_visit(sid, body)


@app.post("/api/sessions/{sid}/checkout")
def checkout(sid: str, body: Checkout):
    return service.checkout(sid, body)


@app.get("/api/metrics")
def metrics():
    return service.metrics()


@app.exception_handler(service.ShopError)
async def shop_error(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/", response_class=HTMLResponse)
def homepage():
    return (store.ROOT / "docs/index.html").read_text().replace(
        '<script defer src="pages-client.js?v=pairs-4"></script>', ''
    )


app.mount("/", StaticFiles(directory=store.ROOT / "docs", html=True), name="shop")
