"""OMNI AGENT - Context Composer
Dynamically build, compress, and manage LLM context windows: slot-based
composition, importance-weighted pruning, token budgeting, and format rendering.

Features:
- Named slots: system, examples, history, retrieved, instructions, scratchpad
- Priority levels: each slot has a weight used during budget pruning
- Token counting: character-based approximation (÷4) with per-slot breakdown
- Budget enforcement: iterative drop of lowest-priority content to fit budget
- Conversation turns: append/pop with automatic token tracking
- Retrieval injection: insert RAG passages into a dedicated slot
- Format templates: chat (role/content), XML, markdown, plain
- Slot pinning: pinned slots are never pruned regardless of budget
- Context diff: compare two contexts and show what changed
- Serialisation: export to dict and restore from dict
- REST API: compose, add-turn, set-slot, render, stats
"""
import time, uuid, logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
logger = logging.getLogger(__name__)

class SlotPriority(int, Enum):
    CRITICAL = 100
    HIGH     = 75
    MEDIUM   = 50
    LOW      = 25
    MINIMAL  = 10

SLOT_DEFAULTS = {
    "system":       SlotPriority.CRITICAL,
    "instructions": SlotPriority.HIGH,
    "examples":     SlotPriority.MEDIUM,
    "retrieved":    SlotPriority.MEDIUM,
    "history":      SlotPriority.LOW,
    "scratchpad":   SlotPriority.MINIMAL,
}

@dataclass
class Slot:
    name: str; content: str = ""; priority: int = SlotPriority.MEDIUM
    pinned: bool = False; max_tokens: Optional[int] = None
    metadata: Dict = field(default_factory=dict)

    @property
    def token_count(self) -> int:
        return max(1, len(self.content) // 4)

    def truncate(self, max_tokens: int) -> "Slot":
        max_chars = max_tokens * 4
        return Slot(name=self.name,
                    content=self.content[:max_chars] + ("…" if len(self.content) > max_chars else ""),
                    priority=self.priority, pinned=self.pinned,
                    max_tokens=self.max_tokens, metadata=self.metadata)

    def to_dict(self):
        return {"name":self.name,"content":self.content[:200],
                "priority":self.priority,"pinned":self.pinned,
                "token_count":self.token_count}

@dataclass
class Turn:
    role: str; content: str
    turn_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: float = field(default_factory=time.time)

    @property
    def token_count(self) -> int:
        return max(1, (len(self.role) + len(self.content)) // 4)

    def to_dict(self):
        return {"role":self.role,"content":self.content,
                "turn_id":self.turn_id,"token_count":self.token_count}

class ContextComposer:
    """
    Slot-based LLM context builder with token budgeting and format rendering.

    Usage:
        ctx = ContextComposer(token_budget=4096)
        ctx.set_slot("system", "You are a helpful assistant.", pinned=True)
        ctx.set_slot("instructions", "Answer in bullet points.")
        ctx.add_turn("user", "What is Python?")
        ctx.inject_retrieval(["Python is a language created in 1991.", "Guido van Rossum created it."])
        print(ctx.render("chat"))
        print(f"Tokens used: {ctx.total_tokens}/{ctx.token_budget}")
    """
    def __init__(self, token_budget: int = 4096, format: str = "chat"):
        self.token_budget = token_budget
        self.default_format = format
        self._slots: Dict[str, Slot] = {}
        self._turns: List[Turn] = []
        self._created_at = time.time()
        # initialise default empty slots
        for name, priority in SLOT_DEFAULTS.items():
            self._slots[name] = Slot(name=name, priority=priority)

    # ── Slot management ───────────────────────────────────────────────────────

    def set_slot(self, name: str, content: str, priority: Optional[int] = None,
                 pinned: bool = False, max_tokens: Optional[int] = None):
        pri = priority or SLOT_DEFAULTS.get(name, SlotPriority.MEDIUM)
        self._slots[name] = Slot(name=name, content=content, priority=pri,
                                  pinned=pinned, max_tokens=max_tokens)
        logger.debug(f"Slot '{name}' set ({self._slots[name].token_count} tokens)")

    def get_slot(self, name: str) -> Optional[Slot]:
        return self._slots.get(name)

    def clear_slot(self, name: str):
        if name in self._slots:
            self._slots[name] = Slot(name=name,
                                      priority=SLOT_DEFAULTS.get(name, SlotPriority.MEDIUM))

    def pin_slot(self, name: str):
        if name in self._slots: self._slots[name].pinned = True

    def unpin_slot(self, name: str):
        if name in self._slots: self._slots[name].pinned = False

    # ── Conversation turns ────────────────────────────────────────────────────

    def add_turn(self, role: str, content: str) -> Turn:
        turn = Turn(role=role, content=content)
        self._turns.append(turn)
        return turn

    def pop_turn(self) -> Optional[Turn]:
        return self._turns.pop() if self._turns else None

    def clear_turns(self):
        self._turns.clear()

    def last_n_turns(self, n: int) -> List[Turn]:
        return self._turns[-n:]

    # ── Retrieval injection ───────────────────────────────────────────────────

    def inject_retrieval(self, passages: List[str], separator: str = "\n\n---\n\n"):
        self.set_slot("retrieved", separator.join(passages),
                      priority=SlotPriority.MEDIUM)

    # ── Token accounting ──────────────────────────────────────────────────────

    @property
    def total_tokens(self) -> int:
        slot_tokens = sum(s.token_count for s in self._slots.values() if s.content)
        turn_tokens = sum(t.token_count for t in self._turns)
        return slot_tokens + turn_tokens

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.token_budget - self.total_tokens)

    def token_breakdown(self) -> Dict[str, int]:
        breakdown = {name: s.token_count for name, s in self._slots.items() if s.content}
        breakdown["_turns"] = sum(t.token_count for t in self._turns)
        breakdown["_total"] = self.total_tokens
        breakdown["_budget"] = self.token_budget
        breakdown["_remaining"] = self.remaining_tokens
        return breakdown

    # ── Budget pruning ────────────────────────────────────────────────────────

    def prune_to_budget(self) -> List[str]:
        """Iteratively drop/truncate lowest-priority unpinned content to fit budget."""
        pruned = []
        while self.total_tokens > self.token_budget:
            # Try dropping oldest non-pinned turn first
            unpinned_turns = [t for t in self._turns]
            if unpinned_turns and self._turns:
                dropped = self._turns.pop(0)
                pruned.append(f"turn:{dropped.turn_id}")
                continue
            # Then drop lowest-priority unpinned slot
            candidates = [(s.priority, name, s) for name, s in self._slots.items()
                          if s.content and not s.pinned]
            if not candidates:
                break  # only pinned content remains
            candidates.sort(key=lambda x: x[0])
            _, name, slot = candidates[0]
            if slot.token_count > 100:
                # Truncate by half first
                half = slot.token_count // 2
                self._slots[name] = slot.truncate(half)
                pruned.append(f"truncated:{name}")
            else:
                self.clear_slot(name)
                pruned.append(f"dropped:{name}")
        return pruned

    # ── Rendering ─────────────────────────────────────────────────────────────

    def render(self, format: Optional[str] = None) -> str:
        fmt = format or self.default_format
        if fmt == "chat":
            return self._render_chat()
        elif fmt == "xml":
            return self._render_xml()
        elif fmt == "markdown":
            return self._render_markdown()
        else:
            return self._render_plain()

    def _render_chat(self) -> str:
        parts = []
        # System
        sys_content = ""
        for slot_name in ["system", "instructions", "examples", "retrieved"]:
            s = self._slots.get(slot_name)
            if s and s.content:
                sys_content += f"[{slot_name.upper()}]\n{s.content}\n\n"
        if sys_content:
            parts.append({"role": "system", "content": sys_content.strip()})
        # Turns
        for turn in self._turns:
            parts.append({"role": turn.role, "content": turn.content})
        # Scratchpad as assistant prefix if present
        scratch = self._slots.get("scratchpad")
        if scratch and scratch.content:
            parts.append({"role": "assistant", "content": scratch.content})
        import json
        return json.dumps(parts, indent=2)

    def _render_xml(self) -> str:
        lines = ["<context>"]
        for name, slot in self._slots.items():
            if slot.content:
                lines.append(f"  <{name}>{slot.content}</{name}>")
        for turn in self._turns:
            lines.append(f"  <turn role='{turn.role}'>{turn.content}</turn>")
        lines.append("</context>")
        return "\n".join(lines)

    def _render_markdown(self) -> str:
        parts = []
        for name, slot in self._slots.items():
            if slot.content:
                parts.append(f"## {name.upper()}\n{slot.content}")
        if self._turns:
            parts.append("## CONVERSATION")
            for turn in self._turns:
                parts.append(f"**{turn.role}**: {turn.content}")
        return "\n\n".join(parts)

    def _render_plain(self) -> str:
        parts = []
        for slot in self._slots.values():
            if slot.content:
                parts.append(slot.content)
        for turn in self._turns:
            parts.append(f"{turn.role}: {turn.content}")
        return "\n\n".join(parts)

    # ── Diff & serialisation ──────────────────────────────────────────────────

    def diff(self, other: "ContextComposer") -> Dict:
        changes = {}
        for name in set(list(self._slots.keys()) + list(other._slots.keys())):
            a = self._slots.get(name)
            b = other._slots.get(name)
            ac = a.content if a else ""
            bc = b.content if b else ""
            if ac != bc:
                changes[name] = {"before": ac[:100], "after": bc[:100]}
        if len(self._turns) != len(other._turns):
            changes["_turns"] = {"before": len(self._turns), "after": len(other._turns)}
        return changes

    def to_dict(self) -> Dict:
        return {"token_budget": self.token_budget,
                "slots": {n: s.to_dict() for n, s in self._slots.items()},
                "turns": [t.to_dict() for t in self._turns],
                "total_tokens": self.total_tokens}

    @classmethod
    def from_dict(cls, d: Dict) -> "ContextComposer":
        ctx = cls(token_budget=d.get("token_budget", 4096))
        for name, sd in d.get("slots", {}).items():
            ctx.set_slot(name, sd.get("content",""), priority=sd.get("priority"),
                          pinned=sd.get("pinned", False))
        for td in d.get("turns", []):
            ctx.add_turn(td["role"], td["content"])
        return ctx

    def stats(self) -> Dict:
        return {"total_tokens":self.total_tokens,"token_budget":self.token_budget,
                "remaining_tokens":self.remaining_tokens,
                "utilisation_pct":round(self.total_tokens/self.token_budget*100,1),
                "slot_count":sum(1 for s in self._slots.values() if s.content),
                "turn_count":len(self._turns)}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        ctx_store: Dict[str, ContextComposer] = {}
        async def create_ep(req):
            d = await req.json()
            cid = str(uuid.uuid4())[:10]
            ctx_store[cid] = ContextComposer(
                token_budget=int(d.get("token_budget", 4096)),
                format=d.get("format", "chat"))
            return web.json_response({"context_id": cid}, status=201)
        async def set_slot_ep(req):
            d = await req.json(); cid = req.match_info["id"]
            ctx = ctx_store.get(cid)
            if not ctx: return web.json_response({"error":"not found"}, status=404)
            ctx.set_slot(d["name"], d["content"],
                          priority=d.get("priority"), pinned=bool(d.get("pinned", False)))
            return web.json_response(ctx.stats())
        async def add_turn_ep(req):
            d = await req.json(); cid = req.match_info["id"]
            ctx = ctx_store.get(cid)
            if not ctx: return web.json_response({"error":"not found"}, status=404)
            t = ctx.add_turn(d["role"], d["content"])
            return web.json_response(t.to_dict())
        async def render_ep(req):
            cid = req.match_info["id"]
            ctx = ctx_store.get(cid)
            if not ctx: return web.json_response({"error":"not found"}, status=404)
            fmt = req.rel_url.query.get("format", ctx.default_format)
            return web.json_response({"rendered": ctx.render(fmt), "stats": ctx.stats()})
        async def stats_ep(req):
            cid = req.match_info["id"]
            ctx = ctx_store.get(cid)
            if not ctx: return web.json_response({"error":"not found"}, status=404)
            return web.json_response(ctx.stats())
        p = f"{prefix}/context"
        app.router.add_post(p, create_ep)
        app.router.add_post(f"{p}/{{id}}/slot", set_slot_ep)
        app.router.add_post(f"{p}/{{id}}/turn", add_turn_ep)
        app.router.add_get(f"{p}/{{id}}/render", render_ep)
        app.router.add_get(f"{p}/{{id}}/stats", stats_ep)
        logger.info(f"Context composer API at {prefix}/context/")
