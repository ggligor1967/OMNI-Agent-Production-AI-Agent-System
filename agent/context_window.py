"""
OMNI AGENT - Context Window Manager
Dynamic context management for LLM conversations: count tokens, detect overflow,
trim intelligently using priority scoring, and summarize older context.

Features:
- Token estimation: fast character-based heuristic (no tiktoken required)
- tiktoken integration: accurate counts when available
- Priority scoring: keep high-value messages (tool results, system, recent)
- Overflow strategies: truncate | summarize | sliding_window | drop_middle
- Message budget: reserve tokens for system prompt + completion
- Pin messages: mark messages as undeletable (system, critical context)
- Compression: collapse repeated assistant/user pairs into summary
- Context diff: show what was trimmed and why
- Per-model limits: GPT-4=128k, Claude=200k, configurable registry
- REST API: count, check, trim, summarize endpoints
"""
import re
import time
import math
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN COUNTING
# ══════════════════════════════════════════════════════════════════════════════

# Approximate characters-per-token for common models
_CHARS_PER_TOKEN = 3.8

# Known model context limits (tokens)
MODEL_LIMITS: Dict[str, int] = {
    # Anthropic
    "claude-3-5-sonnet":       200_000,
    "claude-3-5-haiku":        200_000,
    "claude-3-opus":           200_000,
    "claude-3-haiku":          200_000,
    "claude-sonnet-4-6":       200_000,
    "claude-opus-4-6":         200_000,
    # OpenAI
    "gpt-4o":                  128_000,
    "gpt-4o-mini":             128_000,
    "gpt-4-turbo":             128_000,
    "gpt-4":                     8_192,
    "gpt-3.5-turbo":            16_385,
    "o1":                      200_000,
    "o1-mini":                 128_000,
    # DeepSeek
    "deepseek-v3":              64_000,
    "deepseek-chat":            64_000,
    # Mistral
    "mistral-large":            32_000,
    "mixtral-8x7b":             32_000,
    # Llama
    "llama-3.1-70b":           128_000,
    "llama-3.1-8b":            128_000,
    # Default fallback
    "default":                  16_000,
}


def _tiktoken_count(text: str, model: str = "gpt-4") -> Optional[int]:
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model)
        return len(enc.encode(text))
    except Exception:
        return None


def count_tokens(text: str, model: str = "default") -> int:
    """
    Count tokens in text.
    Uses tiktoken if available, otherwise a fast character heuristic.
    """
    if not text:
        return 0
    # Try tiktoken for OpenAI-style models
    if any(name in model for name in ("gpt", "o1", "text-")):
        result = _tiktoken_count(text, model)
        if result is not None:
            return result
    # Heuristic: ~3.8 chars per token (works reasonably for English + code)
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


def count_message_tokens(message: Dict, model: str = "default") -> int:
    """Count tokens for a single chat message dict."""
    # 4 overhead tokens per message (role + formatting)
    content = message.get("content", "")
    if isinstance(content, list):
        # Multi-modal: count text blocks only
        content = " ".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return count_tokens(str(content), model) + 4


def count_messages_tokens(messages: List[Dict], model: str = "default") -> int:
    """Count total tokens for a list of messages."""
    return sum(count_message_tokens(m, model) for m in messages) + 3  # 3 priming tokens


def model_limit(model: str) -> int:
    """Look up the context window limit for a model."""
    for key, limit in MODEL_LIMITS.items():
        if key in model:
            return limit
    return MODEL_LIMITS["default"]


# ══════════════════════════════════════════════════════════════════════════════
# PRIORITY SCORING
# ══════════════════════════════════════════════════════════════════════════════

class TrimStrategy(str, Enum):
    TRUNCATE       = "truncate"        # drop from the middle
    SLIDING_WINDOW = "sliding_window"  # keep only the N most recent messages
    DROP_MIDDLE    = "drop_middle"     # drop middle messages, keep start + end
    SUMMARIZE      = "summarize"       # summarize dropped messages (requires llm)


def _message_priority(message: Dict, index: int, total: int) -> float:
    """
    Score a message for retention priority (higher = keep).
    Factors: role, position, content markers.
    """
    role = message.get("role", "user")
    score = 0.0

    # Role weights
    if role == "system":
        score += 100.0
    elif role == "tool":
        score += 20.0
    elif role == "assistant":
        score += 10.0
    else:
        score += 8.0

    # Recency: last 20% of messages get a big boost
    recency_fraction = index / max(total - 1, 1)
    if recency_fraction >= 0.8:
        score += 30.0
    elif recency_fraction >= 0.5:
        score += 10.0

    # Pinned messages (marked explicitly)
    if message.get("_pinned"):
        score += 1000.0

    # Content signals: tool calls, errors, key decisions
    content = str(message.get("content", ""))
    if any(kw in content.lower() for kw in ("error", "exception", "failed")):
        score += 5.0
    if message.get("tool_calls"):
        score += 15.0

    return score


# ══════════════════════════════════════════════════════════════════════════════
# TRIM RESULT
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TrimResult:
    messages: List[Dict]
    original_count: int
    trimmed_count: int
    original_tokens: int
    final_tokens: int
    strategy_used: TrimStrategy
    dropped_indices: List[int] = field(default_factory=list)
    summary_injected: bool = False
    summary_text: str = ""

    @property
    def tokens_saved(self) -> int:
        return self.original_tokens - self.final_tokens

    def to_dict(self) -> Dict:
        return {
            "original_count": self.original_count,
            "trimmed_count": self.trimmed_count,
            "original_tokens": self.original_tokens,
            "final_tokens": self.final_tokens,
            "tokens_saved": self.tokens_saved,
            "strategy": self.strategy_used,
            "dropped_indices": self.dropped_indices,
            "summary_injected": self.summary_injected,
        }


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT WINDOW MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class ContextWindowManager:
    """
    Dynamic context manager for LLM conversations.

    Usage:
        cwm = ContextWindowManager(model="claude-3-5-sonnet",
                                   completion_reserve=2048)

        # Check if messages fit
        if cwm.overflows(messages):
            result = cwm.trim(messages)
            messages = result.messages

        # Or use auto-manage which handles it end-to-end
        messages = cwm.prepare(messages, system_prompt=sys_prompt)
    """

    def __init__(self,
                 model: str = "default",
                 completion_reserve: int = 2048,
                 system_reserve: int = 512,
                 default_strategy: TrimStrategy = TrimStrategy.DROP_MIDDLE,
                 summarizer: Callable = None):
        self.model = model
        self._limit = model_limit(model)
        self._completion_reserve = completion_reserve
        self._system_reserve = system_reserve
        self._strategy = default_strategy
        self._summarizer = summarizer    # async fn(messages) → str

    @property
    def available_tokens(self) -> int:
        """Token budget available for messages (after reserves)."""
        return (self._limit
                - self._completion_reserve
                - self._system_reserve)

    def count(self, messages: List[Dict]) -> int:
        """Count total tokens for a list of messages."""
        return count_messages_tokens(messages, self.model)

    def count_text(self, text: str) -> int:
        return count_tokens(text, self.model)

    def fits(self, messages: List[Dict],
             system_prompt: str = "") -> bool:
        """Return True if messages fit within the available token budget."""
        sys_tokens = count_tokens(system_prompt, self.model) if system_prompt else 0
        msg_tokens = self.count(messages)
        return (sys_tokens + msg_tokens + self._completion_reserve) <= self._limit

    def overflows(self, messages: List[Dict],
                  system_prompt: str = "") -> bool:
        return not self.fits(messages, system_prompt)

    def usage(self, messages: List[Dict],
              system_prompt: str = "") -> Dict:
        """Return a usage summary for the current context."""
        sys_tokens = count_tokens(system_prompt, self.model) if system_prompt else 0
        msg_tokens = self.count(messages)
        total = sys_tokens + msg_tokens
        return {
            "model": self.model,
            "limit": self._limit,
            "system_tokens": sys_tokens,
            "message_tokens": msg_tokens,
            "completion_reserve": self._completion_reserve,
            "total_used": total,
            "remaining": self._limit - total - self._completion_reserve,
            "utilization": round(total / self._limit, 4),
            "overflows": total + self._completion_reserve > self._limit,
        }

    # ── Trimming strategies ───────────────────────────────────────────────────

    def trim(self, messages: List[Dict],
             system_prompt: str = "",
             strategy: TrimStrategy = None,
             target_tokens: int = None) -> TrimResult:
        """
        Trim messages to fit within the token budget.
        Returns TrimResult with the new message list and metadata.
        """
        strat = strategy or self._strategy
        original_count = len(messages)
        original_tokens = self.count(messages)
        budget = target_tokens or self.available_tokens
        sys_tokens = count_tokens(system_prompt, self.model) if system_prompt else 0
        effective_budget = budget - sys_tokens

        if original_tokens <= effective_budget:
            return TrimResult(
                messages=messages,
                original_count=original_count,
                trimmed_count=0,
                original_tokens=original_tokens,
                final_tokens=original_tokens,
                strategy_used=strat,
            )

        if strat == TrimStrategy.SLIDING_WINDOW:
            return self._sliding_window(messages, effective_budget, original_tokens)
        elif strat == TrimStrategy.DROP_MIDDLE:
            return self._drop_middle(messages, effective_budget, original_tokens)
        elif strat == TrimStrategy.TRUNCATE:
            return self._truncate(messages, effective_budget, original_tokens)
        else:
            # Default to drop_middle if summarizer not available
            return self._drop_middle(messages, effective_budget, original_tokens)

    def _sliding_window(self, messages: List[Dict],
                        budget: int, original_tokens: int) -> TrimResult:
        """Keep only the most recent messages that fit."""
        # Always keep system messages
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        sys_tokens = count_messages_tokens(system_msgs, self.model)
        remaining = budget - sys_tokens
        kept = []
        dropped = []

        # Walk from most recent backwards
        for i, msg in reversed(list(enumerate(non_system))):
            msg_tokens = count_message_tokens(msg, self.model)
            if remaining >= msg_tokens:
                kept.insert(0, msg)
                remaining -= msg_tokens
            else:
                dropped.append(i)

        final = system_msgs + kept
        return TrimResult(
            messages=final,
            original_count=len(messages),
            trimmed_count=len(dropped),
            original_tokens=original_tokens,
            final_tokens=self.count(final),
            strategy_used=TrimStrategy.SLIDING_WINDOW,
            dropped_indices=sorted(dropped),
        )

    def _drop_middle(self, messages: List[Dict],
                     budget: int, original_tokens: int) -> TrimResult:
        """Keep first few + last few messages; drop the middle."""
        if len(messages) <= 2:
            return self._truncate(messages, budget, original_tokens)

        # Score all messages
        scored = [
            (i, msg, _message_priority(msg, i, len(messages)))
            for i, msg in enumerate(messages)
        ]
        # Sort by priority descending; greedily pick until budget exhausted
        scored_by_priority = sorted(scored, key=lambda x: -x[2])

        kept_indices = set()
        remaining = budget

        for i, msg, _ in scored_by_priority:
            t = count_message_tokens(msg, self.model)
            if remaining >= t:
                kept_indices.add(i)
                remaining -= t
            if remaining <= 0:
                break

        dropped = [i for i in range(len(messages)) if i not in kept_indices]
        final = [messages[i] for i in sorted(kept_indices)]

        return TrimResult(
            messages=final,
            original_count=len(messages),
            trimmed_count=len(dropped),
            original_tokens=original_tokens,
            final_tokens=self.count(final),
            strategy_used=TrimStrategy.DROP_MIDDLE,
            dropped_indices=sorted(dropped),
        )

    def _truncate(self, messages: List[Dict],
                  budget: int, original_tokens: int) -> TrimResult:
        """Simple head truncation: keep messages from the end."""
        kept = []
        dropped = []
        remaining = budget

        for i, msg in reversed(list(enumerate(messages))):
            t = count_message_tokens(msg, self.model)
            if remaining >= t:
                kept.insert(0, (i, msg))
                remaining -= t
            else:
                dropped.append(i)

        final = [msg for _, msg in kept]
        return TrimResult(
            messages=final,
            original_count=len(messages),
            trimmed_count=len(dropped),
            original_tokens=original_tokens,
            final_tokens=self.count(final),
            strategy_used=TrimStrategy.TRUNCATE,
            dropped_indices=sorted(dropped, reverse=True),
        )

    async def summarize_and_trim(self, messages: List[Dict],
                                  system_prompt: str = "") -> TrimResult:
        """
        Trim middle messages but first summarize them using the summarizer fn.
        Falls back to drop_middle if no summarizer is configured.
        """
        if not self._summarizer:
            return self.trim(messages, system_prompt, TrimStrategy.DROP_MIDDLE)

        original_tokens = self.count(messages)
        budget = self.available_tokens
        sys_tokens = count_tokens(system_prompt, self.model) if system_prompt else 0
        effective_budget = budget - sys_tokens

        if original_tokens <= effective_budget:
            return TrimResult(
                messages=messages,
                original_count=len(messages),
                trimmed_count=0,
                original_tokens=original_tokens,
                final_tokens=original_tokens,
                strategy_used=TrimStrategy.SUMMARIZE,
            )

        # Identify which messages to summarize (the middle ones)
        n = len(messages)
        keep_head = max(1, n // 5)
        keep_tail = max(2, n // 3)
        middle = messages[keep_head: n - keep_tail]
        head = messages[:keep_head]
        tail = messages[n - keep_tail:]

        try:
            summary_text = await self._summarizer(middle)
        except Exception as e:
            logger.warning(f"Summarizer failed: {e}, falling back to drop_middle")
            return self.trim(messages, system_prompt, TrimStrategy.DROP_MIDDLE)

        summary_msg = {
            "role": "system",
            "content": f"[Summary of {len(middle)} earlier messages]: {summary_text}",
        }
        final = head + [summary_msg] + tail
        return TrimResult(
            messages=final,
            original_count=n,
            trimmed_count=len(middle),
            original_tokens=original_tokens,
            final_tokens=self.count(final),
            strategy_used=TrimStrategy.SUMMARIZE,
            dropped_indices=list(range(keep_head, n - keep_tail)),
            summary_injected=True,
            summary_text=summary_text,
        )

    def prepare(self, messages: List[Dict],
                system_prompt: str = "",
                strategy: TrimStrategy = None) -> List[Dict]:
        """
        Convenience method: trim if needed and return the final message list.
        Does not modify the input list.
        """
        if self.fits(messages, system_prompt):
            return list(messages)
        result = self.trim(messages, system_prompt, strategy)
        logger.info(
            f"Context trimmed: {result.original_count}→"
            f"{len(result.messages)} messages, "
            f"{result.original_tokens}→{result.final_tokens} tokens "
            f"(saved {result.tokens_saved})"
        )
        return result.messages

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def count_ep(request):
            data = await request.json()
            msgs = data.get("messages", [])
            text = data.get("text", "")
            if text:
                tokens = self.count_text(text)
            else:
                tokens = self.count(msgs)
            return web.json_response({"tokens": tokens, "model": self.model})

        async def usage_ep(request):
            data = await request.json()
            msgs = data.get("messages", [])
            sys_p = data.get("system_prompt", "")
            return web.json_response(self.usage(msgs, sys_p))

        async def trim_ep(request):
            data = await request.json()
            msgs = data.get("messages", [])
            sys_p = data.get("system_prompt", "")
            strat = TrimStrategy(data.get("strategy", self._strategy))
            result = self.trim(msgs, sys_p, strat)
            return web.json_response({
                "messages": result.messages,
                "stats": result.to_dict(),
            })

        app.router.add_post(f"{prefix}/context/count",  count_ep)
        app.router.add_post(f"{prefix}/context/usage",  usage_ep)
        app.router.add_post(f"{prefix}/context/trim",   trim_ep)
        logger.info(f"Context window API routes registered at {prefix}/context/")
