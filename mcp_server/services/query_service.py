"""QueryService：只读查询的编排层。

职责（连接 Tool 与 Security/Database 两端的「胶水」）：
    1. 校验安全（SqlValidator，返回 ValidationResult）
    2. 强制行数上限（未含 LIMIT 时追加，判定用 AST 而非字符串）
    3. 并发限量（信号量，超限立即拒绝而非排队）
    4. 委托执行（QueryExecutor，含字节上限）
    5. 收敛异常（数据库错误 -> dict {"error": ...}，不抛到 MCP 层）

三层职责边界：
    - Tool 层（tools/query.py）只做 MCP 协议转换，不写安全/执行逻辑
    - Security 层只回答「能不能执行」，不懂执行
    - Database 层（executor.py）只负责执行，不懂安全
"""
import threading

from ..auth.context import AuthenticationError, current_request_context
from ..auth.models import RequestContext
from ..authorization.service import AuthorizationService
from ..authorization.audit import log_event
from ..database.executor import QueryExecutor
from ..security import parser
from ..security.validator import SqlValidator


class ConcurrentQueriesExceeded(Exception):
    """并发查询数量已达上限。"""


class QueryService:
    """编排只读 SQL 从校验到执行的完整流程。

    可注入 validator / executor，便于测试隔离（默认各自加载生产实例）。
    """

    def __init__(
        self,
        validator: SqlValidator | None = None,
        executor: QueryExecutor | None = None,
        authorizer: AuthorizationService | None = None,
    ):
        self.validator = validator or SqlValidator()
        self.executor = executor or QueryExecutor()
        self.authorizer = authorizer or AuthorizationService()
        self._semaphore = threading.BoundedSemaphore(self.policy.max_concurrent_queries)

    @property
    def policy(self):
        return self.validator.policy

    def run(self, sql: str, context: RequestContext | None = None) -> dict:
        """执行只读 SQL，返回可直接序列化的 dict（失败时含 error）。"""
        try:
            context = context or current_request_context(source="mcp")
        except AuthenticationError as e:
            return {"error": str(e), "code": "authentication_required"}

        result = self.validator.validate(sql)
        if not result.allowed:
            return {"error": result.reason, "code": result.code}

        authorized = self.authorizer.authorize_sql(
            result.normalized_sql, context, self.executor
        )
        if not authorized.allowed:
            return {"error": authorized.reason, "code": authorized.code}

        # 策略改写后再校验一次，确保 RLS 列和展开列仍受基础安全边界约束。
        post_check = self.validator.validate(authorized.sql)
        if not post_check.allowed:
            return {
                "error": "授权改写后的 SQL 未通过安全校验：%s" % post_check.reason,
                "code": post_check.code,
            }

        policy = self.policy
        safe_sql = self._ensure_limit(
            post_check.normalized_sql, policy.enforce_limit, policy.max_rows
        )
        try:
            if not self._semaphore.acquire(blocking=False):
                raise ConcurrentQueriesExceeded("并发查询数已达上限 %d" % policy.max_concurrent_queries)
            try:
                query_result = self.executor.run(
                    safe_sql, policy.max_rows, policy.timeout_seconds, policy.max_result_bytes
                )
            finally:
                self._semaphore.release()
        except Exception as e:
            # Database 层抛出的 MySQLError 等统一收敛为错误结构，不向 Tool 层上抛
            return {"error": str(e)}
        guard_error = _result_column_guard(query_result.columns, authorized.result_columns)
        if guard_error:
            log_event(
                "query_result_guard_denied",
                principal=context.principal.subject,
                columns=query_result.columns,
                allowed_columns=list(authorized.result_columns),
                reason=guard_error,
            )
            return {"error": guard_error, "code": "result_column_guard_denied"}

        log_event(
            "query_executed",
            principal=context.principal.subject,
            roles=sorted(context.principal.roles),
            tables=list(authorized.tables),
            policies=list(authorized.policy_ids),
            columns=query_result.columns,
            row_count=query_result.row_count,
            truncated=query_result.truncated,
            elapsed_seconds=query_result.elapsed_seconds,
        )
        return query_result.to_dict()

    def _ensure_limit(self, sql: str, enforce: bool, max_rows: int) -> str:
        """未显式 LIMIT 时追加行数上限；has_limit 取自 AST，避免字符串误判。"""
        if not enforce:
            return sql
        parsed = parser.parse(sql)
        if parsed.has_limit:
            return sql
        return "%s LIMIT %d" % (sql, max_rows)


def _result_column_guard(actual_columns: list[str], allowed_columns: tuple[str, ...]) -> str | None:
    """Defense-in-depth check after the database returns a result set."""
    if not allowed_columns:
        return None
    allowed = {_normalize_result_name(column) for column in allowed_columns}
    unexpected = [
        column for column in actual_columns
        if _normalize_result_name(column) not in allowed
    ]
    if unexpected:
        return "结果列未通过授权二次校验：%s" % ", ".join(unexpected)
    return None


def _normalize_result_name(value: str) -> str:
    return " ".join((value or "").strip().lower().split())
