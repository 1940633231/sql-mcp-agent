"""请求级可观测性上下文。"""
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class TraceContext:
    request_id: str = ""
    trace_id: str = ""
    session_id: str = ""
    client_id: str = ""
    tool_name: str = ""
    # V0.7：当前（根）Span 的 span_id，供跨线程/下游 child span 延续父子链。
    span_id: str = ""


_trace_context: ContextVar[TraceContext] = ContextVar(
    "sql_mcp_trace_context", default=TraceContext()
)


def get_trace_context() -> TraceContext:
    return _trace_context.get()


def set_trace_context(context: TraceContext):
    return _trace_context.set(context)


def reset_trace_context(token) -> None:
    _trace_context.reset(token)
