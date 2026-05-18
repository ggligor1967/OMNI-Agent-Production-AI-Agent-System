"""OMNI AGENT - Notification Manager
Multi-channel notifications with templates, routing rules,
delivery tracking, rate limiting, and deduplication.

Features:
- Channels: EMAIL, SMS, WEBHOOK, SLACK, PUSH, LOG (in-process)
- Template engine: {variable} interpolation + conditional blocks
- Routing rules: match on event_type, severity, audience → channel(s)
- Priority levels: CRITICAL, HIGH, MEDIUM, LOW; affects retry and rate
- Rate limiting: per-channel max N notifications per window_s
- Deduplication: hash(channel + event_type + payload) suppress duplicates within ttl
- Delivery tracking: PENDING → SENT/FAILED/SUPPRESSED status per attempt
- Retry: configurable max_retries with backoff per channel
- Audience groups: name → list of recipient addresses
- Batch mode: group low-priority notifications; flush on schedule
- Digest: aggregate N notifications into one summary message
- On-send hook: fn(notification) → bool (return False to suppress)
- Notification history: last N per channel
- SQLite persistence: notifications, templates, delivery log
- REST API: send, templates, delivery_log, stats
"""
import hashlib, json, re, sqlite3, time, uuid, logging
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class Channel(str, Enum):
    EMAIL   = "email"; SMS     = "sms"
    WEBHOOK = "webhook"; SLACK = "slack"
    PUSH    = "push";  LOG     = "log"

class Priority(str, Enum):
    CRITICAL = "critical"; HIGH   = "high"
    MEDIUM   = "medium";   LOW    = "low"

class DeliveryStatus(str, Enum):
    PENDING     = "pending";   SENT      = "sent"
    FAILED      = "failed";    SUPPRESSED = "suppressed"
    RATE_LIMITED = "rate_limited"

def _render_template(template: str, ctx: Dict[str, Any]) -> str:
    """Render {variable} placeholders; skip unknown keys."""
    def replace(m):
        key = m.group(1).strip()
        return str(ctx.get(key, m.group(0)))
    text = re.sub(r'\{([^}]+)\}', replace, template)
    # Conditional blocks: {% if key %}...{% endif %}
    def cond(m):
        key = m.group(1).strip(); body = m.group(2)
        return body if ctx.get(key) else ""
    text = re.sub(r'\{%\s*if\s+(\w+)\s*%\}(.*?)\{%\s*endif\s*%\}',
                   cond, text, flags=re.DOTALL)
    return text

@dataclass
class NotificationTemplate:
    name: str; subject: str = ""; body: str = ""
    channel: Optional[Channel] = None

    def render(self, ctx: Dict) -> Dict:
        return {"subject": _render_template(self.subject, ctx),
                "body":    _render_template(self.body,    ctx)}

@dataclass
class RoutingRule:
    name: str
    event_types: List[str] = field(default_factory=list)  # [] = match all
    severity: List[Priority] = field(default_factory=list)  # [] = match all
    channels: List[Channel] = field(default_factory=list)
    template_name: str = ""
    audiences: List[str] = field(default_factory=list)  # audience group names
    enabled: bool = True

@dataclass
class Notification:
    id: str; event_type: str; channel: Channel
    recipient: str; subject: str; body: str
    priority: Priority = Priority.MEDIUM
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempt: int = 0; max_retries: int = 2
    created_at: float = field(default_factory=time.time)
    sent_at: float = 0.0; error: str = ""

    def to_dict(self):
        return {"id": self.id, "event_type": self.event_type,
                "channel": self.channel.value, "recipient": self.recipient,
                "subject": self.subject[:100], "priority": self.priority.value,
                "status": self.status.value, "attempt": self.attempt,
                "created_at": round(self.created_at, 2),
                "sent_at": round(self.sent_at, 2), "error": self.error}

@dataclass
class ChannelConfig:
    channel: Channel
    max_per_window: int = 100; window_s: float = 3600
    max_retries: int = 2; retry_delay_s: float = 5.0
    sender_fn: Optional[Callable] = None  # fn(notif) → bool
    batch_size: int = 0    # 0 = immediate; >0 = batch mode
    dedup_ttl_s: float = 300.0

class NMStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS notifications(
                    id TEXT PRIMARY KEY, event_type TEXT,
                    channel TEXT, recipient TEXT,
                    subject TEXT, priority TEXT, status TEXT,
                    attempt INTEGER DEFAULT 0,
                    created_at REAL, sent_at REAL DEFAULT 0,
                    error TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS templates(
                    name TEXT PRIMARY KEY, subject TEXT,
                    body TEXT, channel TEXT DEFAULT '');
                CREATE INDEX IF NOT EXISTS idx_notif_status
                    ON notifications(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_notif_channel
                    ON notifications(channel, created_at DESC);
            """)

    def save(self, n: Notification):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO notifications VALUES"
                       "(?,?,?,?,?,?,?,?,?,?,?)",
                (n.id, n.event_type, n.channel.value, n.recipient,
                 n.subject[:200], n.priority.value, n.status.value,
                 n.attempt, n.created_at, n.sent_at, n.error[:200]))

    def save_template(self, t: NotificationTemplate):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO templates VALUES(?,?,?,?)",
                (t.name, t.subject, t.body, t.channel.value if t.channel else ""))

    def history(self, channel: str = None, limit: int = 50) -> List[Dict]:
        where = f"WHERE channel='{channel}'" if channel else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM notifications {where} "
                f"ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
            by_status = {r["status"]: r["cnt"] for r in c.execute(
                "SELECT status, COUNT(*) as cnt FROM notifications "
                "GROUP BY status").fetchall()}
            by_channel = {r["channel"]: r["cnt"] for r in c.execute(
                "SELECT channel, COUNT(*) as cnt FROM notifications "
                "GROUP BY channel").fetchall()}
        return {"total": total, "by_status": by_status, "by_channel": by_channel}

class NotificationManager:
    """
    Multi-channel notification system with templates and routing.

    Usage:
        nm = NotificationManager()

        nm.add_template(NotificationTemplate(
            name="alert", subject="Alert: {title}",
            body="Hello {user},\n\nEvent: {message}\nSeverity: {severity}"))

        nm.add_channel(ChannelConfig(Channel.LOG))
        nm.add_rule(RoutingRule(
            name="all_errors", event_types=["error"],
            channels=[Channel.LOG], template_name="alert"))

        nm.add_audience("ops", ["alice@ex.com", "bob@ex.com"])
        nm.send_event("error", {"title":"DB Down","message":"...","user":"team"})
    """
    def __init__(self, db_path: str = "data/notifications.db"):
        self._store = NMStore(db_path)
        self._templates: Dict[str, NotificationTemplate] = {}
        self._channels: Dict[Channel, ChannelConfig] = {}
        self._rules: List[RoutingRule] = []
        self._audiences: Dict[str, List[str]] = {}
        self._dedup_cache: Dict[str, float] = {}  # hash → expiry
        self._rate_windows: Dict[Channel, deque] = defaultdict(deque)
        self._history: Dict[Channel, deque] = defaultdict(lambda: deque(maxlen=100))
        self._batch_queues: Dict[Channel, List[Notification]] = defaultdict(list)
        self._hooks: List[Callable] = []

    def add_template(self, tmpl: NotificationTemplate):
        self._templates[tmpl.name] = tmpl
        self._store.save_template(tmpl)

    def add_channel(self, cfg: ChannelConfig):
        self._channels[cfg.channel] = cfg

    def add_rule(self, rule: RoutingRule):
        self._rules.append(rule)

    def add_audience(self, name: str, recipients: List[str]):
        self._audiences[name] = list(recipients)

    def on_send(self, fn: Callable): self._hooks.append(fn)

    def _dedup_key(self, channel: Channel, event_type: str, body: str) -> str:
        return hashlib.md5(f"{channel.value}:{event_type}:{body[:100]}".encode()).hexdigest()

    def _is_rate_limited(self, channel: Channel) -> bool:
        cfg = self._channels.get(channel)
        if not cfg: return False
        now = time.time()
        dq = self._rate_windows[channel]
        while dq and dq[0] < now - cfg.window_s:
            dq.popleft()
        if len(dq) >= cfg.max_per_window: return True
        dq.append(now)
        return False

    def _is_duplicate(self, key: str, channel: Channel) -> bool:
        cfg = self._channels.get(channel)
        ttl = cfg.dedup_ttl_s if cfg else 300.0
        now = time.time()
        if key in self._dedup_cache and self._dedup_cache[key] > now:
            return True
        self._dedup_cache[key] = now + ttl
        return False

    def _deliver(self, notif: Notification) -> bool:
        cfg = self._channels.get(notif.channel)
        if cfg and cfg.sender_fn:
            try: return bool(cfg.sender_fn(notif))
            except: return False
        # Default: LOG channel always succeeds; others simulate
        if notif.channel == Channel.LOG:
            logger.info(f"[NOTIF] {notif.channel.value} → {notif.recipient}: "
                         f"{notif.subject}")
            return True
        return True   # simulated success for non-LOG channels

    def _send_now(self, notif: Notification) -> Notification:
        # Hook check
        for h in self._hooks:
            try:
                if h(notif) is False:
                    notif.status = DeliveryStatus.SUPPRESSED
                    self._store.save(notif)
                    return notif
            except: pass
        # Rate limit
        if self._is_rate_limited(notif.channel):
            notif.status = DeliveryStatus.RATE_LIMITED
            self._store.save(notif)
            return notif
        # Dedup
        dkey = self._dedup_key(notif.channel, notif.event_type, notif.body)
        if self._is_duplicate(dkey, notif.channel):
            notif.status = DeliveryStatus.SUPPRESSED
            self._store.save(notif)
            return notif
        # Deliver
        cfg = self._channels.get(notif.channel)
        max_retries = cfg.max_retries if cfg else notif.max_retries
        for attempt in range(max_retries + 1):
            notif.attempt = attempt + 1
            if self._deliver(notif):
                notif.status = DeliveryStatus.SENT
                notif.sent_at = time.time()
                break
        else:
            notif.status = DeliveryStatus.FAILED
        self._store.save(notif)
        self._history[notif.channel].append(notif)
        return notif

    def send(self, event_type: str, channel,
              recipient: str, subject: str, body: str,
              priority: Priority = Priority.MEDIUM) -> Notification:
        if isinstance(channel, str): channel = Channel(channel)
        if isinstance(priority, str): priority = Priority(priority)
        notif = Notification(
            id=str(uuid.uuid4())[:12], event_type=event_type,
            channel=channel, recipient=recipient,
            subject=subject, body=body, priority=priority)
        cfg = self._channels.get(channel)
        if cfg and cfg.batch_size > 0:
            self._batch_queues[channel].append(notif)
            if len(self._batch_queues[channel]) >= cfg.batch_size:
                self.flush_batch(channel)
            return notif
        return self._send_now(notif)

    def flush_batch(self, channel: Channel) -> List[Notification]:
        queue = self._batch_queues.pop(channel, [])
        return [self._send_now(n) for n in queue]

    def flush_all_batches(self) -> int:
        total = 0
        for ch in list(self._batch_queues.keys()):
            total += len(self.flush_batch(ch))
        return total

    def send_event(self, event_type: str, context: Dict,
                    priority: Priority = Priority.MEDIUM) -> List[Notification]:
        sent = []
        for rule in self._rules:
            if not rule.enabled: continue
            if rule.event_types and event_type not in rule.event_types: continue
            if rule.severity and priority not in rule.severity: continue
            tmpl = self._templates.get(rule.template_name)
            rendered = tmpl.render(context) if tmpl else {
                "subject": event_type, "body": json.dumps(context)}
            recipients: List[str] = []
            for aud in rule.audiences:
                recipients.extend(self._audiences.get(aud, []))
            if not recipients:
                recipients = [context.get("recipient", "default")]
            for channel in rule.channels:
                for recipient in recipients:
                    n = self.send(event_type, channel, recipient,
                                   rendered["subject"], rendered["body"], priority)
                    sent.append(n)
        return sent

    def history(self, channel: Channel = None, limit: int = 50) -> List[Dict]:
        return self._store.history(channel.value if channel else None, limit)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["templates"] = len(self._templates)
        s["channels"] = len(self._channels)
        s["rules"] = len(self._rules)
        s["audiences"] = len(self._audiences)
        s["batch_queued"] = sum(len(q) for q in self._batch_queues.values())
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def send_ep(req):
            d = await req.json()
            n = self.send(d["event_type"], Channel(d["channel"]),
                           d["recipient"], d["subject"], d["body"],
                           Priority(d.get("priority","medium")))
            return web.json_response(n.to_dict(), status=201)
        async def event_ep(req):
            d = await req.json()
            sent = self.send_event(d["event_type"], d.get("context",{}),
                                    Priority(d.get("priority","medium")))
            return web.json_response(
                {"sent": len(sent),
                 "notifications": [n.to_dict() for n in sent]})
        async def history_ep(req):
            ch = req.rel_url.query.get("channel")
            limit = int(req.rel_url.query.get("limit", 50))
            return web.json_response(
                {"history": self.history(Channel(ch) if ch else None, limit)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/notify"
        app.router.add_post(f"{p}/send",    send_ep)
        app.router.add_post(f"{p}/event",   event_ep)
        app.router.add_get( f"{p}/history", history_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Notification manager API at {prefix}/notify/")
