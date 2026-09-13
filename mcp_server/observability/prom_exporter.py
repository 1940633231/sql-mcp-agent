"""薄适配层：把自研 Metrics 内核桥接到 prometheus_client 官方导出。

设计取舍（V0.8）：
- 不替换自研 metrics/telemetry 内核，也不动其 14 个调用方——内核已是
  W3C/OTLP 兼容且跨 Server/Agent 共用，强行换 SDK 会破坏链路且无实质收益。
- 只新增一个标准的 ``prometheus_client.Collector``，把：
    * Counter/Histogram（来自 Metrics.iter_histogram_buckets）
    * Gauge（来自连接池 / 生命周期，由调用方注入）
  注册进官方 ``CollectorRegistry``，使 ``/metrics`` 可用
  ``generate_latest()`` 输出，从而被标准 Prometheus / Alertmanager 抓取。

对外入口：``registry``（Prometheus 官方 registry）与 ``new_payload()``。
"""
from __future__ import annotations

import prometheus_client
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)

from . import metrics as metrics_mod

# 敏感 / 高基数标签键：薄适配只负责“透出”，标签卫生仍由 Metrics 内核负责，
# 这里不重复实现（Metrics._sanitize 已丢弃/截断）。仅透出稳定标签。


class _MetricsCollector:
    """自研 Metrics 内核 → prometheus_client 标准 MetricFamily 的适配收集器。"""

    def __init__(self, extra_gauges: dict[str, float] | None = None):
        # 由 metrics_payload 注入的动态 gauge（连接池 / 生命周期等）。
        self._extra_gauges = extra_gauges or {}

    def set_extra_gauges(self, gauges: dict[str, float]) -> None:
        self._extra_gauges = dict(gauges or {})

    def collect(self):
        snap = metrics_mod.metrics.snapshot()
        counters = snap.get("counters", {})
        # 自研快照 key 形如 ``name{label="v",...}`` 或 ``name``；
        # 标准导出需要「名称」与「标签表」。逐序列还原。
        counter_by_name: dict[str, list] = {}
        for key, value in counters.items():
            name, labels = _split_key(key)
            counter_by_name.setdefault(name, []).append((labels, value))
        for name, series in counter_by_name.items():
            labelnames = sorted(series[0][0]) if series[0][0] else None
            fam = CounterMetricFamily(name, name, labels=labelnames) if labelnames else CounterMetricFamily(name, name)
            ordered = set(labelnames) if labelnames else set()
            for labels, value in series:
                if set(labels) != ordered:
                    continue  # 标签键不一致（动态标签残留）：跳过，避免非法序列
                if labelnames is None:
                    fam.add_metric([], value)
                else:
                    fam.add_metric([labels[k] for k in sorted(ordered)], value)
            yield fam

        hist_by_name: dict[str, list] = {}
        for name, labels, buckets, ssum, scount in metrics_mod.metrics.iter_histogram_buckets():
            # buckets 为 [(float_bound, cumulative), ...]；标准导出要求
            # le 上界是字符串（如 "0.1"/"Inf"），计数为数值。
            str_buckets = [(_fmt_le(bound), count) for bound, count in buckets]
            hist_by_name.setdefault(name, []).append((labels, str_buckets, ssum, scount))
        for name, series in hist_by_name.items():
            # 同一直方图所有序列的标签键一致（自研内核按 (name, label_key) 存储）。
            labelnames = sorted(series[0][0]) if series else None
            ordered = set(labelnames) if labelnames else set()
            fam = HistogramMetricFamily(name, name, labels=labelnames) if labelnames else HistogramMetricFamily(name, name)
            for labels, buckets, ssum, scount in series:
                if set(labels) != ordered:
                    continue
                label_values = [labels[k] for k in ordered] if labelnames else []
                fam.add_metric(label_values, buckets=buckets, sum_value=ssum)
            yield fam

        for name, value in sorted(self._extra_gauges.items()):
            fam = GaugeMetricFamily(name, name)
            fam.add_metric([], value)
            yield fam


# 官方默认 REGISTRY 会抓取进程级的默认指标（进程 CPU/内存等），
# 此处用独立 registry，避免引入无关进程指标与潜在冲突。
registry = prometheus_client.CollectorRegistry()
_collector = _MetricsCollector()
registry.register(_collector)


def new_payload(extra_gauges: dict[str, float] | None = None) -> str:
    """以官方文本格式渲染当前指标（含注入的连接池 / 生命周期 gauge）。"""
    _collector.set_extra_gauges(extra_gauges)
    return prometheus_client.generate_latest(registry).decode("utf-8")


def _fmt_le(bound: float) -> str:
    """把水位上界格式化为 Prometheus ``le`` 标签值：Inf / 整数值 / 通用小数。"""
    if bound == float("inf"):
        return "Inf"
    if bound == int(bound):
        return str(int(bound))
    return repr(bound)


def _split_key(key: str) -> tuple[str, dict]:
    """``name{a="1",b="2"}`` -> (name, {a:1, b:2})；无标签返回空 dict。"""
    if "{" not in key:
        return key, {}
    name, rest = key.split("{", 1)
    labels = {}
    for pair in _iter_label_pairs(rest):
        labels[pair[0]] = pair[1]
    return name, labels


def _iter_label_pairs(rest: str):
    i = 0
    n = len(rest)
    while i < n:
        # 读到 ``k="v"``，容忍引号内的转义与逗号。
        if rest[i] in ("", "}", ","):
            i += 1
            continue
        eq = rest.find("=", i)
        if eq == -1:
            break
        key = rest[i:eq].strip()
        start = rest.find('"', eq)
        if start == -1:
            break
        buf = []
        j = start + 1
        while j < n:
            if rest[j] == "\\" and j + 1 < n:
                buf.append(rest[j + 1])
                j += 2
                continue
            if rest[j] == '"':
                break
            buf.append(rest[j])
            j += 1
        yield key, "".join(buf)
        i = j + 1