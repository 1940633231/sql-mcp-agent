"""表结构工具：list_tables / get_schema / search_schema。

仅做参数合法性校验后委托 database.executor / catalog 语义检索；
表名的注入防护靠标识符白名单，语义检索结果为内存数据、不触库。
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

# 语义检索关键词上限（防超长输入拖慢匹配）
_SEARCH_KEYWORD_MAX = 128


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


def search_schema(keyword: str) -> list[dict]:
    """按关键字检索表/列的业务语义（业务名、别名、同义词、描述）。

    命中为内存目录数据、不触库；表级命中按表 ACL 裁剪，列级命中按列 ACL 裁剪，
    敏感列的描述本身也不会泄露。
    """
    if not isinstance(keyword, str):
        raise ValueError("keyword 必须是字符串")
    keyword = keyword.strip()
    if not keyword:
        return []
    if len(keyword) > _SEARCH_KEYWORD_MAX:
        raise ValueError("keyword 过长，最多 %d 字符" % _SEARCH_KEYWORD_MAX)

    policy_context = get_policy_manager().get_context()
    context = current_request_context(source="schema", policy_context=policy_context)
    authorizer = _authorizer.for_policy(policy_context.policy)
    if not authorizer.has_permission(context.principal, "schema:read"):
        raise PermissionError("无权检索表结构")

    hits = _catalog.search(keyword)
    visible: list[dict] = []
    for hit in hits:
        if not authorizer.authorize_table(context.principal, hit.table):
            continue
        if hit.column is not None and not authorizer.authorize_column(
            context.principal, hit.table, hit.column
        ):
            continue
        visible.append(hit.to_dict())
    return visible


def register(server) -> None:
    """把本模块的工具注册到 MCP Server。"""
    server.tool(description="列出业务库中的所有数据表")(list_tables)
    server.tool(description="查看指定数据表的完整结构（列名、类型、是否可空、主键）")(get_schema)
    server.tool(
        description="按关键字检索表/列的业务语义（业务名、别名、同义词、描述）"
    )(search_schema)
