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
import time

from ..auth.context import AuthenticationError, current_request_context
from ..auth.models import RequestContext
from ..authorization.service import AuthorizationService
from ..authorization.audit import log_event
from ..authorization.manager import get_policy_manager
from ..catalog.service import SchemaCatalog
from ..database.executor import QueryExecutor
from ..observability.fingerprint import raw_sql_hash, sql_fingerprint
from ..observability.metrics import metrics
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
        catalog: SchemaCatalog | None = None,
    ):
        self.validator = validator or SqlValidator()
        self.executor = executor or QueryExecutor()
        self.catalog = catalog or SchemaCatalog.shared()
        self.authorizer = authorizer or AuthorizationService()
        self._semaphore = threading.BoundedSemaphore(self.policy.max_concurrent_queries)

    @property
    def policy(self):
        return self.validator.policy

    def run(
        self,
        sql: str,
        context: RequestContext | None = None,
        params: tuple | list | None = None,
    ) -> dict:
        """执行只读 SQL，返回可直接序列化的 dict（失败时含 error）。

        params 为非空时表示 SQL 已含 ? 值占位符（领域工具渲染产物），
        由 Executor 以数据库驱动绑定执行；run_query 等自由 SQL 保持 params=None。
        """
        started = time.monotonic()
        policy_context = get_policy_manager().get_context()
        try:
            context = context or current_request_context(source="mcp")
        except AuthenticationError as e:
            return {"error": str(e), "code": "authentication_required"}

        authorizer = self.authorizer.for_policy(policy_context.policy)

        result = self.validator.validate(sql)
        if not result.allowed:
            return {"error": result.reason, "code": result.code}

        authorized = authorizer.authorize_sql(
            result.normalized_sql, context, self.catalog
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
                if params:
                    query_result = self.executor.run(
                        safe_sql,
                        policy.max_rows,
                        policy.timeout_seconds,
                        policy.max_result_bytes,
                        params=params,
                    )
                else:
                    query_result = self.executor.run(
                        safe_sql, policy.max_rows, policy.timeout_seconds, policy.max_result_bytes
                    )
            finally:
                self._semaphore.release()
        except Exception as e:
            # Database 层抛出的 MySQLError 等统一收敛为错误结构并携带稳定 code，不向 Tool 层上抛。
            # MySQL 3024（Statement exceeded execution time）等超时显式映射为 query_timeout，
            # 其余数据库错误归为 query_error，保证 Agent 侧能稳定判读而非按文本猜。
            metrics.inc("query_executions_total", {"status": "error"})
            return {"error": str(e), "code": _classify_db_error(e)}
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
            request_id=context.request_id,
            trace_id=context.trace_id,
            session_id=context.session_id,
            client_id=context.client_id,
            tool_name=context.tool_name,
            auth_method=context.auth_method,
            policy_version=policy_context.version,
            policy_hash=policy_context.policy_hash,
            decision="allow",
            sql_fingerprint=sql_fingerprint(sql),
            raw_sql_hash=raw_sql_hash(sql),
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            roles=sorted(context.principal.roles),
            tables=list(authorized.tables),
            policies=list(authorized.policy_ids),
            columns=query_result.columns,
            row_count=query_result.row_count,
            rows=query_result.row_count,
            truncated=query_result.truncated,
            elapsed_seconds=query_result.elapsed_seconds,
            result_bytes=query_result.result_bytes,
        )
        metrics.inc("query_executions_total", {"status": "ok"})
        metrics.observe("query_duration_seconds", time.monotonic() - started)
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
    """数据库返回结果集后的纵深防御检查。"""
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


# DB 执行错误 -> 稳定 code 的判定：先看 errno，再看错误文本特征。
# 关键：超时（3024 / Lock wait / timed out）必须映射为 query_timeout，
# 其余未知错误归为 query_error —— 让 Agent 端以稳定 code 判读，而不是猜测文本。
_DB_TIMEOUT_ERRNOS = frozenset({3024})
_DB_TIMEOUT_TEXT_MARKERS = (
    "exceeded execution time",
    "statement exceeded",
    "max_execution_time",
    "lock wait timeout",
    "timed out",
    "query timeout",
    "操作超时",
    "查询超时",
)


def _classify_db_error(exc) -> str:
    """把数据库层异常映射为稳定错误码（query_timeout / query_error）。"""
    errno = None
    try:
        errno = int(exc.args[0])
    except (TypeError, ValueError, IndexError, AttributeError):
        errno = None
    if errno in _DB_TIMEOUT_ERRNOS:
        return "query_timeout"
    low = str(exc).lower()
    if any(marker in low for marker in _DB_TIMEOUT_TEXT_MARKERS):
        return "query_timeout"
    return "query_error"
