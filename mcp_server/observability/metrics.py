"""不依赖第三方库的轻量 Prometheus 指标注册表（V0.7 有界版本）。

- Counter：累计值，语义不变。
- Histogram：改为**固定分桶**存储（每个序列只存每个桶的计数 + sum + max），
  不再在内存里保留全部样本，杜绝长时间运行的无限内存增长。
  P50/P95/P99 由桶分布线性插值近似得到，足够驱动告警与容量趋势。
- 标签卫生：禁止把原始 SQL、完整用户输入或高基数字段放进标签
  （含敏感/高基数键名与超长取值），违规标签会被静默丢弃并仅告警一次，
  保证 /metrics 不会泄露查询内容。
"""
from __future__ import annotations

import threading
from collections import defaultdict

# 敏感 / 高基数字段名：一旦出现在标签键中即丢弃（防御原始 SQL / 用户输入泄漏）。
FORBIDDEN_LABEL_KEYS = frozenset(
    {
        "sql", "statement", "query", "input", "question", "prompt",
        "value", "values", "payload", "text", "content",
        "reason", "error", "error_message", "row", "rows", "body",
        "pid", "uuid", "request_id", "trace_id", "session_id", "client_id",
    }
)
# 标签值上限：过长即截断，避免高基数/超长文本进入指标。
MAX_LABEL_VALUE_LEN = 96

# 延迟类指标的默认分桶（秒），覆盖请求/查询/MCP 等耗时。
DURATION_BUCKETS = (
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05,
    0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


class _BoundedHistogram:
    """固定分桶直方图：内存占用与样本量无关。"""

    __slots__ = ("_bounds", "_buckets", "_count", "_sum", "_max")

    def __init__(self, bounds: tuple):
        self._bounds = bounds
        self._buckets: list[int] = [0] * (len(bounds) + 1)  # 最后一个桶是 +Inf
        self._count = 0
        self._sum = 0.0
        self._max = 0.0

    def observe(self, value: float) -> None:
        value = float(value)
        self._count += 1
        self._sum += value
        if value > self._max:
            self._max = value
        # 找第一个上界 >= value 的分桶
        for i, bound in enumerate(self._bounds):
            if value <= bound:
                self._buckets[i] += 1
                return
        self._buckets[-1] += 1

    def quantile(self, q: float) -> float:
        if self._count == 0:
            return 0.0
        target = q * self._count
        cumulative = 0
        prev_bound = 0.0
        for i, bound in enumerate(self._bounds):
            cumulative += self._buckets[i]
            if cumulative >= target:
                if self._buckets[i] == 0 or target - (cumulative - self._buckets[i]) == 0:
                    return round(prev_bound, 6)
                low = prev_bound
                high = self._bounds[i]
                frac = (target - (cumulative - self._buckets[i])) / self._buckets[i]
                return round(low + (high - low) * frac, 6)
            prev_bound = float(bound)
        # 落在 +Inf 桶：用已记录的最大值收敛，避免无限大
        cumulative += self._buckets[-1]
        if cumulative <= 0:
            return 0.0
        return round(self._max, 6) if self._max else round(prev_bound, 6)

    def summary(self) -> dict:
        return {
            "count": self._count,
            "sum": round(self._sum, 6),
            "p50": self.quantile(0.50),
            "p95": self.quantile(0.95),
            "p99": self.quantile(0.99),
        }


def _fmt_bound(value: float) -> str:
    return str(int(value)) if value == int(value) else ("%g" % value)


class Metrics:
    def __init__(self, buckets: tuple = DURATION_BUCKETS):
        self._buckets = buckets
        self._counters: dict[tuple, float] = defaultdict(float)
        self._histograms: dict[tuple, _BoundedHistogram] = {}
        self._lock = threading.Lock()
        self._warned_label_keys: set = set()

    def _sanitize(self, labels: dict | None) -> dict:
        """丢弃敏感/高基数标签键，并对超长值截断。"""
        if not labels:
            return {}
        out: dict = {}
        for key, value in labels.items():
            key = str(key)
            if key in FORBIDDEN_LABEL_KEYS:
                with self._lock:
                    if key not in self._warned_label_keys:
                        self._warned_label_keys.add(key)
                        import logging
                        logging.getLogger("sql_mcp.metrics").warning(
                            "dropping forbidden high-cardinality metric label key: %s", key
                        )
                continue
            text = str(value)
            if len(text) > MAX_LABEL_VALUE_LEN:
                text = text[:MAX_LABEL_VALUE_LEN] + "..."
            out[key] = text
        return out

    def inc(self, name: str, labels: dict | None = None, value: float = 1.0) -> None:
        key = (name, _label_key(self._sanitize(labels)))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        key = (name, _label_key(self._sanitize(labels)))
        with self._lock:
            hist = self._histograms.get(key)
            if hist is None:
                hist = _BoundedHistogram(self._buckets)
                self._histograms[key] = hist
            hist.observe(value)

    def snapshot(self) -> dict:
        with self._lock:
            counters = {
                _metric_label_key(name, labels): value
                for (name, labels), value in self._counters.items()
            }
            histograms = {
                _metric_label_key(name, labels): hist.summary()
                for (name, labels), hist in self._histograms.items()
            }
        return {"counters": counters, "histograms": histograms}

    def iter_histogram_buckets(self):
        """以标准 Prometheus Histogram 序列形式暴露自研直方图（薄适配专用）。

        Yield ``(name, labels, cumulative_buckets, sum, count)``，其中
        ``cumulative_buckets`` 为 ``[(上界, 累计计数), ...]``（含 +Inf）。
        供 prometheus_client / 标准导出桥读取，不改动既有 snapshot 语义。
        """
        with self._lock:
            for (name, labels), hist in self._histograms.items():
                cumulative = 0
                buckets = []
                for i, bound in enumerate(self._buckets):
                    cumulative += hist._buckets[i]
                    buckets.append((float(bound), cumulative))
                cumulative += hist._buckets[-1]
                buckets.append((float("inf"), cumulative))
                yield name, dict(labels), buckets, hist._sum, hist._count

    def render_prometheus(self, extra_gauges: dict[str, float] | None = None) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append("# TYPE %s counter" % name)
                lines.append("%s%s %s" % (name, _render_labels(labels), _fmt_num(value)))
            for (name, labels), hist in sorted(self._histograms.items()):
                lines.append("# TYPE %s histogram" % name)
                cumulative = 0
                for i, bound in enumerate(self._buckets):
                    cumulative += hist._buckets[i]
                    lines.append(
                        "%s_bucket%s %d"
                        % (name, _render_labels(dict(labels, **{"le": _fmt_bound(bound)})), cumulative)
                    )
                lines.append(
                    "%s_bucket%s %d"
                    % (name, _render_labels(dict(labels, **{"le": "+Inf"})), hist._count)
                )
                lines.append("%s_sum%s %s" % (name, _render_labels(labels), _fmt_num(hist._sum)))
                lines.append("%s_count%s %d" % (name, _render_labels(labels), hist._count))
        for name, value in sorted((extra_gauges or {}).items()):
            lines.append("# TYPE %s gauge" % name)
            lines.append("%s %s" % (name, _fmt_num(value)))
        return "\n".join(lines) + "\n"


def _label_key(labels: dict | None) -> tuple:
    return tuple(sorted((str(key), str(value)) for key, value in (labels or {}).items()))


def _metric_label_key(name: str, labels: tuple) -> str:
    return "%s%s" % (name, _render_labels(labels))


def _render_labels(labels) -> str:
    if not labels:
        return ""
    pairs = labels.items() if isinstance(labels, dict) else labels
    body = ",".join('%s="%s"' % (k, _escape(v)) for k, v in pairs)
    return "{%s}" % body


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_num(value) -> str:
    if isinstance(value, float):
        return ("%g" % value) if (value == int(value) and abs(value) < 1e15) else str(value)
    return str(value)


metrics = Metrics()