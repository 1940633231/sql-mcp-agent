"""Server 侧 Span 封装：把 telemetry 内核与请求级 TraceContext 绑定。

- ``root_span``     中间件入口创建一个基于传入 traceparent 的根 span，
                     并把 trace_id + 根 span_id 写入 TraceContext 供下游延续。
- ``request_child_span``  依据 TraceContext（跨线程也有效）开启 child span，
                     覆盖 Authorization / Database / Query 等同步执行路径。
- ``span_from_remote``    需按传入 trace_id/span_id 造隐式父级时用（Agent 侧注入）。
"""
from __future__ import annotations

from telemetry import (
    Span,
    SpanContext,
    SpanKind,
    build_traceparent,
    flush_exporter,
    parse_traceparent,
    start_exporter,
    tracer,
)
from .context import get_trace_context

__all__ = [
    "tracer",
    "start_exporter",
    "flush_exporter",
    "SpanKind",
    "root_span",
    "request_child_span",
    "trace_context_from_traceparent",
    "child_span_context",
]


def trace_context_from_traceparent(header: str) -> SpanContext | None:
    """从 W3C traceparent 头解析远端 SpanContext（跨服务对齐 trace）。"""
    return parse_traceparent(header)


def child_span_context() -> SpanContext | None:
    """依据当前 TraceContext 构造父 SpanContext；无 span_id 时返回 None。

    TraceContext 由中间件写入，即使在工具执行的线程里也能取到，
    从而让 child span 一定延续与根 span 相同的 trace_id。
    """
    trace = get_trace_context()
    if not trace.trace_id or not trace.span_id:
        return None
    return SpanContext(trace_id=trace.trace_id, span_id=trace.span_id, trace_flags="01")


def root_span(
    name: str,
    *,
    remote: SpanContext | None = None,
    attributes: dict | None = None,
) -> Span:
    """开启根 span。传入远端 traceparent 时沿用其 trace_id/parent，形成跨服务链。"""
    parent = remote if remote is not None else "auto"
    return tracer.start(name, kind=SpanKind.SERVER, parent=parent, attributes=attributes)


def request_child_span(
    name: str,
    *,
    kind: int = SpanKind.INTERNAL,
    attributes: dict | None = None,
) -> Span:
    """开启跟随当前请求的 child span（基于 TraceContext，跨线程安全）。"""
    parent = child_span_context()
    if parent is None:
        # 无请求级 trace 时，退到新的孤立 root（由调用方决定是否使用）。
        return tracer.start(name, kind=kind, parent=None, attributes=attributes)
    return tracer.start(name, kind=kind, parent=parent, attributes=attributes)