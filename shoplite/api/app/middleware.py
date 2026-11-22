"""Request middleware: request IDs, the version header, and request logging.

Every request gets an ID. It is:
  - returned to the caller in the X-Request-ID response header,
  - included in every log line written while handling the request,
  - shown in 500 error messages as a "Reference",
so one request can be traced from the browser through the logs.

Every response also carries X-App-Version, the API release that handled it.

Synthetic traffic (Phase 3): the Playwright shoppers and Locust send
X-Synthetic-Source (e.g. "playwright/buyer", "locust/browse"). It's written
into the request's log line, and nothing else: tagged and untagged requests
are handled exactly the same. The tag only lets later analysis (and Atlas's
evaluations) tell simulated customers from real ones.
"""

import contextvars
import logging
import re
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from app import telemetry
from app.config import get_settings

REQUEST_ID_HEADER = "X-Request-ID"
VERSION_HEADER = "X-App-Version"
SYNTHETIC_HEADER = "X-Synthetic-Source"

# A context variable holds a value per request (per asyncio task), so code
# anywhere in the request can read the current ID without passing it around,
# and concurrent requests never see each other's IDs.
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# An incoming ID is reused only if it's short and plain. Anything else is
# replaced, so callers can't inject odd characters or huge values into logs.
SAFE_INCOMING_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# The synthetic tag gets the same treatment, plus "/" for "tool/persona".
# An invalid tag is ignored (logged as "-"), never rejected: rejecting it would
# treat tagged traffic differently from real traffic.
SAFE_SYNTHETIC_SOURCE = re.compile(r"^[A-Za-z0-9._/-]{1,64}$")

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
    incoming_source = request.headers.get(SYNTHETIC_HEADER, "")
    synthetic_source = incoming_source if SAFE_SYNTHETIC_SOURCE.match(incoming_source) else "-"
    request.state.synthetic_source = synthetic_source
    # Application Insights custom dimensions (no-op when telemetry is off).
    telemetry.tag_request(synthetic_source, request_id)
    started = time.perf_counter()

    try:
        try:
            response = await call_next(request)
        except Exception as error:
            # Nothing else handled this error. Log the full traceback (with the
            # request ID) for us, and give the caller a clean message with a
            # reference instead of a stack trace. Also record it for
            # Application Insights (AppExceptions): since the error becomes a
            # normal 500 response here, the automatic instrumentation would
            # otherwise never see the exception.
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            telemetry.record_exception(error)
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
        # and personal data. "synthetic -" means a real (untagged) request.
        logger.log(level, "%s %s -> %d in %.0f ms (customer %s, synthetic %s)",
                   request.method, path, response.status_code, elapsed_ms, customer,
                   synthetic_source)
        return response
    finally:
        # Restore the previous value so the ID can't leak into later work.
        _request_id.reset(token)
