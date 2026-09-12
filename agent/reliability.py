"""Agent 可靠性基础（V0.6）。

集中承载「模型异常、工具失败、SQL 错误下仍能可控结束」所需的最小原语：

    - ErrorCode      稳定错误码（每次结束必有其一，机器可读）
    - ErrorKind      错误业务分类（决定重试 / 修复 / 终止）
    - classify_error 把工具返回的 code / 文本划到某个 ErrorKind
    - Budget         统一预算（迭代轮数 / 工具调用数 / SQL 修复次数 / Token / 费用）
    - RequestDeadline 全局请求截止时间（超时即停止继续修复或调用）
    - RunResult      运行结果 DTO（答案 + 状态 + 指标），供评测与观测消费
    - AgentError     自定义异常族（带稳定错误码）

设计约束（来自 V0.6 验收标准）：
    * 不存在无限循环、无界修复或无限等待 —— 所有循环都有预算或截止时间边界。
    * 临时错误可重试，永久错误立即终止 —— 只有 TRANSIENT 会走退避重试。
    * SQL Repair 仅针对可修复的语法/字段错误；权限、超时、危险 SQL 一律不进入盲重试。
"""
from enum import Enum

try:
    import time
except SystemError:  # pragma: no cover - 仅防环境缺 time 的编译期干扰
    time = __import__("time")


class ErrorCode(str, Enum):
    """稳定错误码：Agent 每次结束都以某一个码收尾，供上层稳定判读。"""

    SUCCESS = "success"
    TIMEOUT = "timeout"                  # 全局请求截止时间耗尽
    MODEL_ERROR = "model_error"          # 模型调用永久失败（重试耗尽 / 认证失败）
    TOOL_ERROR = "tool_error"            # 工具调用传输层失败（重试耗尽）
    SQL_SYNTAX_ERROR = "sql_syntax_error"  # SQL 语法/字段错误且修复次数耗尽
    PERMISSION_DENIED = "permission_denied"  # 权限/危险操作，立即终止
    BUDGET_EXCEEDED = "budget_exceeded"  # 迭代数/工具调用数/Token/费用任一超限


class ErrorKind(str, Enum):
    """错误业务分类：决定该错误是重试、交给 LLM 修复还是立即终止。"""

    SUCCESS = "success"                       # 正常结果
    TRANSIENT = "transient"                   # 临时错误（并发超限等），可安全退避重试
    REPAIRABLE_SQL = "repairable_sql"         # 语法/字段错误，交给 LLM 修复（有限次数）
    FATAL_PERMISSION = "fatal_permission"     # 权限拒绝，立即终止
    FATAL_TIMEOUT = "fatal_timeout"           # 执行/资源超时，立即终止
    FATAL_DANGEROUS = "fatal_dangerous"       # 危险操作，立即终止
    FATAL_TOOL = "fatal_tool"                 # 工具传输失败且重试耗尽
    FATAL_MODEL = "fatal_model"               # 模型永久失败


# ---- 稳定错误码 -> 用户可读的中文释义 ----
ERROR_CODE_MESSAGES = {
    ErrorCode.SUCCESS: "",
    ErrorCode.TIMEOUT: "已达到请求截止时间，停止继续调用/修复。",
    ErrorCode.MODEL_ERROR: "模型调用永久失败，无法继续。",
    ErrorCode.TOOL_ERROR: "数据工具调用失败，无法继续。",
    ErrorCode.SQL_SYNTAX_ERROR: "SQL 语法/字段错误，且已超过允许的修复次数。",
    ErrorCode.PERMISSION_DENIED: "查询被安全策略拒绝（权限不足或涉及危险操作）。",
    ErrorCode.BUDGET_EXCEEDED: "已达调用/资源预算上限，提前结束。",
}


# ---- 服务器安全校验 / 授权产生的 permission 类 code：一律不进入盲重试 ----
# 注意：parse_error（真正的 SQL 语法错误）不属于此集合，应交给 SQL Repair。
_FATAL_PERMISSION_CODES = frozenset({
    "not_readonly", "blocked_schema", "table_denied", "table_not_allowed",
    "column_not_allowed", "forbidden_keyword", "forbidden_pattern",
    "multi_statement", "sql_too_long", "authentication_required",
    "result_column_guard_denied", "empty", "bad_identifier", "too_many_joins",
    "denied", "permission_denied",
})

# 服务器可解析失败（真正的语法错误）等属于可修复，走 SQL Repair
_REPAIRABLE_CODES = frozenset({"parse_error"})

# 稳定超时 code（Server 侧将 MySQL 3024 等映射为此）：立即终止，不修复不重试
_TIMEOUT_CODES = frozenset({"query_timeout"})

# 可修复的 SQL 错误文本特征（执行层返回，语法 / 字段 / 表 / 库不存在）
_REPAIRABLE_SQL_PATTERNS = (
    "syntax error", "sql syntax", "1064", "unknown column", "unknown table",
    "unknown function", "unknown database", "doesn't exist", "does not exist",
    "not found", "in field list", "invalid column", "bad column",
    "unsupported", "1054", "1146", "1051", "1060",
)

# 超时类错误：立即终止，不重试不修复（含 MySQL 3024 的 Statement exceeded execution time）
_TIMEOUT_PATTERNS = (
    "exceeded execution time", "statement exceeded", "max_execution_time",
    "lock wait timeout", "timed out", "timeout exceeded", "query timeout",
    "操作超时", "查询超时",
)

# 危险 / 系统级操作：立即终止
_DANGEROUS_PATTERNS = ("load_file", "into outfile", "sys_exec", "xp_cmd", "shutdown")

# 可安全重试的临时错误文本（并发超限等）
_TRANSIENT_PATTERNS = (
    "并发查询数已达上限", "too many connections", "pool exhausted",
    "temporarily", "temporary failure", "service unavailable",
)


def classify_error(code: str | None = None, text: str = "") -> ErrorKind:
    """把工具返回的 code / 文本划到某个 ErrorKind 分类。

    优先级：权限码 > 超时码/文本 > 危险 > 可修复 SQL > 临时可重试 > 兜底。
    兜底为 FATAL_TOOL：只有能明确识别为可修复（语法/字段）或临时可重试的问题
    才会进入修复/重试，其余一律终止，避免把超时等误当成 SQL 去消耗修复预算。
    """
    if code:
        if code in _FATAL_PERMISSION_CODES:
            return ErrorKind.FATAL_PERMISSION
        if code in _TIMEOUT_CODES:
            return ErrorKind.FATAL_TIMEOUT
        if code in _REPAIRABLE_CODES:
            return ErrorKind.REPAIRABLE_SQL
    low = (text or "").lower()
    for p in _TIMEOUT_PATTERNS:
        if p in low:
            return ErrorKind.FATAL_TIMEOUT
    for p in _DANGEROUS_PATTERNS:
        if p in low:
            return ErrorKind.FATAL_DANGEROUS
    for p in _REPAIRABLE_SQL_PATTERNS:
        if p in low:
            return ErrorKind.REPAIRABLE_SQL
    for p in _TRANSIENT_PATTERNS:
        if p in low:
            return ErrorKind.TRANSIENT
    # 兜底：无法精确归类的一律视为工具错误立即终止（不进入修复预算）
    return ErrorKind.FATAL_TOOL


def error_code_for_kind(kind: ErrorKind) -> ErrorCode:
    """ErrorKind -> 稳定 ErrorCode 映射（终止类错误的输出码）。"""
    mapping = {
        ErrorKind.FATAL_PERMISSION: ErrorCode.PERMISSION_DENIED,
        ErrorKind.FATAL_DANGEROUS: ErrorCode.PERMISSION_DENIED,
        ErrorKind.FATAL_TIMEOUT: ErrorCode.TIMEOUT,
        ErrorKind.FATAL_TOOL: ErrorCode.TOOL_ERROR,
        ErrorKind.FATAL_MODEL: ErrorCode.MODEL_ERROR,
    }
    return mapping.get(kind, ErrorCode.TOOL_ERROR)


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 4.0) -> float:
    """指数退避 + 抖动：delay = base * 2^(n-1)，上限 cap，叠加 [0, base) 抖动。

    只用于 TRANSIENT（可安全重试）的只读调用。
    """
    import random

    exponent = base * (2 ** max(0, attempt - 1))
    delay = min(exponent, cap)
    return round(delay + random.uniform(0.0, base), 3)


def _seconds_now() -> float:
    return time.monotonic()


def deadline_exceeded(deadline: float) -> bool:
    """判断全局请求截止时间是否已耗尽。"""
    return _seconds_now() >= deadline


def deadline_remaining(deadline: float) -> float:
    """距离截止时间还有多少秒（不足则 0）。"""
    return max(0.0, deadline - _seconds_now())


def make_deadline(seconds: float) -> float:
    """从当前时刻起生成一个截止时间戳。"""
    return _seconds_now() + seconds


class Budget:
    """统一预算：限制迭代轮数、工具调用总数、SQL 修复次数、Token 与费用。

    任一预算被突破即应终止（BUDGET_EXCEEDED），从根上杜绝无限循环 / 无界修复。
    """

    __slots__ = (
        "max_iterations", "max_tool_calls", "max_sql_repairs",
        "max_tokens", "max_cost",
        "iterations_used", "tool_calls_used", "sql_repairs_used",
        "total_tokens", "total_cost",
    )

    def __init__(
        self,
        max_iterations: int,
        max_tool_calls: int,
        max_sql_repairs: int,
        max_tokens: int | None = None,
        max_cost: float | None = None,
    ):
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.max_sql_repairs = max_sql_repairs
        self.max_tokens = max_tokens or None
        self.max_cost = max_cost if max_cost is not None else None
        self.iterations_used = 0
        self.tool_calls_used = 0
        self.sql_repairs_used = 0
        self.total_tokens = 0
        self.total_cost = 0.0

    def iterations_exceeded(self) -> bool:
        return self.iterations_used >= self.max_iterations

    def tool_calls_exceeded(self) -> bool:
        return self.tool_calls_used >= self.max_tool_calls

    def repairs_exceeded(self) -> bool:
        return self.sql_repairs_used >= self.max_sql_repairs

    def tokens_exceeded(self) -> bool:
        return self.max_tokens is not None and self.total_tokens > self.max_tokens

    def cost_exceeded(self) -> bool:
        return self.max_cost is not None and self.total_cost > self.max_cost

    def any_exceeded(self) -> bool:
        return (
            self.iterations_exceeded()
            or self.tool_calls_exceeded()
            or self.repairs_exceeded()
            or self.tokens_exceeded()
            or self.cost_exceeded()
        )

    def account_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        """累计 Token 用量（stdout；费用换算由调用方以模型单价计算）。"""
        self.total_tokens += int(prompt_tokens) + int(completion_tokens)


class RunResult:
    """运行结果 DTO：答案 + 稳定状态码 + 指标。

    供评测集与观测（迭代数 / 工具调用数 / 修复次数 / 耗时 / 最终状态）消费。
    """

    __slots__ = (
        "status", "answer", "error_message",
        "iteration_count", "tool_call_count", "repair_count",
        "elapsed_seconds", "token_count", "cost",
    )

    def __init__(
        self,
        status: ErrorCode,
        answer: str = "",
        iteration_count: int = 0,
        tool_call_count: int = 0,
        repair_count: int = 0,
        elapsed_seconds: float = 0.0,
        token_count: int = 0,
        cost: float = 0.0,
        error_message: str = "",
    ):
        self.status = status
        self.answer = answer
        self.error_message = error_message
        self.iteration_count = iteration_count
        self.tool_call_count = tool_call_count
        self.repair_count = repair_count
        self.elapsed_seconds = elapsed_seconds
        self.token_count = token_count
        self.cost = cost

    def to_dict(self) -> dict:
        return {
            "status": self.status.value if isinstance(self.status, ErrorCode) else self.status,
            "answer": self.answer,
            "error_message": self.error_message,
            "metrics": {
                "iteration_count": self.iteration_count,
                "tool_call_count": self.tool_call_count,
                "repair_count": self.repair_count,
                "elapsed_seconds": self.elapsed_seconds,
                "token_count": self.token_count,
                "cost": self.cost,
            },
        }


class AgentError(Exception):
    """Agent 层统一异常基类，携带稳定错误码。"""

    code = ErrorCode.TOOL_ERROR

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class AgentTimeout(AgentError):
    code = ErrorCode.TIMEOUT


class AgentModelError(AgentError):
    code = ErrorCode.MODEL_ERROR


class AgentToolError(AgentError):
    code = ErrorCode.TOOL_ERROR