"""OMNI Agent — Session Store V2: distributed sessions with TTL, namespaces and events."""
from __future__ import annotations
import hashlib, json, secrets, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class SessionStatus(str, Enum):
    ACTIVE    = "active"
    EXPIRED   = "expired"
    REVOKED   = "revoked"
    LOCKED    = "locked"


class SessionEvent(str, Enum):
    CREATED    = "created"
    ACCESSED   = "accessed"
    UPDATED    = "updated"
    EXTENDED   = "extended"
    REVOKED    = "revoked"
    EXPIRED    = "expired"
    LOCKED     = "locked"
    UNLOCKED   = "unlocked"


@dataclass
class Session:
    session_id: str
    namespace: str = "default"
    user_id: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    access_count: int = 0
    token: str = field(default_factory=lambda: secrets.token_hex(16))

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    @property
    def ttl_remaining(self) -> Optional[float]:
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.time())

    @property
    def is_active(self) -> bool:
        return (self.status == SessionStatus.ACTIVE and
                not self.is_expired)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "namespace": self.namespace,
            "user_id": self.user_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "ttl_remaining": self.ttl_remaining,
            "access_count": self.access_count,
            "is_active": self.is_active,
        }


@dataclass
class SessionEventRecord:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    session_id: str = ""
    event: SessionEvent = SessionEvent.ACCESSED
    user_id: Optional[str] = None
    ip: Optional[str] = None
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class SessionStoreV2:
    """
    Production-grade session store with:
    - TTL-based expiry with auto-cleanup
    - Namespace isolation
    - Token-based session verification
    - Lock/unlock sessions (for concurrent modification)
    - Event audit trail
    - Per-namespace limits
    - Tag-based bulk operations
    - SQLite persistence
    - Lifecycle hooks
    """

    def __init__(
        self,
        default_ttl_s: float = 3600.0,
        max_sessions_per_ns: int = 10000,
        db_path: str = ":memory:",
        cleanup_interval_s: float = 300.0,
    ):
        self.default_ttl_s       = default_ttl_s
        self.max_sessions_per_ns = max_sessions_per_ns
        self._sessions: Dict[str, Session] = {}
        self._ns_index: Dict[str, set] = {}          # namespace → {session_ids}
        self._user_index: Dict[str, set] = {}        # user_id → {session_ids}
        self._hooks: Dict[SessionEvent, List[Callable]] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._last_cleanup = time.time()
        self._cleanup_interval = cleanup_interval_s
        self._create_count = 0
        self._access_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ss_sessions (
                session_id TEXT PRIMARY KEY, namespace TEXT,
                user_id TEXT, status TEXT, data TEXT,
                created_at REAL, expires_at REAL, token TEXT
            );
            CREATE TABLE IF NOT EXISTS ss_events (
                event_id TEXT PRIMARY KEY, session_id TEXT,
                event TEXT, user_id TEXT, ip TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── LIFECYCLE ─────────────────────────────────────────────────────

    def create(self, user_id: Optional[str] = None,
               data: Optional[Dict] = None,
               namespace: str = "default",
               ttl_s: Optional[float] = None,
               tags: Optional[List[str]] = None,
               metadata: Optional[Dict] = None,
               session_id: Optional[str] = None) -> Session:
        self._maybe_cleanup()
        ns_count = len(self._ns_index.get(namespace, set()))
        if ns_count >= self.max_sessions_per_ns:
            raise RuntimeError(
                f"Namespace '{namespace}' at session limit ({self.max_sessions_per_ns})")

        ttl = ttl_s if ttl_s is not None else self.default_ttl_s
        sid = session_id or str(uuid.uuid4())
        sess = Session(
            session_id=sid,
            namespace=namespace,
            user_id=user_id,
            data=dict(data or {}),
            expires_at=time.time() + ttl if ttl > 0 else None,
            tags=list(tags or []),
            metadata=metadata or {})
        self._sessions[sid] = sess
        self._ns_index.setdefault(namespace, set()).add(sid)
        if user_id:
            self._user_index.setdefault(user_id, set()).add(sid)
        self._create_count += 1
        self._persist(sess)
        self._emit(SessionEvent.CREATED, sess)
        return sess

    def get(self, session_id: str,
            touch: bool = True) -> Optional[Session]:
        self._maybe_cleanup()
        sess = self._sessions.get(session_id)
        if not sess:
            return None
        if sess.is_expired:
            sess.status = SessionStatus.EXPIRED
            self._emit(SessionEvent.EXPIRED, sess)
            return None
        if sess.status != SessionStatus.ACTIVE:
            return None
        if touch:
            sess.accessed_at = time.time()
            sess.access_count += 1
            self._access_count += 1
            self._emit(SessionEvent.ACCESSED, sess)
        return sess

    def verify(self, session_id: str, token: str) -> Optional[Session]:
        """Verify session by token (constant-time compare)."""
        sess = self.get(session_id, touch=False)
        if not sess:
            return None
        if not secrets.compare_digest(sess.token, token):
            return None
        sess.access_count += 1
        return sess

    def update(self, session_id: str,
               data: Optional[Dict] = None,
               merge: bool = True) -> Optional[Session]:
        sess = self.get(session_id, touch=False)
        if not sess or sess.status == SessionStatus.LOCKED:
            return None
        if data:
            if merge:
                sess.data.update(data)
            else:
                sess.data = data
        sess.updated_at = time.time()
        self._persist(sess)
        self._emit(SessionEvent.UPDATED, sess)
        return sess

    def set_key(self, session_id: str, key: str, value: Any) -> bool:
        sess = self.get(session_id, touch=False)
        if not sess:
            return False
        sess.data[key] = value
        sess.updated_at = time.time()
        return True

    def get_key(self, session_id: str, key: str,
                default: Any = None) -> Any:
        sess = self.get(session_id, touch=False)
        if not sess:
            return default
        return sess.data.get(key, default)

    def extend(self, session_id: str,
               by_s: Optional[float] = None) -> Optional[Session]:
        sess = self.get(session_id, touch=False)
        if not sess:
            return None
        delta = by_s if by_s is not None else self.default_ttl_s
        if sess.expires_at:
            sess.expires_at = max(sess.expires_at, time.time()) + delta
        else:
            sess.expires_at = time.time() + delta
        self._persist(sess)
        self._emit(SessionEvent.EXTENDED, sess)
        return sess

    def revoke(self, session_id: str) -> bool:
        sess = self._sessions.get(session_id)
        if not sess:
            return False
        sess.status = SessionStatus.REVOKED
        self._persist(sess)
        self._emit(SessionEvent.REVOKED, sess)
        return True

    def lock(self, session_id: str) -> bool:
        sess = self._sessions.get(session_id)
        if not sess:
            return False
        sess.status = SessionStatus.LOCKED
        self._emit(SessionEvent.LOCKED, sess)
        return True

    def unlock(self, session_id: str) -> bool:
        sess = self._sessions.get(session_id)
        if sess and sess.status == SessionStatus.LOCKED:
            sess.status = SessionStatus.ACTIVE
            self._emit(SessionEvent.UNLOCKED, sess)
            return True
        return False

    def delete(self, session_id: str) -> bool:
        sess = self._sessions.pop(session_id, None)
        if not sess:
            return False
        self._ns_index.get(sess.namespace, set()).discard(session_id)
        if sess.user_id:
            self._user_index.get(sess.user_id, set()).discard(session_id)
        self._db.execute("DELETE FROM ss_sessions WHERE session_id=?",
                         (session_id,))
        self._db.commit()
        return True

    # ── BULK OPERATIONS ───────────────────────────────────────────────

    def revoke_user_sessions(self, user_id: str) -> int:
        sids = list(self._user_index.get(user_id, set()))
        for sid in sids:
            self.revoke(sid)
        return len(sids)

    def revoke_by_tag(self, tag: str,
                      namespace: str = "default") -> int:
        count = 0
        for sid in list(self._ns_index.get(namespace, set())):
            sess = self._sessions.get(sid)
            if sess and tag in sess.tags:
                self.revoke(sid)
                count += 1
        return count

    def cleanup_expired(self) -> int:
        expired = [sid for sid, s in self._sessions.items()
                   if s.is_expired]
        for sid in expired:
            s = self._sessions[sid]
            s.status = SessionStatus.EXPIRED
            self.delete(sid)
        return len(expired)

    def _maybe_cleanup(self):
        if time.time() - self._last_cleanup > self._cleanup_interval:
            self.cleanup_expired()
            self._last_cleanup = time.time()

    # ── QUERY ─────────────────────────────────────────────────────────

    def list_sessions(self, namespace: Optional[str] = None,
                      user_id: Optional[str] = None,
                      active_only: bool = True) -> List[Dict]:
        if user_id:
            sids = self._user_index.get(user_id, set())
            sessions = [self._sessions[s] for s in sids if s in self._sessions]
        elif namespace:
            sids = self._ns_index.get(namespace, set())
            sessions = [self._sessions[s] for s in sids if s in self._sessions]
        else:
            sessions = list(self._sessions.values())
        if active_only:
            sessions = [s for s in sessions if s.is_active]
        return [s.to_dict() for s in sessions]

    def count_active(self, namespace: Optional[str] = None) -> int:
        sessions = list(self._sessions.values())
        if namespace:
            sessions = [s for s in sessions if s.namespace == namespace]
        return sum(1 for s in sessions if s.is_active)

    def event_log(self, session_id: Optional[str] = None,
                  limit: int = 50) -> List[Dict]:
        q = ("SELECT event_id,session_id,event,user_id,ts "
             "FROM ss_events")
        params: List[Any] = []
        if session_id:
            q += " WHERE session_id=?"; params.append(session_id)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(q, params).fetchall()
        return [{"id": r[0], "session": r[1], "event": r[2],
                 "user": r[3], "ts": r[4]} for r in rows]

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_event(self, event: SessionEvent, fn: Callable):
        self._hooks.setdefault(event, []).append(fn)

    def _emit(self, event: SessionEvent, sess: Session):
        rec = SessionEventRecord(session_id=sess.session_id,
                                 event=event, user_id=sess.user_id)
        self._db.execute(
            "INSERT INTO ss_events VALUES (?,?,?,?,?,?)",
            (rec.event_id, rec.session_id, event.value,
             rec.user_id, None, rec.ts))
        self._db.commit()
        for fn in self._hooks.get(event, []):
            try: fn(sess)
            except Exception: pass

    def _persist(self, sess: Session):
        self._db.execute(
            "INSERT OR REPLACE INTO ss_sessions VALUES (?,?,?,?,?,?,?,?)",
            (sess.session_id, sess.namespace, sess.user_id,
             sess.status.value, json.dumps(sess.data),
             sess.created_at, sess.expires_at, sess.token))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        active = sum(1 for s in self._sessions.values() if s.is_active)
        return {
            "total_sessions": len(self._sessions),
            "active": active,
            "namespaces": len(self._ns_index),
            "created": self._create_count,
            "accessed": self._access_count,
        }
