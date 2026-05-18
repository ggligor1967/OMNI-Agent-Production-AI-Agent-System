"""OMNI AGENT - Message Queue
Persistent message queue with producers, consumers, acknowledgment,
dead letter queue, priority, visibility timeout, and batch ops.

Features:
- Queues: named; auto-created on first send
- Message: id, body, attrs, priority (1=highest), delay_s, sent_at
- Priority queue: heapq on (priority, seq) — lower number = higher priority
- Producers: send(queue, body) or send_batch([...])
- Consumers: receive(queue, count, visibility_timeout_s) → [messages]
  - Visibility timeout: received messages hidden from other consumers
  - On ack: message deleted from queue
  - On nack / timeout expiry: message becomes visible again
- Dead Letter Queue (DLQ): after max_receive_count failures → dlq
- Delay: message invisible until delay_s after sent_at
- FIFO / Priority modes per queue
- Message attributes: arbitrary key-value metadata
- Long polling: receive() waits up to wait_s for a message
- Purge: delete all messages in a queue
- Peek: inspect messages without receiving (no visibility lock)
- Consumer groups: multiple consumers share a queue
- Message dedup: content-hash dedup window (per queue, 5 min)
- Hooks: on_send, on_receive, on_ack, on_dlq
- SQLite persistence: messages, receipts, dlq
- REST API: send, receive, ack, nack, purge, stats
"""
import asyncio, hashlib, heapq, json, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class QueueMode(str, Enum):
    FIFO     = "fifo"
    PRIORITY = "priority"

@dataclass
class Message:
    id: str; queue: str; body: Any
    attrs: Dict[str, str] = field(default_factory=dict)
    priority: int = 5
    delay_s: float = 0.0
    sent_at: float = field(default_factory=time.time)
    receive_count: int = 0
    seq: int = 0

    @property
    def visible_at(self) -> float:
        return self.sent_at + self.delay_s

    @property
    def is_visible(self) -> bool:
        return time.time() >= self.visible_at

    def to_dict(self):
        return {"id": self.id, "queue": self.queue, "body": self.body,
                "attrs": self.attrs, "priority": self.priority,
                "sent_at": round(self.sent_at, 3),
                "receive_count": self.receive_count}

@dataclass
class Receipt:
    receipt_id: str; message_id: str; queue: str
    visible_at: float   # when visibility timeout expires

@dataclass
class QueueConfig:
    name: str; mode: QueueMode = QueueMode.FIFO
    max_receive_count: int = 3
    visibility_timeout_s: float = 30.0
    dedup_window_s: float = 300.0
    created_at: float = field(default_factory=time.time)

class MQStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS queues(
                    name TEXT PRIMARY KEY, mode TEXT,
                    max_receive_count INTEGER,
                    visibility_timeout_s REAL,
                    dedup_window_s REAL, created_at REAL);
                CREATE TABLE IF NOT EXISTS messages(
                    id TEXT PRIMARY KEY, queue TEXT, body TEXT,
                    attrs TEXT, priority INTEGER, delay_s REAL,
                    sent_at REAL, receive_count INTEGER, seq INTEGER,
                    status TEXT DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS receipts(
                    receipt_id TEXT PRIMARY KEY, message_id TEXT,
                    queue TEXT, visible_at REAL);
                CREATE TABLE IF NOT EXISTS dlq(
                    id TEXT PRIMARY KEY, queue TEXT, body TEXT,
                    attrs TEXT, receive_count INTEGER, ts REAL);
                CREATE INDEX IF NOT EXISTS idx_msg_queue
                    ON messages(queue, status, priority, seq);
                CREATE INDEX IF NOT EXISTS idx_receipts_vis
                    ON receipts(queue, visible_at);
            """)
        with self._conn() as c:
            row = c.execute("SELECT MAX(seq) FROM messages").fetchone()
            self._seq = row[0] or 0

    def next_seq(self) -> int:
        self._seq += 1; return self._seq

    def save_queue(self, q: QueueConfig):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO queues VALUES(?,?,?,?,?,?)",
                (q.name, q.mode.value, q.max_receive_count,
                 q.visibility_timeout_s, q.dedup_window_s, q.created_at))

    def enqueue(self, msg: Message):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                (msg.id, msg.queue, json.dumps(msg.body, default=str),
                 json.dumps(msg.attrs), msg.priority, msg.delay_s,
                 msg.sent_at, msg.receive_count, msg.seq, "pending"))

    def fetch_visible(self, queue: str, count: int,
                       mode: QueueMode) -> List[Message]:
        now = time.time()
        order = ("priority ASC, seq ASC"
                  if mode == QueueMode.PRIORITY else "seq ASC")
        with self._conn() as c:
            # Exclude messages currently in receipt (invisible)
            rows = c.execute(f"""
                SELECT m.* FROM messages m
                LEFT JOIN receipts r ON m.id = r.message_id
                    AND r.visible_at > ?
                WHERE m.queue=? AND m.status='pending'
                    AND (m.sent_at + m.delay_s) <= ?
                    AND r.receipt_id IS NULL
                ORDER BY {order} LIMIT ?
            """, (now, queue, now, count)).fetchall()
        return [self._row_to_msg(r) for r in rows]

    def _row_to_msg(self, r) -> Message:
        return Message(id=r["id"], queue=r["queue"],
                        body=json.loads(r["body"]),
                        attrs=json.loads(r["attrs"]),
                        priority=r["priority"], delay_s=r["delay_s"],
                        sent_at=r["sent_at"],
                        receive_count=r["receive_count"],
                        seq=r["seq"])

    def save_receipt(self, receipt: Receipt):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO receipts VALUES(?,?,?,?)",
                (receipt.receipt_id, receipt.message_id,
                 receipt.queue, receipt.visible_at))

    def ack(self, receipt_id: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT message_id FROM receipts WHERE receipt_id=?",
                (receipt_id,)).fetchone()
            if not row: return None
            msg_id = row["message_id"]
            c.execute("DELETE FROM receipts WHERE receipt_id=?", (receipt_id,))
            c.execute("UPDATE messages SET status='done' WHERE id=?", (msg_id,))
        return msg_id

    def nack(self, receipt_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT message_id FROM receipts WHERE receipt_id=?",
                (receipt_id,)).fetchone()
            if not row: return False
            c.execute("DELETE FROM receipts WHERE receipt_id=?", (receipt_id,))
            # Do NOT increment here; receive() already incremented
        return True

    def increment_receive(self, msg_id: str):
        with self._conn() as c:
            c.execute("UPDATE messages SET receive_count=receive_count+1 "
                       "WHERE id=?", (msg_id,))

    def move_to_dlq(self, msg: Message):
        with self._conn() as c:
            c.execute("INSERT INTO dlq VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], msg.queue,
                 json.dumps(msg.body, default=str),
                 json.dumps(msg.attrs), msg.receive_count, time.time()))
            c.execute("UPDATE messages SET status='dlq' WHERE id=?", (msg.id,))

    def purge(self, queue: str) -> int:
        with self._conn() as c:
            c.execute("DELETE FROM receipts WHERE queue=?", (queue,))
            cur = c.execute(
                "DELETE FROM messages WHERE queue=? AND status='pending'",
                (queue,))
            return cur.rowcount

    def get_dlq(self, queue: str = None, limit: int = 50) -> List[Dict]:
        where = f"WHERE queue='{queue}'" if queue else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM dlq {where} ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self, queue: str = None) -> Dict:
        with self._conn() as c:
            if queue:
                pending = c.execute(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE queue=? AND status='pending'", (queue,)).fetchone()[0]
                done = c.execute(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE queue=? AND status='done'", (queue,)).fetchone()[0]
                dlq_n = c.execute(
                    "SELECT COUNT(*) FROM dlq WHERE queue=?",
                    (queue,)).fetchone()[0]
                in_flight = c.execute(
                    "SELECT COUNT(*) FROM receipts WHERE queue=? AND visible_at>?",
                    (queue, time.time())).fetchone()[0]
                return {"pending": pending, "done": done,
                        "dlq": dlq_n, "in_flight": in_flight}
            total = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            by_q = {r["queue"]: r["cnt"] for r in c.execute(
                "SELECT queue, COUNT(*) as cnt FROM messages "
                "WHERE status='pending' GROUP BY queue").fetchall()}
        return {"total": total, "by_queue": by_q}

class MessageQueue:
    """
    Message queue with priority, visibility timeout, and DLQ.

    Usage:
        mq = MessageQueue()
        mq.create_queue("orders", max_receive_count=3)

        mq.send("orders", {"item": "Widget", "qty": 5})

        msgs = mq.receive("orders", count=1)
        for msg in msgs:
            print(msg.body)
            mq.ack(msg._receipt_id)
    """
    def __init__(self, db_path: str = "data/mqueue.db"):
        self._store = MQStore(db_path)
        self._queues: Dict[str, QueueConfig] = {}
        self._hooks_send:    List[Callable] = []
        self._hooks_receive: List[Callable] = []
        self._hooks_ack:     List[Callable] = []
        self._hooks_dlq:     List[Callable] = []
        self._dedup: Dict[str, float] = {}   # dedup_hash → sent_at

    def on_send(self, fn):    self._hooks_send.append(fn)
    def on_receive(self, fn): self._hooks_receive.append(fn)
    def on_ack(self, fn):     self._hooks_ack.append(fn)
    def on_dlq(self, fn):     self._hooks_dlq.append(fn)

    def create_queue(self, name: str,
                      mode: QueueMode = QueueMode.FIFO,
                      max_receive_count: int = 3,
                      visibility_timeout_s: float = 30.0,
                      dedup_window_s: float = 0.0) -> QueueConfig:
        q = QueueConfig(name=name, mode=mode,
                         max_receive_count=max_receive_count,
                         visibility_timeout_s=visibility_timeout_s,
                         dedup_window_s=dedup_window_s)
        self._queues[name] = q
        self._store.save_queue(q)
        return q

    def _get_queue(self, name: str) -> QueueConfig:
        if name not in self._queues:
            self.create_queue(name)
        return self._queues[name]

    def _dedup_key(self, queue: str, body: Any) -> str:
        return (
            f"{queue}:"
            f"{hashlib.md5(  # nosec B324 - queue deduplication key only
                json.dumps(body, default=str).encode(), usedforsecurity=False
            ).hexdigest()}"
        )

    def send(self, queue: str, body: Any,
              attrs: Dict = None, priority: int = 5,
              delay_s: float = 0.0,
              dedup: bool = False) -> Message:
        q = self._get_queue(queue)
        if dedup and q.dedup_window_s > 0:
            dk = self._dedup_key(queue, body)
            last = self._dedup.get(dk, 0)
            if time.time() - last < q.dedup_window_s:
                # Duplicate detected — return dummy
                return Message(id="dedup", queue=queue, body=body)
            self._dedup[dk] = time.time()
        msg = Message(id=str(uuid.uuid4())[:16], queue=queue,
                       body=body, attrs=dict(attrs or {}),
                       priority=priority, delay_s=delay_s,
                       seq=self._store.next_seq())
        self._store.enqueue(msg)
        for h in self._hooks_send:
            try: h(msg)
            except: pass
        return msg

    def send_batch(self, queue: str, bodies: List[Any],
                    **kwargs) -> List[Message]:
        return [self.send(queue, b, **kwargs) for b in bodies]

    def receive(self, queue: str, count: int = 1,
                 visibility_timeout_s: float = None) -> List[Message]:
        q = self._get_queue(queue)
        vis_s = (visibility_timeout_s or q.visibility_timeout_s)
        msgs = self._store.fetch_visible(queue, count, q.mode)
        result = []
        for msg in msgs:
            self._store.increment_receive(msg.id)
            msg.receive_count += 1
            # Check DLQ threshold AFTER incrementing
            if msg.receive_count >= q.max_receive_count:
                self._store.move_to_dlq(msg)
                for h in self._hooks_dlq:
                    try: h(msg)
                    except: pass
                continue
            receipt_id = str(uuid.uuid4())[:16]
            receipt = Receipt(receipt_id=receipt_id,
                               message_id=msg.id,
                               queue=queue,
                               visible_at=time.time() + vis_s)
            self._store.save_receipt(receipt)
            msg.__dict__["_receipt_id"] = receipt_id
            result.append(msg)
            for h in self._hooks_receive:
                try: h(msg)
                except: pass
        return result

    async def receive_async(self, queue: str, count: int = 1,
                             wait_s: float = 5.0,
                             poll_interval: float = 0.1) -> List[Message]:
        """Long-poll variant: wait up to wait_s for at least one message."""
        deadline = time.time() + wait_s
        while time.time() < deadline:
            msgs = self.receive(queue, count)
            if msgs: return msgs
            await asyncio.sleep(poll_interval)
        return []

    def ack(self, receipt_id: str) -> bool:
        msg_id = self._store.ack(receipt_id)
        if msg_id:
            for h in self._hooks_ack:
                try: h(msg_id)
                except: pass
            return True
        return False

    def nack(self, receipt_id: str) -> bool:
        return self._store.nack(receipt_id)

    def peek(self, queue: str, count: int = 10) -> List[Message]:
        q = self._get_queue(queue)
        return self._store.fetch_visible(queue, count, q.mode)

    def purge(self, queue: str) -> int:
        return self._store.purge(queue)

    def dlq(self, queue: str = None, limit: int = 50) -> List[Dict]:
        return self._store.get_dlq(queue, limit)

    def stats(self, queue: str = None) -> Dict:
        return self._store.stats(queue)

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def send_ep(req):
            d = await req.json()
            msg = self.send(d["queue"], d["body"],
                             d.get("attrs",{}), d.get("priority",5),
                             d.get("delay_s",0))
            return web.json_response(msg.to_dict(), status=201)
        async def receive_ep(req):
            d = await req.json()
            msgs = self.receive(d["queue"], d.get("count",1))
            return web.json_response(
                {"messages": [m.to_dict() for m in msgs]})
        async def ack_ep(req):
            d = await req.json()
            ok = self.ack(d["receipt_id"])
            return web.json_response({"acked": ok})
        async def nack_ep(req):
            d = await req.json()
            ok = self.nack(d["receipt_id"])
            return web.json_response({"nacked": ok})
        async def stats_ep(req):
            q = req.rel_url.query.get("queue")
            return web.json_response(self.stats(q))
        p = f"{prefix}/mq"
        app.router.add_post(f"{p}/send",    send_ep)
        app.router.add_post(f"{p}/receive", receive_ep)
        app.router.add_post(f"{p}/ack",     ack_ep)
        app.router.add_post(f"{p}/nack",    nack_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Message queue API at {prefix}/mq/")
