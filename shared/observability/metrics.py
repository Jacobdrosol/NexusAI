import threading
from typing import Dict, List, Tuple


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class MetricsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter_defs: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
        self._gauge_defs: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
        self._hist_defs: Dict[str, Tuple[str, Tuple[str, ...], Tuple[float, ...]]] = {}
        self._counter_values: Dict[Tuple[str, Tuple[str, ...]], float] = {}
        self._gauge_values: Dict[Tuple[str, Tuple[str, ...]], float] = {}
        self._hist_bucket_values: Dict[Tuple[str, Tuple[str, ...], float], int] = {}
        self._hist_sum_values: Dict[Tuple[str, Tuple[str, ...]], float] = {}
        self._hist_count_values: Dict[Tuple[str, Tuple[str, ...]], int] = {}

    def register_counter(self, name: str, help_text: str, label_names: List[str]) -> None:
        with self._lock:
            self._counter_defs[name] = (help_text, tuple(label_names))

    def register_gauge(self, name: str, help_text: str, label_names: List[str]) -> None:
        with self._lock:
            self._gauge_defs[name] = (help_text, tuple(label_names))

    def register_histogram(
        self,
        name: str,
        help_text: str,
        label_names: List[str],
        buckets: List[float],
    ) -> None:
        with self._lock:
            self._hist_defs[name] = (help_text, tuple(label_names), tuple(sorted(buckets)))

    def inc_counter(self, name: str, labels: Dict[str, str], amount: float = 1.0) -> None:
        with self._lock:
            key = (name, self._label_values(name, labels, self._counter_defs))
            self._counter_values[key] = self._counter_values.get(key, 0.0) + amount

    def set_gauge(self, name: str, labels: Dict[str, str], value: float) -> None:
        with self._lock:
            key = (name, self._label_values(name, labels, self._gauge_defs))
            self._gauge_values[key] = float(value)

    def observe_histogram(self, name: str, labels: Dict[str, str], value: float) -> None:
        with self._lock:
            if name not in self._hist_defs:
                raise KeyError(f"Unknown histogram metric: {name}")
            _, label_names, buckets = self._hist_defs[name]
            label_values = tuple(str(labels.get(k, "")) for k in label_names)
            hkey = (name, label_values)
            self._hist_sum_values[hkey] = self._hist_sum_values.get(hkey, 0.0) + float(value)
            self._hist_count_values[hkey] = self._hist_count_values.get(hkey, 0) + 1
            for le in buckets:
                if float(value) <= le:
                    bkey = (name, label_values, le)
                    self._hist_bucket_values[bkey] = self._hist_bucket_values.get(bkey, 0) + 1

    def render(self) -> str:
        lines: List[str] = []
        with self._lock:
            for name, (help_text, label_names) in self._counter_defs.items():
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} counter")
                for (metric_name, label_values), value in sorted(self._counter_values.items()):
                    if metric_name != name:
                        continue
                    lines.append(self._sample(name, label_names, label_values, value))

            for name, (help_text, label_names) in self._gauge_defs.items():
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} gauge")
                for (metric_name, label_values), value in sorted(self._gauge_values.items()):
                    if metric_name != name:
                        continue
                    lines.append(self._sample(name, label_names, label_values, value))

            for name, (help_text, label_names, buckets) in self._hist_defs.items():
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} histogram")
                distinct_labels = {
                    label_values
                    for (metric_name, label_values), _count in self._hist_count_values.items()
                    if metric_name == name
                }
                for label_values in sorted(distinct_labels):
                    cumulative = 0
                    for le in buckets:
                        cumulative = self._hist_bucket_values.get((name, label_values, le), cumulative)
                        bucket_labels = dict(zip(label_names, label_values))
                        bucket_labels["le"] = str(le)
                        lines.append(self._sample(f"{name}_bucket", tuple(list(label_names) + ["le"]), tuple(bucket_labels[k] for k in list(label_names) + ["le"]), cumulative))
                    bucket_labels = dict(zip(label_names, label_values))
                    bucket_labels["le"] = "+Inf"
                    total = self._hist_count_values.get((name, label_values), 0)
                    lines.append(self._sample(f"{name}_bucket", tuple(list(label_names) + ["le"]), tuple(bucket_labels[k] for k in list(label_names) + ["le"]), total))
                    lines.append(self._sample(f"{name}_sum", label_names, label_values, self._hist_sum_values.get((name, label_values), 0.0)))
                    lines.append(self._sample(f"{name}_count", label_names, label_values, total))

        return "\n".join(lines) + "\n"

    def _label_values(
        self,
        name: str,
        labels: Dict[str, str],
        defs: Dict[str, Tuple[str, Tuple[str, ...]]],
    ) -> Tuple[str, ...]:
        if name not in defs:
            raise KeyError(f"Unknown metric: {name}")
        _, label_names = defs[name]
        return tuple(str(labels.get(k, "")) for k in label_names)

    def _sample(
        self,
        name: str,
        label_names: Tuple[str, ...],
        label_values: Tuple[str, ...],
        value: float,
    ) -> str:
        if not label_names:
            return f"{name} {value}"
        pairs = [f'{k}="{_escape_label_value(v)}"' for k, v in zip(label_names, label_values)]
        return f"{name}{{{','.join(pairs)}}} {value}"

