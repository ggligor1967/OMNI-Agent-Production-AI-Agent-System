"""OMNI Agent — Context Composer V2: dynamic context assembly with budget and priority."""
from __future__ import annotations
import json, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ContextRole(str, Enum):
    SYSTEM    = "system"
    USER      = "user"
    ASSISTANT = "assistant"
    TOOL      = "tool"
    MEMORY    = "memory"
    DOCUMENT  = "document"
    EXAMPLE   = "example"


class ContextPriority(int, Enum):
    CRITICAL = 0    # always included (system prompts, instructions)
    HIGH     = 1    # include before budget exhausted
    NORMAL   = 2
    LOW      = 3
    OPTIONAL = 4    # drop first when budget tight


class TruncationStrategy(str, Enum):
    DROP_LOW_PRIORITY = "drop_low_priority"
    TRIM_CONTENT      = "trim_content"
    SUMMARIZE         = "summarize"
    SLIDING_WINDOW    = "sliding_window"


@dataclass
class ContextBlock:
    block_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    role: ContextRole = ContextRole.USER
    content: str = ""
    priority: ContextPriority = ContextPriority.NORMAL
    token_count: int = 0          # estimated token count
    pinned: bool = False          # always include regardless of budget
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    def to_message(self) -> Dict[str, str]:
        return {"role": self.role.value, "content": self.content}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "block_id": self.block_id,
            "role": self.role.value,
            "priority": self.priority.value,
            "token_count": self.token_count,
            "pinned": self.pinned,
        }


@dataclass
class ComposedContext:
    blocks: List[ContextBlock]
    total_tokens: int
    dropped_count: int
    truncated_count: int
    budget_used: float
    strategy_applied: Optional[str]

    def to_messages(self) -> List[Dict[str, str]]:
        return [b.to_message() for b in self.blocks]

    def to_openai_messages(self) -> List[Dict[str, str]]:
        """OpenAI-compatible message list."""
        result = []
        for b in self.blocks:
            role = b.role.value
            if role in ("memory", "document", "example"):
                role = "user"
            result.append({"role": role, "content": b.content})
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "blocks": len(self.blocks),
            "dropped": self.dropped_count,
            "truncated": self.truncated_count,
        }


class ContextComposerV2:
    """
    Dynamic context assembly engine:
    - Add/remove/update context blocks by role and priority
    - Token budget enforcement with configurable truncation
    - Strategies: drop low priority, trim content, sliding window
    - Pinned blocks (always included regardless of budget)
    - TTL-based block expiry
    - Template variable substitution
    - Per-role token budget caps
    - Named snapshots (save/restore context state)
    - Merge contexts from multiple sources
    - Deduplicate blocks by content hash
    """

    def __init__(
        self,
        token_budget: int = 4096,
        truncation_strategy: TruncationStrategy = TruncationStrategy.DROP_LOW_PRIORITY,
        token_estimator: Optional[Callable[[str], int]] = None,
        role_budgets: Optional[Dict[ContextRole, int]] = None,
    ):
        self.token_budget       = token_budget
        self.truncation_strategy = truncation_strategy
        self._estimate_tokens   = token_estimator or self._default_estimator
        self.role_budgets       = dict(role_budgets or {})
        self._blocks:     Dict[str, ContextBlock] = {}
        self._order:      List[str] = []          # insertion order
        self._snapshots:  Dict[str, List[str]] = {}  # name → [block_ids]
        self._seen_hashes: set = set()
        self._compose_count = 0

    @staticmethod
    def _default_estimator(text: str) -> int:
        """Rough token estimate: ~4 chars per token."""
        return max(1, len(text) // 4)

    # ── BLOCK MANAGEMENT ─────────────────────────────────────────────

    def add(self, content: str,
            role: ContextRole = ContextRole.USER,
            priority: ContextPriority = ContextPriority.NORMAL,
            pinned: bool = False,
            tags: Optional[List[str]] = None,
            ttl_s: Optional[float] = None,
            deduplicate: bool = False,
            block_id: Optional[str] = None,
            metadata: Optional[Dict] = None) -> Optional[ContextBlock]:
        import hashlib
        if deduplicate:
            h = hashlib.md5(content.encode()).hexdigest()
            if h in self._seen_hashes:
                return None
            self._seen_hashes.add(h)

        bid  = block_id or str(uuid.uuid4())[:8]
        toks = self._estimate_tokens(content)
        exp  = time.time() + ttl_s if ttl_s else None
        b    = ContextBlock(
            block_id=bid, role=role, content=content,
            priority=priority, token_count=toks,
            pinned=pinned, tags=list(tags or []),
            expires_at=exp, metadata=metadata or {})
        self._blocks[bid] = b
        if bid not in self._order:
            self._order.append(bid)
        return b

    def add_system(self, content: str, **kwargs) -> Optional[ContextBlock]:
        return self.add(content, ContextRole.SYSTEM,
                        priority=ContextPriority.CRITICAL,
                        pinned=True, **kwargs)

    def add_message(self, role: ContextRole, content: str,
                    **kwargs) -> Optional[ContextBlock]:
        return self.add(content, role, **kwargs)

    def remove(self, block_id: str) -> bool:
        if block_id in self._blocks:
            self._blocks.pop(block_id)
            self._order = [b for b in self._order if b != block_id]
            return True
        return False

    def update(self, block_id: str, content: str) -> bool:
        b = self._blocks.get(block_id)
        if not b: return False
        b.content     = content
        b.token_count = self._estimate_tokens(content)
        return True

    def pin(self, block_id: str):
        b = self._blocks.get(block_id)
        if b: b.pinned = True

    def unpin(self, block_id: str):
        b = self._blocks.get(block_id)
        if b: b.pinned = False

    def clear(self, role: Optional[ContextRole] = None):
        if role:
            to_del = [bid for bid, b in self._blocks.items()
                      if b.role == role and not b.pinned]
            for bid in to_del:
                self._blocks.pop(bid)
                self._order = [b for b in self._order if b != bid]
        else:
            self._blocks.clear()
            self._order.clear()
            self._seen_hashes.clear()

    # ── TEMPLATE ─────────────────────────────────────────────────────

    def add_template(self, template: str,
                     role: ContextRole = ContextRole.SYSTEM,
                     priority: ContextPriority = ContextPriority.CRITICAL,
                     **kwargs) -> Optional[ContextBlock]:
        return self.add(template, role, priority=priority, **kwargs)

    def render_template(self, block_id: str, **vars) -> Optional[str]:
        b = self._blocks.get(block_id)
        if not b: return None
        text = b.content
        for k, v in vars.items():
            text = text.replace(f"{{{k}}}", str(v))
        return text

    # ── COMPOSE ──────────────────────────────────────────────────────

    def compose(self, extra_blocks: Optional[List[ContextBlock]] = None,
                override_budget: Optional[int] = None) -> ComposedContext:
        self._compose_count += 1
        budget = override_budget or self.token_budget

        # Collect + expire
        all_blocks: List[ContextBlock] = []
        for bid in self._order:
            b = self._blocks.get(bid)
            if b and not b.is_expired:
                all_blocks.append(b)
        if extra_blocks:
            all_blocks.extend(extra_blocks)

        # Separate pinned from flexible
        pinned    = [b for b in all_blocks if b.pinned]
        flexible  = [b for b in all_blocks if not b.pinned]
        pinned_tk = sum(b.token_count for b in pinned)
        remaining = budget - pinned_tk

        # Apply per-role caps
        if self.role_budgets:
            role_used: Dict[ContextRole, int] = {}
            capped = []
            for b in flexible:
                cap  = self.role_budgets.get(b.role)
                used = role_used.get(b.role, 0)
                if cap and used + b.token_count > cap:
                    continue
                role_used[b.role] = used + b.token_count
                capped.append(b)
            flexible = capped

        dropped = 0; truncated = 0; strategy = None

        if self.truncation_strategy == TruncationStrategy.DROP_LOW_PRIORITY:
            flexible_sorted = sorted(flexible,
                                     key=lambda b: (b.priority.value, -b.created_at))
            selected = []
            used = 0
            for b in flexible_sorted:
                if used + b.token_count <= remaining:
                    selected.append(b); used += b.token_count
                else:
                    dropped += 1
            strategy = "drop_low_priority"

        elif self.truncation_strategy == TruncationStrategy.TRIM_CONTENT:
            selected = []; used = 0
            for b in flexible:
                if used + b.token_count <= remaining:
                    selected.append(b); used += b.token_count
                elif used < remaining:
                    # Trim
                    allow = remaining - used
                    chars = allow * 4
                    trimmed = ContextBlock(
                        block_id=b.block_id,
                        role=b.role,
                        content=b.content[:chars] + "…",
                        priority=b.priority,
                        token_count=allow,
                        pinned=b.pinned, tags=b.tags)
                    selected.append(trimmed)
                    used += allow; truncated += 1
                    break
                else:
                    dropped += 1
            strategy = "trim_content"

        elif self.truncation_strategy == TruncationStrategy.SLIDING_WINDOW:
            # Keep most recent flexible blocks fitting in budget
            window = []
            used   = 0
            for b in reversed(flexible):
                if used + b.token_count <= remaining:
                    window.append(b); used += b.token_count
                else:
                    dropped += 1
            selected = list(reversed(window))
            strategy = "sliding_window"

        else:
            selected = []; used = 0
            for b in flexible:
                if used + b.token_count <= remaining:
                    selected.append(b); used += b.token_count
                else:
                    dropped += 1
            strategy = "default"

        # Reconstruct in insertion order
        selected_map = {b.block_id: b for b in selected}
        ordered = ([b for b in pinned] +
                   [selected_map[bid]
                    for bid in self._order
                    if bid in selected_map])
        # Deduplicate while preserving order
        seen_ids: set = set()
        final = []
        for b in ordered:
            if b and b.block_id not in seen_ids:
                final.append(b); seen_ids.add(b.block_id)
        # Add any extra blocks not in _order
        for b in selected:
            if b.block_id not in seen_ids:
                final.append(b); seen_ids.add(b.block_id)

        total_tokens = sum(b.token_count for b in final)

        return ComposedContext(
            blocks=final,
            total_tokens=total_tokens,
            dropped_count=dropped,
            truncated_count=truncated,
            budget_used=total_tokens / budget if budget else 0.0,
            strategy_applied=strategy)

    # ── SNAPSHOT ─────────────────────────────────────────────────────

    def snapshot(self, name: str):
        self._snapshots[name] = list(self._order)

    def restore(self, name: str) -> bool:
        ids = self._snapshots.get(name)
        if ids is None: return False
        self._order = [bid for bid in ids if bid in self._blocks]
        return True

    # ── MERGE ────────────────────────────────────────────────────────

    def merge(self, other: "ContextComposerV2",
               priority_filter: Optional[ContextPriority] = None):
        for bid in other._order:
            b = other._blocks.get(bid)
            if not b: continue
            if priority_filter and b.priority.value > priority_filter.value:
                continue
            self.add(b.content, role=b.role, priority=b.priority,
                     pinned=b.pinned, tags=b.tags)

    # ── STATS ────────────────────────────────────────────────────────

    @property
    def block_count(self) -> int:
        return len(self._blocks)

    @property
    def estimated_tokens(self) -> int:
        return sum(b.token_count for b in self._blocks.values()
                   if not b.is_expired)

    def stats(self) -> Dict[str, Any]:
        by_role: Dict[str, int] = {}
        for b in self._blocks.values():
            k = b.role.value
            by_role[k] = by_role.get(k, 0) + 1
        return {
            "blocks": self.block_count,
            "estimated_tokens": self.estimated_tokens,
            "token_budget": self.token_budget,
            "compose_count": self._compose_count,
            "by_role": by_role,
            "snapshots": list(self._snapshots.keys()),
        }
