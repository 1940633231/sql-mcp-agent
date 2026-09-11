"""表结构工具：list_tables / get_schema。

仅做参数合法性校验后委托 database.executor；表名的注入防护靠标识符白名单。
"""
from ..auth.context import current_request_context
from ..authorization.service import AuthorizationService
from ..database.executor import QueryExecutor
from ..security import parser
from ..security.policy import load_policy

_executor = QueryExecutor()
_policy = load_policy()
_authorizer = AuthorizationService()


def list_tables() -> list[str]:
    """列出业务库中的所有数据表。"""
    context = current_request_context(source="schema")
    if not _authorizer.has_permission(context.principal, "schema:list"):
        raise PermissionError("无权列出数据库表")
    return [
        table for table in _executor.list_tables()
        if _authorizer.authorize_table(context.principal, table)
    ]


def get_schema(table_name: str) -> list[dict]:
    """查看指定数据表的完整结构（列名、类型、是否可空、主键）。"""
    if not parser.is_valid_identifier(table_name, _policy.identifier_pattern):
        raise ValueError("非法的表名")
    context = current_request_context(source="schema")
    if not _authorizer.has_permission(context.principal, "schema:read"):
        raise PermissionError("无权读取表结构")
    if not _authorizer.authorize_table(context.principal, table_name):
        raise PermissionError("无权查看表：%s" % table_name)
    return [
        row for row in _executor.get_schema(table_name)
        if _authorizer.authorize_column(
            context.principal, table_name, str(row.get("Field") or row.get("field") or "")
        )
    ]


def register(server) -> None:
    """把本模块的工具注册到 MCP Server。"""
    server.tool(description="列出业务库中的所有数据表")(list_tables)
    server.tool(description="查看指定数据表的完整结构（列名、类型、是否可空、主键）")(get_schema)
