"""OMNI Agent — Resource Monitor: CPU/memory/disk monitoring with alerts."""
from __future__ import annotations
import os, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ResourceType(str, Enum):
    CPU    = "cpu"
    MEMORY = "memory"
    DISK   = "disk"
    NETWORK = "network"
    PROCESS = "process"
    CUSTOM  = "custom"


class AlertSeverity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


@dataclass
class ResourceSample:
    sample_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    resource_type: ResourceType = ResourceType.CPU
    metric: str = ""
    value: float = 0.0
    unit: str = ""
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"resource": self.resource_type.value,
                "metric": self.metric, "value": self.value,
                "unit": self.unit, "ts": self.ts}


@dataclass
class AlertRule:
    rule_id: str
    resource_type: ResourceType
    metric: str
    threshold: float
    operator: str = ">"     # > < >= <= ==
    severity: AlertSeverity = AlertSeverity.WARNING
    cooldown_s: float = 60.0
    enabled: bool = True
    last_fired_at: Optional[float] = None
    fire_count: int = 0

    def evaluate(self, value: float) -> bool:
        if self.operator == ">":  return value >  self.threshold
        if self.operator == "<":  return value <  self.threshold
        if self.operator == ">=": return value >= self.threshold
        if self.operator == "<=": return value <= self.threshold
        if self.operator == "==": return value == self.threshold
        return False

    def can_fire(self) -> bool:
        if not self.enabled: return False
        if self.last_fired_at is None: return True
        return (time.time() - self.last_fired_at) >= self.cooldown_s

    def to_dict(self) -> Dict[str, Any]:
        return {"rule_id": self.rule_id, "metric": self.metric,
                "threshold": self.threshold, "operator": self.operator,
                "severity": self.severity.value, "fire_count": self.fire_count}


@dataclass
class Alert:
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    rule_id: str = ""
    resource_type: ResourceType = ResourceType.CPU
    metric: str = ""
    value: float = 0.0
    threshold: float = 0.0
    severity: AlertSeverity = AlertSeverity.WARNING
    message: str = ""
    ts: float = field(default_factory=time.time)
    acknowledged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"alert_id": self.alert_id, "metric": self.metric,
                "value": self.value, "threshold": self.threshold,
                "severity": self.severity.value, "message": self.message,
                "ts": self.ts, "acknowledged": self.acknowledged}


def _read_cpu_percent() -> float:
    """Read /proc/stat for CPU usage estimate (Linux). Falls back to 0."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        vals   = list(map(int, line.split()[1:]))
        idle   = vals[3]
        total  = sum(vals)
        return max(0.0, min(100.0, (1 - idle / total) * 100)) if total else 0.0
    except Exception:
        return 0.0


def _read_mem_mb() -> Tuple[float, float]:
    """Returns (used_mb, total_mb). Falls back to (0,0)."""
    try:
        info: Dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used  = total - avail
        return used / 1024, total / 1024
    except Exception:
        return 0.0, 0.0


def _read_disk_mb(path: str = "/") -> Tuple[float, float]:
    """Returns (used_mb, total_mb)."""
    try:
        st    = os.statvfs(path)
        total = st.f_blocks * st.f_frsize / (1024 * 1024)
        free  = st.f_bavail * st.f_frsize / (1024 * 1024)
        return total - free, total
    except Exception:
        return 0.0, 0.0


class ResourceMonitor:
    """
    System resource monitor:
    - CPU, memory, disk sampling (stdlib /proc + os.statvfs)
    - Custom metric collectors (pluggable)
    - Configurable polling interval
    - Per-metric history ring buffer
    - Alert rules (threshold + operator + cooldown)
    - Alert handlers (callbacks)
    - Background polling thread
    - Aggregation: min/max/avg/p95 over window
    - Alert acknowledgement
    - Resource snapshot on demand
    - SQLite persistence for samples and alerts
    """

    def __init__(self, db_path: str = ":memory:",
                 history_size: int = 500,
                 poll_interval_s: float = 5.0):
        self._history_size  = history_size
        self._poll_interval = poll_interval_s
        self._samples:  Dict[str, List[ResourceSample]] = {}  # metric → list
        self._rules:    Dict[str, AlertRule] = {}
        self._alerts:   List[Alert] = []
        self._handlers: List[Callable[[Alert], None]] = []
        self._collectors: Dict[str, Callable[[], List[ResourceSample]]] = {}
        self._running   = False
        self._thread:   Optional[threading.Thread] = None
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_db()
        self._register_defaults()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS rm_samples (
                sample_id TEXT PRIMARY KEY, resource_type TEXT,
                metric TEXT, value REAL, unit TEXT, ts REAL
            );
            CREATE TABLE IF NOT EXISTS rm_alerts (
                alert_id TEXT PRIMARY KEY, rule_id TEXT,
                metric TEXT, value REAL, threshold REAL,
                severity TEXT, message TEXT, ts REAL, acknowledged INTEGER
            );
        """)
        self._db.commit()

    def _register_defaults(self):
        self.register_collector("cpu",    self._collect_cpu)
        self.register_collector("memory", self._collect_memory)
        self.register_collector("disk",   self._collect_disk)

    # ── COLLECTORS ───────────────────────────────────────────────────

    def register_collector(self, name: str,
                            fn: Callable[[], List[ResourceSample]]):
        self._collectors[name] = fn

    def _collect_cpu(self) -> List[ResourceSample]:
        pct = _read_cpu_percent()
        return [ResourceSample(resource_type=ResourceType.CPU,
                               metric="cpu_percent", value=pct, unit="%")]

    def _collect_memory(self) -> List[ResourceSample]:
        used, total = _read_mem_mb()
        pct = (used / total * 100) if total else 0.0
        return [
            ResourceSample(resource_type=ResourceType.MEMORY,
                           metric="mem_used_mb", value=round(used, 1), unit="MB"),
            ResourceSample(resource_type=ResourceType.MEMORY,
                           metric="mem_total_mb", value=round(total, 1), unit="MB"),
            ResourceSample(resource_type=ResourceType.MEMORY,
                           metric="mem_percent", value=round(pct, 1), unit="%"),
        ]

    def _collect_disk(self) -> List[ResourceSample]:
        used, total = _read_disk_mb("/")
        pct = (used / total * 100) if total else 0.0
        return [
            ResourceSample(resource_type=ResourceType.DISK,
                           metric="disk_used_mb", value=round(used, 1), unit="MB"),
            ResourceSample(resource_type=ResourceType.DISK,
                           metric="disk_percent", value=round(pct, 1), unit="%"),
        ]

    # ── POLLING ──────────────────────────────────────────────────────

    def start(self):
        if self._running: return
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _poll_loop(self):
        while self._running:
            self.collect_now()
            time.sleep(self._poll_interval)

    def collect_now(self) -> List[ResourceSample]:
        all_samples: List[ResourceSample] = []
        for name, fn in self._collectors.items():
            try:
                samples = fn()
                all_samples.extend(samples)
            except Exception:
                pass
        for s in all_samples:
            self._record(s)
            self._check_rules(s)
        return all_samples

    def _record(self, s: ResourceSample):
        with self._lock:
            lst = self._samples.setdefault(s.metric, [])
            lst.append(s)
            if len(lst) > self._history_size:
                self._samples[s.metric] = lst[-self._history_size:]
        self._db.execute(
            "INSERT OR IGNORE INTO rm_samples VALUES (?,?,?,?,?,?)",
            (s.sample_id, s.resource_type.value,
             s.metric, s.value, s.unit, s.ts))
        self._db.commit()

    # ── ALERT RULES ──────────────────────────────────────────────────

    def add_rule(self, metric: str,
                  threshold: float,
                  operator: str = ">",
                  resource_type: ResourceType = ResourceType.CUSTOM,
                  severity: AlertSeverity = AlertSeverity.WARNING,
                  cooldown_s: float = 60.0,
                  rule_id: Optional[str] = None) -> AlertRule:
        rid  = rule_id or str(uuid.uuid4())[:8]
        rule = AlertRule(rule_id=rid, resource_type=resource_type,
                          metric=metric, threshold=threshold,
                          operator=operator, severity=severity,
                          cooldown_s=cooldown_s)
        self._rules[rid] = rule
        return rule

    def remove_rule(self, rule_id: str) -> bool:
        return self._rules.pop(rule_id, None) is not None

    def enable_rule(self, rule_id: str):
        r = self._rules.get(rule_id)
        if r: r.enabled = True

    def disable_rule(self, rule_id: str):
        r = self._rules.get(rule_id)
        if r: r.enabled = False

    def _check_rules(self, s: ResourceSample):
        for rule in self._rules.values():
            if rule.metric != s.metric: continue
            if not rule.can_fire(): continue
            if rule.evaluate(s.value):
                alert = Alert(
                    rule_id=rule.rule_id,
                    resource_type=s.resource_type,
                    metric=s.metric,
                    value=s.value,
                    threshold=rule.threshold,
                    severity=rule.severity,
                    message=(f"{s.metric} is {s.value:.1f} "
                             f"({rule.operator} {rule.threshold})"))
                rule.last_fired_at = time.time()
                rule.fire_count   += 1
                self._alerts.append(alert)
                self._persist_alert(alert)
                for h in self._handlers:
                    try: h(alert)
                    except Exception: pass

    def on_alert(self, fn: Callable[[Alert], None]):
        self._handlers.append(fn)

    # ── QUERY ────────────────────────────────────────────────────────

    def latest(self, metric: str) -> Optional[ResourceSample]:
        lst = self._samples.get(metric, [])
        return lst[-1] if lst else None

    def history(self, metric: str,
                limit: int = 100) -> List[ResourceSample]:
        return self._samples.get(metric, [])[-limit:]

    def snapshot(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for metric, lst in self._samples.items():
            if lst:
                result[metric] = round(lst[-1].value, 2)
        return result

    def aggregate(self, metric: str,
                   window: int = 60,
                   fn: str = "avg") -> Optional[float]:
        """Aggregate last `window` samples. fn: avg|min|max|p95"""
        lst = self._samples.get(metric, [])[-window:]
        if not lst: return None
        vals = [s.value for s in lst]
        if fn == "avg": return sum(vals) / len(vals)
        if fn == "min": return min(vals)
        if fn == "max": return max(vals)
        if fn == "p95":
            sorted_vals = sorted(vals)
            idx = max(0, int(len(sorted_vals) * 0.95) - 1)
            return sorted_vals[idx]
        return None

    def get_alerts(self, severity: Optional[AlertSeverity] = None,
                   acknowledged: Optional[bool] = None,
                   limit: int = 50) -> List[Dict]:
        alerts = self._alerts
        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        if acknowledged is not None:
            alerts = [a for a in alerts if a.acknowledged == acknowledged]
        return [a.to_dict() for a in alerts[-limit:]]

    def acknowledge(self, alert_id: str) -> bool:
        for a in self._alerts:
            if a.alert_id == alert_id:
                a.acknowledged = True
                return True
        return False

    def _persist_alert(self, a: Alert):
        self._db.execute(
            "INSERT OR IGNORE INTO rm_alerts VALUES (?,?,?,?,?,?,?,?,?)",
            (a.alert_id, a.rule_id, a.metric, a.value, a.threshold,
             a.severity.value, a.message, a.ts, int(a.acknowledged)))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "metrics_tracked": len(self._samples),
            "total_samples": sum(len(v) for v in self._samples.values()),
            "rules": len(self._rules),
            "alerts": len(self._alerts),
            "collectors": len(self._collectors),
        }
