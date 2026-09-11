"""基于 sqlglot 的 SQL AST 解析。

只做「结构识别」，不做安全判定（判定在 policy / validator 中）。
相比旧的 sqlparse 实现，AST 能穿透子查询/别名/CTE，取到真实表引用与列，
从根本上避免字符串匹配的误判与绕过（如 `FROM (SELECT * FROM user) t`）。

公开 API 一律以「SQL 字符串」为入参，内部再转成表达式树：
    parse(sql) -> ParsedSql        汇总解析（含语句数与全部结构信息）
    split_statements(sql)          拆分为多条规范化语句（识别多语句拼接）
    classify(sql)                  (StatementKind, keyword)
    extract_tables / columns / functions / count_joins / subquery_depth

解析失败（非法 SQL）不抛异常：extract_* 返回空、classify 归为 OTHER，
由 validator 依据「非 SELECT / 无法解析」统一拒绝，保证 Tool 层永远拿到结构化结论。
"""
import re

from sqlglot import exp, parse as _glot_parse
from sqlglot.errors import ParseError

from .models import ParsedSql, StatementKind

DIALECT = "mysql"

# 归入「只读查询」的语句类型（UNION/INTERSECT/EXCEPT 也是 SELECT 家族）
_SELECT_FAMILY = {"select", "union", "intersect", "except"}


def normalize(sql: str) -> str:
    """去掉首尾空白。"""
    return (sql or "").strip()


def strip_trailing_semicolon(sql: str) -> str:
    """去掉结尾分号（追加 LIMIT 时避免拼成 '…; LIMIT' 语法错误）。"""
    return (sql or "").strip().rstrip(";").strip()


def _trees(sql: str) -> list:
    """解析为 sqlglot 表达式树列表；解析失败返回空列表。"""
    if not (sql or "").strip():
        return []
    try:
        return [t for t in _glot_parse(sql, read=DIALECT) if t is not None]
    except ParseError:
        return []
    except Exception:
        # sqlglot 对畸形输入可能抛出多种异常，一律视为「无法解析」
        return []


def _first_tree(sql: str):
    trees = _trees(sql)
    return trees[0] if trees else None


def split_statements(sql: str) -> list[str]:
    """拆分为多条规范化语句（由生成器统一格式）。"""
    return [t.sql(dialect=DIALECT) for t in _trees(sql)]


def _kind_of(tree) -> tuple[StatementKind, str]:
    """语句大类与类型关键字。"""
    if tree.key in _SELECT_FAMILY:
        return StatementKind.SELECT, "SELECT"
    # Command（如 REPLACE 回退）、insert、update、drop 等一律 OTHER
    return StatementKind.OTHER, tree.key.upper()


def classify(sql: str) -> tuple[StatementKind, str]:
    tree = _first_tree(sql)
    if tree is None:
        return StatementKind.OTHER, ""
    return _kind_of(tree)


def _table_ref(table: exp.Table) -> str:
    """真实表引用：[catalog.]db.name，不含别名。"""
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts)


def _cte_names(tree) -> set[str]:
    """CTE 别名集合，用于从表引用中排除（查询内临时视图，非真实表）。"""
    return {cte.alias_or_name for cte in tree.find_all(exp.CTE)}


def _tables_of(tree) -> list[str]:
    ctes = _cte_names(tree)
    out: list[str] = []
    for table in tree.find_all(exp.Table):
        ref = _table_ref(table)
        if not ref or ref in ctes or ref in out:
            continue
        out.append(ref)
    return out


def _columns_of(tree) -> list[str]:
    out: list[str] = []
    for col in tree.find_all(exp.Column):
        name = col.sql(dialect=DIALECT)
        if name and name not in out:
            out.append(name)
    return out


def _resolve_column_refs(tree) -> list[tuple[str, str]]:
    """把每个引用列解析为 (真实表, 列名)，返回去重后的列表。

    归属规则：
      - 带限定的列（e.name / employee.name）：经 FROM/JOIN 的 别名→表 映射解析到真实表；
      - 裸列（name）：单表查询归属该表；多表查询无法判定时保守地归属到所有被引表。
    返回的表名是裸表名（与 ACL 配置键一致），列名转小写便于比对。
    仅用于 ACL 判定，不修改 ParsedSql 的 columns 展示。
    """
    # 别名→真实表：Table.alias_or_name 取别名（无别名则表名本身）
    alias_map: dict[str, str] = {}
    for tbl in tree.find_all(exp.Table):
        real = tbl.name
        key = tbl.alias_or_name
        alias_map[key] = real
        alias_map[real] = real

    real_tables = _tables_of(tree)  # 裸表名列表，用于裸列归属
    refs: list[tuple[str, str]] = []
    for col in tree.find_all(exp.Column):
        col_name = col.parts[-1].this.lower() if isinstance(col.parts[-1], exp.Identifier) else str(col.parts[-1]).lower()
        parts = [p.this for p in col.parts] if col.parts else col.name.lower()
        qualifier = parts[0] if parts and len(parts) > 1 else None
        if qualifier:
            # 别名或表名直接解析；无法解析时退化为「限定符即表名」
            table = alias_map.get(qualifier, qualifier)
            target = (table, col_name)
            if target not in refs:
                refs.append(target)
        elif real_tables:
            # 裸列：单表归属该表；多表则归属所有被引表（保守）
            for t in real_tables:
                target = (t, col_name)
                if target not in refs:
                    refs.append(target)
    return refs


def extract_column_refs(sql: str) -> list[tuple[str, str]]:
    """把 SQL 中的引用列解析为 (真实表, 列名) 列表（供列级 ACL 使用）。"""
    tree = _first_tree(sql)
    return _resolve_column_refs(tree) if tree is not None else []


def _functions_of(tree) -> list[str]:
    """内置函数取类型名（Count->COUNT），未知函数（LOAD_FILE/SLEEP）是 Anonymous 取 name。"""
    out: list[str] = []
    for node in tree.walk():
        if isinstance(node, exp.Func):
            name = node.name if isinstance(node, exp.Anonymous) else type(node).__name__
            name = (name or "").upper()
            if name and name not in out:
                out.append(name)
    return out


def _joins_of(tree) -> int:
    return len(list(tree.find_all(exp.Join)))


def _depth_of(tree) -> int:
    """子查询嵌套层数（取所有 Subquery 的最大祖先 Subquery 链长 +1）。"""
    max_depth = 0
    for sub in tree.find_all(exp.Subquery):
        depth = 0
        parent = sub.parent
        while parent is not None:
            if isinstance(parent, exp.Subquery):
                depth += 1
            parent = parent.parent
        max_depth = max(max_depth, depth + 1)
    return max_depth


def extract_tables(sql: str) -> list[str]:
    tree = _first_tree(sql)
    return _tables_of(tree) if tree is not None else []


def extract_columns(sql: str) -> list[str]:
    tree = _first_tree(sql)
    return _columns_of(tree) if tree is not None else []


def extract_functions(sql: str) -> list[str]:
    tree = _first_tree(sql)
    return _functions_of(tree) if tree is not None else []


def count_joins(sql: str) -> int:
    tree = _first_tree(sql)
    return _joins_of(tree) if tree is not None else 0


def subquery_depth(sql: str) -> int:
    tree = _first_tree(sql)
    return _depth_of(tree) if tree is not None else 0


def is_valid_identifier(name: str, pattern: str) -> bool:
    """标识符白名单校验（默认仅字母/数字/下划线）。"""
    return bool(re.fullmatch(pattern, name or ""))


def parse(sql: str) -> ParsedSql:
    """汇总解析结果；多语句时结构字段仅描述首条语句。"""
    raw = normalize(sql)
    trees = _trees(raw)
    if not trees:
        # 空或无法解析成合法 AST：OTHER + 空结构，交由 validator 拒绝
        return ParsedSql(raw=raw, statements=[], kind=StatementKind.OTHER, keyword="")
    statements = [t.sql(dialect=DIALECT) for t in trees]
    first = trees[0]
    kind, keyword = _kind_of(first)
    return ParsedSql(
        raw=raw,
        statements=statements,
        kind=kind,
        keyword=keyword,
        tables=_tables_of(first),
        columns=_columns_of(first),
        functions=_functions_of(first),
        joins=_joins_of(first),
        subquery_depth=_depth_of(first),
        has_limit=first.find(exp.Limit) is not None,
        has_star=first.find(exp.Star) is not None,
    )
