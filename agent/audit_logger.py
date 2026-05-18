"""OMNI AGENT - Audit Logger
Immutable tamper-evident audit trail with hash chaining,
structured events, compliance export, and retention policies.

Features:
- AuditEvent: id, sequence, actor, action, resource, outcome, metadata
- Hash chain: each event stores SHA-256 of (prev_hash + event_json)
- Chain verification: recompute all hashes and confirm linkage
- Event levels: INFO, WARN, SECURITY, COMPLIANCE, DEBUG
- Actor types: user, service, system, agent, api_key
- Outcome: SUCCESS, FAILURE, PARTIAL, UNKNOWN
- Structured metadata: flexible dict for context (ip, session, diff, etc.)
- Retention policy: auto-purge events older than N days
- Redaction: mask sensitive fields in metadata for export
- Search: filter by actor, action, resource, outcome, time range
- Export: JSONL, CSV formats for compliance tools
- Integrity report: chain valid?, total events, hash head
- Alert hook: on_suspicious(event) called for SECURITY-level events
- Stats: events by level, actor, action; timeline bucketing
- SQLite persistence: events table with chain_hash column
- REST API: log, verify, search, export, stats
"""
import csv, hashlib, io, json, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class AuditLevel(str, Enum):
    DEBUG      = "debug"
    INFO       = "info"
    WARN       = "warn"
    SECURITY   = "security"
    COMPLIANCE = "compliance"

class Outcome(str, Enum):
    SUCCESS = "success"; FAILURE = "failure"
    PARTIAL = "partial"; UNKNOWN = "unknown"

_GENESIS_HASH = "0" * 64

def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()

@dataclass
class AuditEvent:
    id: str; seq: int
    actor: str; actor_type: str = "user"
    action: str = ""; resource: str = ""
    outcome: Outcome = Outcome.SUCCESS
    level: AuditLevel = AuditLevel.INFO
    metadata: Dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    prev_hash: str = _GENESIS_HASH
    chain_hash: str = ""
    session_id: str = ""
    ip: str = ""

    def _signable(self) -> str:
        """Canonical JSON for hashing (stable key order, no chain_hash)."""
        return json.dumps({
            "id": self.id, "seq": self.seq,
            "actor": self.actor, "action": self.action,
            "resource": self.resource, "outcome": self.outcome.value,
            "level": self.level.value, "ts": round(self.ts, 3),
            "prev_hash": self.prev_hash,
            "metadata": {k: v for k, v in sorted(self.metadata.items())}
        }, separators=(",", ":"))

    def compute_hash(self) -> str:
        return _sha256(self.prev_hash + self._signable())

    def to_dict(self, redact_keys: List[str] = None):
        meta = dict(self.metadata)
        if redact_keys:
            for k in redact_keys:
                if k in meta: meta[k] = "***"
        return {"id": self.id, "seq": self.seq,
                "actor": self.actor, "actor_type": self.actor_type,
                "action": self.action, "resource": self.resource,
                "outcome": self.outcome.value, "level": self.level.value,
                "metadata": meta, "ts": round(self.ts, 3),
                "session_id": self.session_id, "ip": self.ip,
                "chain_hash": self.chain_hash,
                "prev_hash": self.prev_hash[:16] + "…"}

class ALStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS audit_events(
                    id TEXT PRIMARY KEY, seq INTEGER UNIQUE,
                    actor TEXT, actor_type TEXT DEFAULT 'user',
                    action TEXT DEFAULT '', resource TEXT DEFAULT '',
                    outcome TEXT DEFAULT 'success',
                    level TEXT DEFAULT 'info',
                    metadata TEXT DEFAULT '{}',
                    session_id TEXT DEFAULT '', ip TEXT DEFAULT '',
                    ts REAL, prev_hash TEXT, chain_hash TEXT);
                CREATE INDEX IF NOT EXISTS idx_ae_actor
                    ON audit_events(actor, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_ae_action
                    ON audit_events(action, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_ae_level
                    ON audit_events(level, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_ae_ts
                    ON audit_events(ts DESC);
            """)

    def insert(self, e: AuditEvent):
        with self._conn() as c:
            c.execute("INSERT INTO audit_events VALUES"
                       "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e.id, e.seq, e.actor, e.actor_type, e.action,
                 e.resource, e.outcome.value, e.level.value,
                 json.dumps(e.metadata), e.session_id, e.ip,
                 e.ts, e.prev_hash, e.chain_hash))

    def last(self) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def all_ordered(self) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_events ORDER BY seq ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def search(self, actor: str = None, action: str = None,
                resource: str = None, outcome: str = None,
                level: str = None, since: float = 0,
                until: float = None, limit: int = 100) -> List[Dict]:
        conditions = ["ts >= ?"]
        params: list = [since]
        if until:    conditions.append("ts <= ?");           params.append(until)
        if actor:    conditions.append("actor = ?");         params.append(actor)
        if action:   conditions.append("action LIKE ?");     params.append(f"%{action}%")
        if resource: conditions.append("resource LIKE ?");   params.append(f"%{resource}%")
        if outcome:  conditions.append("outcome = ?");       params.append(outcome)
        if level:    conditions.append("level = ?");         params.append(level)
        where = " AND ".join(conditions)
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM audit_events WHERE {where} "
                f"ORDER BY ts DESC LIMIT ?", params).fetchall()
        return [dict(r) for r in rows]

    def purge_before(self, cutoff_ts: float) -> int:
        with self._conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM audit_events WHERE ts < ?",
                (cutoff_ts,)).fetchone()[0]
            c.execute("DELETE FROM audit_events WHERE ts < ?", (cutoff_ts,))
        return n

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            by_level = {r["level"]: r["cnt"] for r in c.execute(
                "SELECT level, COUNT(*) as cnt FROM audit_events GROUP BY level"
            ).fetchall()}
            by_outcome = {r["outcome"]: r["cnt"] for r in c.execute(
                "SELECT outcome, COUNT(*) as cnt FROM audit_events GROUP BY outcome"
            ).fetchall()}
        return {"total": total, "by_level": by_level, "by_outcome": by_outcome}

class AuditLogger:
    """
    Tamper-evident audit logger with SHA-256 hash chain and compliance export.

    Usage:
        audit = AuditLogger()
        audit.log("alice", "login",  resource="/api/v1",  outcome=Outcome.SUCCESS)
        audit.log("alice", "delete", resource="/data/123", outcome=Outcome.SUCCESS,
                   level=AuditLevel.SECURITY)

        ok, errors = audit.verify_chain()
        print(f"Chain valid: {ok}, issues: {errors}")

        jsonl = audit.export_jsonl(since=time.time()-86400)
    """
    def __init__(self, db_path: str = "data/audit.db",
                 retention_days: int = 365,
                 redact_keys: List[str] = None,
                 alert_on_security: bool = True):
        self._store = ALStore(db_path)
        self._retention_days = retention_days
        self._redact_keys = list(redact_keys or ["password","token","secret"])
        self._head_hash: str = _GENESIS_HASH
        self._seq: int = 0
        self._hooks: List[Callable] = []
        self._alert_on_security = alert_on_security
        # Restore chain state
        last = self._store.last()
        if last:
            self._head_hash = last["chain_hash"] or _GENESIS_HASH
            self._seq = last["seq"]

    def log(self, actor: str, action: str,
             resource: str = "",
             outcome: Outcome = Outcome.SUCCESS,
             level: AuditLevel = AuditLevel.INFO,
             metadata: Dict = None,
             actor_type: str = "user",
             session_id: str = "",
             ip: str = "") -> AuditEvent:
        self._seq += 1
        e = AuditEvent(
            id=str(uuid.uuid4())[:14],
            seq=self._seq,
            actor=actor, actor_type=actor_type,
            action=action, resource=resource,
            outcome=outcome, level=level,
            metadata=dict(metadata or {}),
            session_id=session_id, ip=ip,
            prev_hash=self._head_hash)
        e.chain_hash = e.compute_hash()
        self._head_hash = e.chain_hash
        self._store.insert(e)
        # Alerts
        if self._alert_on_security and level == AuditLevel.SECURITY:
            for h in self._hooks:
                try: h(e)
                except: pass
        return e

    def on_alert(self, fn: Callable):
        self._hooks.append(fn)

    def verify_chain(self) -> Tuple[bool, List[str]]:
        """Recompute all chain hashes. Returns (is_valid, error_list)."""
        rows = self._store.all_ordered()
        errors = []
        prev = _GENESIS_HASH
        for row in rows:
            e = AuditEvent(
                id=row["id"], seq=row["seq"],
                actor=row["actor"], actor_type=row["actor_type"],
                action=row["action"], resource=row["resource"],
                outcome=Outcome(row["outcome"]),
                level=AuditLevel(row["level"]),
                metadata=json.loads(row["metadata"] or "{}"),
                session_id=row["session_id"] or "",
                ip=row["ip"] or "",
                ts=row["ts"], prev_hash=prev)
            expected = e.compute_hash()
            if expected != row["chain_hash"]:
                errors.append(f"seq={row['seq']} hash mismatch")
            prev = row["chain_hash"]
        return (len(errors) == 0, errors)

    def integrity_report(self) -> Dict:
        valid, errors = self.verify_chain()
        total = self._store.count()
        return {"valid": valid, "total_events": total,
                "head_hash": self._head_hash[:20] + "…",
                "errors": errors[:10]}

    def search(self, **kwargs) -> List[Dict]:
        return self._store.search(**kwargs)

    def export_jsonl(self, since: float = 0,
                      until: float = None,
                      redact: bool = True) -> str:
        rows = self._store.search(since=since, until=until, limit=100000)
        lines = []
        for r in rows:
            meta = json.loads(r.get("metadata","{}"))
            if redact:
                for k in self._redact_keys:
                    if k in meta: meta[k] = "***"
            r["metadata"] = meta
            lines.append(json.dumps(r))
        return "\n".join(lines)

    def export_csv(self, since: float = 0, until: float = None) -> str:
        rows = self._store.search(since=since, until=until, limit=100000)
        if not rows: return ""
        output = io.StringIO()
        cols = ["seq","ts","actor","actor_type","action",
                "resource","outcome","level","session_id","ip"]
        w = csv.DictWriter(output, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows: w.writerow(r)
        return output.getvalue()

    def apply_retention(self) -> int:
        cutoff = time.time() - self._retention_days * 86400
        return self._store.purge_before(cutoff)

    def timeline(self, bucket_hours: float = 24,
                  since: float = None, limit: int = 30) -> List[Dict]:
        """Count events per time bucket."""
        since = since or (time.time() - bucket_hours * 3600 * limit)
        rows = self._store.search(since=since, limit=100000)
        buckets: Dict[int, int] = {}
        bucket_s = bucket_hours * 3600
        for r in rows:
            b = int(r["ts"] // bucket_s)
            buckets[b] = buckets.get(b, 0) + 1
        return [{"bucket_start": round(b * bucket_s, 0),
                  "count": cnt}
                 for b, cnt in sorted(buckets.items())]

    def stats(self) -> Dict:
        s = self._store.stats()
        s["head_hash"] = self._head_hash[:16] + "…"
        s["current_seq"] = self._seq
        s["retention_days"] = self._retention_days
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def log_ep(req):
            d = await req.json()
            e = self.log(d["actor"], d["action"],
                          d.get("resource",""),
                          Outcome(d.get("outcome","success")),
                          AuditLevel(d.get("level","info")),
                          d.get("metadata",{}))
            return web.json_response(e.to_dict(), status=201)
        async def verify_ep(req):
            return web.json_response(self.integrity_report())
        async def search_ep(req):
            q = req.rel_url.query
            rows = self.search(
                actor=q.get("actor"), action=q.get("action"),
                since=float(q.get("since",0)))
            return web.json_response({"events": rows[:100]})
        async def export_ep(req):
            fmt = req.rel_url.query.get("format","jsonl")
            since = float(req.rel_url.query.get("since", 0))
            if fmt == "csv":
                return web.Response(text=self.export_csv(since),
                                     content_type="text/csv")
            return web.Response(text=self.export_jsonl(since),
                                 content_type="application/x-ndjson")
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/audit"
        app.router.add_post(f"{p}/log",    log_ep)
        app.router.add_get( f"{p}/verify", verify_ep)
        app.router.add_get( f"{p}/search", search_ep)
        app.router.add_get( f"{p}/export", export_ep)
        app.router.add_get( f"{p}/stats",  stats_ep)
        logger.info(f"Audit logger API at {prefix}/audit/")
