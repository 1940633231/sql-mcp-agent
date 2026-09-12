"""查询执行层：在连接上执行已校验的 SQL / 读取表结构，施加超时、行数与字节上限。"""
import time

from ..observability import tracing
from ..security.models import QueryResult
from .connection import db_cursor


def _row_bytes(row: dict) -> int:
    """估算一行数据的体积（对值做 UTF-8 编码求字节数和）。

    近似值即可：用于防止 TEXT/BLOB 大字段在「行数未超」时仍撑爆内存，
    不追求精确序列化开销。
    """
    total = 0
    for value in row.values():
        total += len(str(value).encode("utf-8", "replace"))
    return total


def _bind_params(sql: str, params: tuple) -> tuple[str, tuple]:
    """把模板值占位符 ? 转为 pymysql 的 %s，并对 SQL 中其它 % 转义为 %%。

    仅在带参数执行时调用（pymysql 为客户端 %-format 绑定）：
      - ? 只出现在服务端模板的值槽位，扫描时跳过字符串字面量内的 ?；
      - SQL 文本中的字面 %（如 DATE_FORMAT 的 '%Y-%m'）必须写成 %%，否则
        Python %-format 会把它当作格式符而报错；
      - 转换后 %s 数量必须与参数数量一致，不一致视为编程错误直接拒绝。
    """
    out: list[str] = []
    quote: str | None = None
    count = 0
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if quote:
            # 字符串字面量内：? 不算占位符，但 % 仍需转义（Python %-format 不认 SQL 引号）
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(sql[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            if ch == "%":
                if i + 1 < n and sql[i + 1] == "s":
                    out.append("%s")
                    i += 1
                else:
                    out.append("%%")
            else:
                out.append(ch)
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
            count += 1
        elif ch == "%":
            if i + 1 < n and sql[i + 1] == "s":
                out.append("%s")
                i += 1
            else:
                out.append("%%")
        else:
            out.append(ch)
        i += 1
    bound = "".join(out)
    if count != len(params):
        raise ValueError(
            "占位符数量与参数数量不一致：%d != %d" % (count, len(params))
        )
    if bound.count("%s") != len(params):
        raise ValueError(
            "SQL 中的 %s 数量与参数数量不一致（模板不得在字符串字面量中使用 %s）"
        )
    return bound, params


class QueryExecutor:
    """只读数据库操作集合；不做安全校验（由 security.validator 前置完成）。"""

    def list_tables(self) -> list[str]:
        with db_cursor() as cur:
            cur.execute("SHOW TABLES")
            return [next(iter(row.values())) for row in cur.fetchall()]

    def get_schema(self, table_name: str) -> list[dict]:
        with db_cursor() as cur:
            cur.execute("SHOW FULL COLUMNS FROM `%s`" % table_name)
            return cur.fetchall()

    def run(
        self,
        sql: str,
        max_rows: int,
        timeout_seconds: int,
        max_result_bytes: int = 0,
        params: tuple | list | None = None,
    ) -> QueryResult:
        """执行只读查询（值参数经驱动绑定，标识符已在模板渲染期白名单内联）。

        设置会话级超时后执行；结果施加双限制：
          - 行数 < max_rows（fetchmany）
          - 累计字节 < max_result_bytes（>0 时生效，逐行累计，防止大字段撑爆）
        任一项超限即截断。
        """
        start = time.time()
        # V0.7：数据库层 Span（依赖请求链延续同一 trace_id，不记录原始 SQL）。
        span = tracing.request_child_span(
            "database.query", kind=tracing.SpanKind.CLIENT,
            attributes={"rows_cap": max_rows, "timeout_s": timeout_seconds},
        )
        try:
            with db_cursor() as cur:
                cur.execute("SET SESSION MAX_EXECUTION_TIME=%d" % (timeout_seconds * 1000))
                if params:
                    sql, params = _bind_params(sql, tuple(params))
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)
                fetched = cur.fetchmany(size=max_rows)
                columns = [d[0] for d in cur.description] if cur.description else []
        except Exception:
            tracing.tracer.end(span, status="error", attributes={"error_code": "database_query_error"})
            raise

        rows: list[dict] = []
        result_bytes = 0
        truncated_reason = ""
        for row in fetched:
            if max_result_bytes and result_bytes + _row_bytes(row) > max_result_bytes:
                truncated_reason = "bytes"
                break
            rows.append(row)
            result_bytes += _row_bytes(row)

        if not truncated_reason and len(rows) >= max_rows:
            truncated_reason = "rows"

        elapsed = round(time.time() - start, 3)
        tracing.tracer.end(span, status="ok", attributes={"duration_ms": span.duration_ms()})
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            elapsed_seconds=elapsed,
            truncated=bool(truncated_reason),
            result_bytes=result_bytes,
            truncated_reason=truncated_reason,
        )
