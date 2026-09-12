"""MySQL SQL MCP Server 入口：组装工具与资源，启动传输层。

对外暴露 6 个数据查询工具与 6 个策略管理工具：
    - list_tables    列出业务库所有表
    - get_schema     查看某张表的结构
    - search_schema  按业务语义检索表/列
    - run_query      执行只读 SQL（仅允许单条 SELECT）
    - sales_summary / company_ranking / industry_analysis
                     领域查询工具（参数白名单 + 驱动绑定，复用 QueryService 安全链路）
策略管理工具按 policy:read/validate/publish 权限分别授权。

另暴露 2 个只读资源（URI 寻址的静态数据，供应用预取/客户端挂载）：
    - database://schema              全库所有表的完整结构 DTO
    - database://table/{table_name}  指定表的完整结构 DTO（URI 模板资源）

生产级防护见 mcp_server/security/：只读白名单、单语句、危险关键字、
系统库封禁、行数上限、执行超时（策略集中在 configs/security.yaml）。

运行（在项目根目录）：
    python -m mcp_server.server                  # HTTP（Streamable HTTP，默认）
    MCP_TRANSPORT=stdio python -m mcp_server.server   # stdio（子进程模式）
"""
import logging
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import config
from .auth.token_verifier import build_auth_settings
from .database.connection import close_pools
from .observability.health import (
    alerts_payload,
    dashboard_payload,
    health,
    metrics_payload,
    readiness,
    traces_payload,
)
from .observability import alerts as alerts_mod
from .observability import tracing as tracing_mod
from .observability.lifecycle import lifecycle
from .observability.middleware import trace_middleware
from .authorization import audit as audit_mod
from .tools import domain as domain_tools
from .tools import policy_admin as policy_admin_tools
from .tools import query as query_tools
from .tools import schema as schema_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sql_mcp")

_auth_settings, _token_verifier = build_auth_settings()

@asynccontextmanager
async def lifespan(server):
    lifecycle.set_ready()
    # V0.7：启动可观测系统（审计后台写入、OTLP 导出、进程内排空结束）。
    audit_mod.audit_start()
    tracing_mod.start_exporter()
    alerts_mod.alert_engine.start()
    logger.info("MCP server ready")
    try:
        yield
    finally:
        lifecycle.set_draining()
        logger.info("MCP server draining in-flight requests")
        await lifecycle.wait_for_drain(config.SHUTDOWN_GRACE_SECONDS)
        audit_mod.audit_flush()
        audit_mod.audit_stop()
        tracing_mod.tracer.buffer.clear()
        close_pools()
        lifecycle.set_stopped()
        logger.info("MCP server stopped")

server = MCPServer(
    name="mysql-sql-agent",
    auth=_auth_settings,
    token_verifier=_token_verifier,
    middleware=[trace_middleware],
    lifespan=lifespan,
)

schema_tools.register(server)
policy_admin_tools.register(server)
query_tools.register(server)
domain_tools.register(server)


@server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(request: Request) -> Response:
    return JSONResponse(await health())


@server.custom_route("/readyz", methods=["GET"], include_in_schema=False)
async def readyz(request: Request) -> Response:
    payload = await readiness()
    return JSONResponse(payload, status_code=200 if payload["status"] == "ready" else 503)


@server.custom_route("/metrics", methods=["GET"], include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    return Response(metrics_payload(), media_type="text/plain; version=0.0.4")


@server.custom_route("/traces", methods=["GET"], include_in_schema=False)
async def traces_endpoint(request: Request) -> Response:
    return JSONResponse(traces_payload())


@server.custom_route("/dashboard", methods=["GET"], include_in_schema=False)
async def dashboard_endpoint(request: Request) -> Response:
    return JSONResponse(dashboard_payload())


@server.custom_route("/alerts", methods=["GET"], include_in_schema=False)
async def alerts_endpoint(request: Request) -> Response:
    return JSONResponse(alerts_payload())


# ---------- Resources：URI 寻址的只读数据 ----------
# Resource 由「应用/用户」控制加载，Tool 由「模型」决定调用；表结构是静态参考数据，
# 适合 GET 语义（可预取、可缓存、可订阅变更），故以 Resource 暴露。

@server.resource(
    "database://schema",
    name="全库表结构",
    mime_type="application/json",
    description="一次读取业务库所有表的完整结构（表级元数据 + 外键/索引 + 列含业务描述与枚举）",
)
def full_schema() -> list[dict]:
    return [schema_tools.get_schema(table) for table in schema_tools.list_tables()]


@server.resource(
    "database://table/{table_name}",
    name="单表结构",
    mime_type="application/json",
    description="按 URI 读取指定表的完整结构（含业务描述/外键/索引/枚举），如 database://table/company",
)
def table_schema(table_name: str) -> dict:
    return schema_tools.get_schema(table_name)


def main() -> None:
    if config.MCP_TRANSPORT == "stdio":
        logger.info("MySQL SQL MCP Server 启动（stdio）")
        server.run(transport="stdio")
    else:
        logger.info(
            "MySQL SQL MCP Server 启动（streamable-http）http://%s:%d%s",
            config.MCP_HOST, config.MCP_PORT, config.MCP_PATH,
        )
        server.run(
            transport="streamable-http",
            host=config.MCP_HOST,
            port=config.MCP_PORT,
            streamable_http_path=config.MCP_PATH,
        )


if __name__ == "__main__":
    main()
