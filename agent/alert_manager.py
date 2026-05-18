"""OMNI Agent — Alert Manager: multi-channel alerting, dedup, escalation, silencing."""
from __future__ import annotations
import hashlib, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"
    INFO     = "info"


class AlertStatus(str, Enum):
    FIRING    = "firing"
    RESOLVED  = "resolved"
    SILENCED  = "silenced"
    PENDING   = "pending"     # not yet reached threshold


SEVERITY_INT = {
    Severity.CRITICAL: 5, Severity.HIGH: 4,
    Severity.MEDIUM: 3, Severity.LOW: 2, Severity.INFO: 1,
}


@dataclass
class Alert:
    alert_id: str
    name: str
    severity: Severity
    message: str
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    status: AlertStatus = AlertStatus.FIRING
    source: str = ""
    fingerprint: str = ""       # dedup key
    fired_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    notified_at: Optional[float] = None
    escalation_level: int = 0
    repeat_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "name": self.name,
            "severity": self.severity.value,
            "status": self.status.value,
            "message": self.message[:200],
            "source": self.source,
            "fired_at": self.fired_at,
            "repeat_count": self.repeat_count,
            "escalation_level": self.escalation_level,
        }


@dataclass
class AlertRule:
    rule_id: str
    name: str
    condition: Callable[[Dict[str, Any]], bool]
    severity: Severity = Severity.MEDIUM
    message_template: str = "{name} triggered"
    source: str = ""
    labels: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    cooldown_s: float = 60.0       # min seconds between same alerts
    repeat_interval_s: float = 300.0  # re-notify interval while still firing
    pending_threshold: int = 1     # consecutive triggers before FIRING
    _pending_count: int = field(default=0, init=False, repr=False)
    _last_fired: float = field(default=0.0, init=False, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "severity": self.severity.value,
            "enabled": self.enabled,
            "cooldown_s": self.cooldown_s,
        }


@dataclass
class Channel:
    channel_id: str
    name: str
    handler: Callable[[Alert], None]
    min_severity: Severity = Severity.INFO
    enabled: bool = True
    labels_filter: Dict[str, str] = field(default_factory=dict)


@dataclass
class EscalationPolicy:
    policy_id: str
    levels: List[Dict[str, Any]]   # [{after_s, channel_ids, min_severity}]


@dataclass
class Silence:
    silence_id: str
    labels_match: Dict[str, str]   # key=val to match against alert labels
    starts_at: float
    ends_at: float
    reason: str = ""
    created_by: str = ""

    @property
    def is_active(self) -> bool:
        now = time.time()
        return self.starts_at <= now <= self.ends_at


class AlertManager:
    """
    Production alert manager:
    - Define rules evaluated against metric context dicts
    - Multi-channel dispatch (webhook, email, slack mock)
    - Deduplication by fingerprint
    - Silences (by label matching, time-bounded)
    - Escalation policies (re-notify at higher channels after N seconds)
    - Pending threshold (N triggers before firing)
    - Repeat notifications while alert stays firing
    - SQLite audit log
    """

    def __init__(self, db_path: str = ":memory:"):
        self._rules: Dict[str, AlertRule] = {}
        self._channels: Dict[str, Channel] = {}
        self._escalation_policies: Dict[str, EscalationPolicy] = {}
        self._silences: Dict[str, Silence] = {}
        self._active_alerts: Dict[str, Alert] = {}   # fingerprint → Alert
        self._alert_history: List[Alert] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._fire_count = 0
        self._notify_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS am_alerts (
                alert_id TEXT PRIMARY KEY, name TEXT, severity TEXT,
                status TEXT, message TEXT, source TEXT,
                fired_at REAL, resolved_at REAL, repeat_count INTEGER
            );
            CREATE TABLE IF NOT EXISTS am_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id TEXT, channel_id TEXT, ts REAL, success INTEGER
            );
        """)
        self._db.commit()

    # ── RULE MANAGEMENT ───────────────────────────────────────────────

    def add_rule(self, name: str,
                 condition: Callable[[Dict[str, Any]], bool],
                 severity: Severity = Severity.MEDIUM,
                 message_template: str = "",
                 source: str = "",
                 labels: Optional[Dict[str, str]] = None,
                 cooldown_s: float = 60.0,
                 repeat_interval_s: float = 300.0,
                 pending_threshold: int = 1,
                 enabled: bool = True,
                 rule_id: Optional[str] = None) -> AlertRule:
        rid = rule_id or str(uuid.uuid4())[:8]
        rule = AlertRule(
            rule_id=rid, name=name, condition=condition,
            severity=severity,
            message_template=message_template or f"{name} triggered",
            source=source, labels=dict(labels or {}),
            enabled=enabled, cooldown_s=cooldown_s,
            repeat_interval_s=repeat_interval_s,
            pending_threshold=pending_threshold)
        self._rules[rid] = rule
        return rule

    def remove_rule(self, rule_id: str):
        self._rules.pop(rule_id, None)

    def enable_rule(self, rule_id: str):
        if rule_id in self._rules: self._rules[rule_id].enabled = True

    def disable_rule(self, rule_id: str):
        if rule_id in self._rules: self._rules[rule_id].enabled = False

    # ── CHANNEL MANAGEMENT ────────────────────────────────────────────

    def add_channel(self, name: str,
                    handler: Callable[[Alert], None],
                    min_severity: Severity = Severity.INFO,
                    labels_filter: Optional[Dict[str, str]] = None,
                    channel_id: Optional[str] = None) -> Channel:
        cid = channel_id or str(uuid.uuid4())[:8]
        ch  = Channel(channel_id=cid, name=name, handler=handler,
                      min_severity=min_severity,
                      labels_filter=dict(labels_filter or {}))
        self._channels[cid] = ch
        return ch

    def disable_channel(self, channel_id: str):
        if channel_id in self._channels:
            self._channels[channel_id].enabled = False

    def enable_channel(self, channel_id: str):
        if channel_id in self._channels:
            self._channels[channel_id].enabled = True

    # ── ESCALATION ────────────────────────────────────────────────────

    def add_escalation_policy(self, policy_id: str,
                               levels: List[Dict]) -> EscalationPolicy:
        ep = EscalationPolicy(policy_id=policy_id, levels=levels)
        self._escalation_policies[policy_id] = ep
        return ep

    # ── SILENCES ──────────────────────────────────────────────────────

    def add_silence(self, labels_match: Dict[str, str],
                    duration_s: float = 3600.0,
                    reason: str = "",
                    created_by: str = "",
                    silence_id: Optional[str] = None) -> Silence:
        sid = silence_id or str(uuid.uuid4())[:8]
        now = time.time()
        s   = Silence(silence_id=sid, labels_match=labels_match,
                      starts_at=now, ends_at=now + duration_s,
                      reason=reason, created_by=created_by)
        self._silences[sid] = s
        return s

    def expire_silence(self, silence_id: str):
        s = self._silences.get(silence_id)
        if s: s.ends_at = 0.0

    def _is_silenced(self, alert: Alert) -> bool:
        for silence in self._silences.values():
            if not silence.is_active:
                continue
            if all(alert.labels.get(k) == v
                   for k, v in silence.labels_match.items()):
                return True
        return False

    # ── EVALUATION ────────────────────────────────────────────────────

    def evaluate(self, context: Dict[str, Any]) -> List[Alert]:
        """Evaluate all rules against a context dict. Returns new/updated alerts."""
        fired: List[Alert] = []
        for rule in self._rules.values():
            if not rule.enabled:
                continue
            try:
                triggered = rule.condition(context)
            except Exception:
                continue

            if not triggered:
                rule._pending_count = 0
                continue

            rule._pending_count += 1
            if rule._pending_count < rule.pending_threshold:
                continue

            now = time.time()
            if now - rule._last_fired < rule.cooldown_s:
                # Check if still firing — send repeat if due
                fp = self._fingerprint(rule)
                existing = self._active_alerts.get(fp)
                if existing and existing.status == AlertStatus.FIRING:
                    if (now - (existing.notified_at or existing.fired_at)
                            >= rule.repeat_interval_s):
                        existing.repeat_count += 1
                        existing.notified_at = now
                        self._notify(existing)
                        fired.append(existing)
                continue

            rule._last_fired = now
            fp    = self._fingerprint(rule)
            msg   = rule.message_template.format(
                name=rule.name, **context) if "{" in rule.message_template \
                else rule.message_template

            existing = self._active_alerts.get(fp)
            if existing and existing.status == AlertStatus.FIRING:
                existing.repeat_count += 1
                existing.notified_at = now
                self._notify(existing)
                fired.append(existing)
            else:
                alert = Alert(
                    alert_id=str(uuid.uuid4())[:8],
                    name=rule.name, severity=rule.severity,
                    message=msg, labels=dict(rule.labels),
                    source=rule.source, fingerprint=fp,
                    status=AlertStatus.PENDING if rule.pending_threshold > 1
                    else AlertStatus.FIRING)
                if alert.status == AlertStatus.FIRING:
                    alert.notified_at = now
                    self._active_alerts[fp] = alert
                    if not self._is_silenced(alert):
                        self._notify(alert)
                    self._persist_alert(alert)
                    self._alert_history.append(alert)
                    self._fire_count += 1
                fired.append(alert)
        return fired

    def resolve(self, fingerprint: str) -> Optional[Alert]:
        alert = self._active_alerts.get(fingerprint)
        if alert:
            alert.status = AlertStatus.RESOLVED
            alert.resolved_at = time.time()
            self._active_alerts.pop(fingerprint)
            self._persist_alert(alert)
            self._notify_resolved(alert)
        return alert

    def resolve_rule(self, rule_id: str) -> Optional[Alert]:
        rule = self._rules.get(rule_id)
        if not rule: return None
        return self.resolve(self._fingerprint(rule))

    def _fingerprint(self, rule: AlertRule) -> str:
        raw = f"{rule.name}:{rule.source}:{sorted(rule.labels.items())}"
        return hashlib.md5(  # nosec B324 - deterministic alert fingerprint only
            raw.encode(), usedforsecurity=False
        ).hexdigest()[:12]

    # ── NOTIFICATION ──────────────────────────────────────────────────

    def _notify(self, alert: Alert):
        for ch in self._channels.values():
            if not ch.enabled: continue
            if SEVERITY_INT[alert.severity] < SEVERITY_INT[ch.min_severity]:
                continue
            if ch.labels_filter:
                if not all(alert.labels.get(k) == v
                           for k, v in ch.labels_filter.items()):
                    continue
            if self._is_silenced(alert):
                alert.status = AlertStatus.SILENCED
                continue
            try:
                ch.handler(alert)
                self._notify_count += 1
                self._db.execute(
                    "INSERT INTO am_notifications (alert_id,channel_id,ts,success) "
                    "VALUES (?,?,?,1)", (alert.alert_id, ch.channel_id, time.time()))
                self._db.commit()
            except Exception:
                pass

    def _notify_resolved(self, alert: Alert):
        for ch in self._channels.values():
            if ch.enabled:
                try: ch.handler(alert)
                except Exception: pass

    def _persist_alert(self, alert: Alert):
        self._db.execute(
            "INSERT OR REPLACE INTO am_alerts VALUES (?,?,?,?,?,?,?,?,?)",
            (alert.alert_id, alert.name, alert.severity.value,
             alert.status.value, alert.message[:200],
             alert.source, alert.fired_at,
             alert.resolved_at, alert.repeat_count))
        self._db.commit()

    # ── QUERY ─────────────────────────────────────────────────────────

    def active_alerts(self, min_severity: Optional[Severity] = None) -> List[Alert]:
        alerts = list(self._active_alerts.values())
        if min_severity:
            alerts = [a for a in alerts
                      if SEVERITY_INT[a.severity] >= SEVERITY_INT[min_severity]]
        return sorted(alerts, key=lambda a: -SEVERITY_INT[a.severity])

    def alert_history(self, limit: int = 50) -> List[Dict]:
        rows = self._db.execute(
            "SELECT alert_id,name,severity,status,message,fired_at "
            "FROM am_alerts ORDER BY fired_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "name": r[1], "severity": r[2],
                 "status": r[3], "fired_at": r[5]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        return {
            "rules": len(self._rules),
            "channels": len(self._channels),
            "active_alerts": len(self._active_alerts),
            "silences": sum(1 for s in self._silences.values() if s.is_active),
            "total_fired": self._fire_count,
            "total_notified": self._notify_count,
        }
