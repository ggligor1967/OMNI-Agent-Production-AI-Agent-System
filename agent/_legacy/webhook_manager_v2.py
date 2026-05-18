"""OMNI Agent — Webhook Manager V2: registry, signing, retry, fanout, delivery log."""
from __future__ import annotations
import hashlib, hmac, json, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class WebhookStatus(str, Enum):
    ACTIVE   = "active"
    DISABLED = "disabled"
    FAILING  = "failing"   # too many consecutive failures


class DeliveryStatus(str, Enum):
    PENDING  = "pending"
    SENT     = "sent"
    FAILED   = "failed"
    RETRYING = "retrying"


@dataclass
class WebhookEndpoint:
    endpoint_id: str
    url: str
    secret: str = ""
    events: List[str] = field(default_factory=list)   # [] = all events
    status: WebhookStatus = WebhookStatus.ACTIVE
    headers: Dict[str, str] = field(default_factory=dict)
    max_retries: int = 3
    retry_delay_s: float = 5.0
    timeout_s: float = 10.0
    owner: str = ""
    description: str = ""
    consecutive_failures: int = 0
    failure_threshold: int = 5
    created_at: float = field(default_factory=time.time)
    last_delivery_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def matches_event(self, event: str) -> bool:
        return not self.events or event in self.events or "*" in self.events

    def to_dict(self) -> Dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "url": self.url,
            "events": self.events,
            "status": self.status.value,
            "consecutive_failures": self.consecutive_failures,
            "last_delivery_at": self.last_delivery_at,
        }


@dataclass
class WebhookDelivery:
    delivery_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    endpoint_id: str = ""
    event: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempt: int = 0
    response_code: Optional[int] = None
    response_body: str = ""
    error: Optional[str] = None
    sent_at: Optional[float] = None
    duration_ms: float = 0.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "endpoint_id": self.endpoint_id,
            "event": self.event,
            "status": self.status.value,
            "attempt": self.attempt,
            "response_code": self.response_code,
            "duration_ms": round(self.duration_ms, 2),
        }


class WebhookManagerV2:
    """
    Webhook management system:
    - Register endpoints with event subscriptions
    - HMAC-SHA256 payload signing
    - Sync and async delivery (pluggable HTTP sender)
    - Per-endpoint retry with delay
    - Fan-out to all matching endpoints per event
    - Consecutive-failure tracking → auto-disable
    - Delivery log with response tracking
    - Signature verification helper
    - Endpoint health check
    - Re-enable disabled endpoints
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:",
                 http_sender: Optional[Callable] = None):
        """
        http_sender: fn(url, payload_bytes, headers) → (status_code, body)
                     Defaults to a mock that always returns (200, "ok")
        """
        self._endpoints:  Dict[str, WebhookEndpoint] = {}
        self._deliveries: List[WebhookDelivery] = []
        self._http_sender = http_sender or self._mock_sender
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_db()
        self._sent_count   = 0
        self._failed_count = 0

    def _mock_sender(self, url: str, payload: bytes,
                     headers: Dict) -> tuple:
        return (200, "ok")

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS wh_endpoints (
                endpoint_id TEXT PRIMARY KEY, url TEXT, secret TEXT,
                events TEXT, status TEXT, headers TEXT,
                max_retries INTEGER, owner TEXT, created_at REAL,
                consecutive_failures INTEGER
            );
            CREATE TABLE IF NOT EXISTS wh_deliveries (
                delivery_id TEXT PRIMARY KEY, endpoint_id TEXT,
                event TEXT, payload TEXT, status TEXT,
                attempt INTEGER, response_code INTEGER,
                response_body TEXT, error TEXT,
                sent_at REAL, duration_ms REAL, created_at REAL
            );
        """)
        self._db.commit()

    # ── ENDPOINT MANAGEMENT ───────────────────────────────────────────

    def register(self, url: str,
                  events: Optional[List[str]] = None,
                  secret: str = "",
                  headers: Optional[Dict[str, str]] = None,
                  max_retries: int = 3,
                  retry_delay_s: float = 0.0,
                  timeout_s: float = 10.0,
                  owner: str = "",
                  description: str = "",
                  failure_threshold: int = 5,
                  endpoint_id: Optional[str] = None,
                  metadata: Optional[Dict] = None) -> WebhookEndpoint:
        eid = endpoint_id or str(uuid.uuid4())[:10]
        ep  = WebhookEndpoint(
            endpoint_id=eid, url=url, secret=secret,
            events=list(events or []),
            headers=dict(headers or {}),
            max_retries=max_retries, retry_delay_s=retry_delay_s,
            timeout_s=timeout_s, owner=owner,
            description=description,
            failure_threshold=failure_threshold,
            metadata=metadata or {})
        self._endpoints[eid] = ep
        self._persist_endpoint(ep)
        return ep

    def unregister(self, endpoint_id: str) -> bool:
        ep = self._endpoints.pop(endpoint_id, None)
        if ep:
            self._db.execute(
                "DELETE FROM wh_endpoints WHERE endpoint_id=?",
                (endpoint_id,))
            self._db.commit()
        return ep is not None

    def enable(self, endpoint_id: str):
        ep = self._endpoints.get(endpoint_id)
        if ep:
            ep.status = WebhookStatus.ACTIVE
            ep.consecutive_failures = 0
            self._persist_endpoint(ep)

    def disable(self, endpoint_id: str):
        ep = self._endpoints.get(endpoint_id)
        if ep:
            ep.status = WebhookStatus.DISABLED
            self._persist_endpoint(ep)

    def update_secret(self, endpoint_id: str, secret: str):
        ep = self._endpoints.get(endpoint_id)
        if ep:
            ep.secret = secret
            self._persist_endpoint(ep)

    def subscribe(self, endpoint_id: str, event: str):
        ep = self._endpoints.get(endpoint_id)
        if ep and event not in ep.events:
            ep.events.append(event)

    def unsubscribe(self, endpoint_id: str, event: str):
        ep = self._endpoints.get(endpoint_id)
        if ep and event in ep.events:
            ep.events.remove(event)

    # ── SIGNING ───────────────────────────────────────────────────────

    def sign_payload(self, payload: bytes, secret: str) -> str:
        return hmac.new(
            secret.encode(), payload, hashlib.sha256).hexdigest()

    def verify_signature(self, payload: bytes,
                          signature: str, secret: str) -> bool:
        expected = self.sign_payload(payload, secret)
        return hmac.compare_digest(expected, signature)

    # ── DISPATCH ──────────────────────────────────────────────────────

    def dispatch(self, event: str,
                  payload: Dict[str, Any],
                  async_delivery: bool = False) -> List[WebhookDelivery]:
        matching = [ep for ep in self._endpoints.values()
                    if ep.status == WebhookStatus.ACTIVE
                    and ep.matches_event(event)]
        deliveries = []
        for ep in matching:
            d = WebhookDelivery(
                endpoint_id=ep.endpoint_id,
                event=event, payload=payload)
            self._deliveries.append(d)
            if async_delivery:
                t = threading.Thread(
                    target=self._deliver, args=(ep, d), daemon=True)
                t.start()
            else:
                self._deliver(ep, d)
            deliveries.append(d)
        return deliveries

    def _deliver(self, ep: WebhookEndpoint, d: WebhookDelivery):
        body   = json.dumps({
            "event": d.event,
            "delivery_id": d.delivery_id,
            "timestamp": time.time(),
            "payload": d.payload,
        }, default=str).encode()

        headers = dict(ep.headers)
        headers["Content-Type"] = "application/json"
        if ep.secret:
            headers["X-Webhook-Signature"] = self.sign_payload(body, ep.secret)

        attempt = 0
        while True:
            attempt += 1
            d.attempt = attempt
            t0 = time.time()
            try:
                code, resp_body = self._http_sender(ep.url, body, headers)
                d.response_code = code
                d.response_body = str(resp_body)[:500]
                d.duration_ms   = (time.time() - t0) * 1000
                d.sent_at       = time.time()

                if 200 <= code < 300:
                    d.status = DeliveryStatus.SENT
                    ep.consecutive_failures = 0
                    ep.last_delivery_at     = time.time()
                    self._sent_count += 1
                    break
                else:
                    raise RuntimeError(f"HTTP {code}")

            except Exception as exc:
                d.error = str(exc)
                ep.consecutive_failures += 1
                if ep.consecutive_failures >= ep.failure_threshold:
                    ep.status = WebhookStatus.FAILING

                if attempt <= ep.max_retries:
                    d.status = DeliveryStatus.RETRYING
                    if ep.retry_delay_s > 0:
                        time.sleep(ep.retry_delay_s)
                else:
                    d.status = DeliveryStatus.FAILED
                    self._failed_count += 1
                    break

        self._persist_delivery(d)
        self._persist_endpoint(ep)

    def retry_failed(self, delivery_id: str) -> Optional[WebhookDelivery]:
        d = next((x for x in self._deliveries
                  if x.delivery_id == delivery_id), None)
        if not d or d.status != DeliveryStatus.FAILED:
            return None
        ep = self._endpoints.get(d.endpoint_id)
        if not ep: return None
        d.attempt = 0
        d.status  = DeliveryStatus.PENDING
        self._deliver(ep, d)
        return d

    # ── QUERY ────────────────────────────────────────────────────────

    def get_endpoint(self, endpoint_id: str) -> Optional[WebhookEndpoint]:
        return self._endpoints.get(endpoint_id)

    def list_endpoints(self, event: Optional[str] = None,
                        status: Optional[WebhookStatus] = None) -> List[Dict]:
        eps = list(self._endpoints.values())
        if event:  eps = [e for e in eps if e.matches_event(event)]
        if status: eps = [e for e in eps if e.status == status]
        return [e.to_dict() for e in eps]

    def delivery_log(self, endpoint_id: Optional[str] = None,
                      status: Optional[DeliveryStatus] = None,
                      limit: int = 50) -> List[Dict]:
        rows = self._db.execute(
            "SELECT delivery_id,endpoint_id,event,status,attempt,"
            "response_code,duration_ms,sent_at FROM wh_deliveries "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        result = [{"id": r[0], "endpoint": r[1], "event": r[2],
                   "status": r[3], "attempt": r[4],
                   "code": r[5]} for r in rows]
        if endpoint_id:
            result = [r for r in result if r["endpoint"] == endpoint_id]
        if status:
            result = [r for r in result if r["status"] == status.value]
        return result

    def get_delivery(self, delivery_id: str) -> Optional[WebhookDelivery]:
        return next((d for d in self._deliveries
                     if d.delivery_id == delivery_id), None)

    def health(self) -> Dict[str, Any]:
        active   = sum(1 for e in self._endpoints.values()
                       if e.status == WebhookStatus.ACTIVE)
        failing  = sum(1 for e in self._endpoints.values()
                       if e.status == WebhookStatus.FAILING)
        disabled = sum(1 for e in self._endpoints.values()
                       if e.status == WebhookStatus.DISABLED)
        return {
            "total": len(self._endpoints),
            "active": active, "failing": failing, "disabled": disabled,
        }

    def _persist_endpoint(self, ep: WebhookEndpoint):
        self._db.execute(
            "INSERT OR REPLACE INTO wh_endpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ep.endpoint_id, ep.url, ep.secret,
             json.dumps(ep.events), ep.status.value,
             json.dumps(ep.headers), ep.max_retries,
             ep.owner, ep.created_at, ep.consecutive_failures))
        self._db.commit()

    def _persist_delivery(self, d: WebhookDelivery):
        self._db.execute(
            "INSERT OR REPLACE INTO wh_deliveries VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            (d.delivery_id, d.endpoint_id, d.event,
             json.dumps(d.payload, default=str), d.status.value,
             d.attempt, d.response_code,
             d.response_body[:500] if d.response_body else "",
             d.error, d.sent_at, d.duration_ms, d.created_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "endpoints": len(self._endpoints),
            "deliveries": len(self._deliveries),
            "sent": self._sent_count,
            "failed": self._failed_count,
        }
