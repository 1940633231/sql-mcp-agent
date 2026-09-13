"""V0.7 Observability Hardening：有界直方图、标签卫生、traceparent/span、告警、审计。"""

import pytest

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


# ---------------- 7. 告警：可控时钟的 pending -> firing -> resolved ----------------
class _FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_alert_rule_reaches_firing_and_resolved(monkeypatch):
    from mcp_server.observability import alerts as alerts_mod
    from mcp_server.observability.metrics import metrics as m

    clock = _FakeClock()
    engine = alerts_mod.AlertEngine(interval_seconds=60, clock=clock)
    err = {"code": "query_error", "tool": "run_query", "principal": "query"}

    def breach():
        m.inc("query_executions_total", {}, 1)
        m.inc("query_errors_total", err, 1)

    # 第 1 次：触发条件为真，规则进入 pending（记录 pending_since）
    breach()
    states = {r["name"]: r for r in engine.evaluate()["rules"]}
    assert states["error_rate_high"]["status"] == "pending"

    # 未达 for=60：仍 pending
    clock.advance(10)
    breach()
    states = {r["name"]: r for r in engine.evaluate()["rules"]}
    assert states["error_rate_high"]["status"] == "pending"

    # 累计达 60s：firing
    clock.advance(50)
    breach()
    states = {r["name"]: r for r in engine.evaluate()["rules"]}
    assert states["error_rate_high"]["status"] == "firing"

    # 保持 firing 不重复
    clock.advance(1)
    breach()
    states = {r["name"]: r for r in engine.evaluate()["rules"]}
    assert states["error_rate_high"]["status"] == "firing"

    # 条件恢复：firing -> resolved
    clock.advance(1)
    states = {r["name"]: r for r in engine.evaluate()["rules"]}
    assert states["error_rate_high"]["status"] == "resolved"


# ---------------- 8. 采样：未采样不再记录/导出，导出队列不重复 ----------------
def test_sampling_ratio_zero_stops_recording(monkeypatch):
    import telemetry as telem
    monkeypatch.setattr(telem, "OTEL_SAMPLE_RATIO", 0.0)
    before = len(telem.tracer.buffer.snapshot())
    span = telem.tracer.start("unsampled.span")
    assert span.recording is False
    telem.tracer.end(span)
    # 未采样：不进入查看缓冲，也不进入导出队列
    assert len(telem.tracer.buffer.snapshot()) == before
    assert telem._EXPORT_QUEUE.empty()


def test_export_queue_is_consumed_not_resent(monkeypatch):
    import telemetry as telem
    monkeypatch.setattr(telem, "OTEL_SAMPLE_RATIO", 1.0)
    # 模拟 exporter 已启动，使 _record 把 span 写入导出队列
    monkeypatch.setattr(telem, "_EXPORTER_THREAD", object())
    while not telem._EXPORT_QUEUE.empty():
        try:
            telem._EXPORT_QUEUE.get_nowait()
        except Exception:
            break
    span = telem.tracer.start("exportable.span")
    telem.tracer.end(span)
    assert telem._EXPORT_QUEUE.qsize() == 1
    drained = telem._drain_export_queue()
    assert len(drained) == 1
    # 出队后再取为空：同一批 span 不会被无限重复导出
    assert telem._drain_export_queue() == []


# ---------------- 9. Agent 根 Span 接通后续链路 ----------------
def test_set_current_links_children_and_traceparent():
    from telemetry import tracer as t, build_traceparent
    root = t.start("agent.run")
    token = t.set_current(root)
    try:
        # child span 沿用 root 的 trace_id 与 parent 链
        child = t.start("agent.tool.run_query", parent=root)
        assert child.context.trace_id == root.context.trace_id
        assert child.parent_span_id == root.context.span_id
        t.end(child)
        # current_traceparent 反映 root，供 client 注入
        tp = t.current_traceparent()
        assert tp == build_traceparent(root.context.trace_id, root.context.span_id)
    finally:
        t.restore(token)
        t.end(root)


# ---------------- 10. 审计 AUDIT_REQUIRED 直接同步并传播异常 ----------------
def test_audit_required_bypasses_queue_and_persists(monkeypatch):
    from mcp_server.authorization import audit as audit_mod

    written = []

    def fake_write(rows):
        written.extend(rows)

    monkeypatch.setattr(audit_mod, "_write_rows", fake_write)
    monkeypatch.setattr(audit_mod.config, "AUDIT_STORE", "mysql")
    monkeypatch.setattr(audit_mod.config, "AUDIT_REQUIRED", True)

    audit_mod.log_event("required_audit", principal="alice")
    assert len(written) == 1
    assert written[0][0] == "required_audit"


def test_audit_required_propagates_write_error(monkeypatch):
    from mcp_server.authorization import audit as audit_mod

    def boom(rows):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(audit_mod, "_write_rows", boom)
    monkeypatch.setattr(audit_mod.config, "AUDIT_STORE", "mysql")
    monkeypatch.setattr(audit_mod.config, "AUDIT_REQUIRED", True)

    with pytest.raises(RuntimeError, match="db unavailable"):
        audit_mod.log_event("required_audit", principal="alice")


def test_audit_file_logging_zero_infra(tmp_path, monkeypatch):
    """零基础设施层：配置 AUDIT_LOG_FILE 后，审计事件以纯 JSON 独立落盘。"""
    import logging
    from mcp_server.authorization import audit as audit_mod

    logfile = tmp_path / "audit" / "events.log"
    monkeypatch.setattr(audit_mod.config, "AUDIT_LOG_FILE", str(logfile))
    monkeypatch.setattr(audit_mod.config, "AUDIT_LOG_MAX_BYTES", 1048576)
    monkeypatch.setattr(audit_mod.config, "AUDIT_LOG_BACKUP_COUNT", 2)
    monkeypatch.setattr(audit_mod.config, "AUDIT_STORE", "log")

    audit_mod._file_handler = None
    audit_mod._ensure_file_logging()
    try:
        audit_mod.log_event("authorization_decision", principal="alice", decision="deny")
        for h in audit_mod.logger.handlers:
            h.flush()
        content = logfile.read_text(encoding="utf-8")
        # 事件行是完整 JSON；不依赖底材料，验证 JSON 反序列化。
        import json
        rows = [line for line in content.splitlines() if line.strip().startswith("{")]
        assert rows, "expected at least one JSON audit row"
        event = json.loads(rows[0])
        assert event["event"] == "authorization_decision"
        assert event["principal"] == "alice"
        # 每行必须是纯 JSON（无日志级别/时间前缀）
        assert all(line.startswith("{") for line in content.splitlines() if line.strip())
    finally:
        # 清理单例，避免污染其它测试的审计日志行为
        for h in list(audit_mod.logger.handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                h.close()
                audit_mod.logger.removeHandler(h)
        audit_mod.logger.propagate = True
        audit_mod._file_handler = None


# ---------------- 11. 错误率统一分母 + 工具失败率计入业务错误 ----------------
def test_error_rate_uses_unified_denominator(monkeypatch):
    from mcp_server.observability import alerts as alerts_mod
    from mcp_server.observability.metrics import Metrics

    # 用全新 Metrics 隔离，避免被文件内其它测试的计数污染
    monkeypatch.setattr(alerts_mod.metrics_mod, "metrics", Metrics())
    m = alerts_mod.metrics_mod.metrics
    engine = alerts_mod.AlertEngine(interval_seconds=60)

    # 只有权限拒绝、根本没有进入数据库执行：错误率应为 1.0（而非 0.0 / 超 100%）
    m.inc("query_requests_total", {}, 5)
    m.inc("query_errors_total", {"code": "permission_denied", "tool": "x", "principal": "query"}, 5)
    sig = engine.evaluate()["signals"]
    assert sig["error_rate"] == 1.0

    # 混入成功请求（加分母不加分子）+ 校验错误：错误率回落到 (0,1]
    m.inc("query_requests_total", {}, 2)
    m.inc("query_errors_total", {"code": "validation_error", "tool": "x", "principal": "query"}, 1)
    sig = engine.evaluate()["signals"]
    assert 0 <= sig["error_rate"] <= 1.0
    assert sig["error_rate"] == 1 / 2


def test_tool_failure_rate_includes_business_errors(monkeypatch):
    from mcp_server.observability import alerts as alerts_mod
    from mcp_server.observability.metrics import Metrics

    monkeypatch.setattr(alerts_mod.metrics_mod, "metrics", Metrics())
    m = alerts_mod.metrics_mod.metrics
    engine = alerts_mod.AlertEngine(interval_seconds=60)

    # 分母只统计 tools/call 的 MCP 请求；业务错误（tool_business_errors_total）进入分子。
    m.inc("mcp_requests_total", {"method": "initialize", "status": "ok"}, 100)  # 不计入分母
    m.inc("mcp_requests_total", {"method": "tools/call", "status": "ok"}, 4)
    m.inc("tool_business_errors_total", {"tool": "run_query"}, 1)
    sig = engine.evaluate()["signals"]
    # 分子=业务错误 1，分母=tools/call 4
    assert sig["tool_failure_rate"] == 1 / 4


# ---------------- 12. V0.7.1：管理端点保护 ----------------
class _FakeReq:
    def __init__(self, host="127.0.0.1", headers=None):
        class _H:
            pass
        self.client = _H()
        self.client.host = host
        self.headers = headers or {}


def test_management_guard_restricts_non_loopback(monkeypatch):
    from mcp_server import server as server_mod
    monkeypatch.setattr(server_mod.config, "MANAGEMENT_AUTH_TOKEN", "")
    monkeypatch.setattr(server_mod.config, "MANAGEMENT_LOOPBACK_ONLY", True)
    assert server_mod._management_guard(_FakeReq(host="127.0.0.1")) is None
    assert server_mod._management_guard(_FakeReq(host="::1")) is None
    denied = server_mod._management_guard(_FakeReq(host="10.0.0.8"))
    assert denied is not None and denied.status_code == 403
    monkeypatch.setattr(server_mod.config, "MANAGEMENT_LOOPBACK_ONLY", False)
    assert server_mod._management_guard(_FakeReq(host="10.0.0.8")) is None


def test_management_guard_requires_bearer_when_token_set(monkeypatch):
    from mcp_server import server as server_mod
    monkeypatch.setattr(server_mod.config, "MANAGEMENT_AUTH_TOKEN", "s3cret")
    ok = server_mod._management_guard(_FakeReq(host="10.0.0.8",
                                               headers={"authorization": "Bearer s3cret"}))
    assert ok is None
    denied = server_mod._management_guard(_FakeReq(host="127.0.0.1"))
    assert denied is not None and denied.status_code == 401


# ---------------- 13. V0.7.1：全零 traceparent 拒绝 + 采样继承 ----------------
def test_traceparent_rejects_all_zero_trace_and_span():
    assert parse_traceparent("00-" + "0" * 32 + "-" + "1" * 16 + "-01") is None
    assert parse_traceparent("00-" + "1" * 32 + "-" + "0" * 16 + "-01") is None


def test_sampling_inherited_from_parent(monkeypatch):
    import telemetry as telem
    # 全局高采样，但父未采样时应继承为未采样（不重新决策）
    span = telem.tracer.start("a", parent=telem.SpanContext("1" * 32, "2" * 16, "00"))
    assert span.recording is False and span.context.trace_flags == "00"
    # 全局低采样，但父已采样时应继承为已采样
    monkeypatch.setattr(telem, "OTEL_SAMPLE_RATIO", 0.0)
    span = telem.tracer.start("b", parent=telem.SpanContext("1" * 32, "2" * 16, "01"))
    assert span.recording is True and span.context.trace_flags == "01"


# ---------------- 14. V0.7.1：required audit 同步更新统一统计 ----------------
def test_audit_required_persist_sync_updates_stats(monkeypatch):
    from mcp_server.authorization import audit as audit_mod

    written = []

    def fake_write(rows):
        written.extend(rows)

    monkeypatch.setattr(audit_mod, "_write_rows", fake_write)
    monkeypatch.setattr(audit_mod.config, "AUDIT_STORE", "mysql")
    monkeypatch.setattr(audit_mod.config, "AUDIT_REQUIRED", True)

    before = audit_mod.audit_stats()["persisted"]
    audit_mod.log_event("required_audit", principal="alice")
    assert len(written) == 1
    stats = audit_mod.audit_stats()
    assert stats["persisted"] == before + 1
    assert stats["healthy"] is True