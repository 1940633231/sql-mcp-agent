"""零外部依赖的共享遥测内核：OpenTelemetry 数据模型 + W3C traceparent 传播。

被 Server 侧（mcp_server/observability/tracing.py）与 Agent 侧（agent/）复用，
从而让 Agent → MCP → Authorization → Database 四个环节落在同一条 trace 里，
跨服务只靠一份 W3C ``traceparent`` 头对齐 trace_id。

设计取舍：
- 只依赖标准库（无需安装 opentelemetry-* SDK）。
- 完全遵循 W3C Trace Context 的 traceparent 头格式，可被任何 OTel 兼容组件识别。
- Spans 默认落进有界内存环形缓冲（可经 /traces 查看）；配置 OTLP 端点时，
  由后台线程把完成的 spans 以 OTLP/HTTP + JSON 投递到 collector（best-effort）。
- 一律不记录原始 SQL / 完整用户输入等高基数或敏感字段，只记指纹与稳定标签。
"""
from __future__ import annotations

import base64
import json
import os
import queue
import secrets
import threading
import time
import urllib.request
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from contextvars import ContextVar

__all__ = [
    "OTEL_ENDPOINT",
    "OTEL_SERVICE_NAME",
    "OTEL_SAMPLE_RATIO",
    "SpanKind",
    "SpanContext",
    "Span",
    "Tracer",
    "tracer",
    "parse_traceparent",
    "build_traceparent",
    "trace_context_from_traceparent",
    "start_exporter",
    "flush_exporter",
]

OTEL_ENDPOINT = os.getenv("OTEL_OTLP_HTTP_ENDPOINT", os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")).rstrip("/")
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "mysql-sql-agent")
OTEL_SAMPLE_RATIO = float(os.getenv("OTEL_SAMPLE_RATIO", "1.0"))
OTEL_MAX_SPANS = int(os.getenv("OTEL_MAX_SPANS", "2048"))


class SpanKind:
    INTERNAL = 0
    SERVER = 1
    CLIENT = 2
    PRODUCER = 3
    CONSUMER = 4


def _random_hex(nbytes: int) -> str:
    return secrets.token_hex(nbytes)


@dataclass(frozen=True)
class SpanContext:
    """对齐 OTel 的 SpanContext：trace_id / span_id 均为基础十六进制小写串。"""

    trace_id: str
    span_id: str
    trace_flags: str = "01"
    trace_state: str = ""


@dataclass
class Span:
    name: str
    kind: int
    context: SpanContext
    parent_span_id: str
    start_ns: int
    end_ns: int = 0
    status: str = "ok"                # ok|error
    status_message: str = ""
    attributes: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    # True=纳入采样并落地/导出；False=裁剪采样，仅作 trace 血缘传播（不记不导）。
    recording: bool = True

    @property
    def duration_ns(self) -> int:
        return (self.end_ns - self.start_ns) if self.end_ns else 0

    def record_error(self, message: str = "") -> None:
        self.status = "error"
        self.status_message = message or "error"

    def duration_ms(self) -> float:
        return round(self.duration_ns / 1_000_000, 3)


def parse_traceparent(header: str) -> SpanContext | None:
    """解析 W3C traceparent：``version-traceid-spanid-flags``（可含 trace-state）。

    加固：校验非零（全零 trace_id/span_id 视为无效），避免恶意/破损头被当成合法远端。
    """
    if not header:
        return None
    first = header.split(",")[0].strip()
    parts = first.split("-")
    if len(parts) < 4:
        return None
    version, trace_id, span_id, flags = parts[0], parts[1], parts[2], parts[3]
    if version != "00":
        return None
    if len(trace_id) != 32 or len(span_id) != 16:
        return None
    try:
        int(trace_id, 16)
        int(span_id, 16)
        int(flags, 16)
    except ValueError:
        return None
    # 全零属于无效 trace/span id（W3C/OTel 要求），视为无远端上下文。
    if set(trace_id) == {"0"} or set(span_id) == {"0"}:
        return None
    return SpanContext(trace_id=trace_id, span_id=span_id, trace_flags=flags[:2])


def build_traceparent(trace_id: str, span_id: str, flags: str = "01") -> str:
    return "00-%s-%s-%s" % (trace_id, span_id, flags)


def sampled(trace_flags: str) -> bool:
    return (trace_flags or "00") != "00"


class SpanBuffer:
    """有界内存缓冲：只保留最近 N 个完成的 span。

    存储上限恒定（deque(maxlen)），避免 trace 长时间运行撑爆内存。
    """

    def __init__(self, maxlen: int = OTEL_MAX_SPANS):
        self._spans: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, span: Span) -> None:
        with self._lock:
            self._spans.append(span)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [_span_payload(s) for s in list(self._spans)]

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


def _span_payload(span: Span) -> dict:
    return {
        "name": span.name,
        "kind": span.kind,
        "trace_id": span.context.trace_id,
        "span_id": span.context.span_id,
        "parent_span_id": span.parent_span_id,
        "trace_flags": span.context.trace_flags,
        "status": span.status,
        "status_message": span.status_message,
        "start_ns": span.start_ns,
        "end_ns": span.end_ns,
        "duration_ms": span.duration_ms(),
        "attributes": span.attributes,
        "events": span.events,
    }


def _to_otlp_value(value):
    if isinstance(value, bool):
        return {"value": {"boolValue": value}}
    if isinstance(value, int):
        return {"value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"value": {"doubleValue": value}}
    return {"value": {"stringValue": str(value)}}


def _to_otlp_bytes(hex_str: str) -> str:
    """把 hex 串（trace_id/span_id）按 OTLP 的 bytes 字段编码为 base64。"""
    return base64.b64encode(bytes.fromhex(hex_str)).decode("ascii")


def _span_to_otlp(payload: dict) -> dict:
    """把 buffer.snapshot() 里的 span 字典转为 OTLP/JSON span 结构。"""
    status_code = 2 if payload.get("status") == "error" else 1
    s = {
        "traceId": _to_otlp_bytes(payload["trace_id"]),
        "spanId": _to_otlp_bytes(payload["span_id"]),
        "name": payload["name"],
        "kind": payload.get("kind", 0),
        "startTimeUnixNano": str(payload.get("start_ns", 0)),
        "endTimeUnixNano": str(payload.get("end_ns", 0)),
        "attributes": [
            {"key": k, "value": _to_otlp_value(v)}
            for k, v in (payload.get("attributes") or {}).items()
        ],
        "status": {
            "code": status_code,
            "message": payload.get("status_message") or "",
        },
    }
    if payload.get("parent_span_id"):
        s["parentSpanId"] = _to_otlp_bytes(payload["parent_span_id"])
    if payload.get("events"):
        s["events"] = [
            {
                "timeUnixNano": str(ev.get("ts", 0)),
                "name": str(ev.get("name", "")),
            }
            for ev in payload["events"]
        ]
    return s


_EXPORTER_THREAD = None
_EXPORTER_SENT_COUNTER = 0
# 已完成的 Span 的导出队列：worker 消费即出队，绝不重复导出同一批。
_EXPORT_QUEUE: "queue.Queue[Span]" = queue.Queue(maxsize=10000)


def _drain_export_queue() -> list[Span]:
    items: list[Span] = []
    try:
        while True:
            items.append(_EXPORT_QUEUE.get_nowait())
    except queue.Empty:
        return items


def _exporter_worker(endpoint: str) -> None:
    """定时消费导出队列，把已完成的 spans 批量投递到 OTLP/HTTP（JSON）。"""
    batch_seconds = float(os.getenv("OTEL_EXPORTER_BATCH_SECONDS", "5.0"))
    while True:
        time.sleep(batch_seconds)
        items = _drain_export_queue()
        if not items:
            continue
        _post_batch(items, endpoint)


def _post_batch(spans: list[Span], endpoint: str) -> None:
    payload = [_span_payload(s) for s in spans]
    merged: dict[str, list[dict]] = {}
    for item in payload:
        merged.setdefault(item["trace_id"], []).append(item)
    for trace_id, group in merged.items():
        _post_otlp(trace_id, group, endpoint)


def _post_otlp(trace_id: str, spans: list[dict], endpoint: str) -> None:
    global _EXPORTER_SENT_COUNTER
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": OTEL_SERVICE_NAME}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "sql-mcp"},
                            "spans": [_span_to_otlp(s) for s in spans],
                        }
                    ],
                }
            ]
        }
        data = json.dumps(body).encode("utf-8")
        url = endpoint if endpoint.endswith("/v1/traces") else endpoint + "/v1/traces"
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "sql-mcp-telemetry",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            _EXPORTER_SENT_COUNTER += len(spans)
    except Exception:
        # best-effort：导出失败不阻塞业务，落到日志即可。
        import logging
        logging.getLogger("sql_mcp").debug("OTLP export skipped: spans=%d", len(spans))


class Tracer:
    """极简 Tracer：维护当前 span 的 ContextVar，并提供 start/end/contextmanager。"""

    def __init__(self, buffer: SpanBuffer | None = None):
        self._buffer = buffer or SpanBuffer()
        self._current: ContextVar[Span | None] = ContextVar("telemetry_current_span", default=None)

    @property
    def buffer(self) -> SpanBuffer:
        return self._buffer

    def now_ns(self) -> int:
        return time.time_ns()

    def new_trace_id(self) -> str:
        return _random_hex(16)

    def new_span_id(self) -> str:
        return _random_hex(8)

    def get_current(self) -> Span | None:
        return self._current.get()

    def _resolve_parent(self, parent) -> tuple[str, str, str]:
        """返回 (trace_id, parent_span_id, sample_flag)。

        parent 为 None/"auto" 时回退到当前 span；仍无则返空表示新建 root trace。
        """
        if parent is None or parent == "auto":
            parent = self.get_current()
        if parent is None:
            return "", "", ""
        if isinstance(parent, Span):
            return parent.context.trace_id, parent.context.span_id, parent.context.trace_flags
        return parent.trace_id, parent.span_id, parent.trace_flags

    def start(
        self,
        name: str,
        *,
        kind: int = SpanKind.INTERNAL,
        parent: Span | SpanContext | None = "auto",
        attributes: dict | None = None,
    ) -> Span:
        """开启一个新 span。parent 缺省按 当前 span → 新建 root trace 解析。

        采样决定在创建时落定：未采样（recording=False）的 span 只做 trace 血缘
        传播（child 继续沿用同一 trace_id），不写入缓冲、也不进入导出队列。
        """
        trace_id, parent_span_id, flag = self._resolve_parent(parent)
        if not trace_id:
            trace_id = self.new_trace_id()
        sample_flag = _sample_flag(flag)
        span = Span(
            name=name,
            kind=kind,
            context=SpanContext(
                trace_id=trace_id,
                span_id=self.new_span_id(),
                trace_flags=sample_flag,
            ),
            parent_span_id=parent_span_id,
            start_ns=self.now_ns(),
            attributes=dict(attributes or {}),
            recording=(sample_flag == "01"),
        )
        return span

    def _record(self, span: Span) -> None:
        """记录一个完成 span：采样认为不记录时不落地不导出。"""
        if not span.recording:
            return
        self._buffer.add(span)
        if _EXPORTER_THREAD is not None:
            try:
                _EXPORT_QUEUE.put_nowait(span)
            except queue.Full:
                pass  # 队列有界：导出慢时不阻塞业务，直接丢弃最旧批次

    @contextmanager
    def span(self, name, *, kind=SpanKind.INTERNAL, parent="auto", attributes=None):
        """在 ContextVar 上下文里运行一个 span，异常时自动标记 error。"""
        span = self.start(name, kind=kind, parent=parent, attributes=attributes)
        token = self._current.set(span)
        try:
            yield span
        except Exception as exc:
            span.record_error(str(exc)[:512])
            raise
        finally:
            span.end_ns = self.now_ns()
            span.attributes.update({"duration_ms": span.duration_ms()})
            self._record(span)
            self._current.reset(token)

    def end(self, span: Span, status: str = "ok", message: str = "", attributes: dict | None = None):
        if span.end_ns == 0:
            span.end_ns = self.now_ns()
        if attributes:
            span.attributes.update(attributes)
        span.duration_ns
        if status == "error":
            span.status = "error"
            span.status_message = message or span.status_message
        else:
            span.status = "ok"
        span.attributes.update({"duration_ms": span.duration_ms()})
        self._record(span)
        return span

    def set_current(self, span: Span):
        """把 span 设为当前（返回 token，完成后需 restore），供显式根链路使用。"""
        return self._current.set(span)

    def restore(self, token) -> None:
        self._current.reset(token)

    def attach_remote_parent(self, ctx: SpanContext) -> None:
        """把远端传入的 SpanContext 作为隐式父级挂到当前上下文。"""
        current = self.get_current()
        if current is None:
            phantom = Span(
                name="remote",
                kind=SpanKind.INTERNAL,
                context=ctx,
                parent_span_id="",
                start_ns=0,
                end_ns=0,
            )
            self._current.set(phantom)

    def current_traceparent(self) -> str:
        """供注入到下行请求头的 traceparent，值取当前 span。"""
        current = self.get_current()
        if current is None:
            return ""
        return build_traceparent(
            current.context.trace_id,
            current.context.span_id,
            current.context.trace_flags,
        )


def _sample_flag(parent_flag: str) -> str:
    """采样继承：父已采样（01）则子采样，父未采样（00）则子也不采样（不重新决策）；
    仅在无父（新根 trace）时按 OTEL_SAMPLE_RATIO 决策。"""
    if parent_flag == "01":
        return "01"
    if parent_flag == "00":
        return "00"
    if OTEL_SAMPLE_RATIO <= 0:
        return "00"
    if OTEL_SAMPLE_RATIO >= 1.0 or secrets.randbelow(10000) / 10000.0 < OTEL_SAMPLE_RATIO:
        return "01"
    return "00"


tracer = Tracer()


def start_exporter(endpoint: str | None = None) -> None:
    """若配置了 OTLP 端点，启动后台导出线程（幂等）。"""
    global _EXPORTER_THREAD
    endpoint = (endpoint or OTEL_ENDPOINT).rstrip("/")
    if not endpoint or _EXPORTER_THREAD is not None:
        return
    thread = threading.Thread(
        target=_exporter_worker,
        args=(endpoint,),
        daemon=True,
        name="otel-exporter",
    )
    _EXPORTER_THREAD = thread
    thread.start()


def flush_exporter(endpoint: str | None = None) -> None:
    """同步把导出队列里的 Span 立即投递（供 CLI 退出前兜底，避免 daemon 线程丢失）。"""
    endpoint = (endpoint or OTEL_ENDPOINT).rstrip("/")
    if _EXPORTER_THREAD is None or not endpoint:
        return
    items = _drain_export_queue()
    if items:
        _post_batch(items, endpoint)