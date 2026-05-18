"""OMNI Agent — Context Window Manager: dynamic context packing with priority and truncation."""
from __future__ import annotations
import json, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ContextItemType(str, Enum):
    SYSTEM_PROMPT  = "system_prompt"
    USER_MESSAGE   = "user_message"
    ASSISTANT_MSG  = "assistant_message"
    TOOL_RESULT    = "tool_result"
    MEMORY         = "memory"
    DOCUMENT       = "document"
    EXAMPLE        = "example"
    INSTRUCTION    = "instruction"


class TruncationStrategy(str, Enum):
    DROP_OLDEST    = "drop_oldest"
    DROP_LOWEST    = "drop_lowest_priority"
    SUMMARIZE      = "summarize"
    TRIM_CONTENT   = "trim_content"
    MIDDLE_OUT     = "middle_out"         # keep head + tail, drop middle


@dataclass
class ContextItem:
    item_id: str
    item_type: ContextItemType
    content: str
    priority: int = 5          # 1 = must-keep, 10 = can drop
    tokens: int = 0
    role: str = ""             # for messages: user/assistant/system
    pinned: bool = False       # pinned items are never dropped
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_message(self) -> Dict[str, str]:
        role = self.role or {
            ContextItemType.SYSTEM_PROMPT: "system",
            ContextItemType.USER_MESSAGE:  "user",
            ContextItemType.ASSISTANT_MSG: "assistant",
            ContextItemType.TOOL_RESULT:   "tool",
        }.get(self.item_type, "user")
        return {"role": role, "content": self.content}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "type": self.item_type.value,
            "tokens": self.tokens,
            "priority": self.priority,
            "pinned": self.pinned,
            "preview": self.content[:60] + "…" if len(self.content) > 60 else self.content,
        }


@dataclass
class PackResult:
    items: List[ContextItem]
    total_tokens: int
    dropped_items: List[ContextItem]
    truncated: bool
    strategy_used: TruncationStrategy
    utilization: float               # total_tokens / max_tokens

    def to_messages(self) -> List[Dict[str, str]]:
        return [item.to_message() for item in self.items]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": len(self.items),
            "total_tokens": self.total_tokens,
            "dropped": len(self.dropped_items),
            "truncated": self.truncated,
            "strategy": self.strategy_used.value,
            "utilization": round(self.utilization, 4),
        }


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class ContextWindowManager:
    """
    Dynamic context window manager:
    - Add items with type, priority, and token count
    - Pack context within a token budget
    - Multiple truncation strategies
    - Pinned items (never dropped)
    - Per-type token budgets
    - Summarization hook for compressing dropped content
    - Context diff tracking
    """

    def __init__(
        self,
        max_tokens: int = 4096,
        reserved_tokens: int = 512,      # reserve for completion
        strategy: TruncationStrategy = TruncationStrategy.DROP_OLDEST,
        summarize_fn: Optional[Callable[[List[ContextItem]], str]] = None,
        auto_estimate: bool = True,      # auto-estimate token counts
    ):
        self.max_tokens      = max_tokens
        self.reserved_tokens = reserved_tokens
        self.strategy        = strategy
        self.summarize_fn    = summarize_fn
        self.auto_estimate   = auto_estimate
        self._items: List[ContextItem] = []
        self._type_budgets: Dict[ContextItemType, int] = {}
        self._pack_count = 0
        self._drop_count = 0

    @property
    def available_tokens(self) -> int:
        return self.max_tokens - self.reserved_tokens

    # ── ITEM MANAGEMENT ───────────────────────────────────────────────

    def add(self, content: str,
            item_type: ContextItemType = ContextItemType.USER_MESSAGE,
            priority: int = 5,
            role: str = "",
            pinned: bool = False,
            tokens: Optional[int] = None,
            metadata: Optional[Dict] = None,
            item_id: Optional[str] = None) -> ContextItem:
        tok = tokens if tokens is not None else (
            _estimate_tokens(content) if self.auto_estimate else 0)
        item = ContextItem(
            item_id=item_id or str(uuid.uuid4())[:8],
            item_type=item_type, content=content,
            priority=priority, tokens=tok, role=role,
            pinned=pinned, metadata=metadata or {})
        self._items.append(item)
        return item

    def add_system(self, content: str, **kwargs) -> ContextItem:
        return self.add(content, ContextItemType.SYSTEM_PROMPT,
                        priority=1, pinned=True, **kwargs)

    def add_user(self, content: str, priority: int = 5, **kwargs) -> ContextItem:
        return self.add(content, ContextItemType.USER_MESSAGE,
                        role="user", priority=priority, **kwargs)

    def add_assistant(self, content: str, priority: int = 6, **kwargs) -> ContextItem:
        return self.add(content, ContextItemType.ASSISTANT_MSG,
                        role="assistant", priority=priority, **kwargs)

    def add_memory(self, content: str, priority: int = 7, **kwargs) -> ContextItem:
        return self.add(content, ContextItemType.MEMORY, priority=priority, **kwargs)

    def add_document(self, content: str, priority: int = 4, **kwargs) -> ContextItem:
        return self.add(content, ContextItemType.DOCUMENT, priority=priority, **kwargs)

    def remove(self, item_id: str) -> bool:
        before = len(self._items)
        self._items = [i for i in self._items if i.item_id != item_id]
        return len(self._items) < before

    def pin(self, item_id: str):
        for item in self._items:
            if item.item_id == item_id:
                item.pinned = True

    def unpin(self, item_id: str):
        for item in self._items:
            if item.item_id == item_id:
                item.pinned = False

    def clear(self, keep_pinned: bool = True):
        if keep_pinned:
            self._items = [i for i in self._items if i.pinned]
        else:
            self._items.clear()

    # ── BUDGETS ───────────────────────────────────────────────────────

    def set_type_budget(self, item_type: ContextItemType, max_tokens: int):
        self._type_budgets[item_type] = max_tokens

    # ── PACKING ───────────────────────────────────────────────────────

    def pack(self, extra_reserve: int = 0) -> PackResult:
        """Pack items within token budget, applying truncation strategy as needed."""
        self._pack_count += 1
        budget = self.available_tokens - extra_reserve
        dropped: List[ContextItem] = []
        strategy_used = self.strategy

        # Separate pinned and droppable
        pinned    = [i for i in self._items if i.pinned]
        droppable = [i for i in self._items if not i.pinned]

        pinned_tokens = sum(i.tokens for i in pinned)
        remaining     = budget - pinned_tokens

        if remaining < 0:
            # Can't even fit pinned items — return them anyway, truncated
            return PackResult(items=pinned, total_tokens=pinned_tokens,
                              dropped_items=droppable, truncated=True,
                              strategy_used=strategy_used,
                              utilization=pinned_tokens / budget if budget > 0 else 1.0)

        # Apply type budgets
        selected: List[ContextItem] = []
        type_used: Dict[ContextItemType, int] = {}
        for item in droppable:
            tb = self._type_budgets.get(item.item_type)
            used = type_used.get(item.item_type, 0)
            if tb is not None and used + item.tokens > tb:
                dropped.append(item)
                continue
            selected.append(item)
            type_used[item.item_type] = used + item.tokens

        # Apply truncation strategy
        selected, more_dropped = self._truncate(selected, remaining, strategy_used)
        dropped.extend(more_dropped)
        self._drop_count += len(dropped)

        all_items = pinned + selected
        total = sum(i.tokens for i in all_items)
        return PackResult(
            items=all_items, total_tokens=total,
            dropped_items=dropped, truncated=len(dropped) > 0,
            strategy_used=strategy_used,
            utilization=total / budget if budget > 0 else 0.0)

    def _truncate(self, items: List[ContextItem], budget: int,
                   strategy: TruncationStrategy) -> Tuple[List[ContextItem], List[ContextItem]]:
        total = sum(i.tokens for i in items)
        if total <= budget:
            return items, []

        dropped: List[ContextItem] = []

        if strategy == TruncationStrategy.DROP_OLDEST:
            # Drop from front (oldest), keep newest
            candidates = list(items)
            candidates.sort(key=lambda x: x.created_at)
            while sum(i.tokens for i in candidates) > budget and candidates:
                dropped.append(candidates.pop(0))
            return candidates, dropped

        if strategy == TruncationStrategy.DROP_LOWEST:
            # Drop by ascending priority (high priority number = more droppable)
            candidates = list(items)
            candidates.sort(key=lambda x: -x.priority)  # most droppable first
            while sum(i.tokens for i in candidates) > budget and candidates:
                dropped.append(candidates.pop(0))
            # Restore original order
            orig_ids = {i.item_id: idx for idx, i in enumerate(items)}
            candidates.sort(key=lambda x: orig_ids.get(x.item_id, 0))
            return candidates, dropped

        if strategy == TruncationStrategy.MIDDLE_OUT:
            # Keep first 1/3 and last 1/3, drop middle
            n = len(items)
            head = items[:n // 3]
            tail = items[n - n // 3:]
            middle = items[n // 3: n - n // 3]
            kept = head + tail
            if sum(i.tokens for i in kept) <= budget:
                return kept, middle
            # Fall through to drop_oldest
            return self._truncate(kept, budget, TruncationStrategy.DROP_OLDEST)

        if strategy == TruncationStrategy.TRIM_CONTENT:
            # Trim content of lowest-priority items proportionally
            candidates = list(items)
            excess = sum(i.tokens for i in candidates) - budget
            sorted_by_prio = sorted(candidates, key=lambda x: -x.priority)
            for item in sorted_by_prio:
                if excess <= 0:
                    break
                trim = min(item.tokens, excess)
                old_tokens = item.tokens
                item.tokens -= trim
                chars_to_keep = max(20, len(item.content) - trim * 4)
                item.content = item.content[:chars_to_keep] + "…"
                excess -= old_tokens - item.tokens
            return candidates, []

        if strategy == TruncationStrategy.SUMMARIZE and self.summarize_fn:
            # Summarize dropped items
            candidates = list(items)
            candidates.sort(key=lambda x: x.created_at)
            to_summarize: List[ContextItem] = []
            while sum(i.tokens for i in candidates) > budget and len(candidates) > 1:
                to_summarize.append(candidates.pop(0))
            if to_summarize:
                summary_text = self.summarize_fn(to_summarize)
                summary_item = ContextItem(
                    item_id=str(uuid.uuid4())[:8],
                    item_type=ContextItemType.MEMORY,
                    content=summary_text,
                    tokens=_estimate_tokens(summary_text),
                    priority=3,
                    metadata={"summarized_count": len(to_summarize)})
                candidates.insert(0, summary_item)
                dropped.extend(to_summarize)
            return candidates, dropped

        # Default: drop oldest
        return self._truncate(items, budget, TruncationStrategy.DROP_OLDEST)

    # ── QUERY ─────────────────────────────────────────────────────────

    def current_tokens(self) -> int:
        return sum(i.tokens for i in self._items)

    def utilization(self) -> float:
        return self.current_tokens() / self.available_tokens \
               if self.available_tokens > 0 else 0.0

    def list_items(self,
                   item_type: Optional[ContextItemType] = None) -> List[ContextItem]:
        if item_type:
            return [i for i in self._items if i.item_type == item_type]
        return list(self._items)

    def to_messages(self, pack: bool = True) -> List[Dict[str, str]]:
        if pack:
            return self.pack().to_messages()
        return [i.to_message() for i in self._items]

    def stats(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for i in self._items:
            by_type[i.item_type.value] = by_type.get(i.item_type.value, 0) + 1
        return {
            "items": len(self._items),
            "current_tokens": self.current_tokens(),
            "max_tokens": self.max_tokens,
            "available_tokens": self.available_tokens,
            "utilization": round(self.utilization(), 4),
            "packs_performed": self._pack_count,
            "total_dropped": self._drop_count,
            "by_type": by_type,
            "strategy": self.strategy.value,
        }
