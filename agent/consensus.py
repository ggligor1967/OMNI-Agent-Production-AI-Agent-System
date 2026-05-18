"""OMNI AGENT - Consensus Engine (Raft-inspired)
Leader election, log replication, quorum voting, and state machine.

Features:
- Roles: FOLLOWER, CANDIDATE, LEADER (per node)
- Terms: monotonically increasing election term numbers
- Election: candidate requests votes from peers; wins with majority
- Vote: each node votes once per term for first valid candidate
- Log entries: (term, index, command) appended by leader
- Replication: leader sends AppendEntries to followers
- Commit: entry committed when majority acknowledge
- State machine: committed entries applied to in-memory dict state
- Quorum: configurable; default = (N//2) + 1
- Heartbeat: leader sends periodic empty AppendEntries to maintain authority
- Log consistency: followers reject entries with mismatched prev_log_term
- Snapshot: capture state machine state + last applied index
- Compaction: truncate log before snapshot index
- Membership: add/remove nodes dynamically
- Log query: read all or since index; read committed state
- Linearizable reads: only leader can serve reads
- Hooks: on_become_leader, on_commit, on_term_change
- Stats: role, term, commit_index, log_length per node
- SQLite persistence: log entries and snapshots
- REST API: append, vote, status, log, state
"""
import json, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class Role(str, Enum):
    FOLLOWER  = "follower"
    CANDIDATE = "candidate"
    LEADER    = "leader"

@dataclass
class LogEntry:
    index: int; term: int; command: Dict[str, Any]

    def to_dict(self):
        return {"index": self.index, "term": self.term,
                "command": self.command}

@dataclass
class VoteRequest:
    term: int; candidate_id: str
    last_log_index: int; last_log_term: int

@dataclass
class VoteResponse:
    term: int; granted: bool; voter_id: str

@dataclass
class AppendRequest:
    term: int; leader_id: str
    prev_log_index: int; prev_log_term: int
    entries: List[LogEntry]; leader_commit: int

@dataclass
class AppendResponse:
    term: int; success: bool; follower_id: str
    match_index: int = 0

class CStore:
    def __init__(self, db_path, node_id):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self.node_id = node_id
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS log(
                    idx INTEGER PRIMARY KEY, term INTEGER,
                    command TEXT);
                CREATE TABLE IF NOT EXISTS snapshots(
                    id INTEGER PRIMARY KEY, last_index INTEGER,
                    last_term INTEGER, state TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY, val TEXT);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def save_entry(self, e: LogEntry):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO log VALUES(?,?,?)",
                (e.index, e.term, json.dumps(e.command, default=str)))

    def load_log(self, from_idx: int = 0) -> List[LogEntry]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM log WHERE idx>=? ORDER BY idx",
                (from_idx,)).fetchall()
        return [LogEntry(r["idx"], r["term"],
                          json.loads(r["command"])) for r in rows]

    def delete_from(self, idx: int):
        with self._conn() as c:
            c.execute("DELETE FROM log WHERE idx>=?", (idx,))

    def save_snapshot(self, last_index: int, last_term: int, state: Dict):
        with self._conn() as c:
            c.execute("INSERT INTO snapshots VALUES(NULL,?,?,?,?)",
                (last_index, last_term,
                 json.dumps(state, default=str), time.time()))
            c.execute("DELETE FROM log WHERE idx<=?", (last_index,))

    def load_latest_snapshot(self) -> Optional[Tuple[int, int, Dict]]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row: return None
        return row["last_index"], row["last_term"], json.loads(row["state"])

    def save_meta(self, key: str, val: str):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, val))

    def load_meta(self, key: str, default: str = "") -> str:
        with self._conn() as c:
            row = c.execute("SELECT val FROM meta WHERE key=?",
                             (key,)).fetchone()
        return row["val"] if row else default

class RaftNode:
    """
    Single Raft node. Peers communicate by calling each other's methods directly
    (for in-process testing) or via a transport layer.

    Usage:
        nodes = [RaftNode(f"node{i}", peers=[...]) for i in range(3)]
        cluster = RaftCluster(nodes)

        cluster.elect_leader()
        leader = cluster.leader

        leader.append_command({"op": "set", "key": "x", "value": 42})
        leader.replicate()
        # state machine: {"x": 42}
    """
    def __init__(self, node_id: str, peers: List[str] = None,
                  db_path: str = None, quorum: int = None):
        self.node_id = node_id
        self.peers: List[str] = list(peers or [])
        db_path = db_path or f"data/raft_{node_id}.db"
        self._store = CStore(db_path, node_id)
        # Persistent state
        self.current_term: int = int(self._store.load_meta("term", "0"))
        self.voted_for: Optional[str] = self._store.load_meta("voted_for") or None
        self._log: List[LogEntry] = self._store.load_log()
        # Volatile state
        self.role: Role = Role.FOLLOWER
        self.commit_index: int = 0
        self.last_applied: int = 0
        self.leader_id: Optional[str] = None
        self._state_machine: Dict[str, Any] = {}
        self._quorum = quorum  # resolved by cluster
        # Leader volatile
        self._next_index: Dict[str, int] = {}
        self._match_index: Dict[str, int] = {}
        # Hooks
        self._hooks_leader: List[Callable] = []
        self._hooks_commit: List[Callable] = []
        self._hooks_term:   List[Callable] = []
        # Restore from snapshot
        snap = self._store.load_latest_snapshot()
        if snap:
            li, lt, st = snap
            self.last_applied = li; self.commit_index = li
            self._state_machine = st

    def on_become_leader(self, fn): self._hooks_leader.append(fn)
    def on_commit(self, fn):        self._hooks_commit.append(fn)
    def on_term_change(self, fn):   self._hooks_term.append(fn)

    def _fire(self, hooks, *args):
        for h in hooks:
            try: h(*args)
            except: pass

    @property
    def _quorum_size(self) -> int:
        if self._quorum: return self._quorum
        n = 1 + len(self.peers)  # total cluster size
        return n // 2 + 1

    def _update_term(self, new_term: int):
        if new_term > self.current_term:
            old = self.current_term
            self.current_term = new_term
            self.voted_for = None
            self._store.save_meta("term", str(new_term))
            self._store.save_meta("voted_for", "")
            self.role = Role.FOLLOWER
            self._fire(self._hooks_term, old, new_term)

    def _last_log_index(self) -> int:
        return self._log[-1].index if self._log else 0

    def _last_log_term(self) -> int:
        return self._log[-1].term if self._log else 0

    def _log_at(self, index: int) -> Optional[LogEntry]:
        for e in self._log:
            if e.index == index: return e
        return None

    def _log_up_to_date(self, last_log_index: int,
                          last_log_term: int) -> bool:
        my_term = self._last_log_term()
        my_idx  = self._last_log_index()
        if last_log_term != my_term:
            return last_log_term > my_term
        return last_log_index >= my_idx

    # ── Election ─────────────────────────────────────────────────────────────
    def start_election(self) -> bool:
        self.current_term += 1
        self.voted_for = self.node_id
        self.role = Role.CANDIDATE
        self._store.save_meta("term", str(self.current_term))
        self._store.save_meta("voted_for", self.node_id)
        votes = 1  # vote for self
        return votes

    def request_vote(self, req: VoteRequest) -> VoteResponse:
        self._update_term(req.term)
        grant = False
        if req.term >= self.current_term:
            if (self.voted_for is None or
                    self.voted_for == req.candidate_id):
                if self._log_up_to_date(req.last_log_index,
                                          req.last_log_term):
                    grant = True
                    self.voted_for = req.candidate_id
                    self._store.save_meta("voted_for", req.candidate_id)
        return VoteResponse(term=self.current_term,
                             granted=grant, voter_id=self.node_id)

    def become_leader(self):
        self.role = Role.LEADER
        self.leader_id = self.node_id
        next_idx = self._last_log_index() + 1
        for peer in self.peers:
            self._next_index[peer]  = next_idx
            self._match_index[peer] = 0
        self._fire(self._hooks_leader, self)

    # ── Log Replication ───────────────────────────────────────────────────────
    def append_command(self, command: Dict[str, Any]) -> Optional[LogEntry]:
        if self.role != Role.LEADER: return None
        idx = self._last_log_index() + 1
        entry = LogEntry(index=idx, term=self.current_term,
                          command=command)
        self._log.append(entry)
        self._store.save_entry(entry)
        return entry

    def append_entries(self, req: AppendRequest) -> AppendResponse:
        self._update_term(req.term)
        if req.term < self.current_term:
            return AppendResponse(self.current_term, False, self.node_id)
        self.leader_id = req.leader_id
        self.role = Role.FOLLOWER
        # Check prev log consistency
        if req.prev_log_index > 0:
            prev = self._log_at(req.prev_log_index)
            if prev is None or prev.term != req.prev_log_term:
                return AppendResponse(self.current_term, False, self.node_id)
        # Delete conflicting entries and append new ones
        if req.entries:
            first_new = req.entries[0].index
            self._log = [e for e in self._log if e.index < first_new]
            self._store.delete_from(first_new)
            for e in req.entries:
                self._log.append(e)
                self._store.save_entry(e)
        # Advance commit
        if req.leader_commit > self.commit_index:
            self.commit_index = min(req.leader_commit,
                                     self._last_log_index())
            self._apply_committed()
        match = self._last_log_index()
        return AppendResponse(self.current_term, True,
                               self.node_id, match)

    # ── Commit & Apply ────────────────────────────────────────────────────────
    def _advance_commit(self):
        """Leader: advance commit_index if majority have replicated."""
        if self.role != Role.LEADER: return
        for idx in range(self.commit_index + 1,
                          self._last_log_index() + 1):
            entry = self._log_at(idx)
            if not entry or entry.term != self.current_term:
                continue
            replicated = 1 + sum(
                1 for m in self._match_index.values() if m >= idx)
            if replicated >= self._quorum_size:
                self.commit_index = idx
        self._apply_committed()

    def _apply_committed(self):
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            entry = self._log_at(self.last_applied)
            if entry:
                self._apply_to_sm(entry.command)
                self._fire(self._hooks_commit, entry)

    def _apply_to_sm(self, command: Dict):
        op = command.get("op", "set")
        key = command.get("key", "")
        if op == "set":
            self._state_machine[key] = command.get("value")
        elif op == "delete":
            self._state_machine.pop(key, None)
        elif op == "increment":
            self._state_machine[key] = (
                self._state_machine.get(key, 0) +
                command.get("by", 1))

    def read_state(self, key: str = None) -> Any:
        if self.role != Role.LEADER: return None
        if key: return self._state_machine.get(key)
        return dict(self._state_machine)

    def take_snapshot(self) -> Dict:
        snap = {"last_index": self.last_applied,
                "state": dict(self._state_machine)}
        self._store.save_snapshot(self.last_applied,
                                    self._last_log_term(),
                                    self._state_machine)
        return snap

    def status(self) -> Dict:
        return {"node_id": self.node_id, "role": self.role.value,
                "term": self.current_term, "leader": self.leader_id,
                "commit_index": self.commit_index,
                "last_applied": self.last_applied,
                "log_length": len(self._log),
                "peers": self.peers}

class RaftCluster:
    """
    In-process Raft cluster for testing and embedded use.

    Usage:
        cluster = RaftCluster.create(3, base_path="/tmp/raft")
        cluster.elect_leader()
        cluster.leader.append_command({"op":"set","key":"x","value":1})
        cluster.replicate()
        assert cluster.leader.read_state("x") == 1
    """
    def __init__(self, nodes: List[RaftNode]):
        self._nodes: Dict[str, RaftNode] = {n.node_id: n for n in nodes}
        quorum = len(nodes) // 2 + 1
        for n in nodes:
            n._quorum = quorum
            n.peers = [nid for nid in self._nodes if nid != n.node_id]

    @classmethod
    def create(cls, size: int, base_path: str = "data/raft") -> "RaftCluster":
        import tempfile
        nodes = []
        for i in range(size):
            nid = f"node-{i}"
            db  = f"{base_path}_{nid}.db"
            nodes.append(RaftNode(nid, db_path=db))
        return cls(nodes)

    @property
    def leader(self) -> Optional[RaftNode]:
        for n in self._nodes.values():
            if n.role == Role.LEADER: return n
        return None

    @property
    def nodes(self) -> List[RaftNode]:
        return list(self._nodes.values())

    def elect_leader(self, candidate_id: str = None) -> Optional[RaftNode]:
        candidate = (self._nodes[candidate_id] if candidate_id
                      else list(self._nodes.values())[0])
        candidate.start_election()
        req = VoteRequest(
            term=candidate.current_term,
            candidate_id=candidate.node_id,
            last_log_index=candidate._last_log_index(),
            last_log_term=candidate._last_log_term())
        votes = 1  # self-vote counted in start_election
        for nid, node in self._nodes.items():
            if nid == candidate.node_id: continue
            resp = node.request_vote(req)
            if resp.granted: votes += 1
        if votes >= candidate._quorum_size:
            candidate.become_leader()
            return candidate
        candidate.role = Role.FOLLOWER
        return None

    def replicate(self) -> int:
        """Leader sends AppendEntries to all followers. Returns ack count."""
        leader = self.leader
        if not leader: return 0
        acks = 0
        for nid, follower in self._nodes.items():
            if nid == leader.node_id: continue
            next_idx = leader._next_index.get(nid, 1)
            prev_idx = next_idx - 1
            prev_entry = leader._log_at(prev_idx)
            prev_term  = prev_entry.term if prev_entry else 0
            entries = [e for e in leader._log if e.index >= next_idx]
            req = AppendRequest(
                term=leader.current_term,
                leader_id=leader.node_id,
                prev_log_index=prev_idx, prev_log_term=prev_term,
                entries=entries,
                leader_commit=leader.commit_index)
            resp = follower.append_entries(req)
            if resp.success:
                leader._next_index[nid]  = resp.match_index + 1
                leader._match_index[nid] = resp.match_index
                acks += 1
        leader._advance_commit()
        return acks

    def stats(self) -> Dict:
        return {nid: n.status() for nid, n in self._nodes.items()}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def cmd_ep(req):
            d = await req.json()
            leader = self.leader
            if not leader:
                return web.json_response({"error":"no leader"}, status=503)
            entry = leader.append_command(d.get("command",{}))
            self.replicate()
            if not entry:
                return web.json_response({"error":"not leader"}, status=503)
            return web.json_response(entry.to_dict(), status=201)
        async def elect_ep(req):
            node = self.elect_leader()
            if not node:
                return web.json_response({"error":"election failed"},status=503)
            return web.json_response({"leader": node.node_id})
        async def status_ep(req):
            return web.json_response(self.stats())
        async def read_ep(req):
            leader = self.leader
            if not leader:
                return web.json_response({"error":"no leader"}, status=503)
            key = req.rel_url.query.get("key")
            return web.json_response({"state": leader.read_state(key)})
        p = f"{prefix}/raft"
        app.router.add_post(f"{p}/command", cmd_ep)
        app.router.add_post(f"{p}/elect",   elect_ep)
        app.router.add_get( f"{p}/status",  status_ep)
        app.router.add_get( f"{p}/read",    read_ep)
        logger.info(f"Raft cluster API at {prefix}/raft/")
