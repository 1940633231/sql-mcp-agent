"""V0.7 Observability Hardening：有界直方图、标签卫生、traceparent/span、告警、审计。"""

from mcp_server.observability.metrics import Metrics
from mcp_server.observability.context import TraceContext, set_trace_context, reset_trace_context
from mcp_server.observability import tracing
from telemetry import parse_traceparent, build_traceparent


# ---------------- 1. 有界直方图 ----------------
def test_bounded_histogram_computes_quantiles_without_keeping_samples():
    m = Metrics()
    for _ in range(10000):
        m.observe("latency", 0.3, {"method": "run_query"})
    summary = m.snapshot()["histograms"]['latency{method="run_query"}']
    assert summary["count"] == 10000
    assert 0.2 < summary["p95"] < 0.6
    assert summary["sum"] > 0


def test_histogram_render_prometheus_buckets():
    m = Metrics()
    m.observe("duration_seconds", 0.1, {"method": "tools/call"})
    rendered = m.render_prometheus()
    assert "duration_seconds_bucket{method=\"tools/call\",le=\"0.1\"} 1" in rendered
    assert "duration_seconds_count{method=\"tools/call\"} 1" in rendered


# ---------------- 2. 标签卫生 ----------------
def test_forbidden_high_cardinality_labels_are_dropped():
    m = Metrics()
    m.inc("requests_total", {"status": "ok", "sql": "SELECT * FROM secret"})
    m.inc("requests_total", {"status": "err", "input": "x" * 500})
    rendered = m.render_prometheus()
    # sql / input 键被丢弃，原始 SQL 与超长输入不进入 /metrics
    assert 'sql="' not in rendered
    assert 'input="' not in rendered
    assert '"SELECT * FROM secret"' not in rendered
    # 白名单标签仍在
    assert 'status="ok"' in rendered


# ---------------- 3. W3C traceparent + Span ----------------
def test_traceparent_roundtrip():
    trace_id = "0123456789abcdef0123456789abcdef"
    span_id = "abcdef0123456789"
    ctx = parse_traceparent(build_traceparent(trace_id, span_id))
    assert ctx is not None
    assert ctx.trace_id == trace_id
    assert ctx.span_id == span_id
    assert ctx.trace_flags == "01"


def test_traceparent_rejects_bad_input():
    assert parse_traceparent("garbage") is None
    assert parse_traceparent("00-" + "0" * 31 + "-" + "0" * 16 + "-01") is None


def test_request_child_span_extends_trace_context():
    token = set_trace_context(TraceContext(request_id="r1", trace_id="a" * 32, span_id="b" * 16))
    try:
        span = tracing.request_child_span("authorization.decision")
        assert span.context.trace_id == "a" * 32
        assert span.parent_span_id == "b" * 16
        tracing.tracer.end(span)
    finally:
        reset_trace_context(token)


def test_span_error_status_records_error():
    before = len(tracing.tracer.buffer.snapshot())
    span = tracing.request_child_span("test.span")
    tracing.tracer.end(span, status="error", attributes={"code": "boom"})
    snap = tracing.tracer.buffer.snapshot()
    assert len(snap) == before + 1
    last = snap[-1]
    assert last["status"] == "error"
    assert last["attributes"]["code"] == "boom"


# ---------------- 4. 告警信号聚合 ----------------
def test_alert_engine_computes_signals_and_rules():
    from mcp_server.observability import alerts as alerts_mod
    engine = alerts_mod.AlertEngine(interval_seconds=60)
    over = engine.evaluate()
    for key in ("error_rate", "timeout_rate", "tool_failure_rate",
                "latency_p95_seconds", "latency_p99_seconds", "pool_saturation"):
        assert key in over["signals"]
    assert len(over["rules"]) >= 3


# ---------------- 5. 审计（不依赖真实数据库） ----------------
def test_audit_writer_persists_records(monkeypatch):
    from mcp_server.authorization import audit as audit_mod

    written = []

    def fake_write(rows):
        written.extend(rows)

    monkeypatch.setattr(audit_mod, "_write_rows", fake_write)
    monkeypatch.setattr(audit_mod.config, "AUDIT_STORE", "mysql")
    monkeypatch.setattr(audit_mod.config, "AUDIT_ASYNC", False)
    monkeypatch.setattr(audit_mod.config, "AUDIT_REQUIRED", False)

    writer = audit_mod.AuditWriter()
    writer.enqueue({"event": "test", "principal": "alice"})
    writer.stop()
    assert len(written) == 1
    assert writer.stats()["persisted"] == 1
    assert writer.stats()["healthy"] is True


def test_audit_writer_failure_policy_marks_unhealthy(monkeypatch):
    from mcp_server.authorization import audit as audit_mod

    def boom(rows):
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_mod, "_write_rows", boom)
    monkeypatch.setattr(audit_mod.config, "AUDIT_STORE", "mysql")
    monkeypatch.setattr(audit_mod.config, "AUDIT_ASYNC", False)
    monkeypatch.setattr(audit_mod.config, "AUDIT_REQUIRED", False)
    monkeypatch.setattr(audit_mod.config, "AUDIT_FAILURE", "fail")

    writer = audit_mod.AuditWriter()
    writer.enqueue({"event": "test"})
    writer.stop()
    assert writer.stats()["failed"] >= 1
    assert writer.stats()["healthy"] is False


# ---------------- 6. 验收：request_id + trace_id 可定位 ----------------
def test_request_maps_request_id_and_trace_id():
    token = set_trace_context(TraceContext(request_id="req-777", trace_id="f" * 32, span_id="e" * 16))
    try:
        span = tracing.request_child_span("acceptance.check")
        assert span.context.trace_id == "f" * 32
        tracing.tracer.end(span)
    finally:
        reset_trace_context(token)