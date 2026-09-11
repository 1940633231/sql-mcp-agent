"""只读查询工具：run_query。

仅做 MCP 协议转换——把 QueryService 的结果返回给客户端。
安全校验、LIMIT 限制、执行、错误收敛全部在 QueryService 完成。
"""
from ..services.query_service import QueryService

_service = QueryService()


def run_query(sql: str) -> dict:
    """执行只读 SQL（仅限单条 SELECT），返回最多 max_rows 行数据。"""
    return _service.run(sql)


def register(server) -> None:
    """把本模块的工具注册到 MCP Server。"""
    server.tool(
        description="执行只读 SQL（仅限单条 SELECT），返回最多 %d 行数据"
        % _service.policy.max_rows
    )(run_query)