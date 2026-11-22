"""Tests for API 1.2.0 (Phase 5): telemetry and the search_ranking_v2 flag.

Run with the other tests (from shoplite/api):  python -m pytest

Telemetry never leaves the machine here: the conftest forces an empty
connection string, so the Azure exporter is never configured. The span tests
use OpenTelemetry's own SDK with an in-memory exporter: real spans, kept in a
list, to check what the API attaches to them.

The endpoint tests use the local SalesDB like the smoke tests.
"""

import time
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app import flags, ranking, telemetry
from app.config import get_settings


@pytest.fixture
def spans():
    """A tracer whose finished spans land in a list: (tracer, exporter)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def ranking_settings(monkeypatch, delay_ms: int, error_rate: float):
    """Point app/ranking.py at test values for the simulated ranking service."""
    fake = SimpleNamespace(ranking_service_delay_ms=delay_ms, ranking_service_error_rate=error_rate)
    monkeypatch.setattr(ranking, "get_settings", lambda: fake)


# -----------------------------------------------------------------------------
# Telemetry
# -----------------------------------------------------------------------------
def test_telemetry_is_off_without_a_connection_string():
    assert telemetry.configure(get_settings()) is False


def test_request_spans_get_the_custom_dimensions(spans):
    tracer, exporter = spans
    with tracer.start_as_current_span("GET /api/products"):
        telemetry.tag_request("locust/browse", "abc123")
    attributes = exporter.get_finished_spans()[0].attributes
    assert attributes["shoplite.synthetic_source"] == "locust/browse"
    assert attributes["shoplite.request_id"] == "abc123"


def test_exceptions_are_recorded_on_the_span(spans):
    tracer, exporter = spans
    with tracer.start_as_current_span("GET /api/products"):
        telemetry.record_exception(ValueError("boom"))
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" and event.attributes["exception.type"] == "ValueError"
               for event in span.events)


def test_tagging_without_telemetry_does_nothing():
    # No current span (telemetry off): must not raise.
    telemetry.tag_request("-", "x")
    telemetry.record_exception(RuntimeError("ignored"))


# -----------------------------------------------------------------------------
# search_ranking_v2
# -----------------------------------------------------------------------------
def test_flag_is_registered_and_off_by_default():
    assert "search_ranking_v2" in flags.KNOWN_FLAGS
    assert flags.is_enabled("search_ranking_v2") is False


def test_rerank_puts_name_matches_first_and_keeps_order(monkeypatch):
    ranking_settings(monkeypatch, delay_ms=0, error_rate=0)
    items = [SimpleNamespace(name=n) for n in ("Claw Hammer", "Hammer Drill", "Ball Peen Hammer", "hammer tacker")]
    ordered = [i.name for i in ranking.rerank(items, "Hammer")]
    assert ordered == ["Hammer Drill", "hammer tacker", "Claw Hammer", "Ball Peen Hammer"]


def test_rerank_fails_at_full_error_rate(monkeypatch):
    ranking_settings(monkeypatch, delay_ms=0, error_rate=1)
    with pytest.raises(ranking.RankingServiceError, match="503"):
        ranking.rerank([SimpleNamespace(name="x")], "x")


def test_rerank_waits_the_configured_delay(monkeypatch):
    ranking_settings(monkeypatch, delay_ms=150, error_rate=0)
    started = time.perf_counter()
    ranking.rerank([SimpleNamespace(name="x")], "x")
    assert time.perf_counter() - started >= 0.15


def test_ranking_call_is_a_client_dependency_span(monkeypatch, spans):
    tracer, exporter = spans
    ranking_settings(monkeypatch, delay_ms=0, error_rate=1)
    monkeypatch.setattr(ranking, "tracer", tracer)
    with pytest.raises(ranking.RankingServiceError):
        ranking.rerank([SimpleNamespace(name="x")], "x")
    span = exporter.get_finished_spans()[0]
    assert span.name == "POST /v2/rank" and span.kind.name == "CLIENT"
    assert span.attributes["peer.service"] == "ranking-service"
    assert span.attributes["http.status_code"] == 503
    assert span.status.status_code == StatusCode.ERROR


def flag_on(monkeypatch):
    original = flags.is_enabled
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "search_ranking_v2" or original(name))


def test_search_with_the_flag_on_and_failing_returns_500(client, monkeypatch):
    flag_on(monkeypatch)
    ranking_settings(monkeypatch, delay_ms=0, error_rate=1)
    response = client.get("/api/products", params={"q": "hammer"})
    assert response.status_code == 500
    assert "Reference" in response.json()["detail"]


def test_search_with_the_flag_on_ranks_name_matches_first(client, monkeypatch):
    flag_on(monkeypatch)
    ranking_settings(monkeypatch, delay_ms=0, error_rate=0)
    response = client.get("/api/products", params={"q": "hammer", "page_size": 60})
    assert response.status_code == 200
    names = [item["name"].lower() for item in response.json()["items"]]
    starts = [name.startswith("hammer") for name in names]
    assert starts == sorted(starts, reverse=True)       # every match before every non-match


def test_flag_off_leaves_search_untouched(client, monkeypatch):
    ranking_settings(monkeypatch, delay_ms=0, error_rate=1)   # would fail if it were called
    response = client.get("/api/products", params={"q": "hammer"})
    assert response.status_code == 200
