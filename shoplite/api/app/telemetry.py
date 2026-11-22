"""Application Insights for the API (Phase 5, API 1.2.0), via OpenTelemetry.

OpenTelemetry is the vendor-neutral standard for traces, metrics, and logs;
Microsoft's "Azure Monitor OpenTelemetry distro" (azure-monitor-opentelemetry)
packages it with an exporter that sends to Application Insights. One call,
configure(), sets it all up. What arrives in the Log Analytics workspace:

  AppRequests      one row per HTTP request: route, status code, duration,
                   plus custom dimensions (below). FastAPI is instrumented
                   automatically by the distro.
  AppDependencies  one row per SQL call (SQLAlchemy instrumentation): the
                   parameterized SQL text, never the values, and its duration.
                   Also calls to the (simulated) ranking service (ranking.py).
  AppExceptions    unhandled errors and 500-level database errors, with the
                   stack trace, linked to their request by OperationId.

Custom dimensions on every request (Properties in AppRequests):
  shoplite.synthetic_source   the X-Synthetic-Source tag ("playwright/buyer",
                              "locust/browse", "-" for real people)
  shoplite.request_id         the X-Request-ID, also in the API's log lines

Choices:
  - Off unless APPLICATIONINSIGHTS_CONNECTION_STRING is set: local runs and
    tests send nothing and need no Azure.
  - Sampling: only TELEMETRY_SAMPLING_RATIO of requests (default 0.25) are
    kept, each with all its dependencies. Every kept row carries an ItemCount
    (4 at 25%), so sum(ItemCount) still gives true totals, which is how the
    alert rules count (observability/alerts/api-*.md). Keeps the volume far
    inside Log Analytics' free allowance.
  - No log export: the API's log lines already go to files on the VM; sending
    them again would double the ingestion for little gain.
  - service.name "shoplite-api" and service.version (the app version) become
    the cloud role name and application version in Application Insights, so a
    regression can be tied to the release that introduced it.
"""

import logging
import socket

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from app.config import Settings

logger = logging.getLogger("shoplite.telemetry")

# Python's logging is collected only from this logger, which nothing writes
# to: the distro's log export is effectively off (see the module docstring).
UNUSED_LOGGER = "shoplite.telemetry.export-disabled"

_enabled = False


def configure(settings: Settings) -> bool:
    """Turn on Application Insights if a connection string is configured.
    Must run BEFORE the FastAPI app is created: the distro instruments FastAPI
    by hooking into the app's creation."""
    global _enabled
    secret = settings.applicationinsights_connection_string
    # "Not set" includes an empty line in .env (APPLICATIONINSIGHTS_CONNECTION_STRING=),
    # which pydantic reads as an empty string, not as None.
    if secret is None or not secret.get_secret_value().strip():
        logger.info("Telemetry off (APPLICATIONINSIGHTS_CONNECTION_STRING not set)")
        return False

    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.sdk.resources import Resource

    configure_azure_monitor(
        connection_string=secret.get_secret_value(),
        resource=Resource.create({
            "service.name": "shoplite-api",
            "service.version": settings.app_version,
            "service.instance.id": socket.gethostname(),
        }),
        sampling_ratio=settings.telemetry_sampling_ratio,
        logger_name=UNUSED_LOGGER,
    )
    _enabled = True
    logger.info("Telemetry on: Application Insights, sampling %.0f%% of requests",
                settings.telemetry_sampling_ratio * 100)
    return True


def instrument_engine(engine) -> None:
    """Record every SQL call made through this SQLAlchemy engine as a dependency.
    Called by app/db.py when it creates the engine. SQL comments that carry
    trace IDs into SQL Server ("sqlcommenter") stay off: they would change
    every statement's text and defeat SQL Server's plan reuse."""
    if not _enabled:
        return
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    SQLAlchemyInstrumentor().instrument(engine=engine, enable_commenter=False)


def tag_request(synthetic_source: str, request_id: str) -> None:
    """Add ShopLite's custom dimensions to the current request's span. Does
    nothing when telemetry is off (the span then isn't recording)."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("shoplite.synthetic_source", synthetic_source)
        span.set_attribute("shoplite.request_id", request_id)


def record_exception(error: BaseException) -> None:
    """Attach an error to the current request's span (-> AppExceptions) and mark
    the request failed. For errors the API turns into a 500 itself, which the
    automatic instrumentation therefore never sees as exceptions."""
    span = trace.get_current_span()
    if span.is_recording():
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, type(error).__name__))
