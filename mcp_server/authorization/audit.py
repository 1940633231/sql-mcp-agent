"""结构化 JSON 审计事件。"""
import json
import logging
import threading
from datetime import datetime, timezone

from .. import config
from ..database.connection import policy_db_cursor

logger = logging.getLogger("sql_mcp.audit")
_audit_schema_lock = threading.Lock()
_audit_schema_ready = False


def log_event(event: str, **fields) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    record.update(fields)
    logger.info(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
    if config.AUDIT_STORE == "mysql":
        try:
            _mysql_emit(record)
        except Exception:
            logger.exception("failed to persist audit event")
            if config.AUDIT_REQUIRED:
                raise


def _mysql_emit(record: dict) -> None:
    global _audit_schema_ready
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    with policy_db_cursor() as cur:
        if not _audit_schema_ready:
            with _audit_schema_lock:
                if not _audit_schema_ready:
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
                    _audit_schema_ready = True
        cur.execute(
            "INSERT INTO `%s` (event, request_id, trace_id, principal, policy_version, sql_fingerprint, payload) "
            "VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s)" % config.AUDIT_TABLE,
            (
                str(record.get("event") or ""),
                str(record.get("request_id") or ""),
                str(record.get("trace_id") or ""),
                str(record.get("principal") or ""),
                str(record.get("policy_version") or ""),
                str(record.get("sql_fingerprint") or ""),
                payload,
            ),
        )
