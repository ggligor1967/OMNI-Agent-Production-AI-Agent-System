"""
OMNI AGENT - Conversation Summarizer
Automatically compresses long conversation histories to fit model context windows.
Strategies: sliding window, LLM summary, extractive key-points.
"""
import re
import time
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SummaryResult:
    original_messages: int
    compressed_messages: int
    strategy: str
    summary_text: str
    tokens_saved_estimate: int


class ConversationSummarizer:
    """
    Manages conversation history to prevent context overflow.

    Three strategies:
    1. SLIDING_WINDOW  — keep only the last N messages
    2. LLM_SUMMARY     — use LLM to summarize old messages into one block
    3. EXTRACTIVE       — keyword extraction of important points (no LLM needed)

    Usage:
        summarizer = ConversationSummarizer(llm=agent.llm)
        compressed, meta = await summarizer.maybe_compress(messages, threshold=20)
    """

    def __init__(self, llm=None, threshold: int = 20,
                 keep_recent: int = 6, max_summary_tokens: int = 500):
        self.llm = llm
        self.threshold = threshold          # compress when > this many messages
        self.keep_recent = keep_recent      # always keep last N messages verbatim
        self.max_summary_tokens = max_summary_tokens

    # ── Public API ────────────────────────────────────────────────────────────

    async def maybe_compress(
        self,
        messages: List[Dict],
        threshold: int = None,
        strategy: str = "auto",
    ) -> Tuple[List[Dict], Optional[SummaryResult]]:
        """
        Compress history if it exceeds threshold.
        Returns (compressed_messages, SummaryResult or None).
        strategy: 'auto' | 'sliding_window' | 'llm_summary' | 'extractive'
        """
        limit = threshold or self.threshold
        if len(messages) <= limit:
            return messages, None

        if strategy == "auto":
            strategy = "llm_summary" if self.llm else "extractive"

        logger.info(f"Compressing {len(messages)} messages → strategy={strategy}")

        if strategy == "sliding_window":
            return self._sliding_window(messages)
        elif strategy == "llm_summary":
            return await self._llm_summary(messages)
        elif strategy == "extractive":
            return self._extractive(messages)
        else:
            return self._sliding_window(messages)

    # ── Sliding Window ────────────────────────────────────────────────────────

    def _sliding_window(self, messages: List[Dict]) -> Tuple[List[Dict], SummaryResult]:
        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        kept = non_system[-self.keep_recent:]
        compressed = system + kept
        return compressed, SummaryResult(
            original_messages=len(messages),
            compressed_messages=len(compressed),
            strategy="sliding_window",
            summary_text=f"Kept last {self.keep_recent} messages",
            tokens_saved_estimate=(len(messages) - len(compressed)) * 50,
        )

    # ── LLM Summary ───────────────────────────────────────────────────────────

    async def _llm_summary(self, messages: List[Dict]) -> Tuple[List[Dict], SummaryResult]:
        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # Split: summarize old part, keep recent verbatim
        to_summarize = non_system[:-self.keep_recent]
        to_keep = non_system[-self.keep_recent:]

        if not to_summarize:
            return messages, None

        # Build summary prompt
        convo_text = "\n".join(
            f"{m['role'].upper()}: {str(m.get('content',''))[:300]}"
            for m in to_summarize
        )
        summary_prompt = (
            f"Summarize this conversation concisely (max {self.max_summary_tokens} words). "
            f"Preserve key facts, decisions, and context needed to continue:\n\n{convo_text}"
        )

        try:
            resp = await self.llm.chat(
                messages=[{"role": "user", "content": summary_prompt}],
                model="gpt-oss:20b-cloud",  # use fast model for summaries
                temperature=0.3,
                session_id="summarizer",
                auto_route=False,
            )
            summary_text = resp.get("content", "")
        except Exception as e:
            logger.warning(f"LLM summary failed: {e}, falling back to extractive")
            return self._extractive(messages)

        # Inject summary as a system note
        summary_msg = {
            "role": "system",
            "content": f"[CONVERSATION SUMMARY — {len(to_summarize)} earlier messages]\n{summary_text}",
        }

        compressed = system + [summary_msg] + to_keep
        return compressed, SummaryResult(
            original_messages=len(messages),
            compressed_messages=len(compressed),
            strategy="llm_summary",
            summary_text=summary_text,
            tokens_saved_estimate=max(0, len(convo_text.split()) - len(summary_text.split())) * 4,
        )

    # ── Extractive Summary ────────────────────────────────────────────────────

    def _extractive(self, messages: List[Dict]) -> Tuple[List[Dict], SummaryResult]:
        """
        No-LLM extractive summarization:
        - Extract questions (? sentences) from user turns
        - Extract first sentence from each assistant turn
        """
        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        to_compress = non_system[:-self.keep_recent]
        to_keep = non_system[-self.keep_recent:]

        key_points = []
        for msg in to_compress:
            content = str(msg.get("content", ""))
            role = msg.get("role", "")
            if role == "user":
                # Keep questions and short messages
                sentences = re.split(r'(?<=[.!?])\s+', content)
                qs = [s for s in sentences if "?" in s]
                key_points.append(f"User asked: {qs[0][:150]}" if qs
                                  else f"User: {content[:100]}")
            elif role == "assistant":
                first_sent = re.split(r'(?<=[.!?])\s+', content)
                key_points.append(f"Assistant: {first_sent[0][:150]}")

        summary_text = "\n".join(key_points[:20])
        summary_msg = {
            "role": "system",
            "content": f"[PRIOR CONTEXT — {len(to_compress)} earlier messages]\n{summary_text}",
        }

        compressed = system + [summary_msg] + to_keep
        return compressed, SummaryResult(
            original_messages=len(messages),
            compressed_messages=len(compressed),
            strategy="extractive",
            summary_text=summary_text,
            tokens_saved_estimate=(len(to_compress)) * 40,
        )

    # ── Token Estimator ───────────────────────────────────────────────────────

    @staticmethod
    def estimate_tokens(messages: List[Dict]) -> int:
        """Rough estimate: ~4 chars per token."""
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        return total_chars // 4

    def needs_compression(self, messages: List[Dict],
                          model_context: int = 131072) -> bool:
        """True if estimated token count exceeds 80% of model's context window."""
        estimated = self.estimate_tokens(messages)
        return estimated > (model_context * 0.8)

    def compression_stats(self, messages: List[Dict]) -> Dict:
        return {
            "message_count": len(messages),
            "estimated_tokens": self.estimate_tokens(messages),
            "needs_compression_128k": self.needs_compression(messages, 131072),
            "needs_compression_32k": self.needs_compression(messages, 32768),
        }
