"""OMNI Agent — Notification Router V2: multi-channel routing, templates, throttling."""
from __future__ import annotations
import json, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class Channel(str, Enum):
    EMAIL    = "email"
    SMS      = "sms"
    PUSH     = "push"
    SLACK    = "slack"
    WEBHOOK  = "webhook"
    IN_APP   = "in_app"
    LOG      = "log"
    CUSTOM   = "custom"


class Priority(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    NORMAL   = "normal"
    LOW      = "low"


class DeliveryStatus(str, Enum):
    PENDING   = "pending"
    SENT      = "sent"
    FAILED    = "failed"
    THROTTLED = "throttled"
    SCHEDULED = "scheduled"


@dataclass
class NotificationTemplate:
    template_id: str
    name: str
    channel: Channel
    subject_template: str = ""
    body_template: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def render(self, variables: Dict[str, Any]) -> Tuple[str, str]:
        def _fill(t: str) -> str:
            for k, v in variables.items():
                t = t.replace(f"{{{k}}}", str(v))
            return t
        return _fill(self.subject_template), _fill(self.body_template)

    def to_dict(self) -> Dict[str, Any]:
        return {"template_id": self.template_id, "name": self.name,
                "channel": self.channel.value}


@dataclass
class Notification:
    notification_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    recipient: str = ""
    channel: Channel = Channel.IN_APP
    subject: str = ""
    body: str = ""
    priority: Priority = Priority.NORMAL
    status: DeliveryStatus = DeliveryStatus.PENDING
    template_id: Optional[str] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    scheduled_at: Optional[float] = None
    sent_at: Optional[float] = None
    error: Optional[str] = None
    attempts: int = 0
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"notification_id": self.notification_id,
                "recipient": self.recipient,
                "channel": self.channel.value,
                "subject": self.subject,
                "status": self.status.value,
                "priority": self.priority.value,
                "attempts": self.attempts}


@dataclass
class ThrottleRule:
    rule_id: str
    channel: Channel
    max_per_window: int
    window_s: float
    per_recipient: bool = True   # False = global limit


class NotificationRouterV2:
    """
    Multi-channel notification router:
    - Channel adapters: email/sms/push/slack/webhook/in_app/log/custom
    - Template engine with variable substitution
    - Priority queue (critical → high → normal → low)
    - Per-recipient, per-channel throttling (rate limiting)
    - Retry logic with max attempts
    - Scheduled delivery (deliver at timestamp)
    - Delivery status tracking (pending/sent/failed/throttled/scheduled)
    - Batch send (fan-out to multiple recipients)
    - Recipient preferences (opt-out per channel)
    - Dead-letter queue for permanently failed notifications
    - Routing rules: auto-select channel by notification type
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:",
                 max_retries: int = 3):
        self._adapters:    Dict[Channel, Callable] = {}
        self._templates:   Dict[str, NotificationTemplate] = {}
        self._queue:       List[Notification] = []
        self._dlq:         List[Notification] = []
        self._history:     List[Notification] = []
        self._throttle:    Dict[str, List[float]] = {}   # key → [timestamps]
        self._throttle_rules: List[ThrottleRule] = []
        self._opt_outs:    Dict[str, List[Channel]] = {}  # recipient → channels
        self._routing_rules: List[Tuple[Callable, Channel]] = []
        self._max_retries  = max_retries
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        # Default LOG adapter
        self._adapters[Channel.LOG] = lambda n: True

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS nr_notifications (
                notification_id TEXT PRIMARY KEY, recipient TEXT,
                channel TEXT, subject TEXT, status TEXT,
                priority TEXT, attempts INTEGER, ts REAL, sent_at REAL
            );
        """)
        self._db.commit()

    # ── ADAPTERS & TEMPLATES ──────────────────────────────────────────

    def register_adapter(self, channel: Channel,
                          fn: Callable[[Notification], bool]):
        """fn receives Notification, returns True on success."""
        self._adapters[channel] = fn

    def add_template(self, name: str,
                      channel: Channel,
                      subject_template: str = "",
                      body_template: str = "",
                      template_id: Optional[str] = None,
                      metadata: Optional[Dict] = None) -> NotificationTemplate:
        tid = template_id or str(uuid.uuid4())[:8]
        t   = NotificationTemplate(template_id=tid, name=name,
                                    channel=channel,
                                    subject_template=subject_template,
                                    body_template=body_template,
                                    metadata=metadata or {})
        self._templates[tid] = t
        return t

    def get_template(self, template_id: str) -> Optional[NotificationTemplate]:
        return self._templates.get(template_id)

    def find_template(self, name: str) -> Optional[NotificationTemplate]:
        return next((t for t in self._templates.values()
                     if t.name == name), None)

    # ── THROTTLE ──────────────────────────────────────────────────────

    def add_throttle_rule(self, channel: Channel,
                           max_per_window: int,
                           window_s: float,
                           per_recipient: bool = True,
                           rule_id: Optional[str] = None):
        self._throttle_rules.append(ThrottleRule(
            rule_id=rule_id or str(uuid.uuid4())[:8],
            channel=channel,
            max_per_window=max_per_window,
            window_s=window_s,
            per_recipient=per_recipient))

    def _is_throttled(self, notif: Notification) -> bool:
        now = time.time()
        for rule in self._throttle_rules:
            if rule.channel != notif.channel: continue
            key = (f"{notif.recipient}:{notif.channel.value}"
                   if rule.per_recipient
                   else notif.channel.value)
            window = self._throttle.get(key, [])
            window = [ts for ts in window if now - ts < rule.window_s]
            if len(window) >= rule.max_per_window:
                self._throttle[key] = window
                return True
            window.append(now)
            self._throttle[key] = window
        return False

    # ── OPT-OUT ───────────────────────────────────────────────────────

    def opt_out(self, recipient: str, channel: Channel):
        self._opt_outs.setdefault(recipient, [])
        if channel not in self._opt_outs[recipient]:
            self._opt_outs[recipient].append(channel)

    def opt_in(self, recipient: str, channel: Channel):
        opts = self._opt_outs.get(recipient, [])
        if channel in opts:
            opts.remove(channel)

    def _is_opted_out(self, notif: Notification) -> bool:
        return notif.channel in self._opt_outs.get(notif.recipient, [])

    # ── ROUTING RULES ─────────────────────────────────────────────────

    def add_routing_rule(self, predicate: Callable[[Notification], bool],
                          channel: Channel):
        """If predicate matches, override channel."""
        self._routing_rules.append((predicate, channel))

    def _apply_routing(self, notif: Notification):
        for pred, ch in self._routing_rules:
            try:
                if pred(notif):
                    notif.channel = ch
                    break
            except Exception:
                pass

    # ── SEND ──────────────────────────────────────────────────────────

    def send(self, recipient: str,
              subject: str = "",
              body: str = "",
              channel: Channel = Channel.IN_APP,
              priority: Priority = Priority.NORMAL,
              template_id: Optional[str] = None,
              variables: Optional[Dict] = None,
              scheduled_at: Optional[float] = None,
              tags: Optional[List[str]] = None,
              metadata: Optional[Dict] = None) -> Notification:
        n = Notification(
            recipient=recipient, channel=channel,
            subject=subject, body=body, priority=priority,
            template_id=template_id,
            variables=dict(variables or {}),
            scheduled_at=scheduled_at,
            tags=list(tags or []),
            metadata=metadata or {})

        # Render template if provided
        if template_id and template_id in self._templates:
            tmpl = self._templates[template_id]
            n.subject, n.body = tmpl.render(n.variables)
            n.channel = tmpl.channel

        # Apply routing rules
        self._apply_routing(n)

        # Schedule?
        if scheduled_at and scheduled_at > time.time():
            n.status = DeliveryStatus.SCHEDULED
            self._queue.append(n)
            self._persist(n)
            return n

        return self._deliver(n)

    def _deliver(self, n: Notification) -> Notification:
        if self._is_opted_out(n):
            n.status = DeliveryStatus.FAILED
            n.error  = "Recipient opted out"
            self._history.append(n)
            self._persist(n)
            return n

        if self._is_throttled(n):
            n.status = DeliveryStatus.THROTTLED
            self._queue.append(n)
            self._persist(n)
            return n

        adapter = self._adapters.get(n.channel)
        if not adapter:
            n.status = DeliveryStatus.FAILED
            n.error  = f"No adapter for channel {n.channel.value}"
            self._dlq.append(n)
            self._history.append(n)
            self._persist(n)
            return n

        while n.attempts <= self._max_retries:
            n.attempts += 1
            try:
                ok = adapter(n)
                if ok:
                    n.status  = DeliveryStatus.SENT
                    n.sent_at = time.time()
                    break
                else:
                    n.error = "Adapter returned False"
            except Exception as exc:
                n.error = str(exc)
        else:
            n.status = DeliveryStatus.FAILED
            self._dlq.append(n)

        self._history.append(n)
        self._persist(n)
        return n

    def send_batch(self, recipients: List[str], **kwargs) -> List[Notification]:
        return [self.send(r, **kwargs) for r in recipients]

    # ── QUEUE FLUSH ───────────────────────────────────────────────────

    def flush_scheduled(self) -> int:
        now     = time.time()
        ready   = [n for n in self._queue
                   if n.status == DeliveryStatus.SCHEDULED
                   and (n.scheduled_at or 0) <= now]
        for n in ready:
            self._queue.remove(n)
            self._deliver(n)
        return len(ready)

    def flush_throttled(self) -> int:
        throttled = [n for n in self._queue
                     if n.status == DeliveryStatus.THROTTLED]
        for n in throttled:
            self._queue.remove(n)
            self._deliver(n)
        return len(throttled)

    # ── QUERY ─────────────────────────────────────────────────────────

    def history(self, recipient: Optional[str] = None,
                 channel: Optional[Channel] = None,
                 status: Optional[DeliveryStatus] = None,
                 limit: int = 50) -> List[Dict]:
        h = self._history
        if recipient: h = [n for n in h if n.recipient == recipient]
        if channel:   h = [n for n in h if n.channel == channel]
        if status:    h = [n for n in h if n.status == status]
        return [n.to_dict() for n in h[-limit:]]

    def dlq(self) -> List[Dict]:
        return [n.to_dict() for n in self._dlq]

    def _persist(self, n: Notification):
        self._db.execute(
            "INSERT OR REPLACE INTO nr_notifications VALUES (?,?,?,?,?,?,?,?,?)",
            (n.notification_id, n.recipient, n.channel.value,
             n.subject[:200], n.status.value, n.priority.value,
             n.attempts, n.ts, n.sent_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        sent = sum(1 for n in self._history
                   if n.status == DeliveryStatus.SENT)
        return {
            "total_sent": len(self._history),
            "delivered": sent,
            "failed": sum(1 for n in self._history
                          if n.status == DeliveryStatus.FAILED),
            "dlq_size": len(self._dlq),
            "queued": len(self._queue),
            "templates": len(self._templates),
        }
