"""把请求、Trace 和会话元数据绑定到当前 MCP 请求的中间件。"""
import uuid
import time

from .lifecycle import lifecycle
from .metrics import metrics
from .context import TraceContext, reset_trace_context, set_trace_context


def _header(ctx, name: str) -> str:
    headers = getattr(getattr(ctx, "request", None), "headers", None) or {}
    try:
        return str(headers.get(name) or "")
    except Exception:
        return ""


def _trace_id(ctx) -> str:
    traceparent = _header(ctx, "traceparent")
    parts = traceparent.split("-")
    if len(parts) >= 3 and len(parts[1]) == 32:
        return parts[1]
    return uuid.uuid4().hex


def _client_id(ctx) -> str:
    params = ctx.session.client_params
    info = getattr(params, "client_info", None) if params else None
    if not info:
        return ""
    name = getattr(info, "name", "") or ""
    version = getattr(info, "version", "") or ""
    return "%s@%s" % (name, version) if version else str(name)


def _tool_name(ctx) -> str:
    if ctx.method == "tools/call":
        params = ctx.params or {}
        return str(params.get("name") or "")
    return str(ctx.method or "")


async def trace_middleware(ctx, call_next):
    lifecycle.begin_request()
    started = time.monotonic()
    token = set_trace_context(
        TraceContext(
            request_id=str(ctx.request_id) if ctx.request_id is not None else "",
            trace_id=_trace_id(ctx),
            session_id=_header(ctx, "mcp-session-id"),
            client_id=_client_id(ctx),
            tool_name=_tool_name(ctx),
        )
    )
    try:
        result = await call_next(ctx)
        metrics.inc("mcp_requests_total", {"method": ctx.method, "status": "ok"})
        return result
    except Exception:
        metrics.inc("mcp_requests_total", {"method": ctx.method, "status": "error"})
        raise
    finally:
        metrics.observe(
            "mcp_request_duration_seconds", time.monotonic() - started,
            {"method": ctx.method}
        )
        lifecycle.end_request()
        reset_trace_context(token)
