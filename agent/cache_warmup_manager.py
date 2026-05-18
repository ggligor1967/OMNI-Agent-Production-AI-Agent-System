"""OMNI Agent — Cache Warmup Manager: intelligent pre-warming with priority scheduling."""
from __future__ import annotations
import sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class WarmupStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    SKIPPED   = "skipped"
    SCHEDULED = "scheduled"


class WarmupPriority(int, Enum):
    CRITICAL = 0
    HIGH     = 1
    NORMAL   = 2
    LOW      = 3


@dataclass
class WarmupEntry:
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    key: str = ""
    loader: Callable = field(default=lambda: None)
    priority: WarmupPriority = WarmupPriority.NORMAL
    ttl_s: Optional[float] = None
    status: WarmupStatus = WarmupStatus.PENDING
    value: Any = None
    loaded_at: Optional[float] = None
    expires_at: Optional[float] = None
    error: Optional[str] = None
    load_count: int = 0
    hit_count: int = 0
    miss_count: int = 0
    tags: List[str] = field(default_factory=list)
    schedule_at: Optional[float] = None   # run at this time
    refresh_interval_s: Optional[float] = None  # periodic refresh
    next_refresh_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return (self.expires_at is not None
                and time.time() > self.expires_at)

    @property
    def hit_rate(self) -> float:
        total = self.hit_count + self.miss_count
        return self.hit_count / total if total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "key": self.key,
            "status": self.status.value,
            "priority": self.priority.value,
            "loaded_at": self.loaded_at,
            "expires_at": self.expires_at,
            "hit_rate": round(self.hit_rate, 3),
            "load_count": self.load_count,
        }


@dataclass
class WarmupRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    total: int = 0
    loaded: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "total": self.total,
            "loaded": self.loaded,
            "failed": self.failed,
            "duration_ms": round(self.duration_ms, 2),
        }


class CacheWarmupManager:
    """
    Intelligent cache pre-warming:
    - Register cache entries with loader functions and priorities
    - Priority-ordered warmup (CRITICAL → LOW)
    - Scheduled warmup (run at specific time)
    - Periodic refresh (TTL-based re-warm)
    - Background warmup thread
    - Per-entry TTL management
    - Hit/miss tracking per key
    - Bulk warmup with concurrency control
    - Tag-based grouping and selective warmup
    - Eviction by TTL or explicit
    - Warmup run history
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:",
                 max_workers: int = 4):
        self._entries:  Dict[str, WarmupEntry] = {}
        self._runs:     List[WarmupRun] = []
        self._running   = False
        self._thread:   Optional[threading.Thread] = None
        self._max_workers = max_workers
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS cw_entries (
                entry_id TEXT PRIMARY KEY, key_name TEXT, priority INTEGER,
                status TEXT, loaded_at REAL, expires_at REAL,
                load_count INTEGER, hit_count INTEGER
            );
            CREATE TABLE IF NOT EXISTS cw_runs (
                run_id TEXT PRIMARY KEY, started_at REAL,
                finished_at REAL, total INTEGER,
                loaded INTEGER, failed INTEGER
            );
        """)
        self._db.commit()

    # ── REGISTRATION ─────────────────────────────────────────────────

    def register(self, key: str,
                  loader: Callable[[], Any],
                  priority: WarmupPriority = WarmupPriority.NORMAL,
                  ttl_s: Optional[float] = None,
                  tags: Optional[List[str]] = None,
                  schedule_at: Optional[float] = None,
                  refresh_interval_s: Optional[float] = None,
                  entry_id: Optional[str] = None,
                  metadata: Optional[Dict] = None) -> WarmupEntry:
        eid = entry_id or str(uuid.uuid4())[:8]
        e   = WarmupEntry(
            entry_id=eid, key=key, loader=loader,
            priority=priority, ttl_s=ttl_s,
            tags=list(tags or []),
            schedule_at=schedule_at,
            refresh_interval_s=refresh_interval_s,
            metadata=metadata or {})
        self._entries[eid] = e
        return e

    def unregister(self, entry_id: str) -> bool:
        return self._entries.pop(entry_id, None) is not None

    def find(self, key: str) -> Optional[WarmupEntry]:
        return next((e for e in self._entries.values()
                     if e.key == key), None)

    # ── LOADING ──────────────────────────────────────────────────────

    def warm(self, entry_id: str, force: bool = False) -> bool:
        e = self._entries.get(entry_id)
        if not e: return False
        if not force and e.status == WarmupStatus.DONE and not e.is_expired:
            e.hit_count += 1
            return True
        e.miss_count += 1
        e.status = WarmupStatus.RUNNING
        try:
            val = e.loader()
            e.value      = val
            e.status     = WarmupStatus.DONE
            e.loaded_at  = time.time()
            e.load_count += 1
            if e.ttl_s:
                e.expires_at = time.time() + e.ttl_s
            if e.refresh_interval_s:
                e.next_refresh_at = time.time() + e.refresh_interval_s
            self._persist_entry(e)
            return True
        except Exception as exc:
            e.error  = str(exc)
            e.status = WarmupStatus.FAILED
            self._persist_entry(e)
            return False

    def get(self, key: str) -> Any:
        e = self.find(key)
        if not e: return None
        if e.is_expired:
            self.warm(e.entry_id, force=True)
        if e.status == WarmupStatus.DONE:
            e.hit_count += 1
            return e.value
        e.miss_count += 1
        return None

    def get_or_load(self, key: str) -> Any:
        e = self.find(key)
        if not e: return None
        if e.status != WarmupStatus.DONE or e.is_expired:
            self.warm(e.entry_id, force=True)
        return e.value

    def invalidate(self, key: str) -> bool:
        e = self.find(key)
        if not e: return False
        e.status    = WarmupStatus.PENDING
        e.value     = None
        e.loaded_at = None
        e.expires_at = None
        return True

    def evict_expired(self) -> int:
        evicted = 0
        for e in list(self._entries.values()):
            if e.is_expired:
                e.status = WarmupStatus.PENDING
                e.value  = None
                evicted += 1
        return evicted

    # ── BULK WARMUP ──────────────────────────────────────────────────

    def warm_all(self, tag: Optional[str] = None,
                  priority: Optional[WarmupPriority] = None,
                  force: bool = False) -> WarmupRun:
        entries = list(self._entries.values())
        if tag:      entries = [e for e in entries if tag in e.tags]
        if priority: entries = [e for e in entries if e.priority == priority]
        # Sort by priority
        entries.sort(key=lambda e: e.priority.value)

        run = WarmupRun(total=len(entries))
        results: Dict[str, bool] = {}
        lock = threading.Lock()

        def _warm_entry(e):
            ok = self.warm(e.entry_id, force)
            with lock:
                if ok: run.loaded += 1
                else:  run.failed += 1

        # Run in batches of max_workers
        i = 0
        while i < len(entries):
            batch   = entries[i:i + self._max_workers]
            threads = [threading.Thread(target=_warm_entry, args=(e,),
                                        daemon=True) for e in batch]
            for t in threads: t.start()
            for t in threads: t.join()
            i += len(batch)

        run.finished_at = time.time()
        self._runs.append(run)
        self._persist_run(run)
        return run

    def warm_by_priority(self, priority: WarmupPriority,
                          **kwargs) -> WarmupRun:
        return self.warm_all(priority=priority, **kwargs)

    # ── BACKGROUND THREAD ─────────────────────────────────────────────

    def start(self, check_interval_s: float = 30.0):
        if self._running: return
        self._running = True
        self._thread = threading.Thread(
            target=self._background_loop,
            args=(check_interval_s,), daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _background_loop(self, interval: float):
        while self._running:
            now = time.time()
            for e in list(self._entries.values()):
                # Scheduled warmup
                if (e.schedule_at and e.status == WarmupStatus.SCHEDULED
                        and now >= e.schedule_at):
                    self.warm(e.entry_id)
                # Periodic refresh
                if (e.next_refresh_at and now >= e.next_refresh_at
                        and e.status == WarmupStatus.DONE):
                    self.warm(e.entry_id, force=True)
                # Expired
                if e.is_expired and e.refresh_interval_s:
                    self.warm(e.entry_id, force=True)
            time.sleep(interval)

    # ── STATS ─────────────────────────────────────────────────────────

    def list_entries(self, tag: Optional[str] = None,
                      status: Optional[WarmupStatus] = None) -> List[Dict]:
        entries = list(self._entries.values())
        if tag:    entries = [e for e in entries if tag in e.tags]
        if status: entries = [e for e in entries if e.status == status]
        return [e.to_dict() for e in entries]

    def run_history(self, limit: int = 20) -> List[Dict]:
        return [r.to_dict() for r in self._runs[-limit:]]

    def _persist_entry(self, e: WarmupEntry):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cw_entries VALUES (?,?,?,?,?,?,?,?)",
                (e.entry_id, e.key, e.priority.value,
                 e.status.value, e.loaded_at, e.expires_at,
                 e.load_count, e.hit_count))
            self._db.commit()

    def _persist_run(self, r: WarmupRun):
        self._db.execute(
            "INSERT OR REPLACE INTO cw_runs VALUES (?,?,?,?,?,?)",
            (r.run_id, r.started_at, r.finished_at,
             r.total, r.loaded, r.failed))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        entries = list(self._entries.values())
        done  = sum(1 for e in entries if e.status == WarmupStatus.DONE)
        total_hits   = sum(e.hit_count  for e in entries)
        total_misses = sum(e.miss_count for e in entries)
        return {
            "total": len(entries),
            "done": done,
            "pending": sum(1 for e in entries
                           if e.status == WarmupStatus.PENDING),
            "failed": sum(1 for e in entries
                          if e.status == WarmupStatus.FAILED),
            "total_hits": total_hits,
            "total_misses": total_misses,
            "warmup_runs": len(self._runs),
        }
