"""OMNI Agent — Conversation State: multi-turn state, compression, branching, and replay."""
from __future__ import annotations
import json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class Role(str, Enum):
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"
    TOOL      = "tool"
    FUNCTION  = "function"


class TurnStatus(str, Enum):
    ACTIVE    = "active"
    EDITED    = "edited"
    DELETED   = "deleted"
    BRANCHED  = "branched"


@dataclass
class Turn:
    turn_id: str
    role: Role
    content: str
    turn_index: int
    status: TurnStatus = TurnStatus.ACTIVE
    model_id: str = ""
    tokens: int = 0
    latency_ms: float = 0.0
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    branch_id: str = "main"

    @property
    def word_count(self) -> int:
        return len(self.content.split())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "role": self.role.value,
            "content": self.content,
            "turn_index": self.turn_index,
            "status": self.status.value,
            "model_id": self.model_id,
            "tokens": self.tokens,
            "branch_id": self.branch_id,
        }

    def to_message(self) -> Dict[str, str]:
        """OpenAI-compatible message dict."""
        return {"role": self.role.value, "content": self.content}


@dataclass
class Branch:
    branch_id: str
    parent_branch: str = "main"
    fork_at_index: int = 0
    created_at: float = field(default_factory=time.time)
    label: str = ""


class ConversationState:
    """
    Full conversation state manager:
    - Multi-turn message history with roles
    - Named branches (fork conversations at any turn)
    - Token budget tracking
    - History compression with custom summarizer
    - Edit/delete turns
    - Export to OpenAI format
    - SQLite persistence
    """

    def __init__(
        self,
        conversation_id: Optional[str] = None,
        system_prompt: str = "",
        max_turns: int = 100,
        max_tokens: int = 4096,
        db_path: str = ":memory:",
    ):
        self.conversation_id = conversation_id or str(uuid.uuid4())[:12]
        self.system_prompt   = system_prompt
        self.max_turns       = max_turns
        self.max_tokens      = max_tokens
        self._turns: Dict[str, List[Turn]] = {"main": []}   # branch_id → turns
        self._branches: Dict[str, Branch] = {
            "main": Branch(branch_id="main")}
        self._active_branch = "main"
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._total_tokens = 0
        self._compress_hooks: List[Callable] = []

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS cs_turns (
                turn_id TEXT PRIMARY KEY, conversation_id TEXT,
                branch_id TEXT, role TEXT, content TEXT,
                turn_index INTEGER, status TEXT, model_id TEXT,
                tokens INTEGER, ts REAL
            );
            CREATE TABLE IF NOT EXISTS cs_branches (
                branch_id TEXT PRIMARY KEY, conversation_id TEXT,
                parent TEXT, fork_at INTEGER, created_at REAL, label TEXT
            );
        """)
        self._db.commit()

    # ── TURNS ─────────────────────────────────────────────────────────

    def add_turn(self, role: Role, content: str,
                 model_id: str = "",
                 tokens: int = 0,
                 latency_ms: float = 0.0,
                 metadata: Optional[Dict] = None,
                 branch_id: Optional[str] = None) -> Turn:
        bid = branch_id or self._active_branch
        branch_turns = self._turns.setdefault(bid, [])
        idx = len(branch_turns)
        turn = Turn(
            turn_id=str(uuid.uuid4()),
            role=role, content=content,
            turn_index=idx, model_id=model_id,
            tokens=tokens, latency_ms=latency_ms,
            metadata=metadata or {}, branch_id=bid)
        branch_turns.append(turn)
        self._total_tokens += tokens
        self._db.execute(
            "INSERT INTO cs_turns VALUES (?,?,?,?,?,?,?,?,?,?)",
            (turn.turn_id, self.conversation_id, bid, role.value,
             content, idx, TurnStatus.ACTIVE.value, model_id, tokens, turn.ts))
        self._db.commit()
        return turn

    def add_user(self, content: str, **kwargs) -> Turn:
        return self.add_turn(Role.USER, content, **kwargs)

    def add_assistant(self, content: str, **kwargs) -> Turn:
        return self.add_turn(Role.ASSISTANT, content, **kwargs)

    def add_system(self, content: str, **kwargs) -> Turn:
        return self.add_turn(Role.SYSTEM, content, **kwargs)

    def edit_turn(self, turn_id: str, new_content: str) -> bool:
        for branch_turns in self._turns.values():
            for turn in branch_turns:
                if turn.turn_id == turn_id:
                    turn.content = new_content
                    turn.status  = TurnStatus.EDITED
                    self._db.execute(
                        "UPDATE cs_turns SET content=?, status=? WHERE turn_id=?",
                        (new_content, TurnStatus.EDITED.value, turn_id))
                    self._db.commit()
                    return True
        return False

    def delete_turn(self, turn_id: str) -> bool:
        for branch_turns in self._turns.values():
            for turn in branch_turns:
                if turn.turn_id == turn_id:
                    turn.status = TurnStatus.DELETED
                    return True
        return False

    # ── BRANCHING ─────────────────────────────────────────────────────

    def fork(self, from_index: int = -1, label: str = "",
             branch_id: Optional[str] = None) -> str:
        """Fork current branch at turn index, return new branch_id."""
        new_bid = branch_id or str(uuid.uuid4())[:8]
        parent_turns = self.active_turns()
        if from_index == -1:
            from_index = len(parent_turns)
        # Copy turns up to fork point
        forked = [Turn(**{**t.__dict__})
                  for t in parent_turns[:from_index]]
        for t in forked:
            t.branch_id = new_bid
        self._turns[new_bid] = forked
        branch = Branch(branch_id=new_bid,
                        parent_branch=self._active_branch,
                        fork_at_index=from_index,
                        label=label)
        self._branches[new_bid] = branch
        self._db.execute(
            "INSERT INTO cs_branches VALUES (?,?,?,?,?,?)",
            (new_bid, self.conversation_id, self._active_branch,
             from_index, branch.created_at, label))
        self._db.commit()
        return new_bid

    def switch_branch(self, branch_id: str):
        if branch_id not in self._branches:
            raise KeyError(f"Branch '{branch_id}' not found")
        self._active_branch = branch_id

    def merge_branch(self, branch_id: str) -> int:
        """Append unique turns from branch to main (naive merge)."""
        main_ids = {t.turn_id for t in self._turns.get("main", [])}
        added = 0
        for turn in self._turns.get(branch_id, []):
            if turn.turn_id not in main_ids:
                turn.branch_id = "main"
                self._turns["main"].append(turn)
                added += 1
        return added

    # ── COMPRESSION ───────────────────────────────────────────────────

    def compress(self, summarize_fn: Optional[Callable[[List[Turn]], str]] = None,
                 keep_last_n: int = 10) -> str:
        """Compress old turns into a summary, keep last N turns intact."""
        turns = self.active_turns()
        if len(turns) <= keep_last_n:
            return ""
        old_turns = [t for t in turns[:-keep_last_n]
                     if t.status == TurnStatus.ACTIVE]
        if not old_turns:
            return ""
        if summarize_fn:
            summary = summarize_fn(old_turns)
        else:
            summary = f"[Summary of {len(old_turns)} earlier turns covering: " + \
                      "; ".join(t.content[:30] for t in old_turns[:3]) + "...]"
        # Delete old turns
        for t in old_turns:
            t.status = TurnStatus.DELETED
        # Insert summary as system turn
        self.add_system(summary, metadata={"compressed": True})
        for fn in self._compress_hooks:
            try: fn(summary, old_turns)
            except Exception: pass
        return summary

    def on_compress(self, fn: Callable):
        self._compress_hooks.append(fn)

    # ── QUERY ─────────────────────────────────────────────────────────

    def active_turns(self, branch_id: Optional[str] = None) -> List[Turn]:
        bid = branch_id or self._active_branch
        return [t for t in self._turns.get(bid, [])
                if t.status != TurnStatus.DELETED]

    def get_turn(self, turn_id: str) -> Optional[Turn]:
        for branch_turns in self._turns.values():
            for t in branch_turns:
                if t.turn_id == turn_id:
                    return t
        return None

    def last_turn(self, role: Optional[Role] = None) -> Optional[Turn]:
        turns = self.active_turns()
        if role:
            turns = [t for t in turns if t.role == role]
        return turns[-1] if turns else None

    def search(self, query: str, case_sensitive: bool = False) -> List[Turn]:
        q = query if case_sensitive else query.lower()
        results = []
        for t in self.active_turns():
            c = t.content if case_sensitive else t.content.lower()
            if q in c:
                results.append(t)
        return results

    # ── EXPORT ────────────────────────────────────────────────────────

    def to_messages(self, branch_id: Optional[str] = None,
                    include_system: bool = True) -> List[Dict[str, str]]:
        """Export as OpenAI-compatible messages list."""
        msgs = []
        if include_system and self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        for t in self.active_turns(branch_id):
            if t.role == Role.SYSTEM and not include_system:
                continue
            msgs.append(t.to_message())
        return msgs

    def to_text(self, branch_id: Optional[str] = None) -> str:
        lines = []
        for t in self.active_turns(branch_id):
            lines.append(f"{t.role.value.upper()}: {t.content}")
        return "\n".join(lines)

    def token_count(self, branch_id: Optional[str] = None) -> int:
        return sum(t.tokens for t in self.active_turns(branch_id))

    def list_branches(self) -> List[Dict[str, Any]]:
        return [{"branch_id": b.branch_id, "label": b.label,
                 "parent": b.parent_branch,
                 "turn_count": len(self._turns.get(b.branch_id, []))}
                for b in self._branches.values()]

    def stats(self) -> Dict[str, Any]:
        active = self.active_turns()
        return {
            "conversation_id": self.conversation_id,
            "active_branch": self._active_branch,
            "total_turns": len(active),
            "total_tokens": self.token_count(),
            "branches": len(self._branches),
            "roles": {r.value: sum(1 for t in active if t.role == r)
                      for r in Role},
        }
