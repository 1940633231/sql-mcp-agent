"""MySQL SQL MCP Server 入口：组装工具与资源，启动传输层。

对外暴露 3 个数据查询工具与 6 个策略管理工具：
    - list_tables    列出业务库所有表
    - get_schema     查看某张表的结构
    - run_query      执行只读 SQL（仅允许单条 SELECT）
策略管理工具按 policy:read/validate/publish 权限分别授权。

另暴露 2 个只读资源（URI 寻址的静态数据，供应用预取/客户端挂载）：
    - database://schema              全库所有表的字段结构
    - database://table/{table_name}  指定表的字段结构（URI 模板资源）

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
from .observability.health import health, metrics_payload, readiness
from .observability.lifecycle import lifecycle
from .observability.middleware import trace_middleware
from .tools import policy_admin as policy_admin_tools
from .tools import query as query_tools
from .tools import schema as schema_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sql_mcp")

_auth_settings, _token_verifier = build_auth_settings()

@asynccontextmanager
async def lifespan(server):
    lifecycle.set_ready()
    logger.info("MCP server ready")
    try:
        yield
    finally:
        lifecycle.set_draining()
        logger.info("MCP server draining in-flight requests")
        await lifecycle.wait_for_drain(config.SHUTDOWN_GRACE_SECONDS)
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


# ---------- Resources：URI 寻址的只读数据 ----------
# Resource 由「应用/用户」控制加载，Tool 由「模型」决定调用；表结构是静态参考数据，
# 适合 GET 语义（可预取、可缓存、可订阅变更），故以 Resource 暴露。

@server.resource(
    "database://schema",
    name="全库表结构",
    mime_type="application/json",
    description="一次读取业务库所有表的字段结构（表名 + 列名/类型/可空/主键/注释）",
)
def full_schema() -> list[dict]:
    return [
        {"table": table, "columns": schema_tools.get_schema(table)}
        for table in schema_tools.list_tables()
    ]


@server.resource(
    "database://table/{table_name}",
    name="单表结构",
    mime_type="application/json",
    description="按 URI 读取指定表的字段结构，如 database://table/company",
)
def table_schema(table_name: str) -> list[dict]:
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
