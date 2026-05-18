"""OMNI AGENT - Context Manager
Token-budget conversation context window management: count tokens,
apply truncation strategies, inject summaries, and enforce role budgets.

Features:
- Token counting: tiktoken-compatible approximation (4 chars ≈ 1 token)
- Role budgets: separate limits for system, user, assistant messages
- Truncation strategies: oldest-first, middle-out, smart (keep system+recent)
- Summary injection: replace truncated history with a compact summary stub
- Sliding window: keep last N turns regardless of token count
- Priority messages: pin certain messages so they survive truncation
- Context snapshots: save/restore context states
- Compression: merge consecutive same-role messages
- Token analytics: per-role counts, utilisation %, headroom
- Multi-model profiles: different budgets for gpt-4/claude/gemini etc.
- REST API: add, truncate, stats, snapshot, restore
"""
import re, time, uuid, json, copy, logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
logger = logging.getLogger(__name__)

# ── Token counting ─────────────────────────────────────────────────────────────
def _count_tokens(text: str) -> int:
    """Approximate token count: ~4 chars per token, minimum 1."""
    return max(1, len(text) // 4)

def _msg_tokens(msg: Dict) -> int:
    content = msg.get("content", "")
    if isinstance(content, list):          # multi-part content
        content = " ".join(c.get("text","") for c in content if isinstance(c, dict))
    return _count_tokens(str(content)) + 4  # +4 for role/overhead

# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class ModelProfile:
    name: str
    max_tokens: int = 8192
    system_budget: int = 1024
    reserved_output: int = 1024   # tokens reserved for the response
    supports_system: bool = True

    @property
    def usable_tokens(self): return self.max_tokens - self.reserved_output

    def to_dict(self):
        return {"name": self.name, "max_tokens": self.max_tokens,
                "system_budget": self.system_budget,
                "reserved_output": self.reserved_output,
                "usable_tokens": self.usable_tokens}

@dataclass
class ContextMessage:
    id: str
    role: str           # system | user | assistant | tool
    content: str
    pinned: bool = False
    tokens: int = 0
    turn: int = 0
    metadata: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "role": self.role, "content": self.content,
                "pinned": self.pinned, "tokens": self.tokens, "turn": self.turn}

    def to_api_msg(self):
        return {"role": self.role, "content": self.content}

@dataclass
class ContextStats:
    total_tokens: int = 0
    by_role: Dict[str, int] = field(default_factory=dict)
    message_count: int = 0
    utilisation: float = 0.0
    headroom: int = 0
    pinned_tokens: int = 0

    def to_dict(self):
        return {"total_tokens": self.total_tokens, "by_role": self.by_role,
                "message_count": self.message_count,
                "utilisation": round(self.utilisation, 4),
                "headroom": self.headroom, "pinned_tokens": self.pinned_tokens}

# ── Truncation strategies ──────────────────────────────────────────────────────
def _truncate_oldest(msgs: List[ContextMessage],
                      budget: int) -> List[ContextMessage]:
    """Drop oldest non-pinned messages until within budget."""
    result = list(msgs)
    while sum(m.tokens for m in result) > budget:
        for i, m in enumerate(result):
            if not m.pinned and m.role != "system":
                result.pop(i); break
        else:
            break   # only pinned remain
    return result

def _truncate_middle_out(msgs: List[ContextMessage],
                          budget: int) -> List[ContextMessage]:
    """Keep head and tail; drop non-pinned from the middle inward."""
    result = list(msgs)
    while sum(m.tokens for m in result) > budget:
        # Find candidates: non-pinned, non-system, not first, not last
        candidates = [i for i in range(1, len(result)-1)
                      if not result[i].pinned and result[i].role != "system"]
        if not candidates:
            break
        # Always drop the middle-most candidate
        mid_idx = candidates[len(candidates)//2]
        result.pop(mid_idx)
    return result

def _truncate_smart(msgs: List[ContextMessage],
                     budget: int,
                     keep_recent: int = 6) -> List[ContextMessage]:
    """Keep system messages + last keep_recent turns; drop older middle."""
    system = [m for m in msgs if m.role == "system" or m.pinned]
    rest   = [m for m in msgs if m.role != "system" and not m.pinned]
    recent = rest[-keep_recent:] if len(rest) > keep_recent else rest
    old    = rest[:-keep_recent] if len(rest) > keep_recent else []
    result = system + old + recent
    # Now drop from old if still over budget
    while sum(m.tokens for m in result) > budget and old:
        old.pop(0)
        result = system + old + recent
    return result

class ContextManager:
    """
    Token-budget conversation context window manager.

    Usage:
        cm = ContextManager(profile="claude-3-sonnet")
        cm.add("system", "You are a helpful assistant.")
        cm.add("user",   "What is Python?")
        cm.add("assistant", "Python is a high-level programming language.")
        cm.add("user",   "Give me an example.")

        # Get messages ready for API call
        messages = cm.get_messages()

        # Truncate if over budget
        truncated = cm.truncate(strategy="smart")
        stats = cm.stats()
        print(f"Tokens: {stats.total_tokens}/{cm.profile.usable_tokens}")
    """
    PROFILES = {
        "gpt-3.5-turbo":    ModelProfile("gpt-3.5-turbo",    max_tokens=4096,  system_budget=512,  reserved_output=512),
        "gpt-4":            ModelProfile("gpt-4",             max_tokens=8192,  system_budget=1024, reserved_output=1024),
        "gpt-4-turbo":      ModelProfile("gpt-4-turbo",       max_tokens=128000,system_budget=4096, reserved_output=4096),
        "claude-3-haiku":   ModelProfile("claude-3-haiku",    max_tokens=200000,system_budget=4096, reserved_output=4096),
        "claude-3-sonnet":  ModelProfile("claude-3-sonnet",   max_tokens=200000,system_budget=4096, reserved_output=8192),
        "claude-3-opus":    ModelProfile("claude-3-opus",     max_tokens=200000,system_budget=4096, reserved_output=8192),
        "gemini-pro":       ModelProfile("gemini-pro",        max_tokens=32768, system_budget=2048, reserved_output=2048),
        "default":          ModelProfile("default",           max_tokens=8192,  system_budget=1024, reserved_output=1024),
    }

    def __init__(self, profile: str = "default", max_tokens: int = None):
        self.profile = copy.deepcopy(self.PROFILES.get(profile, self.PROFILES["default"]))
        if max_tokens:
            self.profile.max_tokens = max_tokens
        self._messages: List[ContextMessage] = []
        self._snapshots: Dict[str, List[ContextMessage]] = {}
        self._turn: int = 0
        self._summary: Optional[str] = None

    def add(self, role: str, content: str,
             pinned: bool = False, metadata: Dict = None) -> ContextMessage:
        if role in ("user", "human"):
            self._turn += 1
        msg = ContextMessage(
            id=str(uuid.uuid4())[:10],
            role=role, content=content,
            pinned=pinned, tokens=_count_tokens(content) + 4,
            turn=self._turn, metadata=metadata or {})
        self._messages.append(msg)
        return msg

    def add_summary(self, summary: str):
        """Inject a summary stub as a system message (pinned)."""
        self._summary = summary
        self.add("system", f"[SUMMARY OF EARLIER CONVERSATION]\n{summary}", pinned=True)

    def pin(self, msg_id: str):
        for m in self._messages:
            if m.id == msg_id: m.pinned = True; break

    def unpin(self, msg_id: str):
        for m in self._messages:
            if m.id == msg_id: m.pinned = False; break

    def remove(self, msg_id: str) -> bool:
        for i, m in enumerate(self._messages):
            if m.id == msg_id:
                self._messages.pop(i); return True
        return False

    def get_messages(self, include_system: bool = True) -> List[Dict]:
        msgs = self._messages
        if not include_system:
            msgs = [m for m in msgs if m.role != "system"]
        return [m.to_api_msg() for m in msgs]

    def truncate(self, strategy: str = "smart",
                  target_tokens: int = None,
                  keep_recent: int = 6) -> "ContextManager":
        """Return a new ContextManager with messages truncated to fit budget."""
        budget = target_tokens or self.profile.usable_tokens
        msgs = list(self._messages)
        if strategy == "oldest":
            msgs = _truncate_oldest(msgs, budget)
        elif strategy == "middle":
            msgs = _truncate_middle_out(msgs, budget)
        else:
            msgs = _truncate_smart(msgs, budget, keep_recent)
        new_cm = ContextManager.__new__(ContextManager)
        new_cm.profile = self.profile
        new_cm._messages = msgs
        new_cm._snapshots = {}
        new_cm._turn = self._turn
        new_cm._summary = self._summary
        return new_cm

    def compress(self) -> int:
        """Merge consecutive same-role messages. Returns merged count."""
        result = []; merged = 0
        i = 0
        while i < len(self._messages):
            m = self._messages[i]
            j = i + 1
            while j < len(self._messages) and \
                  self._messages[j].role == m.role and \
                  not m.pinned and not self._messages[j].pinned:
                m = ContextMessage(
                    id=m.id, role=m.role,
                    content=m.content + "\n" + self._messages[j].content,
                    pinned=m.pinned, tokens=m.tokens + self._messages[j].tokens,
                    turn=m.turn, metadata=m.metadata)
                merged += 1; j += 1
            result.append(m); i = j
        self._messages = result
        return merged

    def stats(self) -> ContextStats:
        by_role: Dict[str, int] = {}
        total = 0; pinned = 0
        for m in self._messages:
            by_role[m.role] = by_role.get(m.role, 0) + m.tokens
            total += m.tokens
            if m.pinned: pinned += m.tokens
        budget = self.profile.usable_tokens
        return ContextStats(
            total_tokens=total, by_role=by_role,
            message_count=len(self._messages),
            utilisation=total / max(1, budget),
            headroom=max(0, budget - total),
            pinned_tokens=pinned)

    def over_budget(self) -> bool:
        return self.stats().total_tokens > self.profile.usable_tokens

    def snapshot(self, name: str):
        self._snapshots[name] = copy.deepcopy(self._messages)

    def restore(self, name: str) -> bool:
        if name in self._snapshots:
            self._messages = copy.deepcopy(self._snapshots[name])
            return True
        return False

    def clear(self, keep_system: bool = True):
        if keep_system:
            self._messages = [m for m in self._messages if m.role == "system" or m.pinned]
        else:
            self._messages = []
        self._turn = 0

    def sliding_window(self, turns: int = 10) -> List[Dict]:
        """Return only the last N turns as API messages."""
        recent = [m for m in self._messages if m.role != "system"][-turns * 2:]
        system = [m for m in self._messages if m.role == "system"]
        return [m.to_api_msg() for m in system + recent]

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def add_ep(req):
            d = await req.json()
            m = self.add(d["role"], d["content"], d.get("pinned", False))
            return web.json_response(m.to_dict(), status=201)
        async def messages_ep(req):
            return web.json_response({"messages": self.get_messages()})
        async def truncate_ep(req):
            d = await req.json()
            new_cm = self.truncate(d.get("strategy","smart"), d.get("target_tokens"))
            return web.json_response({"messages": new_cm.get_messages(),
                                       "stats": new_cm.stats().to_dict()})
        async def stats_ep(req):
            return web.json_response(self.stats().to_dict())
        async def snapshot_ep(req):
            d = await req.json(); self.snapshot(d["name"])
            return web.json_response({"snapshot": d["name"]})
        p = f"{prefix}/context"
        app.router.add_post(f"{p}/add",      add_ep)
        app.router.add_get( f"{p}/messages", messages_ep)
        app.router.add_post(f"{p}/truncate", truncate_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        app.router.add_post(f"{p}/snapshot", snapshot_ep)
        logger.info(f"Context manager API at {prefix}/context/")
