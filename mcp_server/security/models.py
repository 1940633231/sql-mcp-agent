"""安全与查询相关的数据模型（dataclass / enum），不含任何业务逻辑。"""
from dataclasses import dataclass, field
from enum import Enum


class StatementKind(str, Enum):
    """语句大类：只读查询 vs 其它。"""

    SELECT = "select"
    OTHER = "other"


@dataclass(frozen=True)
class ParsedSql:
    """parser.parse() 的结构化产物（sqlglot AST 视角）。

    多语句时各结构字段仅描述首条语句；statement_count 反映真实语句数。
    """

    raw: str                      # 去首尾空白的原始 SQL
    statements: list[str]         # 拆分并规范化后的语句列表
    kind: StatementKind           # 首条语句的大类（SELECT / OTHER）
    keyword: str                  # 首条语句的类型（SELECT / DELETE / UPDATE / ...）
    tables: list[str] = field(default_factory=list)        # 真实表引用（含库名前缀，已排除 CTE 别名）
    columns: list[str] = field(default_factory=list)       # 引用的列（table.column 形式，去重）
    functions: list[str] = field(default_factory=list)     # 调用的函数名（大写，去重）
    joins: int = 0                # JOIN 数量
    subquery_depth: int = 0       # 子查询嵌套深度
    has_limit: bool = False       # 是否已含 LIMIT
    has_star: bool = False        # 是否含 SELECT *

    @property
    def statement_count(self) -> int:
        return len(self.statements)


@dataclass
class SecurityPolicy:
    """一条完整的安全策略，字段与 configs/security.yaml 一一对应。"""

    allowed_statements: list[str] = field(default_factory=lambda: ["SELECT"])
    max_statements: int = 1
    forbidden_keywords: list[str] = field(default_factory=list)
    forbidden_patterns: list[str] = field(default_factory=list)
    blocked_schemas: list[str] = field(default_factory=list)
    allowed_tables: list[str] = field(default_factory=list)   # 空=不限；非空=只允许这些表（Allowlist 优先）
    denied_tables: list[str] = field(default_factory=list)    # 显式禁用的表（优先于 allowed 判定）
    columns_acl: dict = field(default_factory=dict)           # 按表配列 {table: {allowed:[], denied:[]}}
    max_joins: int = 0                    # 最大 JOIN 数；0/None=不限
    max_rows: int = 200                   # 单次最大返回行数
    max_result_bytes: int = 5 * 1024 * 1024   # 单次最大返回字节数（防 TEXT 大字段，默认 5MB）
    max_sql_length: int = 10000           # 单条 SQL 最大长度（超长直接拒绝，不解析）
    max_concurrent_queries: int = 10      # 最大并发查询数（信号量，超限拒绝）
    timeout_seconds: int = 15
    enforce_limit: bool = True
    identifier_pattern: str = r"^[A-Za-z0-9_]+$"


@dataclass(frozen=True)
class ValidationContext:
    """校验上下文：为 V0.3 RBAC / 按请求策略预留扩展位。

    目前仅携带请求来源标识；后续可加 principal / role / 允许的表集等。
    """

    source: str = "mcp"


@dataclass(frozen=True)
class ValidationResult:
    """校验结论（V0.2 契约）。

    allowed=True 时 normalized_sql 为可执行语句，结构化字段描述这条 SQL；
    allowed=False 时 reason 说明拒绝原因（code 为其机器可读分类）。
    """

    allowed: bool
    reason: str | None = None                 # 拒绝原因（人类可读）
    normalized_sql: str = ""                  # 放行时的可执行 SQL（已去尾分号）
    statement_type: str = ""                  # 语句类型（SELECT / DELETE / ...）
    tables: list[str] = field(default_factory=list)   # AST 视角的真实表引用
    columns: list[str] = field(default_factory=list)  # 引用的列（table.column 形式，去重）
    warnings: list[str] = field(default_factory=list) # 不拦截但值得记录的提示
    code: str | None = None                   # 拒绝分类（not_readonly / blocked_schema / ...）

    @classmethod
    def approve(
        cls,
        normalized_sql: str,
        statement_type: str = "",
        tables: list[str] | None = None,
        columns: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> "ValidationResult":
        return cls(
            allowed=True,
            normalized_sql=normalized_sql,
            statement_type=statement_type,
            tables=tables or [],
            columns=columns or [],
            warnings=warnings or [],
        )

    @classmethod
    def reject(
        cls,
        reason: str,
        code: str = "rejected",
        statement_type: str = "",
        tables: list[str] | None = None,
        columns: list[str] | None = None,
    ) -> "ValidationResult":
        return cls(
            allowed=False,
            reason=reason,
            code=code,
            statement_type=statement_type,
            tables=tables or [],
            columns=columns or [],
        )


@dataclass(frozen=True)
class QueryResult:
    """只读查询的执行结果。"""

    columns: list[str]
    rows: list[dict]
    row_count: int
    elapsed_seconds: float
    truncated: bool
    result_bytes: int = 0            # 实际返回的数据字节数（近似 UTF-8 编码长度）
    truncated_reason: str = ""       # 截断原因：rows（行数超限）/ bytes（字节超限）

    def to_dict(self) -> dict:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "elapsed_seconds": self.elapsed_seconds,
            "truncated": self.truncated,
            "result_bytes": self.result_bytes,
            "truncated_reason": self.truncated_reason,
        }
