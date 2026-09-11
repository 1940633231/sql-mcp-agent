"""Structured JSON audit events."""
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("sql_mcp.audit")


def log_event(event: str, **fields) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    record.update(fields)
    logger.info(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
