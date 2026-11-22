"""Search ranking v2: relevance ordering for product search, behind the
feature flag search_ranking_v2 (off by default).

What it does when on: for a search with the default sort, the page of results
is re-ordered so products whose NAME starts with the search term come first
(a "relevance" ranking), as scored by a ranking service.

The ranking service is SIMULATED, like the payment provider at checkout: it
isn't a real network call. It waits RANKING_SERVICE_DELAY_MS, and fails
RANKING_SERVICE_ERROR_RATE of the time. That's deliberate: this flag is the
Phase 5 "API fault" incident, a realistic buggy rollout. Turning it on makes
searches slow (the latency alert) and some of them fail with a 500 (the
error-rate alert), and the evidence points where a real investigation would
look: a new feature switched on, a slow and failing dependency in
Application Insights (AppDependencies, target "ranking-service"), and
RankingServiceError in AppExceptions. The incident injector (Phase 5, B6)
turns the flag on and off; its delay and error rate are settings, so one
incident can be "slow only" (error rate 0) or "errors only" (delay 0).

The call is recorded as an OpenTelemetry CLIENT span, so Application Insights
shows it as a dependency of the search request, with its duration and result,
exactly like a real HTTP call. With telemetry off (locally) the span is a
no-op.
"""

import random
import time

from opentelemetry import trace
from opentelemetry.trace import SpanKind

from app.config import get_settings

RANKING_SERVICE_URL = "http://ranking-service.internal/v2/rank"

tracer = trace.get_tracer("shoplite.ranking")


class RankingServiceError(RuntimeError):
    """The ranking service answered with an error. Not caught by the search
    endpoint: it becomes a 500, as an unhandled dependency failure would."""


def rerank(items: list, term: str) -> list:
    """Return the page of products in relevance order (name starts with the term
    first, otherwise the original order). Calls the ranking service first."""
    settings = get_settings()
    attributes = {
        "peer.service": "ranking-service",
        # Both the older and the newer OpenTelemetry attribute names, so the
        # Azure Monitor exporter shows it as an HTTP dependency either way.
        "http.method": "POST", "http.request.method": "POST",
        "http.url": RANKING_SERVICE_URL, "url.full": RANKING_SERVICE_URL,
    }
    # start_as_current_span records an exception raised inside it on the span
    # (and marks the span failed) before letting it propagate.
    with tracer.start_as_current_span("POST /v2/rank", kind=SpanKind.CLIENT, attributes=attributes) as span:
        time.sleep(settings.ranking_service_delay_ms / 1000)
        if random.random() < settings.ranking_service_error_rate:
            span.set_attribute("http.status_code", 503)
            span.set_attribute("http.response.status_code", 503)
            raise RankingServiceError("Ranking service returned 503 Service Unavailable for POST /v2/rank")
        span.set_attribute("http.status_code", 200)
        span.set_attribute("http.response.status_code", 200)

    prefix = term.lower()
    # sorted() is stable: within each group the original order is kept.
    return sorted(items, key=lambda item: not item.name.lower().startswith(prefix))
