"""V0.7 告警引擎 + Dashboard 信号聚合。

在进程内按 ``ALERT_EVAL_SECONDS`` 周期采样指标，计算：
  - 错误率（query_errors_total / query_executions_total）
  - P95 / P99 延迟（query_latency_overall_seconds）
  - 超时率（错误里 code 属于超时类别的占比）
  - 工具失败率（mcp_requests_total status=error 占比）
  - 连接池饱和度（business pool in_use / size）
并用水位法（pending -> firing -> resolved）驱动告警状态机。

规则从 ``configs/alerts.yaml`` 外置加载（阈值/窗口/严重度，无需改代码）。
同时提供 ``summary()`` 给 ``/dashboard`` 与 ``/alerts`` 使用。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

import yaml

from .. import config
from ..database.connection import pool_stats
from . import metrics as metrics_mod
from .lifecycle import lifecycle

logger = logging.getLogger("sql_mcp.alerts")

_DEFAULT_RULES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "configs", "alerts.yaml",
)

_TIMEOUT_CODES = frozenset({"query_timeout", "timeout"})


def _load_rules(path: str) -> list[dict]:
    """从 YAML 加载告警规则；失败时回退到一套合理默认规则，保证告警可用。"""
    path = path or config.ALERT_RULES_PATH or _DEFAULT_RULES_PATH
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            return list(data.get("rules") or [])
        except Exception as exc:
            logger.warning("failed to load alert rules from %s: %s", path, exc)
    return [
        {"name": "error_rate_high", "signal": "error_rate", "op": ">", "threshold": 0.05,
         "for": 60, "severity": "warning"},
        {"name": "p95_latency_high", "signal": "latency_p95_seconds", "op": ">", "threshold": 5.0,
         "for": 60, "severity": "warning"},
        {"name": "pool_saturation_high", "signal": "pool_saturation", "op": ">", "threshold": 0.9,
         "for": 60, "severity": "critical"},
    ]


def _split_counter(key: str) -> tuple[str, dict]:
    """``name{a=\"1\",b=\"2\"}`` -> (name, {a:1, b:2})；无标签返回 name 与空 dict。"""
    if "{" not in key:
        return key, {}
    name, rest = key.split("{", 1)
    labels = {}
    for pair in re.findall(r'(\w+)="([^"]*)"', rest):
        labels[pair[0]] = pair[1].replace('\\"', '"')
    return name, labels


class AlertEngine:
    def __init__(self, interval_seconds: float | None = None, clock=None):
        self._interval = interval_seconds or config.ALERT_EVAL_SECONDS
        self._rules = _load_rules(config.ALERT_RULES_PATH)
        self._prev: dict = {}
        self._prev_t: float = 0.0
        self._states: dict[str, dict] = {}   # rule name -> state
        self._lock = threading.Lock()
        self._last_eval: float = 0.0
        self._last_signals: dict = {}
        # 可注入时钟，便于测试精确驱动 pending -> firing -> resolved 状态迁移。
        self._clock = clock or time.time

    def start(self) -> None:
        thread = threading.Thread(
            target=self._loop, name="alert-engine", daemon=True
        )
        thread.start()

    def _loop(self) -> None:
        while True:
            self.evaluate()
            time.sleep(self._interval)

    # ---- 信号聚合 ----
    def _compute_signals(self, snap: dict, prev: dict, window: float) -> dict:
        def delta(name: str, filter_fn=None, prev_src=None, now_src=None):
            now_p = now_src or snap["counters"]
            prev_p = prev_src or (prev and prev["counters"]) or {}
            now_val = prev_val = 0.0
            for key, value in now_p.items():
                name_k, labels = _split_counter(key)
                if name_k == name and (filter_fn is None or filter_fn(labels)):
                    now_val += value
            for key, value in prev_p.items():
                name_k, labels = _split_counter(key)
                if name_k == name and (filter_fn is None or filter_fn(labels)):
                    prev_val += value
            return max(0.0, now_val - prev_val)

        err_delta = delta(
            "query_errors_total",
            lambda lab: True,
            prev.get("counters"), snap["counters"],
        )
        # 超时类错误
        timeout_delta = delta(
            "query_errors_total",
            lambda lab: lab.get("code") in _TIMEOUT_CODES,
            prev.get("counters"), snap["counters"],
        )
        exec_delta = delta(
            "query_executions_total", None, prev.get("counters"), snap["counters"]
        )
        # 统一分母：query_requests_total 覆盖认证/校验/权限/并发/DB 全路径错误。
        requests_delta = delta(
            "query_requests_total", None, prev.get("counters"), snap["counters"]
        )
        # 业务错误返回（不抛异常型）的工具失败，补进分子。
        biz_tool_err = delta(
            "tool_business_errors_total", None, prev.get("counters"), snap["counters"]
        )
        tool_total = delta(
            "mcp_requests_total", None, prev.get("counters"), snap["counters"]
        )
        tool_err = delta(
            "mcp_requests_total",
            lambda lab: lab.get("status") == "error",
            prev.get("counters"), snap["counters"],
        )

        # error_rate 分母用统一 requests 口径；缺 requests 时回退到 exec（兼容运行中实例）。
        rate_denom = requests_delta if requests_delta > 0 else exec_delta
        error_rate = err_delta / rate_denom if rate_denom > 0 else 0.0
        tool_denom = tool_total if tool_total > 0 else requests_delta
        tool_failure = (tool_err + biz_tool_err) / tool_denom if tool_denom > 0 else 0.0

        hist = snap.get("histograms", {})
        latency_summary = hist.get("query_latency_overall_seconds") or {}
        pool = pool_stats()
        biz = pool.get("business") or {}
        pool_size = biz.get("size") or 0
        pool_saturation = (biz.get("in_use") or 0) / pool_size if pool_size else 0.0

        return {
            "error_rate": error_rate,
            "timeout_rate": (timeout_delta / err_delta) if err_delta > 0 else 0.0,
            "tool_failure_rate": tool_failure,
            "latency_p95_seconds": latency_summary.get("p95", 0.0),
            "latency_p99_seconds": latency_summary.get("p99", 0.0),
            "pool_saturation": round(pool_saturation, 4),
            "inflight": lifecycle.inflight,
            "window_seconds": round(window, 3),
            "pool_waits": biz.get("waits", 0),
            "pool_timeouts": biz.get("timeouts", 0),
        }

    # ---- 评估 ----
    def evaluate(self) -> dict:
        with self._lock:
            now = time.monotonic()
            window = now - self._prev_t if self._prev_t else float(self._interval)
            snap = metrics_mod.metrics.snapshot()
            signals = self._compute_signals(snap, self._prev, window)
            self._last_signals = signals
            self._prev = snap
            self._prev_t = now
            self._last_eval = now
            for rule in self._rules:
                self._eval_rule(rule, signals)
            return self._overview_locked()

    def _eval_rule(self, rule: dict, signals: dict) -> None:
        name = rule.get("name")
        signal = signals.get(rule.get("signal"))
        threshold = rule.get("threshold", 0.0)
        op = rule.get("op", ">")
        for_seconds = rule.get("for", 60)
        now = self._clock()
        firing = False
        if signal is not None:
            firing = (signal > threshold) if op == ">" else (signal < threshold)

        state = self._states.get(name)
        if state is None:
            # 首次进入 pending 时记录 pending_since，供持续达到 for 窗口后 firing。
            state = {
                "name": name, "signal": rule.get("signal"),
                "severity": rule.get("severity", "warning"),
                "threshold": threshold, "status": "pending",
                "started": now, "pending_since": now,
                "fired_at": None, "resolved_at": None,
                "last_value": signal, "for_seconds": for_seconds,
            }
            self._states[name] = state
        state["last_value"] = signal

        if firing:
            if state["status"] == "resolved":
                # 曾恢复后再触发：重新开启 pending 计时窗口。
                state["status"] = "pending"
                state["pending_since"] = now
            if state["status"] == "pending":
                if now - state["pending_since"] >= state["for_seconds"]:
                    state["status"] = "firing"
                    state["fired_at"] = now
                    logger.warning(
                        "ALERT %s (%s) firing: %s=%s %s %s",
                        name, state["severity"], state["signal"],
                        signal, op, state["threshold"],
                    )
            # 已是 firing：保持，不再重复告警。
        else:
            if state["status"] == "firing":
                state["status"] = "resolved"
                state["resolved_at"] = now
                logger.info("ALERT %s resolved", name)
            elif state["status"] == "pending":
                # 未到 firing 就恢复：pending_since 顺延，保持绿色并清空累计窗口。
                state["pending_since"] = now
            # resolved（已恢复）保持 resolved，直到再次触发

    def overview(self) -> dict:
        with self._lock:
            return self._overview_locked()

    def _overview_locked(self) -> dict:
        # 调用方需持有 self._lock；否则 overview() 会二次加锁（threading.Lock 不可重入）。
        return {
            "signals": dict(self._last_signals),
            "rules": [
                {
                    "name": st["name"],
                    "signal": st["signal"],
                    "severity": st["severity"],
                    "threshold": st["threshold"],
                    "status": st["status"],
                    "last_value": st["last_value"],
                    "fired_at": st.get("fired_at"),
                }
                for st in self._states.values()
            ],
        }


alert_engine = AlertEngine()