"""AFRC FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from .customer_routes import router as customer_router
from .db import init_db
from .routers import admin_routes, auth_routes, checkout_routes, menu_routes, pages, selfservice_routes
from .services.bill_service import _run_stale_bills_cleanup_sync, _stale_bills_watcher, _sync_legacy_scans_sync
from .services.config_service import _sync_caddy_domain_sync
from .services.payment_service import _amount_sen, _record_captured_payment  # noqa: F401 (re-exported for scripts/test_loyalty_e2e.py)
from .services.r2_storage import _sync_r2_objects_async, close_http_client

logger = logging.getLogger("afrc")

_BACKGROUND_TASKS: set[asyncio.Task] = set()

app = FastAPI(title="AFRC Prototype", version="1.0", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def add_anti_indexing_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response


def _track_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


@app.on_event("startup")
async def startup():
    init_db()
    await asyncio.to_thread(_sync_caddy_domain_sync)
    await asyncio.to_thread(_sync_legacy_scans_sync)
    await asyncio.to_thread(_run_stale_bills_cleanup_sync)
    _track_task(_sync_r2_objects_async(force=True))
    _track_task(_stale_bills_watcher())


@app.on_event("shutdown")
async def shutdown():
    tasks = list(_BACKGROUND_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass
    await close_http_client()


# Serve static frontend assets and register domain routers
app.mount("/static", StaticFiles(directory="frontend"), name="static")

app.include_router(pages.router)
app.include_router(auth_routes.router)
app.include_router(menu_routes.router)
app.include_router(checkout_routes.router)
app.include_router(admin_routes.router)
app.include_router(selfservice_routes.router)
app.include_router(customer_router)
