"""Health、Readiness 与 Metrics / Trace / Dashboard / Alert 响应构造。"""
from __future__ import annotations

import asyncio

from .. import config
from ..authorization.manager import get_policy_manager
from ..authorization import audit as audit_mod
from ..database.connection import business_pool, policy_pool, pool_stats
from .alerts import alert_engine
from .lifecycle import lifecycle
from .prom_exporter import new_payload
from telemetry import tracer


async def health() -> dict:
    return {
        "status": "ok",
        **lifecycle.snapshot(),
        "audit": audit_mod.audit_stats(),
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

    # 审计健康：队列积压或写失败超过策略阈值时视为不健康。
    audit = audit_mod.audit_stats()
    checks["audit"] = audit.get("healthy", True)

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
        gauges["db_pool_saturation_%s" % name] = (
            stats["in_use"] / stats["size"] if stats.get("size") else 0.0
        )
    gauges["process_inflight_requests"] = lifecycle.inflight
    # V0.8：自研 Metrics 内核 → prometheus_client 官方导出（薄适配层）。
    return new_payload(gauges)


def traces_payload() -> dict:
    """有界内存缓冲里的最近 span（供 /traces 查看与调试跨服务链路）。"""
    return {"spans": tracer.buffer.snapshot()}


def dashboard_payload() -> dict:
    """单请求返回的运行态概览：信号 + 告警 + 连接池 + 审计 + 进程。"""
    over = alert_engine.overview()
    audits = audit_mod.audit_stats()
    return {
        "signals": over.get("signals", {}),
        "active_alerts": [
            r for r in over.get("rules", []) if r["status"] in ("pending", "firing")
        ],
        "alert_rules": [
            r for r in over.get("rules", []) if r["status"] == "resolved"
        ],
        "pool": pool_stats(),
        "audit": audits,
        "lifecycle": lifecycle.snapshot(),
    }


def alerts_payload() -> dict:
    return alert_engine.overview()


def evaluate_alerts() -> dict:
    """触发一次告警评估并返回概览（供手动触发 / 测试）。"""
    return alert_engine.evaluate()
