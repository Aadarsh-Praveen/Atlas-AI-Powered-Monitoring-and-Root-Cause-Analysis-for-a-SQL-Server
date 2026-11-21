"""Request middleware: request IDs, the version header, and request logging.

Every request gets an ID. It is:
  - returned to the caller in the X-Request-ID response header,
  - included in every log line written while handling the request,
  - shown in 500 error messages as a "Reference",
so one request can be traced from the browser through the logs.

Every response also carries X-App-Version, the API release that handled it.
"""

import contextvars
import logging
import re
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import get_settings

REQUEST_ID_HEADER = "X-Request-ID"
VERSION_HEADER = "X-App-Version"

# A context variable holds a value per request (per asyncio task), so code
# anywhere in the request can read the current ID without passing it around,
# and concurrent requests never see each other's IDs.
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# An incoming ID is reused only if it's short and plain. Anything else is
# replaced, so callers can't inject odd characters or huge values into logs.
SAFE_INCOMING_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

logger = logging.getLogger("shoplite.request")


def current_request_id() -> str:
    """The ID of the request being handled ('-' outside a request)."""
    return _request_id.get()


class RequestIdLogFilter(logging.Filter):
    """Adds the current request ID to every log record as %(request_id)s."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def configure_logging(level: str) -> None:
    """Send the API's logs (the "shoplite" logger tree) to the console.

    Format: time, level, logger name, [request id], message. Only the
    shoplite.* loggers are configured; Uvicorn and SQLAlchemy keep their own.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s"))
    handler.addFilter(RequestIdLogFilter())

    app_logger = logging.getLogger("shoplite")
    app_logger.handlers[:] = [handler]   # replace, so reloads don't add duplicates
    app_logger.setLevel(level)
    app_logger.propagate = False         # don't also print via the root logger


async def request_context_middleware(request: Request, call_next):
    """Wrap every request: assign an ID, time it, add headers, log the outcome."""
    incoming = request.headers.get(REQUEST_ID_HEADER, "")
    request_id = incoming if SAFE_INCOMING_ID.match(incoming) else uuid.uuid4().hex
    token = _request_id.set(request_id)
    request.state.request_id = request_id
    started = time.perf_counter()

    try:
        try:
            response = await call_next(request)
        except Exception:
            # Nothing else handled this error. Log the full traceback (with the
            # request ID) for us, and give the caller a clean message with a
            # reference instead of a stack trace.
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            response = JSONResponse(
                status_code=500,
                content={"detail": f"Unexpected error. Reference: {request_id}"},
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[VERSION_HEADER] = get_settings().app_version

        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        customer = request.headers.get("X-Customer-Id") or "-"
        level = logging.ERROR if response.status_code >= 500 else logging.INFO
        # Never log X-Admin-Token or request bodies: they can hold secrets
        # and personal data.
        logger.log(level, "%s %s -> %d in %.0f ms (customer %s)",
                   request.method, path, response.status_code, elapsed_ms, customer)
        return response
    finally:
        # Restore the previous value so the ID can't leak into later work.
        _request_id.reset(token)
