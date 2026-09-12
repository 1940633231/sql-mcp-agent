"""表结构工具：list_tables / get_schema / search_schema。

参数校验、授权裁剪后委托 catalog（表结构走 TTL 缓存；语义检索主要基于内存
元数据，物理名命中在冷缓存/TTL 过期时可能触发一次表结构懒加载）。
表名的注入防护靠标识符白名单。
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


def get_schema(table_name: str) -> dict:
    """查看指定数据表的完整结构（表级元数据 + 外键 + 索引 + 列含业务描述/枚举）。

    返回统一语义 DTO；按列 ACL 裁剪可见列，并把主键/外键/索引裁剪到可见列，
    避免隐藏列名经这些结构泄露。
    """
    if not parser.is_valid_identifier(table_name, _policy.identifier_pattern):
        raise ValueError("非法的表名")
    policy_context = get_policy_manager().get_context()
    context = current_request_context(source="schema", policy_context=policy_context)
    authorizer = _authorizer.for_policy(policy_context.policy)
    if not authorizer.has_permission(context.principal, "schema:read"):
        raise PermissionError("无权读取表结构")
    if not authorizer.authorize_table(context.principal, table_name):
        raise PermissionError("无权查看表：%s" % table_name)

    dto = _catalog.get_table_dict(table_name)
    visible = [
        col for col in dto["columns"]
        if authorizer.authorize_column(context.principal, table_name, col["name"])
    ]
    visible_names = {col["name"] for col in visible}

    def _visible_fk(fk: dict) -> bool:
        local = str(fk.get("COLUMN_NAME") or fk.get("column_name") or "")
        ref_table = str(fk.get("REFERENCED_TABLE_NAME") or fk.get("referenced_table_name") or "")
        ref_col = str(fk.get("REFERENCED_COLUMN_NAME") or fk.get("referenced_column_name") or "")
        if local not in visible_names:
            return False
        if not ref_table:
            return False
        # 被引用表/列同样须通过 ACL，避免外键泄露无权访问的对象
        if not authorizer.authorize_table(context.principal, ref_table):
            return False
        if ref_col and not authorizer.authorize_column(context.principal, ref_table, ref_col):
            return False
        return True

    dto["columns"] = visible
    dto["primary_keys"] = [pk for pk in dto["primary_keys"] if pk in visible_names]
    dto["foreign_keys"] = [fk for fk in dto["foreign_keys"] if _visible_fk(fk)]
    dto["indexes"] = [
        idx for idx in dto["indexes"]
        if str(idx.get("column") or idx.get("Column") or "") in visible_names
    ]
    return dto


def search_schema(keyword: str, deep: bool = False) -> list[dict]:
    """按关键字检索表/列的业务语义（业务名、别名、同义词、描述）。

    命中来自 Catalog 内存语义元数据 + 物理表/列名匹配（物理名匹配在冷缓存或
    TTL 过期时可能触发一次表结构懒加载）；表级命中按表 ACL 裁剪，列级命中按
    列 ACL 裁剪，敏感列的描述本身也不会泄露。deep=True 时对全库每张表的物理
    列名全量扫描（代价：可能对每张表发一次结构查询）。
    """
    if not isinstance(keyword, str):
        raise ValueError("keyword 必须是字符串")
    if not isinstance(deep, bool):
        raise ValueError("deep 必须是布尔值")
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

    hits = _catalog.search(keyword, scan_all=deep)
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
    server.tool(description="查看指定数据表的完整结构（表级元数据/外键/索引/列含业务描述与枚举）")(get_schema)
    server.tool(
        description="按关键字检索表/列的业务语义（业务名/别名/同义词/描述）；deep=true 全库列扫描"
    )(search_schema)
