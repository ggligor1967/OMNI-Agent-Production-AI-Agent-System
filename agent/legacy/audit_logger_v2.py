"""OMNI Agent — Audit Logger V2: immutable structured audit log with tamper detection."""
from __future__ import annotations
import hashlib, json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class AuditEventType(str, Enum):
    AUTH       = "auth"
    ACCESS     = "access"
    DATA_READ  = "data_read"
    DATA_WRITE = "data_write"
    DATA_DELETE = "data_delete"
    CONFIG     = "config"
    SYSTEM     = "system"
    SECURITY   = "security"
    COMPLIANCE = "compliance"
    CUSTOM     = "custom"


class AuditOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass
class AuditEntry:
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: AuditEventType = AuditEventType.CUSTOM
    outcome: AuditOutcome = AuditOutcome.SUCCESS
    actor_id: str = ""          # user/service performing action
    actor_type: str = "user"    # user | service | system
    action: str = ""            # e.g. "login", "delete_document"
    resource: str = ""          # e.g. "document:123"
    resource_type: str = ""
    ip_address: str = ""
    session_id: str = ""
    trace_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    sequence: int = 0           # monotonic sequence number
    prev_hash: str = ""         # hash chain for tamper detection
    entry_hash: str = ""        # hash of this entry

    def compute_hash(self) -> str:
        payload = json.dumps({
            "entry_id": self.entry_id,
            "event_type": self.event_type.value,
            "outcome": self.outcome.value,
            "actor_id": self.actor_id,
            "action": self.action,
            "resource": self.resource,
            "ts": self.ts,
            "sequence": self.sequence,
            "prev_hash": self.prev_hash,
            "details": self.details,
        }, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "event_type": self.event_type.value,
            "outcome": self.outcome.value,
            "actor_id": self.actor_id,
            "action": self.action,
            "resource": self.resource,
            "ip_address": self.ip_address,
            "ts": self.ts,
            "sequence": self.sequence,
            "entry_hash": self.entry_hash,
        }


@dataclass
class AuditQuery:
    actor_id: Optional[str] = None
    event_type: Optional[AuditEventType] = None
    outcome: Optional[AuditOutcome] = None
    resource: Optional[str] = None
    action: Optional[str] = None
    from_ts: Optional[float] = None
    to_ts: Optional[float] = None
    tags: Optional[List[str]] = None
    limit: int = 100
    offset: int = 0


class AuditLoggerV2:
    """
    Immutable audit logger with:
    - Typed audit events (AUTH/ACCESS/DATA/CONFIG/SECURITY/COMPLIANCE)
    - Hash chain for tamper detection (each entry hashes prev)
    - Monotonic sequence numbers
    - Rich structured details per event
    - Flexible query (by actor, event type, outcome, resource, time range)
    - Tag filtering
    - Retention policy (purge old entries)
    - Export to JSON/CSV
    - Integrity verification
    - Alert hooks for specific events
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:",
                 retention_days: Optional[float] = None):
        self.retention_days = retention_days
        self._sequence   = 0
        self._last_hash  = ""
        self._hooks: Dict[AuditEventType, List[Callable]] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        # Restore last sequence and hash from DB
        row = self._db.execute(
            "SELECT sequence, entry_hash FROM al_entries "
            "ORDER BY sequence DESC LIMIT 1").fetchone()
        if row:
            self._sequence  = row[0]
            self._last_hash = row[1] or ""

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS al_entries (
                entry_id TEXT PRIMARY KEY,
                event_type TEXT, outcome TEXT,
                actor_id TEXT, actor_type TEXT,
                action TEXT, resource TEXT, resource_type TEXT,
                ip_address TEXT, session_id TEXT, trace_id TEXT,
                details TEXT, tags TEXT,
                ts REAL, sequence INTEGER, prev_hash TEXT, entry_hash TEXT
            );
            CREATE INDEX IF NOT EXISTS al_ts ON al_entries(ts);
            CREATE INDEX IF NOT EXISTS al_actor ON al_entries(actor_id);
            CREATE INDEX IF NOT EXISTS al_event ON al_entries(event_type);
        """)
        self._db.commit()

    # ── LOGGING ───────────────────────────────────────────────────────

    def log(self, event_type: AuditEventType,
            action: str,
            actor_id: str = "",
            actor_type: str = "user",
            outcome: AuditOutcome = AuditOutcome.SUCCESS,
            resource: str = "",
            resource_type: str = "",
            ip_address: str = "",
            session_id: str = "",
            trace_id: str = "",
            details: Optional[Dict] = None,
            tags: Optional[List[str]] = None,
            ts: Optional[float] = None) -> AuditEntry:

        self._sequence += 1
        entry = AuditEntry(
            event_type=event_type,
            outcome=outcome,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            resource=resource,
            resource_type=resource_type,
            ip_address=ip_address,
            session_id=session_id,
            trace_id=trace_id,
            details=dict(details or {}),
            tags=list(tags or []),
            ts=ts or time.time(),
            sequence=self._sequence,
            prev_hash=self._last_hash)

        entry.entry_hash = entry.compute_hash()
        self._last_hash  = entry.entry_hash

        self._persist(entry)

        for fn in self._hooks.get(event_type, []):
            try: fn(entry)
            except Exception: pass

        return entry

    # ── CONVENIENCE METHODS ───────────────────────────────────────────

    def log_auth(self, actor_id: str, action: str,
                 outcome: AuditOutcome = AuditOutcome.SUCCESS, **kw) -> AuditEntry:
        return self.log(AuditEventType.AUTH, action,
                        actor_id=actor_id, outcome=outcome, **kw)

    def log_access(self, actor_id: str, resource: str,
                   action: str = "read",
                   outcome: AuditOutcome = AuditOutcome.SUCCESS, **kw) -> AuditEntry:
        return self.log(AuditEventType.ACCESS, action,
                        actor_id=actor_id, resource=resource,
                        outcome=outcome, **kw)

    def log_data(self, actor_id: str, resource: str,
                 action: str,
                 outcome: AuditOutcome = AuditOutcome.SUCCESS, **kw) -> AuditEntry:
        etype = (AuditEventType.DATA_DELETE if "delete" in action.lower()
                 else AuditEventType.DATA_WRITE if action.lower() in
                 ("write", "create", "update", "insert") else AuditEventType.DATA_READ)
        return self.log(etype, action, actor_id=actor_id,
                        resource=resource, outcome=outcome, **kw)

    def log_security(self, actor_id: str, action: str,
                     details: Optional[Dict] = None, **kw) -> AuditEntry:
        return self.log(AuditEventType.SECURITY, action,
                        actor_id=actor_id, details=details, **kw)

    # ── QUERY ─────────────────────────────────────────────────────────

    def query(self, q: AuditQuery) -> List[AuditEntry]:
        sql    = "SELECT * FROM al_entries WHERE 1=1"
        params: List[Any] = []
        if q.actor_id:
            sql += " AND actor_id=?"; params.append(q.actor_id)
        if q.event_type:
            sql += " AND event_type=?"; params.append(q.event_type.value)
        if q.outcome:
            sql += " AND outcome=?"; params.append(q.outcome.value)
        if q.resource:
            sql += " AND resource LIKE ?"; params.append(f"%{q.resource}%")
        if q.action:
            sql += " AND action LIKE ?"; params.append(f"%{q.action}%")
        if q.from_ts:
            sql += " AND ts>=?"; params.append(q.from_ts)
        if q.to_ts:
            sql += " AND ts<=?"; params.append(q.to_ts)
        sql += " ORDER BY sequence DESC LIMIT ? OFFSET ?"
        params += [q.limit, q.offset]
        rows = self._db.execute(sql, params).fetchall()
        entries = [self._row_to_entry(r) for r in rows]
        if q.tags:
            entries = [e for e in entries
                       if all(t in e.tags for t in q.tags)]
        return entries

    def get_entry(self, entry_id: str) -> Optional[AuditEntry]:
        row = self._db.execute(
            "SELECT * FROM al_entries WHERE entry_id=?",
            (entry_id,)).fetchone()
        return self._row_to_entry(row) if row else None

    def get_by_actor(self, actor_id: str, limit: int = 50) -> List[AuditEntry]:
        return self.query(AuditQuery(actor_id=actor_id, limit=limit))

    def get_failures(self, limit: int = 50) -> List[AuditEntry]:
        return self.query(AuditQuery(outcome=AuditOutcome.FAILURE, limit=limit))

    def get_recent(self, limit: int = 50) -> List[AuditEntry]:
        return self.query(AuditQuery(limit=limit))

    # ── INTEGRITY ─────────────────────────────────────────────────────

    def verify_chain(self, limit: int = 1000) -> Tuple[bool, Optional[int]]:
        """Verify hash chain integrity. Returns (ok, first_bad_sequence)."""
        rows = self._db.execute(
            "SELECT sequence,prev_hash,entry_hash,entry_id,event_type,"
            "outcome,actor_id,action,resource,ts,details "
            "FROM al_entries ORDER BY sequence ASC LIMIT ?",
            (limit,)).fetchall()
        prev_hash = ""
        for row in rows:
            seq, ph, eh = row[0], row[1], row[2]
            if ph != prev_hash:
                return False, seq
            # Recompute hash
            entry = AuditEntry(
                entry_id=row[3],
                event_type=AuditEventType(row[4]),
                outcome=AuditOutcome(row[5]),
                actor_id=row[6], action=row[7], resource=row[8],
                ts=row[9], sequence=seq, prev_hash=ph,
                details=json.loads(row[10] or "{}"))
            computed = entry.compute_hash()
            if computed != eh:
                return False, seq
            prev_hash = eh
        return True, None

    # ── RETENTION ─────────────────────────────────────────────────────

    def purge_old(self, before_ts: Optional[float] = None) -> int:
        if before_ts is None and self.retention_days:
            before_ts = time.time() - (self.retention_days * 86400)
        if before_ts is None: return 0
        cur = self._db.execute(
            "DELETE FROM al_entries WHERE ts < ?", (before_ts,))
        self._db.commit()
        return cur.rowcount

    # ── EXPORT ────────────────────────────────────────────────────────

    def export_json(self, q: Optional[AuditQuery] = None) -> str:
        entries = self.query(q or AuditQuery(limit=10000))
        return json.dumps([e.to_dict() for e in entries], default=str)

    def export_csv(self, q: Optional[AuditQuery] = None) -> str:
        entries = self.query(q or AuditQuery(limit=10000))
        if not entries: return ""
        header = "entry_id,event_type,outcome,actor_id,action,resource,ts"
        lines  = [header]
        for e in entries:
            lines.append(
                f"{e.entry_id},{e.event_type.value},{e.outcome.value},"
                f"{e.actor_id},{e.action},{e.resource},{e.ts:.0f}")
        return "\n".join(lines)

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_event(self, event_type: AuditEventType,
                 fn: Callable[[AuditEntry], None]):
        self._hooks.setdefault(event_type, []).append(fn)

    # ── PERSISTENCE ───────────────────────────────────────────────────

    def _persist(self, entry: AuditEntry):
        self._db.execute(
            "INSERT INTO al_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry.entry_id, entry.event_type.value, entry.outcome.value,
             entry.actor_id, entry.actor_type, entry.action,
             entry.resource, entry.resource_type, entry.ip_address,
             entry.session_id, entry.trace_id,
             json.dumps(entry.details, default=str),
             json.dumps(entry.tags),
             entry.ts, entry.sequence,
             entry.prev_hash, entry.entry_hash))
        self._db.commit()

    def _row_to_entry(self, row) -> AuditEntry:
        return AuditEntry(
            entry_id=row[0],
            event_type=AuditEventType(row[1]),
            outcome=AuditOutcome(row[2]),
            actor_id=row[3] or "", actor_type=row[4] or "user",
            action=row[5] or "", resource=row[6] or "",
            resource_type=row[7] or "", ip_address=row[8] or "",
            session_id=row[9] or "", trace_id=row[10] or "",
            details=json.loads(row[11] or "{}"),
            tags=json.loads(row[12] or "[]"),
            ts=row[13], sequence=row[14],
            prev_hash=row[15] or "", entry_hash=row[16] or "")

    def stats(self) -> Dict[str, Any]:
        total = self._db.execute(
            "SELECT COUNT(*) FROM al_entries").fetchone()[0]
        failures = self._db.execute(
            "SELECT COUNT(*) FROM al_entries WHERE outcome='failure'").fetchone()[0]
        return {
            "total_entries": total,
            "current_sequence": self._sequence,
            "failures": failures,
            "retention_days": self.retention_days,
        }
