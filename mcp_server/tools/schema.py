"""表结构工具：list_tables / get_schema。

仅做参数合法性校验后委托 database.executor；表名的注入防护靠标识符白名单。
"""
from ..database.executor import QueryExecutor
from ..security import parser
from ..security.policy import load_policy

_executor = QueryExecutor()
_policy = load_policy()


def list_tables() -> list[str]:
    """列出业务库中的所有数据表。"""
    return _executor.list_tables()


def get_schema(table_name: str) -> list[dict]:
    """查看指定数据表的完整结构（列名、类型、是否可空、主键）。"""
    if not parser.is_valid_identifier(table_name, _policy.identifier_pattern):
        raise ValueError("非法的表名")
    return _executor.get_schema(table_name)


def register(server) -> None:
    """把本模块的工具注册到 MCP Server。"""
    server.tool(description="列出业务库中的所有数据表")(list_tables)
    server.tool(description="查看指定数据表的完整结构（列名、类型、是否可空、主键）")(get_schema)
