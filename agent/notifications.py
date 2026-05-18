"""
OMNI AGENT - Notification System
Multi-channel notification dispatcher with retry logic, Jinja-style
templating, per-channel throttling, and a delivery log.

Supported channels:
  - webhook  — HTTP POST to any URL (Slack, Discord, custom)
  - email    — SMTP with HTML/plain body
  - console  — Log to stdout/logger (always available, useful for dev)
  - memory   — Write to agent memory (in-process channel)

Features:
  - Message templates with {{variable}} substitution
  - Per-channel rate limiting (max N per minute)
  - Retry with exponential backoff (up to max_retries)
  - Delivery log stored in SQLite
  - Priority levels: LOW, NORMAL, HIGH, CRITICAL
  - Notification grouping / digest mode
  - Async dispatch — non-blocking fire-and-forget
"""
import re
import json
import time
import uuid
import asyncio
import logging
import sqlite3
import smtplib
import email.mime.text
import email.mime.multipart
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from collections import deque

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# TYPES
# ══════════════════════════════════════════════════════════════════════════════

class Priority(str, Enum):
    LOW      = "low"
    NORMAL   = "normal"
    HIGH     = "high"
    CRITICAL = "critical"


class DeliveryStatus(str, Enum):
    PENDING   = "pending"
    SENT      = "sent"
    FAILED    = "failed"
    THROTTLED = "throttled"
    RETRYING  = "retrying"


@dataclass
class NotificationChannel:
    name: str
    channel_type: str           # webhook | email | console | memory
    config: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    max_per_minute: int = 60    # rate limit
    tags: List[str] = field(default_factory=list)


@dataclass
class Notification:
    id: str
    title: str
    body: str
    channel: str
    priority: Priority = Priority.NORMAL
    template_vars: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def render(self, title_tpl: str = None,
               body_tpl: str = None) -> "Notification":
        """Apply template variable substitution."""
        def sub(text: str) -> str:
            for k, v in self.template_vars.items():
                text = text.replace(f"{{{{{k}}}}}", str(v))
            return text
        rendered = Notification(
            id=self.id,
            title=sub(title_tpl or self.title),
            body=sub(body_tpl or self.body),
            channel=self.channel,
            priority=self.priority,
            template_vars=self.template_vars,
            metadata=self.metadata,
            created_at=self.created_at,
        )
        return rendered


@dataclass
class DeliveryRecord:
    notification_id: str
    channel: str
    status: DeliveryStatus
    attempt: int = 1
    latency_ms: float = 0.0
    error: str = ""
    sent_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "id": self.notification_id,
            "channel": self.channel,
            "status": self.status.value,
            "attempt": self.attempt,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
            "sent_at": self.sent_at,
        }


# ══════════════════════════════════════════════════════════════════════════════
# CHANNEL SENDERS
# ══════════════════════════════════════════════════════════════════════════════

async def _send_webhook(channel: NotificationChannel,
                         notif: Notification) -> str:
    """POST JSON payload to a webhook URL."""
    try:
        import aiohttp
    except ImportError:
        raise RuntimeError("aiohttp required for webhook channel")

    url = channel.config.get("url", "")
    if not url:
        raise ValueError("webhook channel missing 'url' config")

    payload = {
        "title": notif.title,
        "body": notif.body,
        "priority": notif.priority.value,
        "metadata": notif.metadata,
        "id": notif.id,
    }

    # Slack-style message format
    if channel.config.get("format") == "slack":
        payload = {
            "text": f"*{notif.title}*\n{notif.body}",
            "attachments": [{
                "color": {
                    Priority.CRITICAL: "danger",
                    Priority.HIGH: "warning",
                    Priority.NORMAL: "good",
                    Priority.LOW: "#aaa",
                }.get(notif.priority, "#aaa"),
                "footer": f"Priority: {notif.priority.value}",
            }]
        }

    headers = channel.config.get("headers", {})
    timeout_s = channel.config.get("timeout", 10)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Webhook returned {resp.status}")
            return f"HTTP {resp.status}"


async def _send_email(channel: NotificationChannel,
                       notif: Notification) -> str:
    """Send notification via SMTP."""
    cfg = channel.config
    smtp_host = cfg.get("smtp_host", "localhost")
    smtp_port = int(cfg.get("smtp_port", 587))
    username   = cfg.get("username", "")
    password   = cfg.get("password", "")
    from_addr  = cfg.get("from", username)
    to_addrs   = cfg.get("to", [])

    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]

    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = notif.title
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    msg["X-Priority"] = {
        Priority.CRITICAL: "1",
        Priority.HIGH: "2",
        Priority.NORMAL: "3",
        Priority.LOW: "5",
    }.get(notif.priority, "3")

    # Plain text version
    msg.attach(email.mime.text.MIMEText(notif.body, "plain"))
    # HTML version if provided
    html_body = cfg.get("html_template", "").replace("{{body}}", notif.body)
    if html_body:
        msg.attach(email.mime.text.MIMEText(html_body, "html"))

    def _smtp_send():
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            if username:
                smtp.starttls()
                smtp.login(username, password)
            smtp.sendmail(from_addr, to_addrs, msg.as_string())

    # Run blocking SMTP in thread
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _smtp_send)
    return f"sent to {', '.join(to_addrs)}"


async def _send_console(channel: NotificationChannel,
                          notif: Notification) -> str:
    """Log the notification to console/logger."""
    level_map = {
        Priority.CRITICAL: logging.CRITICAL,
        Priority.HIGH:     logging.WARNING,
        Priority.NORMAL:   logging.INFO,
        Priority.LOW:      logging.DEBUG,
    }
    level = level_map.get(notif.priority, logging.INFO)
    logger.log(level, f"[NOTIFY/{channel.name}] {notif.title}: {notif.body}")
    return "logged"


async def _send_memory(channel: NotificationChannel,
                        notif: Notification, memory=None) -> str:
    """Save notification as a memory entry."""
    if memory:
        memory.save_memory(
            f"notification:{notif.id}",
            json.dumps({"title": notif.title, "body": notif.body,
                       "priority": notif.priority.value}),
            category="notification",
        )
    return "saved to memory"


SENDER_MAP = {
    "webhook": _send_webhook,
    "email":   _send_email,
    "console": _send_console,
    "memory":  _send_memory,
}


# ══════════════════════════════════════════════════════════════════════════════
# DELIVERY LOG
# ══════════════════════════════════════════════════════════════════════════════

class DeliveryLog:
    """SQLite-backed delivery history."""

    def __init__(self, db_path: str = "data/notifications.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    notification_id TEXT,
                    channel         TEXT,
                    status          TEXT,
                    attempt         INTEGER DEFAULT 1,
                    latency_ms      REAL DEFAULT 0,
                    error           TEXT DEFAULT '',
                    sent_at         REAL
                );
                CREATE INDEX IF NOT EXISTS idx_del_time ON deliveries(sent_at);
                CREATE INDEX IF NOT EXISTS idx_del_status ON deliveries(status);
            """)

    def record(self, rec: DeliveryRecord):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)",
                (rec.notification_id, rec.channel, rec.status.value,
                 rec.attempt, rec.latency_ms, rec.error, rec.sent_at)
            )

    def get_recent(self, limit: int = 50,
                   channel: str = None,
                   status: DeliveryStatus = None) -> List[Dict]:
        with self._conn() as conn:
            q = "SELECT * FROM deliveries"
            params = []
            filters = []
            if channel:
                filters.append("channel=?"); params.append(channel)
            if status:
                filters.append("status=?"); params.append(status.value)
            if filters:
                q += " WHERE " + " AND ".join(filters)
            q += " ORDER BY sent_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
            by_status = dict(conn.execute(
                "SELECT status, COUNT(*) FROM deliveries GROUP BY status"
            ).fetchall())
            by_channel = dict(conn.execute(
                "SELECT channel, COUNT(*) FROM deliveries GROUP BY channel"
            ).fetchall())
        return {"total": total, "by_status": by_status, "by_channel": by_channel}


# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICATION TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class NotificationTemplate:
    name: str
    title_template: str
    body_template: str
    default_channel: str = ""
    default_priority: Priority = Priority.NORMAL


BUILTIN_TEMPLATES: Dict[str, NotificationTemplate] = {
    "alert": NotificationTemplate(
        name="alert",
        title_template="🚨 Alert: {{event}}",
        body_template="An alert was triggered.\n\nEvent: {{event}}\nDetails: {{details}}\nTime: {{time}}",
        default_priority=Priority.HIGH,
    ),
    "info": NotificationTemplate(
        name="info",
        title_template="ℹ️ {{title}}",
        body_template="{{body}}",
        default_priority=Priority.NORMAL,
    ),
    "task_done": NotificationTemplate(
        name="task_done",
        title_template="✅ Task Complete: {{task_name}}",
        body_template="Task '{{task_name}}' finished successfully.\nDuration: {{duration}}\nResult: {{result}}",
        default_priority=Priority.NORMAL,
    ),
    "task_failed": NotificationTemplate(
        name="task_failed",
        title_template="❌ Task Failed: {{task_name}}",
        body_template="Task '{{task_name}}' failed after {{attempts}} attempt(s).\nError: {{error}}",
        default_priority=Priority.HIGH,
    ),
    "model_error": NotificationTemplate(
        name="model_error",
        title_template="⚠️ Model Error: {{model}}",
        body_template="Model '{{model}}' returned an error.\nError: {{error}}\nSession: {{session_id}}",
        default_priority=Priority.HIGH,
    ),
    "security": NotificationTemplate(
        name="security",
        title_template="🔒 Security Event: {{event_type}}",
        body_template="Security event detected.\nType: {{event_type}}\nUser: {{user_id}}\nDetails: {{details}}",
        default_priority=Priority.CRITICAL,
    ),
    "daily_summary": NotificationTemplate(
        name="daily_summary",
        title_template="📊 Daily Summary — {{date}}",
        body_template=(
            "Agent summary for {{date}}:\n\n"
            "• Messages processed: {{messages}}\n"
            "• Models used: {{models}}\n"
            "• Errors: {{errors}}\n"
            "• Uptime: {{uptime}}"
        ),
        default_priority=Priority.LOW,
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# NOTIFIER (main class)
# ══════════════════════════════════════════════════════════════════════════════

class Notifier:
    """
    Multi-channel notification dispatcher.

    Usage:
        notifier = Notifier()
        notifier.add_channel(NotificationChannel(
            name="slack",
            channel_type="webhook",
            config={"url": "https://hooks.slack.com/...", "format": "slack"},
        ))

        # Simple send
        await notifier.send("slack", title="Deploy done", body="v1.2 deployed")

        # Template send
        await notifier.send_template(
            "slack", "task_done",
            vars={"task_name": "data_sync", "duration": "3m", "result": "OK"},
        )

        # Fire and forget
        notifier.fire("slack", title="Event", body="Something happened")
    """

    def __init__(self, db_path: str = "data/notifications.db",
                 max_retries: int = 3, retry_delay_s: float = 2.0,
                 memory=None):
        self._channels: Dict[str, NotificationChannel] = {}
        self._templates: Dict[str, NotificationTemplate] = dict(BUILTIN_TEMPLATES)
        self.log = DeliveryLog(db_path)
        self.max_retries = max_retries
        self.retry_delay_s = retry_delay_s
        self.memory = memory
        # Per-channel rate limiting: {channel_name: deque of timestamps}
        self._rate_windows: Dict[str, deque] = {}

        # Register console channel by default
        self.add_channel(NotificationChannel(
            name="console", channel_type="console",
            config={}, max_per_minute=1000
        ))

    # ── Channels ──────────────────────────────────────────────────────────────

    def add_channel(self, channel: NotificationChannel):
        self._channels[channel.name] = channel
        self._rate_windows[channel.name] = deque()
        logger.info(f"Notification channel added: {channel.name} ({channel.channel_type})")

    def remove_channel(self, name: str):
        self._channels.pop(name, None)
        self._rate_windows.pop(name, None)

    def list_channels(self) -> List[Dict]:
        return [
            {"name": c.name, "type": c.channel_type,
             "enabled": c.enabled, "max_per_minute": c.max_per_minute}
            for c in self._channels.values()
        ]

    # ── Templates ─────────────────────────────────────────────────────────────

    def add_template(self, template: NotificationTemplate):
        self._templates[template.name] = template

    def list_templates(self) -> List[str]:
        return list(self._templates.keys())

    # ── Send ─────────────────────────────────────────────────────────────────

    async def send(self, channel_name: str, title: str, body: str,
                   priority: Priority = Priority.NORMAL,
                   metadata: Dict = None) -> DeliveryRecord:
        """Send a notification to a named channel."""
        channel = self._channels.get(channel_name)
        if not channel:
            rec = DeliveryRecord(
                notification_id=str(uuid.uuid4())[:8],
                channel=channel_name,
                status=DeliveryStatus.FAILED,
                error=f"Unknown channel '{channel_name}'",
            )
            self.log.record(rec)
            return rec

        if not channel.enabled:
            rec = DeliveryRecord(
                notification_id=str(uuid.uuid4())[:8],
                channel=channel_name,
                status=DeliveryStatus.FAILED,
                error="Channel is disabled",
            )
            self.log.record(rec)
            return rec

        notif = Notification(
            id=str(uuid.uuid4())[:8],
            title=title, body=body,
            channel=channel_name, priority=priority,
            metadata=metadata or {},
        )
        return await self._dispatch(channel, notif)

    async def send_template(self, channel_name: str, template_name: str,
                             vars: Dict = None,
                             priority: Priority = None) -> DeliveryRecord:
        """Send using a named template."""
        tpl = self._templates.get(template_name)
        if not tpl:
            return await self.send(
                channel_name,
                title=f"[{template_name}]",
                body=f"Template '{template_name}' not found",
                priority=Priority.HIGH,
            )

        notif = Notification(
            id=str(uuid.uuid4())[:8],
            title=tpl.title_template,
            body=tpl.body_template,
            channel=channel_name,
            priority=priority or tpl.default_priority,
            template_vars=vars or {},
        )
        rendered = notif.render()
        return await self.send(
            channel_name, rendered.title, rendered.body,
            priority=rendered.priority
        )

    def fire(self, channel_name: str, title: str, body: str,
             priority: Priority = Priority.NORMAL) -> asyncio.Task:
        """Non-blocking fire-and-forget."""
        return asyncio.ensure_future(
            self.send(channel_name, title, body, priority)
        )

    async def broadcast(self, title: str, body: str,
                        priority: Priority = Priority.NORMAL,
                        tags: List[str] = None) -> List[DeliveryRecord]:
        """Send to all enabled channels (optionally filtered by tag)."""
        channels = [c for c in self._channels.values() if c.enabled]
        if tags:
            channels = [c for c in channels
                       if any(t in c.tags for t in tags)]
        tasks = [self.send(c.name, title, body, priority) for c in channels]
        return list(await asyncio.gather(*tasks))

    # ── Internal dispatch ─────────────────────────────────────────────────────

    async def _dispatch(self, channel: NotificationChannel,
                         notif: Notification) -> DeliveryRecord:
        """Dispatch with rate-limiting and retry."""
        # Rate limit check
        now = time.time()
        window = self._rate_windows[channel.name]
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= channel.max_per_minute:
            rec = DeliveryRecord(
                notification_id=notif.id, channel=channel.name,
                status=DeliveryStatus.THROTTLED,
                error=f"Rate limit: {channel.max_per_minute}/min",
            )
            self.log.record(rec)
            logger.warning(f"Notification throttled on '{channel.name}'")
            return rec

        window.append(now)
        sender = SENDER_MAP.get(channel.channel_type)
        if not sender:
            rec = DeliveryRecord(
                notification_id=notif.id, channel=channel.name,
                status=DeliveryStatus.FAILED,
                error=f"No sender for type '{channel.channel_type}'",
            )
            self.log.record(rec)
            return rec

        # Retry loop
        for attempt in range(1, self.max_retries + 1):
            start = time.time()
            try:
                if channel.channel_type == "memory":
                    result = await sender(channel, notif, self.memory)
                else:
                    result = await sender(channel, notif)
                latency = (time.time() - start) * 1000
                rec = DeliveryRecord(
                    notification_id=notif.id, channel=channel.name,
                    status=DeliveryStatus.SENT, attempt=attempt,
                    latency_ms=latency,
                )
                self.log.record(rec)
                logger.debug(f"Notification sent via '{channel.name}' [{result}] "
                            f"in {latency:.0f}ms")
                return rec
            except Exception as e:
                latency = (time.time() - start) * 1000
                error = str(e)
                logger.warning(f"Notification attempt {attempt}/{self.max_retries} "
                              f"failed on '{channel.name}': {error}")
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay_s * (2 ** (attempt - 1)))

        rec = DeliveryRecord(
            notification_id=notif.id, channel=channel.name,
            status=DeliveryStatus.FAILED, attempt=self.max_retries,
            latency_ms=(time.time() - now) * 1000, error=error,
        )
        self.log.record(rec)
        return rec

    # ── Stats ─────────────────────────────────────────────────────────────────

    def delivery_stats(self) -> Dict:
        return self.log.stats()

    def recent_deliveries(self, limit: int = 20,
                          channel: str = None,
                          status: DeliveryStatus = None) -> List[Dict]:
        return self.log.get_recent(limit=limit, channel=channel, status=status)
