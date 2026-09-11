"""查询执行层：在连接上执行已校验的 SQL / 读取表结构，施加超时、行数与字节上限。"""
import time

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
    ) -> QueryResult:
        """执行只读查询。

        设置会话级超时后执行；结果施加双限制：
          - 行数 < max_rows（fetchmany）
          - 累计字节 < max_result_bytes（>0 时生效，逐行累计，防止大字段撑爆）
        任一项超限即截断。
        """
        start = time.time()
        with db_cursor() as cur:
            cur.execute("SET SESSION MAX_EXECUTION_TIME=%d" % (timeout_seconds * 1000))
            cur.execute(sql)
            fetched = cur.fetchmany(size=max_rows)
            columns = [d[0] for d in cur.description] if cur.description else []

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

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            elapsed_seconds=round(time.time() - start, 3),
            truncated=bool(truncated_reason),
            result_bytes=result_bytes,
            truncated_reason=truncated_reason,
        )
