"""OMNI Agent — Notification Manager V2: multi-channel, templates, delivery tracking."""
from __future__ import annotations
import json, queue, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class NotifChannel(str, Enum):
    EMAIL   = "email"
    SMS     = "sms"
    SLACK   = "slack"
    WEBHOOK = "webhook"
    PUSH    = "push"
    IN_APP  = "in_app"


class NotifStatus(str, Enum):
    PENDING   = "pending"
    SENT      = "sent"
    FAILED    = "failed"
    RETRYING  = "retrying"
    CANCELLED = "cancelled"


class NotifPriority(int, Enum):
    URGENT = 0
    HIGH   = 1
    NORMAL = 2
    LOW    = 3


@dataclass
class NotifTemplate:
    template_id: str
    name: str
    subject_tpl: str = ""
    body_tpl: str = ""
    channel: NotifChannel = NotifChannel.EMAIL
    variables: List[str] = field(default_factory=list)

    def render(self, **kwargs) -> Dict[str, str]:
        subject = self.subject_tpl
        body    = self.body_tpl
        for k, v in kwargs.items():
            subject = subject.replace(f"{{{k}}}", str(v))
            body    = body.replace(f"{{{k}}}", str(v))
        return {"subject": subject, "body": body}

    def to_dict(self) -> Dict[str, Any]:
        return {"template_id": self.template_id, "name": self.name,
                "channel": self.channel.value}


@dataclass
class Notification:
    notif_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    recipient_id: str = ""
    channel: NotifChannel = NotifChannel.IN_APP
    priority: NotifPriority = NotifPriority.NORMAL
    subject: str = ""
    body: str = ""
    template_id: Optional[str] = None
    status: NotifStatus = NotifStatus.PENDING
    attempt: int = 0
    max_retries: int = 2
    retry_delay_s: float = 5.0
    scheduled_at: Optional[float] = None   # None = send now
    sent_at: Optional[float] = None
    error: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "notif_id": self.notif_id,
            "recipient_id": self.recipient_id,
            "channel": self.channel.value,
            "priority": self.priority.value,
            "subject": self.subject[:80],
            "status": self.status.value,
            "attempt": self.attempt,
        }


class NotificationManagerV2:
    """
    Multi-channel notification manager:
    - Named channel handlers (email, SMS, Slack, webhook, push, in-app)
    - Template system with variable substitution
    - Priority queue (URGENT → LOW)
    - Scheduled notifications (send after timestamp)
    - Per-notification retry with configurable delay
    - Delivery tracking (sent_at, error, attempt)
    - Recipient preferences (preferred channel, opt-outs)
    - Batch send
    - Notification history query
    - Stats per channel
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._handlers:     Dict[NotifChannel, Callable] = {}
        self._templates:    Dict[str, NotifTemplate] = {}
        self._preferences:  Dict[str, Dict] = {}   # recipient_id → prefs
        self._opt_outs:     Dict[str, set] = {}    # recipient_id → {channels}
        self._notif_queue:  queue.PriorityQueue = queue.PriorityQueue()
        self._notifications: Dict[str, Notification] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_db()
        self._sent_count   = 0
        self._failed_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS nm_notifications (
                notif_id TEXT PRIMARY KEY, recipient_id TEXT,
                channel TEXT, priority INTEGER, subject TEXT,
                body TEXT, status TEXT, attempt INTEGER,
                sent_at REAL, error TEXT, created_at REAL
            );
        """)
        self._db.commit()

    # ── HANDLERS ─────────────────────────────────────────────────────

    def register_handler(self, channel: NotifChannel,
                          fn: Callable[[Notification], bool]):
        self._handlers[channel] = fn

    # ── TEMPLATES ────────────────────────────────────────────────────

    def add_template(self, name: str,
                     body_tpl: str,
                     subject_tpl: str = "",
                     channel: NotifChannel = NotifChannel.EMAIL,
                     template_id: Optional[str] = None) -> NotifTemplate:
        import re
        tid = template_id or str(uuid.uuid4())[:8]
        variables = re.findall(r"\{(\w+)\}", body_tpl + subject_tpl)
        t = NotifTemplate(template_id=tid, name=name,
                          subject_tpl=subject_tpl, body_tpl=body_tpl,
                          channel=channel, variables=list(set(variables)))
        self._templates[tid] = t
        return t

    def get_template(self, template_id: str) -> Optional[NotifTemplate]:
        return self._templates.get(template_id)

    # ── PREFERENCES ──────────────────────────────────────────────────

    def set_preference(self, recipient_id: str,
                        preferred_channel: NotifChannel,
                        **extras):
        self._preferences[recipient_id] = {
            "preferred_channel": preferred_channel, **extras}

    def opt_out(self, recipient_id: str, channel: NotifChannel):
        self._opt_outs.setdefault(recipient_id, set()).add(channel)

    def opt_in(self, recipient_id: str, channel: NotifChannel):
        self._opt_outs.get(recipient_id, set()).discard(channel)

    def _is_opted_out(self, recipient_id: str,
                       channel: NotifChannel) -> bool:
        return channel in self._opt_outs.get(recipient_id, set())

    # ── SEND ─────────────────────────────────────────────────────────

    def notify(self, recipient_id: str,
               channel: Optional[NotifChannel] = None,
               subject: str = "",
               body: str = "",
               template_id: Optional[str] = None,
               template_vars: Optional[Dict] = None,
               priority: NotifPriority = NotifPriority.NORMAL,
               max_retries: int = 2,
               retry_delay_s: float = 0.0,
               scheduled_at: Optional[float] = None,
               tags: Optional[List[str]] = None,
               metadata: Optional[Dict] = None,
               notif_id: Optional[str] = None) -> Notification:

        # Resolve channel
        if channel is None:
            pref = self._preferences.get(recipient_id, {})
            channel = pref.get("preferred_channel", NotifChannel.IN_APP)

        # Opt-out check
        if self._is_opted_out(recipient_id, channel):
            n = Notification(notif_id=notif_id or str(uuid.uuid4())[:10],
                             recipient_id=recipient_id, channel=channel,
                             priority=priority, subject=subject, body=body,
                             status=NotifStatus.CANCELLED)
            self._notifications[n.notif_id] = n
            self._persist(n)
            return n

        # Template rendering
        if template_id and template_id in self._templates:
            tmpl = self._templates[template_id]
            rendered = tmpl.render(**(template_vars or {}))
            subject = rendered.get("subject", subject)
            body    = rendered.get("body", body)
            channel = channel or tmpl.channel

        n = Notification(
            notif_id=notif_id or str(uuid.uuid4())[:10],
            recipient_id=recipient_id, channel=channel,
            priority=priority, subject=subject, body=body,
            template_id=template_id, max_retries=max_retries,
            retry_delay_s=retry_delay_s, scheduled_at=scheduled_at,
            tags=list(tags or []), metadata=metadata or {})

        self._notifications[n.notif_id] = n

        if scheduled_at and scheduled_at > time.time():
            self._persist(n)
            return n   # Will be dispatched by flush_scheduled

        self._dispatch(n)
        return n

    def notify_batch(self, recipients: List[str], **kwargs) -> List[Notification]:
        return [self.notify(r, **kwargs) for r in recipients]

    def _dispatch(self, n: Notification) -> bool:
        handler = self._handlers.get(n.channel)
        if not handler:
            n.status = NotifStatus.FAILED
            n.error  = f"No handler for channel {n.channel.value}"
            self._persist(n)
            self._failed_count += 1
            return False

        n.attempt += 1
        try:
            success = handler(n)
            if success:
                n.status  = NotifStatus.SENT
                n.sent_at = time.time()
                self._sent_count += 1
            else:
                raise RuntimeError("Handler returned False")
        except Exception as exc:
            n.error = str(exc)
            if n.attempt <= n.max_retries:
                n.status = NotifStatus.RETRYING
                self._persist(n)
                if n.retry_delay_s > 0:
                    time.sleep(n.retry_delay_s)
                return self._dispatch(n)
            else:
                n.status = NotifStatus.FAILED
                self._failed_count += 1

        self._persist(n)
        return n.status == NotifStatus.SENT

    def resend(self, notif_id: str) -> bool:
        n = self._notifications.get(notif_id)
        if not n: return False
        n.attempt = 0
        n.status  = NotifStatus.PENDING
        return self._dispatch(n)

    def cancel(self, notif_id: str) -> bool:
        n = self._notifications.get(notif_id)
        if n and n.status == NotifStatus.PENDING:
            n.status = NotifStatus.CANCELLED
            self._persist(n)
            return True
        return False

    def flush_scheduled(self) -> List[Notification]:
        """Dispatch all scheduled notifications whose time has come."""
        now  = time.time()
        sent = []
        for n in self._notifications.values():
            if (n.scheduled_at and n.scheduled_at <= now
                    and n.status == NotifStatus.PENDING):
                self._dispatch(n)
                sent.append(n)
        return sent

    # ── QUERY ────────────────────────────────────────────────────────

    def get_notification(self, notif_id: str) -> Optional[Notification]:
        return self._notifications.get(notif_id)

    def list_for_recipient(self, recipient_id: str,
                            limit: int = 50) -> List[Dict]:
        return [n.to_dict() for n in self._notifications.values()
                if n.recipient_id == recipient_id][-limit:]

    def list_by_status(self, status: NotifStatus,
                        limit: int = 50) -> List[Dict]:
        return [n.to_dict() for n in self._notifications.values()
                if n.status == status][-limit:]

    def history(self, limit: int = 50) -> List[Dict]:
        rows = self._db.execute(
            "SELECT notif_id,recipient_id,channel,status,subject,sent_at "
            "FROM nm_notifications ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"id": r[0], "recipient": r[1], "channel": r[2],
                 "status": r[3]} for r in rows]

    def channel_stats(self) -> Dict[str, Dict]:
        stats: Dict[str, Dict] = {}
        for n in self._notifications.values():
            ch = n.channel.value
            if ch not in stats:
                stats[ch] = {"sent": 0, "failed": 0, "pending": 0}
            if n.status == NotifStatus.SENT:    stats[ch]["sent"]    += 1
            elif n.status == NotifStatus.FAILED: stats[ch]["failed"]  += 1
            elif n.status == NotifStatus.PENDING: stats[ch]["pending"] += 1
        return stats

    def _persist(self, n: Notification):
        self._db.execute(
            "INSERT OR REPLACE INTO nm_notifications VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (n.notif_id, n.recipient_id, n.channel.value,
             n.priority.value, n.subject[:200], n.body[:1000],
             n.status.value, n.attempt, n.sent_at, n.error, n.created_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "total": len(self._notifications),
            "sent": self._sent_count,
            "failed": self._failed_count,
            "templates": len(self._templates),
            "handlers": len(self._handlers),
        }
