"""表结构工具：list_tables / get_schema。

仅做参数合法性校验后委托 database.executor；表名的注入防护靠标识符白名单。
"""
from ..auth.context import current_request_context
from ..authorization.manager import get_policy_manager
from ..authorization.service import AuthorizationService
from ..catalog.service import SchemaCatalog
from ..security import parser
from ..security.policy import load_policy

_catalog = SchemaCatalog.shared()
_policy = load_policy()
_authorizer = AuthorizationService()


def list_tables() -> list[str]:
    """列出业务库中的所有数据表。"""
    policy_context = get_policy_manager().get_context()
    context = current_request_context(source="schema", policy_context=policy_context)
    authorizer = _authorizer.for_policy(policy_context.policy)
    if not authorizer.has_permission(context.principal, "schema:list"):
        raise PermissionError("无权列出数据库表")
    return [
        table for table in _catalog.list_tables()
        if authorizer.authorize_table(context.principal, table)
    ]


def get_schema(table_name: str) -> list[dict]:
    """查看指定数据表的完整结构（列名、类型、是否可空、主键）。"""
    if not parser.is_valid_identifier(table_name, _policy.identifier_pattern):
        raise ValueError("非法的表名")
    policy_context = get_policy_manager().get_context()
    context = current_request_context(source="schema", policy_context=policy_context)
    authorizer = _authorizer.for_policy(policy_context.policy)
    if not authorizer.has_permission(context.principal, "schema:read"):
        raise PermissionError("无权读取表结构")
    if not authorizer.authorize_table(context.principal, table_name):
        raise PermissionError("无权查看表：%s" % table_name)
    return [
        row for row in _catalog.get_schema(table_name)
        if authorizer.authorize_column(
            context.principal, table_name, str(row.get("Field") or row.get("field") or "")
        )
    ]


def register(server) -> None:
    """把本模块的工具注册到 MCP Server。"""
    server.tool(description="列出业务库中的所有数据表")(list_tables)
    server.tool(description="查看指定数据表的完整结构（列名、类型、是否可空、主键）")(get_schema)
