"""SQL 校验流水线：把一次 SQL 字符串变成「放行 / 拒绝」的结论。

结构信息全部来自 parser 的 sqlglot AST（真实表引用/列/函数），非字符串猜测。
顺序（先易后难、先廉价后昂贵，命中即返回）：
    1. 非空
    2. SQL 长度上限（超长直接拒绝，不进 AST）
    3. 可解析（AST 成功）
    4. 单语句
    5. 只读（语句类型白名单）
    6. JOIN 数量上限
    7. 关键字黑名单（辅助防线，兜住 AST 看不到的符号 token，如 @@version）
    8. 正则黑名单（辅助防线）
    9. 系统库封禁（比对 AST 真实表引用）
    10. 表级 Allowlist/Denylist（Allowlist 优先：配置了 allowed 则只允许这些真实表）
    11. 列级 ACL（按表 allowed/denied 列）
    12. 表标识符合法性
"""
from . import parser
from .models import SecurityPolicy, ValidationContext, ValidationResult
from .policy import (
    find_blocked_schemas,
    find_denied_tables,
    find_disallowed_tables,
    find_forbidden_columns,
    find_forbidden_keywords,
    find_forbidden_patterns,
    is_statement_allowed,
    load_policy,
)


class SqlValidator:
    """无状态校验器，持有不可变的策略对象。

    统一接口 validate(sql, context) -> ValidationResult（V0.2 契约）。
    context 目前未参与判定，仅为 V0.3 RBAC / 按请求策略预留。
    """

    def __init__(self, policy: SecurityPolicy | None = None):
        self.policy = policy or load_policy()

    def validate(
        self, sql: str, context: ValidationContext | None = None
    ) -> ValidationResult:
        raw = parser.normalize(sql)
        if not raw:
            return ValidationResult.reject("SQL 不能为空", "empty")
        if self.policy.max_sql_length and len(raw) > self.policy.max_sql_length:
            return ValidationResult.reject(
                "SQL 长度 %d 超过上限 %d" % (len(raw), self.policy.max_sql_length),
                "sql_too_long",
            )

        parsed = parser.parse(raw)
        if parsed.statement_count == 0:
            return ValidationResult.reject("无法解析的 SQL 语句", "parse_error")
        if parsed.statement_count > self.policy.max_statements:
            return ValidationResult.reject("仅允许执行单条 SQL 语句", "multi_statement")
        if not is_statement_allowed(parsed.keyword, self.policy):
            return ValidationResult.reject(
                "仅允许 SELECT 只读查询", "not_readonly", statement_type=parsed.keyword
            )

        if self.policy.max_joins and parsed.joins > self.policy.max_joins:
            return ValidationResult.reject(
                "JOIN 数量 %d 超过上限 %d" % (parsed.joins, self.policy.max_joins),
                "too_many_joins",
                statement_type=parsed.keyword,
                tables=parsed.tables,
                columns=parsed.columns,
            )

        keyword_hits = find_forbidden_keywords(raw, self.policy)
        if keyword_hits:
            return ValidationResult.reject(
                "SQL 中包含被禁止的关键字：%s" % ", ".join(keyword_hits),
                "forbidden_keyword",
                statement_type=parsed.keyword,
                tables=parsed.tables,
                columns=parsed.columns,
            )

        pattern_hits = find_forbidden_patterns(raw, self.policy)
        if pattern_hits:
            return ValidationResult.reject(
                "SQL 命中被禁止的模式：%s" % ", ".join(pattern_hits),
                "forbidden_pattern",
                statement_type=parsed.keyword,
                tables=parsed.tables,
                columns=parsed.columns,
            )

        schema_hits = find_blocked_schemas(raw, self.policy, parsed.tables)
        if schema_hits:
            return ValidationResult.reject(
                "禁止访问系统库：%s" % ", ".join(schema_hits),
                "blocked_schema",
                statement_type=parsed.keyword,
                tables=parsed.tables,
                columns=parsed.columns,
            )

        # 表级 Deny/Allow：先 denied（更具体、优先），再 allowed（Allowlist 兜底）
        denied_hits = find_denied_tables(parsed.tables, self.policy)
        if denied_hits:
            return ValidationResult.reject(
                "访问被禁用的表：%s" % ", ".join(denied_hits),
                "table_denied",
                statement_type=parsed.keyword,
                tables=parsed.tables,
                columns=parsed.columns,
            )

        disallowed = find_disallowed_tables(parsed.tables, self.policy)
        if disallowed:
            return ValidationResult.reject(
                "引用了未允许的表：%s" % ", ".join(disallowed),
                "table_not_allowed",
                statement_type=parsed.keyword,
                tables=parsed.tables,
                columns=parsed.columns,
            )

        # 列级 ACL：把引用列归属到真实表后按表比对
        col_hits = find_forbidden_columns(
            parser.extract_column_refs(raw), self.policy
        )
        if col_hits:
            return ValidationResult.reject(
                "访问被禁止的列：%s" % ", ".join(col_hits),
                "column_not_allowed",
                statement_type=parsed.keyword,
                tables=parsed.tables,
                columns=parsed.columns,
            )

        for table in parsed.tables:
            for segment in table.split("."):
                if segment and not parser.is_valid_identifier(segment, self.policy.identifier_pattern):
                    return ValidationResult.reject(
                        "非法的表名：%s" % table,
                        "bad_identifier",
                        statement_type=parsed.keyword,
                        tables=parsed.tables,
                        columns=parsed.columns,
                    )

        return ValidationResult.approve(
            normalized_sql=parser.strip_trailing_semicolon(parsed.statements[0]),
            statement_type=parsed.keyword,
            tables=parsed.tables,
            columns=parsed.columns,
        )
