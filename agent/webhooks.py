"""
OMNI AGENT - Webhook Dispatcher
Send outbound webhooks with HMAC-SHA256 signing, exponential backoff retry,
event filtering, delivery logging, and a management API.

Features:
- Register webhook endpoints with URL, secret, and event filter
- HMAC-SHA256 signing: X-Webhook-Signature header on every delivery
- Async HTTP delivery with configurable timeout
- Exponential backoff retry: up to 5 attempts on failure
- Per-webhook delivery log: status, latency, response body preview
- Event routing: dispatch to all matching webhooks for an event type
- Enable/disable individual webhooks without deleting
- Batch mode: collect events and flush periodically
- Management REST API: CRUD for webhooks + delivery history
"""
import time
import uuid
import json
import hmac
import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import Enum

try:
    import aiohttp as _aiohttp
    _AIOHTTP_OK = True
except ImportError:
    _AIOHTTP_OK = False

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# EVENT TYPES
# ══════════════════════════════════════════════════════════════════════════════

class WebhookEvent(str, Enum):
    """Built-in event types the agent can emit."""
    CHAT_COMPLETE   = "chat.complete"
    CHAT_ERROR      = "chat.error"
    TOOL_CALLED     = "tool.called"
    TOOL_ERROR      = "tool.error"
    PIPELINE_DONE   = "pipeline.done"
    WORKFLOW_DONE   = "workflow.done"
    MEMORY_SAVED    = "memory.saved"
    MODEL_ROUTED    = "model.routed"
    EVAL_DONE       = "eval.done"
    KG_UPDATED      = "kg.updated"
    AGENT_START     = "agent.start"
    AGENT_STOP      = "agent.stop"
    CUSTOM          = "custom"
    WILDCARD        = "*"           # receives all events


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK DEFINITION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Webhook:
    """A registered webhook endpoint."""
    id: str
    url: str
    secret: str                                  # Used for HMAC signing
    events: Set[str] = field(default_factory=lambda: {"*"})  # event filter
    enabled: bool = True
    description: str = ""
    headers: Dict[str, str] = field(default_factory=dict)   # extra headers
    timeout_s: float = 10.0
    max_retries: int = 4
    created_at: float = field(default_factory=time.time)
    last_delivery: Optional[float] = None

    def matches(self, event_type: str) -> bool:
        return ("*" in self.events or
                event_type in self.events or
                WebhookEvent.WILDCARD in self.events)

    def to_dict(self, mask_secret: bool = True) -> Dict:
        return {
            "id": self.id,
            "url": self.url,
            "secret": "***" if mask_secret else self.secret,
            "events": list(self.events),
            "enabled": self.enabled,
            "description": self.description,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "created_at": self.created_at,
            "last_delivery": self.last_delivery,
        }


# ══════════════════════════════════════════════════════════════════════════════
# PAYLOAD & DELIVERY LOG
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class WebhookPayload:
    """The JSON body sent to a webhook endpoint."""
    id: str
    event: str
    timestamp: float
    data: Dict[str, Any]
    source: str = "omni_agent"
    version: str = "1.0"

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id,
            "event": self.event,
            "timestamp": self.timestamp,
            "source": self.source,
            "version": self.version,
            "data": self.data,
        }, default=str)

    def sign(self, secret: str) -> str:
        """Compute HMAC-SHA256 signature: 'sha256=<hex>'."""
        body = self.to_json().encode("utf-8")
        sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return f"sha256={sig}"


@dataclass
class DeliveryRecord:
    """Result of a single webhook delivery attempt."""
    delivery_id: str
    webhook_id: str
    payload_id: str
    event: str
    url: str
    attempt: int          # 1-based attempt number
    status_code: int      # HTTP status or 0 for network error
    success: bool
    latency_ms: float
    response_preview: str = ""
    error: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "id": self.delivery_id,
            "webhook_id": self.webhook_id,
            "event": self.event,
            "attempt": self.attempt,
            "status_code": self.status_code,
            "success": self.success,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
            "response_preview": self.response_preview[:200],
            "timestamp": self.timestamp,
        }


# ══════════════════════════════════════════════════════════════════════════════
# HTTP SENDER
# ══════════════════════════════════════════════════════════════════════════════

class _WebhookSender:
    """Low-level async HTTP POST with retry and signing."""

    def __init__(self):
        self._session = None

    async def _get_session(self):
        if not _AIOHTTP_OK:
            return None
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, webhook: Webhook,
                   payload: WebhookPayload) -> List[DeliveryRecord]:
        """
        Deliver payload to webhook with exponential backoff retry.
        Returns list of DeliveryRecord (one per attempt).
        """
        records = []
        body = payload.to_json()
        sig = payload.sign(webhook.secret)

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
            "X-Webhook-ID": webhook.id,
            "X-Webhook-Event": payload.event,
            "User-Agent": "OmniAgent-Webhook/1.0",
            **webhook.headers,
        }

        for attempt in range(1, webhook.max_retries + 1):
            start = time.time()
            delivery_id = str(uuid.uuid4())[:8]
            try:
                session = await self._get_session()
                if session is None:
                    # aiohttp not available — simulate success in testing
                    raise ImportError("aiohttp not installed")

                async with session.post(
                    webhook.url,
                    data=body,
                    headers=headers,
                    timeout=_aiohttp.ClientTimeout(total=webhook.timeout_s),
                ) as resp:
                    latency_ms = (time.time() - start) * 1000
                    resp_text = await resp.text()
                    success = 200 <= resp.status < 300
                    record = DeliveryRecord(
                        delivery_id=delivery_id,
                        webhook_id=webhook.id,
                        payload_id=payload.id,
                        event=payload.event,
                        url=webhook.url,
                        attempt=attempt,
                        status_code=resp.status,
                        success=success,
                        latency_ms=latency_ms,
                        response_preview=resp_text[:300],
                    )
                    records.append(record)

                    if success:
                        logger.info(f"Webhook delivered: id={webhook.id} "
                                   f"event={payload.event} status={resp.status} "
                                   f"attempt={attempt}")
                        return records

            except Exception as e:
                latency_ms = (time.time() - start) * 1000
                record = DeliveryRecord(
                    delivery_id=delivery_id,
                    webhook_id=webhook.id,
                    payload_id=payload.id,
                    event=payload.event,
                    url=webhook.url,
                    attempt=attempt,
                    status_code=0,
                    success=False,
                    latency_ms=latency_ms,
                    error=str(e)[:200],
                )
                records.append(record)
                logger.warning(f"Webhook attempt {attempt}/{webhook.max_retries} failed: "
                              f"id={webhook.id} error={e}")

            # Exponential backoff: 1s, 2s, 4s, 8s …
            if attempt < webhook.max_retries:
                delay = 2 ** (attempt - 1)
                await asyncio.sleep(delay)

        logger.error(f"Webhook all attempts failed: id={webhook.id} event={payload.event}")
        return records

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK DISPATCHER
# ══════════════════════════════════════════════════════════════════════════════

class WebhookDispatcher:
    """
    Manages webhook registrations and dispatches events.

    Usage:
        dispatcher = WebhookDispatcher()

        # Register a webhook
        hook_id = dispatcher.register(
            url="https://myapp.com/webhook",
            secret="my_signing_secret",
            events=["chat.complete", "eval.done"],
            description="Prod alerts",
        )

        # Fire an event (dispatches to all matching webhooks)
        await dispatcher.dispatch("chat.complete", {
            "session_id": "sess_123",
            "model": "gpt-4",
            "tokens": 312,
        })

        # Get delivery history
        history = dispatcher.delivery_history(webhook_id=hook_id)
    """

    def __init__(self, max_log: int = 5000):
        self._webhooks: Dict[str, Webhook] = {}
        self._sender = _WebhookSender()
        self._delivery_log: List[DeliveryRecord] = []
        self._max_log = max_log
        self._dispatch_queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, url: str, secret: str,
                 events: List[str] = None,
                 description: str = "",
                 headers: Dict[str, str] = None,
                 timeout_s: float = 10.0,
                 max_retries: int = 4) -> str:
        """Register a new webhook. Returns the webhook ID."""
        hook_id = str(uuid.uuid4())[:12]
        hook = Webhook(
            id=hook_id,
            url=url,
            secret=secret,
            events=set(events) if events else {"*"},
            description=description,
            headers=headers or {},
            timeout_s=timeout_s,
            max_retries=max_retries,
        )
        self._webhooks[hook_id] = hook
        logger.info(f"Webhook registered: id={hook_id} url={url} events={hook.events}")
        return hook_id

    def update(self, hook_id: str, **kwargs) -> bool:
        hook = self._webhooks.get(hook_id)
        if not hook:
            return False
        for k, v in kwargs.items():
            if k == "events":
                hook.events = set(v)
            elif hasattr(hook, k):
                setattr(hook, k, v)
        return True

    def delete(self, hook_id: str) -> bool:
        return bool(self._webhooks.pop(hook_id, None))

    def enable(self, hook_id: str) -> bool:
        return self.update(hook_id, enabled=True)

    def disable(self, hook_id: str) -> bool:
        return self.update(hook_id, enabled=False)

    def get(self, hook_id: str) -> Optional[Webhook]:
        return self._webhooks.get(hook_id)

    def list_webhooks(self) -> List[Dict]:
        return [h.to_dict() for h in self._webhooks.values()]

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def dispatch(self, event_type: str,
                       data: Dict[str, Any],
                       source: str = "omni_agent") -> List[DeliveryRecord]:
        """
        Dispatch an event to all matching enabled webhooks.
        Runs deliveries concurrently. Returns all delivery records.
        """
        payload = WebhookPayload(
            id=str(uuid.uuid4())[:12],
            event=event_type,
            timestamp=time.time(),
            data=data,
            source=source,
        )

        matching = [
            h for h in self._webhooks.values()
            if h.enabled and h.matches(event_type)
        ]

        if not matching:
            return []

        # Deliver to all matching webhooks concurrently
        tasks = [self._sender.send(hook, payload) for hook in matching]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        all_records = []
        for records in results_list:
            if isinstance(records, Exception):
                logger.error(f"Webhook dispatch error: {records}")
                continue
            all_records.extend(records)
            # Update last_delivery on success
            for rec in records:
                if rec.success:
                    hook = self._webhooks.get(rec.webhook_id)
                    if hook:
                        hook.last_delivery = rec.timestamp

        # Append to log (trim if needed)
        self._delivery_log.extend(all_records)
        if len(self._delivery_log) > self._max_log:
            self._delivery_log = self._delivery_log[-self._max_log:]

        return all_records

    async def dispatch_background(self, event_type: str,
                                   data: Dict[str, Any]) -> None:
        """
        Queue an event for background delivery (non-blocking).
        Start the worker first with start_worker().
        """
        await self._dispatch_queue.put((event_type, data))

    # ── Background Worker ─────────────────────────────────────────────────────

    async def start_worker(self):
        """Start background dispatch worker."""
        async def _worker():
            while True:
                try:
                    event_type, data = await self._dispatch_queue.get()
                    await self.dispatch(event_type, data)
                    self._dispatch_queue.task_done()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Webhook worker error: {e}")

        self._worker_task = asyncio.create_task(_worker())
        logger.info("Webhook background worker started")

    async def stop_worker(self):
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        await self._sender.close()

    # ── Delivery Log ──────────────────────────────────────────────────────────

    def delivery_history(self, webhook_id: str = None,
                         event_type: str = None,
                         limit: int = 50) -> List[Dict]:
        records = list(self._delivery_log)
        if webhook_id:
            records = [r for r in records if r.webhook_id == webhook_id]
        if event_type:
            records = [r for r in records if r.event == event_type]
        return [r.to_dict() for r in reversed(records[-limit:])]

    def stats(self) -> Dict:
        total = len(self._delivery_log)
        success = sum(1 for r in self._delivery_log if r.success)
        by_event: Dict[str, int] = {}
        for r in self._delivery_log:
            by_event[r.event] = by_event.get(r.event, 0) + 1
        avg_latency = (
            sum(r.latency_ms for r in self._delivery_log) / total
            if total else 0.0
        )
        return {
            "total_deliveries": total,
            "successful": success,
            "failed": total - success,
            "success_rate": round(success / total, 3) if total else 0.0,
            "avg_latency_ms": round(avg_latency, 1),
            "webhooks_registered": len(self._webhooks),
            "webhooks_enabled": sum(1 for h in self._webhooks.values() if h.enabled),
            "by_event": by_event,
        }

    # ── Signature Verification ────────────────────────────────────────────────

    @staticmethod
    def verify_signature(body: bytes, secret: str, signature: str) -> bool:
        """
        Verify an inbound webhook signature.
        Use this when receiving webhooks from other services.
        Expected format: 'sha256=<hex>'
        """
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    # ── REST API ──────────────────────────────────────────────────────────────

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def list_hooks(request):
            return web.json_response({"webhooks": self.list_webhooks()})

        async def create_hook(request):
            data = await request.json()
            hook_id = self.register(
                url=data["url"],
                secret=data.get("secret", str(uuid.uuid4())),
                events=data.get("events", ["*"]),
                description=data.get("description", ""),
                headers=data.get("headers", {}),
                timeout_s=float(data.get("timeout_s", 10.0)),
                max_retries=int(data.get("max_retries", 4)),
            )
            return web.json_response({"id": hook_id,
                                     **self._webhooks[hook_id].to_dict()},
                                    status=201)

        async def update_hook(request):
            hook_id = request.match_info["id"]
            data = await request.json()
            ok = self.update(hook_id, **data)
            if not ok:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response(self._webhooks[hook_id].to_dict())

        async def delete_hook(request):
            hook_id = request.match_info["id"]
            ok = self.delete(hook_id)
            return web.json_response({"deleted": ok})

        async def toggle_hook(request):
            hook_id = request.match_info["id"]
            data = await request.json() if request.content_length else {}
            enabled = data.get("enabled")
            if enabled is None:
                hook = self.get(hook_id)
                if hook:
                    enabled = not hook.enabled
            if enabled:
                self.enable(hook_id)
            else:
                self.disable(hook_id)
            hook = self.get(hook_id)
            return web.json_response(hook.to_dict() if hook else {"error": "not found"})

        async def test_hook(request):
            hook_id = request.match_info["id"]
            hook = self.get(hook_id)
            if not hook:
                return web.json_response({"error": "not found"}, status=404)
            records = await self.dispatch("webhook.test", {"test": True, "webhook_id": hook_id})
            return web.json_response({"records": [r.to_dict() for r in records]})

        async def history(request):
            hook_id = request.rel_url.query.get("webhook_id")
            event   = request.rel_url.query.get("event")
            limit   = int(request.rel_url.query.get("limit", 50))
            return web.json_response({
                "history": self.delivery_history(hook_id, event, limit)
            })

        async def webhook_stats(request):
            return web.json_response(self.stats())

        app.router.add_get(  f"{prefix}/webhooks",            list_hooks)
        app.router.add_post( f"{prefix}/webhooks",            create_hook)
        app.router.add_patch(f"{prefix}/webhooks/{{id}}",     update_hook)
        app.router.add_delete(f"{prefix}/webhooks/{{id}}",    delete_hook)
        app.router.add_post( f"{prefix}/webhooks/{{id}}/toggle", toggle_hook)
        app.router.add_post( f"{prefix}/webhooks/{{id}}/test",   test_hook)
        app.router.add_get(  f"{prefix}/webhooks/history",    history)
        app.router.add_get(  f"{prefix}/webhooks/stats",      webhook_stats)
        logger.info(f"Webhook API routes registered at {prefix}/webhooks")
