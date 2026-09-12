"""SQL 指纹、生命周期、Health 和 Metrics 的可观测性测试。"""
import asyncio

from mcp_server.observability import health
from mcp_server.observability.fingerprint import canonical_sql, sql_fingerprint
from mcp_server.observability.lifecycle import Lifecycle
from mcp_server.observability.metrics import Metrics


def test_sql_fingerprint_normalizes_literals():
    first = "SELECT * FROM sale_records WHERE company_id = 1"
    second = "select * from sale_records where company_id=999"
    assert sql_fingerprint(first) == sql_fingerprint(second)
    assert "?" in canonical_sql(first)


def test_metrics_render_prometheus():
    metrics = Metrics()
    metrics.inc("requests_total", {"status": "ok"})
    metrics.observe("duration_seconds", 0.1, {"method": "tools/call"})
    rendered = metrics.render_prometheus({"inflight": 2})
    assert "requests_total" in rendered
    assert "duration_seconds_count" in rendered
    assert "inflight 2" in rendered


def test_lifecycle_exposes_drain_state():
    lifecycle = Lifecycle()
    assert lifecycle.state.value == "starting"
    lifecycle.begin_request()
    lifecycle.set_draining()
    assert lifecycle.inflight == 1
    lifecycle.end_request()
    assert asyncio.run(lifecycle.wait_for_drain(0.1)) is True


def test_readiness_combines_policy_and_database(monkeypatch):
    class Policy:
        default_principal = "local-dev"

    class Context:
        policy = Policy()
        version = "policy-test"
        policy_hash = "hash"
        source = "memory"

    class Manager:
        def get_context(self):
            return Context()

    health.lifecycle.set_ready()
    monkeypatch.setattr(health, "get_policy_manager", lambda: Manager())
    monkeypatch.setattr(health.business_pool, "probe", lambda: True)
    payload = asyncio.run(health.readiness())
    assert payload["status"] == "ready"
    assert payload["policy_version"] == "policy-test"


def test_metrics_payload_contains_pool_gauges(monkeypatch):
    monkeypatch.setattr(
        health,
        "pool_stats",
        lambda: {
            "business": {"in_use": 1, "available": 2, "created": 3},
            "policy": {"in_use": 0, "available": 1, "created": 1},
        },
    )
    rendered = health.metrics_payload()
    assert "db_pool_in_use_business 1" in rendered
    assert "db_pool_created_policy 1" in rendered
