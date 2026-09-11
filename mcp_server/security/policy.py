"""安全策略：从 configs/security.yaml 加载规则，并提供规则匹配工具函数。

只负责「规则本身」——策略怎么读、某条 SQL 命中哪些规则；是否放行的编排在 validator。
"""
import re
from pathlib import Path

import yaml

from . import parser
from .models import SecurityPolicy

# 默认策略文件：<项目根>/configs/security.yaml
DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "configs" / "security.yaml"

# YAML 缺失时的兜底默认值（与 configs/security.yaml 保持一致）
_DEFAULTS = {
    "allowed_statements": ["SELECT"],
    "max_statements": 1,
    "forbidden_keywords": [],
    "forbidden_patterns": [],
    "blocked_schemas": [],
    "tables": {"allowed": [], "denied": []},
    "columns": {},
    "max_joins": 0,
    "limits": {
        "max_rows": 200,
        "max_result_bytes": 5 * 1024 * 1024,
        "max_sql_length": 10000,
        "max_concurrent_queries": 10,
        "timeout_seconds": 15,
        "enforce_limit": True,
    },
    "identifier_pattern": r"^[A-Za-z0-9_]+$",
}


def load_policy(path: str | Path | None = None) -> SecurityPolicy:
    """读取 YAML 策略；文件不存在或字段缺失时回退到默认值。"""
    policy_path = Path(path) if path else DEFAULT_POLICY_PATH
    data = {}
    if policy_path.exists():
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    merged = {**_DEFAULTS, **data}
    limits = {**_DEFAULTS["limits"], **(data.get("limits") or {})}
    tables = {**_DEFAULTS["tables"], **(data.get("tables") or {})}
    columns = data.get("columns") or {}
    return SecurityPolicy(
        allowed_statements=[str(s).upper() for s in merged["allowed_statements"]],
        max_statements=int(merged["max_statements"]),
        forbidden_keywords=list(merged["forbidden_keywords"]),
        forbidden_patterns=list(merged["forbidden_patterns"]),
        blocked_schemas=list(merged["blocked_schemas"]),
        allowed_tables=list(tables.get("allowed") or []),
        denied_tables=list(tables.get("denied") or []),
        columns_acl=_normalize_acl(columns),
        max_joins=int(merged["max_joins"] or 0),
        max_rows=int(limits["max_rows"]),
        max_result_bytes=int(limits.get("max_result_bytes") or _DEFAULTS["limits"]["max_result_bytes"]),
        max_sql_length=int(limits.get("max_sql_length") or _DEFAULTS["limits"]["max_sql_length"]),
        max_concurrent_queries=int(limits.get("max_concurrent_queries") or _DEFAULTS["limits"]["max_concurrent_queries"]),
        timeout_seconds=int(limits["timeout_seconds"]),
        enforce_limit=bool(limits["enforce_limit"]),
        identifier_pattern=str(merged["identifier_pattern"]),
    )


def _normalize_acl(columns: dict) -> dict:
    """把 YAML columns 归一化为 {表名: {"allowed": [], "denied": []}}（键与列名小写）。"""
    out: dict = {}
    for table, rules in (columns or {}).items():
        rules = rules or {}
        out[str(table).lower()] = {
            "allowed": [str(c).lower() for c in rules.get("allowed") or []],
            "denied": [str(c).lower() for c in rules.get("denied") or []],
        }
    return out


def is_statement_allowed(keyword: str, policy: SecurityPolicy) -> bool:
    """语句首关键字是否在白名单内。"""
    return (keyword or "").upper() in {s.upper() for s in policy.allowed_statements}


def find_forbidden_keywords(sql: str, policy: SecurityPolicy) -> list[str]:
    """返回命中的关键字黑名单（大小写不敏感、按单词边界匹配）。"""
    hits = {
        kw.upper()
        for kw in policy.forbidden_keywords
        if re.search(r"\b" + re.escape(kw) + r"\b", sql, re.IGNORECASE)
    }
    return sorted(hits)


def find_forbidden_patterns(sql: str, policy: SecurityPolicy) -> list[str]:
    """返回命中的正则黑名单。"""
    return [pat for pat in policy.forbidden_patterns if re.search(pat, sql, re.IGNORECASE)]


def _bare_table(ref: str) -> str:
    """把表引用归一化为裸表名（去掉引号与库名前缀）。

    例如 `sales.employee` / `sales_demo`.employee / `employee` 都归一为 employee，
    与配置里的裸表名比较。
    """
    seg = ref.strip("`\"[]")
    return seg.rsplit(".", 1)[-1]


def find_denied_tables(tables: list[str], policy: SecurityPolicy) -> list[str]:
    """返回命中的显式禁用表（denied 优先于 allowed）。"""
    denied = {_bare_table(t).lower() for t in policy.denied_tables}
    return [ref for ref in tables if _bare_table(ref).lower() in denied]


def find_disallowed_tables(tables: list[str], policy: SecurityPolicy) -> list[str]:
    """allowed 配置非空时，返回不在允许集合内的表引用（Allowlist 优先）。"""
    if not policy.allowed_tables:
        return []
    allowed = {_bare_table(t).lower() for t in policy.allowed_tables}
    return [ref for ref in tables if _bare_table(ref).lower() not in allowed]


def find_forbidden_columns(
    refs: list[tuple[str, str]], policy: SecurityPolicy
) -> list[str]:
    """按列级 ACL 返回被禁止的引用（"表.列" 形式，含 denied 与 allowed 兜底）。

    refs 形如 [("company", "employees"), ...]（表/列均小写）。
    规则：
      - 该表配置了 denied 且列在其中           -> 拒绝（精确命中敏感列）
      - 该表配置了 allowed 且列不在其中        -> 拒绝（Allowlist 兜底）
    注意：SELECT * 不产生列引用，ACL 无法覆盖；属已知边界。
    """
    hits: list[str] = []
    for table, column in refs:
        rules = policy.columns_acl.get(table)
        if not rules:
            continue
        denied = rules.get("denied") or []
        allowed = rules.get("allowed") or []
        if column in denied or (allowed and column not in allowed):
            hits.append("%s.%s" % (table, column))
    return hits


def find_blocked_schemas(
    sql: str, policy: SecurityPolicy, tables: list[str] | None = None
) -> list[str]:
    """返回命中的系统库。

    两条判据：
      1. `库名.` 限定符（正则可穿透子查询，如 `information_schema.tables`）；
      2. 抽取到的表名其库名前缀命中（覆盖裸表名引用）。
    """
    blocked = {s.lower() for s in policy.blocked_schemas}
    hits: set[str] = set()
    for schema in blocked:
        if re.search(r"\b" + re.escape(schema) + r"\s*\.", sql, re.IGNORECASE):
            hits.add(schema)
    refs = tables if tables is not None else parser.extract_tables(sql)
    for ref in refs:
        qualifier = ref.split(".", 1)[0].strip("`\"[]").lower()
        if qualifier in blocked:
            hits.add(qualifier)
    return sorted(hits)
