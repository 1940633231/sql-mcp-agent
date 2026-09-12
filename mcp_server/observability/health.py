"""Health、Readiness 和 Metrics 响应构造。"""
from __future__ import annotations

import asyncio

from .. import config
from ..authorization.manager import get_policy_manager
from ..database.connection import business_pool, policy_pool, pool_stats
from .lifecycle import lifecycle
from .metrics import metrics


async def health() -> dict:
    return {
        "status": "ok",
        **lifecycle.snapshot(),
    }


async def readiness() -> dict:
    checks = {}
    checks["lifecycle"] = lifecycle.state.value == "ready"
    try:
        policy_context = await asyncio.to_thread(get_policy_manager().get_context)
        checks["policy"] = bool(policy_context.version)
        policy_version = policy_context.version
    except Exception as exc:
        checks["policy"] = False
        policy_version = ""
        checks["policy_error"] = str(exc)

    checks["business_db"] = await asyncio.to_thread(business_pool.probe)
    if config.POLICY_STORE == "mysql":
        checks["policy_db"] = await asyncio.to_thread(policy_pool.probe)
    else:
        checks["policy_db"] = True

    return {
        "status": "ready" if all(checks.values()) else "not_ready",
        "checks": checks,
        "policy_version": policy_version,
        **lifecycle.snapshot(),
    }


def metrics_payload() -> str:
    gauges = {}
    for name, stats in pool_stats().items():
        gauges["db_pool_in_use_%s" % name] = stats["in_use"]
        gauges["db_pool_available_%s" % name] = stats["available"]
        gauges["db_pool_created_%s" % name] = stats["created"]
    gauges["process_inflight_requests"] = lifecycle.inflight
    return metrics.render_prometheus(gauges)
