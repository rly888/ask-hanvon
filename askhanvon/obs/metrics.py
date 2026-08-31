"""进程内指标注册表，/metrics 输出 Prometheus 文本格式。"""
import threading
from collections import defaultdict

_HIST_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 8, 10, 30, 60]


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict = defaultdict(float)
        self._hist: dict = defaultdict(list)

    @staticmethod
    def _key(name, labels):
        if not labels:
            return (name, ())
        return (name, tuple(sorted(labels.items())))

    def inc(self, name: str, labels: dict | None = None, v: float = 1) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += v

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        with self._lock:
            hist = self._hist[self._key(name, labels)]
            hist.append(value)
            if len(hist) > 10000:
                del hist[: len(hist) - 10000]

    def timer(self, name: str, labels: dict | None = None):
        return _Timer(self, name, labels)

    def snapshot(self) -> dict:
        with self._lock:
            counters = {k: v for k, v in self._counters.items()}
            hists = {k: list(v) for k, v in self._hist.items()}
        return {"counters": counters, "hists": hists}

    def render(self) -> str:
        """Prometheus 文本格式。"""
        snap = self.snapshot()
        lines = []

        def emit(name, labels, value):
            label_str = ""
            if labels:
                pairs = ",".join(f'{k}="{v}"' for k, v in labels)
                label_str = "{" + pairs + "}"
            lines.append(f"{name}{label_str} {value}")

        for (name, labels), v in snap["counters"].items():
            emit(name, labels, v)
        for (name, labels), values in snap["hists"].items():
            base = name
            for b in _HIST_BUCKETS:
                emit(base + "_bucket", labels + (("le", str(b)),), sum(1 for x in values if x <= b))
            emit(base + "_bucket", labels + (("le", "+Inf"),), len(values))
            emit(base + "_sum", labels, sum(values))
            emit(base + "_count", labels, len(values))
        return "\n".join(lines) + "\n"


class _Timer:
    def __init__(self, m: Metrics, name: str, labels):
        self.m, self.name, self.labels = m, name, labels

    def __enter__(self):
        import time

        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        import time

        self.m.observe(self.name, (time.perf_counter() - self.t0) * 1000, self.labels)
        return False


metrics = Metrics()
