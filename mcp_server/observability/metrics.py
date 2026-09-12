"""不依赖第三方库的轻量 Prometheus 指标注册表。"""
from __future__ import annotations

import threading
from collections import defaultdict


class Metrics:
    def __init__(self):
        self._counters: dict[tuple, float] = defaultdict(float)
        self._histograms: dict[tuple, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def inc(self, name: str, labels: dict | None = None, value: float = 1.0) -> None:
        key = (name, _label_key(labels))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        key = (name, _label_key(labels))
        with self._lock:
            self._histograms[key].append(value)

    def snapshot(self) -> dict:
        with self._lock:
            counters = {
                _metric_key(name, labels): value
                for (name, labels), value in self._counters.items()
            }
            histograms = {
                _metric_key(name, labels): _hist_summary(values)
                for (name, labels), values in self._histograms.items()
            }
        return {"counters": counters, "histograms": histograms}

    def render_prometheus(self, extra_gauges: dict[str, float] | None = None) -> str:
        lines = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append("# TYPE %s counter" % name)
                lines.append("%s%s %s" % (name, _render_labels(labels), value))
            for (name, labels), values in sorted(self._histograms.items()):
                summary = _hist_summary(values)
                lines.append("# TYPE %s summary" % name)
                for quantile, observed in summary.items():
                    gauge_labels = dict(labels)
                    gauge_labels["quantile"] = quantile
                    lines.append(
                        "%s%s %s" % (name, _render_labels(gauge_labels), observed)
                    )
                lines.append(
                    "%s_count%s %s"
                    % (name, _render_labels(labels), summary["count"])
                )
        for name, value in sorted((extra_gauges or {}).items()):
            lines.append("# TYPE %s gauge" % name)
            lines.append("%s %s" % (name, value))
        return "\n".join(lines) + "\n"


def _label_key(labels: dict | None) -> tuple:
    return tuple(sorted((str(key), str(value)) for key, value in (labels or {}).items()))


def _metric_key(name: str, labels: tuple) -> str:
    return "%s%s" % (name, _render_labels(labels))


def _render_labels(labels) -> str:
    if not labels:
        return ""
    pairs = labels.items() if isinstance(labels, dict) else labels
    body = ",".join('%s="%s"' % (key, _escape(value)) for key, value in pairs)
    return "{%s}" % body


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _hist_summary(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "count": 0}
    return {
        "p50": _quantile(ordered, 0.50),
        "p95": _quantile(ordered, 0.95),
        "p99": _quantile(ordered, 0.99),
        "count": len(ordered),
    }


def _quantile(values: list[float], quantile: float) -> float:
    index = min(len(values) - 1, int(round((len(values) - 1) * quantile)))
    return round(values[index], 6)


metrics = Metrics()
