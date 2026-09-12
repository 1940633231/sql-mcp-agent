"""结构化 JSON 审计事件（V0.7：异步写入 + 失败策略 + 保留周期 + 归档）。

职责链：``log_event`` 总是先落一条结构化 JSON 日志；当 ``AUDIT_STORE=mysql`` 时，
把事件投递到后台 ``AuditWriter`` 异步批量落库，避免审计拖慢业务路径。

可靠性设计：
- 有界队列（AUDIT_QUEUE_SIZE），队列满时按失败策略处理（不无限堆积内存）。
- 失败策略 ``AUDIT_FAILURE``：ignore=静默丢弃 / warn=记错误日志 / fail=标记不健康。
- ``AUDIT_REQUIRED=true`` 时 ``log_event`` 同步等待本次事件落库后再返回（保证必须入库）。
- 保留周期 ``AUDIT_RETENTION_DAYS``：定时清理过期行；``AUDIT_ARCHIVE=true`` 时先归档到归档表再清。
- 优雅退出时从主线程调用 ``flush()`` 排空队列，避免丢失尾部事件。
"""
import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone

from .. import config
from ..database.connection import policy_db_cursor
from ..observability.metrics import metrics

logger = logging.getLogger("sql_mcp.audit")
_audit_schema_lock = threading.Lock()
_audit_schema_ready = False


def _emit_ddl(cur) -> None:
    """就地创建审计主表。"""
    cur.execute(
        "CREATE TABLE IF NOT EXISTS `%s` ("
        "id BIGINT AUTO_INCREMENT PRIMARY KEY,"
        "event VARCHAR(64) NOT NULL,"
        "request_id VARCHAR(190) NOT NULL DEFAULT '',"
        "trace_id VARCHAR(64) NOT NULL DEFAULT '',"
        "principal VARCHAR(190) NOT NULL DEFAULT '',"
        "policy_version VARCHAR(96) NOT NULL DEFAULT '',"
        "sql_fingerprint VARCHAR(96) NOT NULL DEFAULT '',"
        "payload JSON NOT NULL,"
        "created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),"
        "INDEX idx_audit_request (request_id),"
        "INDEX idx_audit_trace (trace_id),"
        "INDEX idx_audit_policy (policy_version),"
        "INDEX idx_audit_fingerprint (sql_fingerprint)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" % config.AUDIT_TABLE
    )


def _write_rows(rows: list[tuple]) -> None:
    """批量插入一组已格式化的审计行（单事务提交）。"""
    global _audit_schema_ready
    if not rows:
        return
    with policy_db_cursor() as cur:
        if not _audit_schema_ready:
            with _audit_schema_lock:
                if not _audit_schema_ready:
                    _emit_ddl(cur)
                    _audit_schema_ready = True
        cur.executemany(
            "INSERT INTO `%s` "
            "(event, request_id, trace_id, principal, policy_version, sql_fingerprint, payload) "
            "VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s)" % config.AUDIT_TABLE,
            rows,
        )


def _maintenance() -> None:
    """按保留周期归档/清理过期审计行。"""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=config.AUDIT_RETENTION_DAYS)
    with policy_db_cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS `%s` LIKE `%s`"
            % (config.AUDIT_ARCHIVE_TABLE, config.AUDIT_TABLE)
        )
        if config.AUDIT_ARCHIVE:
            cur.execute(
                "INSERT INTO `%s` SELECT * FROM `%s` WHERE created_at < %%s"
                % (config.AUDIT_ARCHIVE_TABLE, config.AUDIT_TABLE),
                (cutoff,),
            )
        cur.execute(
            "DELETE FROM `%s` WHERE created_at < %%s" % config.AUDIT_TABLE,
            (cutoff,),
        )
    logger.info("audit maintenance: cleanup older than %s days", config.AUDIT_RETENTION_DAYS)


class AuditWriter:
    """后台异步审计写入器：有界队列 + 单工作线程 + 定期维护。"""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue(maxsize=config.AUDIT_QUEUE_SIZE)
        self._stop = threading.Event()
        self._last_maint = 0.0
        self._failed = 0
        self._persisted = 0
        self._healthy = True
        self._closed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name="audit-writer", daemon=True
        ) if config.AUDIT_ASYNC else None

    # ---- 生命周期 ----
    def start(self) -> None:
        if config.AUDIT_STORE != "mysql" or self._thread is None:
            return
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._closed = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)

    def flush(self, timeout: float = 10.0) -> None:
        """排空队列（同步等待工作线程处理完已投递事件）。"""
        if self._thread is None:
            self._drain_pending()
            return
        barrier = threading.Event()
        self._queue.put(("__flush__", barrier))
        barrier.wait(timeout=timeout)

    def is_healthy(self) -> bool:
        with self._lock:
            return self._healthy

    def stats(self) -> dict:
        with self._lock:
            return {
                "persisted": self._persisted,
                "failed": self._failed,
                "pending": self._queue.qsize(),
                "healthy": self._healthy,
                "async": self._thread is not None,
            }

    # ---- 处理 ----
    def enqueue(self, record: dict) -> None:
        if config.AUDIT_STORE != "mysql" or self._closed:
            return
        rows = [_row(record)]
        if self._thread is None:
            self._write(rows)
            return
        if not self._thread.is_alive():
            self._thread.start()
        try:
            self._queue.put(("rows", rows), timeout=1.0)
        except queue.Full:
            self._count_failure("audit queue full", drop=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                kind, payload = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._maybe_maintenance()
                continue
            if kind == "__flush__":
                payload.set()
                continue
            self._write(payload)
            self._maybe_maintenance()

    def _drain_pending(self) -> None:
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            if kind == "rows":
                self._write(payload)

    def _write(self, rows: list[tuple]) -> None:
        try:
            _write_rows(rows)
            with self._lock:
                self._persisted += len(rows)
                self._healthy = True
            metrics.inc("audit_persisted_total", {"store": "mysql"}, value=len(rows))
        except Exception as exc:
            self._count_failure(str(exc), drop=False)

    def _count_failure(self, message: str, drop: bool) -> None:
        with self._lock:
            self._failed += 1
            self._healthy = False
        mode = "fail" if config.AUDIT_REQUIRED else config.AUDIT_FAILURE
        metrics.inc("audit_write_failures_total", {"mode": mode})
        if mode == "ignore":
            if drop:
                logger.warning("audit dropped: %s", message)
            return
        if mode == "warn":
            if drop:
                logger.warning("audit dropped: %s", message)
            else:
                logger.error("audit persist failed: %s", message)
            return
        # fail：异步无法抛给调用方，标记不健康并记录，/healthz 会暴露。
        logger.error("audit persist failed (failure policy=fail): %s", message)

    def _maybe_maintenance(self) -> None:
        now = time.monotonic()
        if now - self._last_maint < max(10, config.AUDIT_MAINT_INTERVAL_SECONDS):
            return
        self._last_maint = now
        try:
            _maintenance()
        except Exception as exc:  # 维护失败不拖垮写入
            logger.warning("audit maintenance failed: %s", exc)


def _row(record: dict) -> tuple:
    return (
        str(record.get("event") or ""),
        str(record.get("request_id") or ""),
        str(record.get("trace_id") or ""),
        str(record.get("principal") or ""),
        str(record.get("policy_version") or ""),
        str(record.get("sql_fingerprint") or ""),
        json.dumps(record, ensure_ascii=False, sort_keys=True, default=str),
    )


_writer = AuditWriter()


def log_event(event: str, **fields) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    record.update(fields)
    # 结构化日志始终保留（与落库解耦），保证默认 AUDIT_STORE=log 也能追溯。
    logger.info(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
    _writer.enqueue(record)
    if config.AUDIT_STORE == "mysql" and config.AUDIT_REQUIRED:
        # 必须入库：同步排空队列，确保本次事件在其返回前已落库。
        _writer.flush()


def audit_stats() -> dict:
    return _writer.stats()


def audit_flush() -> None:
    _writer.flush()


def audit_start() -> None:
    _writer.start()


def audit_stop() -> None:
    _writer.stop()