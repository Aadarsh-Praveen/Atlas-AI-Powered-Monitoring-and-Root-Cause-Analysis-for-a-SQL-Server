"""The ShopLite API application: settings, logging, middleware, error
handling, and routers, put together.

Run (from shoplite/api):
    uvicorn app.main:app --reload --port 8000 --no-access-log
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app import flags, telemetry
from app.config import get_settings
from app.db import QUERY_TIMEOUT_SQLSTATE, describe_sql_error, dispose_engine, is_database_unavailable
from app.middleware import (REQUEST_ID_HEADER, SYNTHETIC_HEADER, VERSION_HEADER, configure_logging,
                            current_request_id, request_context_middleware)
from app.routers import account, admin, cart, catalog, checkout, health, orders

# Loading settings here means a missing or invalid value stops the API at
# startup with a message naming the setting.
settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger("shoplite.app")

# Application Insights (Phase 5): must be set up before FastAPI(...) below,
# because the OpenTelemetry distro instruments FastAPI apps as they're created.
# Off when no connection string is configured (local runs, tests).
telemetry.configure(settings)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Runs once at startup (before yield) and once at shutdown (after)."""
    flags.validate_configured_flags()   # unknown flag names stop the API here
    logger.info("ShopLite API %s starting; session tag %s; allowed origins %s; feature flags on: %s",
                settings.app_version, settings.session_tag,
                ", ".join(settings.allowed_origin_list), ", ".join(flags.enabled_flags()) or "none")
    yield
    dispose_engine()                    # close pooled connections cleanly
    logger.info("ShopLite API stopped")


app = FastAPI(
    title="ShopLite API",
    version=settings.app_version,
    description="Backend for the ShopLite hardware store. Demo sign-in only: not real authentication.",
    lifespan=lifespan,
)

# --- Middleware --------------------------------------------------------------
# Each add wraps everything added before it, so the LAST one added is the
# OUTERMOST. Order here: request context first (inner), CORS last (outer).
# CORS outermost means even error responses get CORS headers, so the browser
# shows the real error instead of a misleading "CORS error".
app.middleware("http")(request_context_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origin_list,               # exact origins only, never "*"
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    # X-Synthetic-Source: the Playwright shoppers send it from the storefront,
    # and browsers ask permission (a preflight) before sending custom headers.
    allow_headers=["Content-Type", "X-Customer-Id", "X-Admin-Token", REQUEST_ID_HEADER,
                   SYNTHETIC_HEADER],
    # Browsers hide custom response headers from JavaScript unless they're
    # exposed. The storefront reads these for error references and display.
    expose_headers=[REQUEST_ID_HEADER, VERSION_HEADER],
    allow_credentials=False,                                   # identity travels in headers, not cookies
)


# --- Database error handling ---------------------------------------------------
# Routers handle the errors they expect (409 stock, 400 report input). These
# handlers catch the rest, so callers get a clear status instead of a crash.

@app.exception_handler(DBAPIError)
async def database_error(_request: Request, error: DBAPIError):
    details = describe_sql_error(error)
    if is_database_unavailable(error):
        # A query timeout usually means the query was blocked by someone else's
        # locks; a connection failure means SQL Server can't be reached.
        timed_out = details.sqlstate == QUERY_TIMEOUT_SQLSTATE
        logger.warning("Database %s (SQLSTATE %s): %s",
                       "query timed out" if timed_out else "unavailable", details.sqlstate, details.message)
        return JSONResponse(status_code=503, content={
            "detail": "The store is busy right now. Please try again in a moment."})

    logger.error("Database error %s (SQLSTATE %s): %s", details.number, details.sqlstate,
                 details.message, exc_info=error)
    telemetry.record_exception(error)       # -> AppExceptions (it's a 500, not a crash)
    return JSONResponse(status_code=500, content={
        "detail": f"Unexpected database error. Reference: {current_request_id()}"})


@app.exception_handler(PoolTimeoutError)
async def pool_exhausted(_request: Request, _error: PoolTimeoutError):
    # Every pooled connection stayed busy for DB_POOL_TIMEOUT_SECONDS. Usually
    # a sign that queries are slow or blocked, not that the pool is too small.
    logger.warning("Connection pool exhausted (size %d + %d overflow)",
                   settings.db_pool_size, settings.db_pool_max_overflow)
    return JSONResponse(status_code=503, content={
        "detail": "The store is busy right now. Please try again in a moment."})


# --- Routers -------------------------------------------------------------------
for module in (health, catalog, account, cart, checkout, orders, admin):
    app.include_router(module.router)
