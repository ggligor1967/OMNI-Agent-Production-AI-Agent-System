"""
OMNI AGENT - Observability & Tracing
Lightweight OpenTelemetry-inspired tracing for every LLM call,
tool execution, and pipeline run — with token counting and cost estimation.

No external dependencies. Stores spans in-memory with optional SQLite export.
"""
import time
import uuid
import json
import math
import logging
import statistics
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from contextlib import asynccontextmanager, contextmanager
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# COST MODEL
# ══════════════════════════════════════════════════════════════════════════════

# Estimated cost per 1M tokens (input/output) in USD
# These are approximate — update as pricing changes
MODEL_COSTS: Dict[str, Dict[str, float]] = {
    "qwen3-vl:235b-instruct-cloud": {"input": 4.0,  "output": 12.0},
    "qwen3-coder-next:cloud":       {"input": 3.0,  "output": 9.0},
    "glm-5:cloud":                  {"input": 3.5,  "output": 10.5},
    "deepseek-v3.1:671b-cloud":     {"input": 2.0,  "output": 8.0},
    "qwen3-coder:480b-cloud":       {"input": 5.0,  "output": 15.0},
    "gpt-oss:120b-cloud":           {"input": 3.0,  "output": 9.0},
    "gpt-oss:20b-cloud":            {"input": 0.8,  "output": 2.4},
    "gemma3:4b-cloud":              {"input": 0.2,  "output": 0.6},
    "mistral-large-3:675b-cloud":   {"input": 4.0,  "output": 12.0},
    "minimax-m2:cloud":             {"input": 3.0,  "output": 9.0},
    "cogito-2.1:671b-cloud":        {"input": 2.5,  "output": 7.5},
    "glm-4.7:cloud":                {"input": 1.5,  "output": 4.5},
    "gemini-3-flash-preview:cloud": {"input": 0.5,  "output": 1.5},
    "devstral-2:123b-cloud":        {"input": 2.5,  "output": 7.5},
    "devstral-small-2:24b-cloud":   {"input": 0.6,  "output": 1.8},
    "nemotron-3-nano:30b-cloud":    {"input": 0.4,  "output": 1.2},
    "qwen3-next:80b-cloud":         {"input": 1.0,  "output": 3.0},
    "rnj-1:8b-cloud":               {"input": 0.1,  "output": 0.3},
    "ministral-3:8b-cloud":         {"input": 0.1,  "output": 0.3},
    "qwen3-vl:235b-cloud":          {"input": 3.5,  "output": 10.5},
    "qwen3.5:cloud":                {"input": 0.5,  "output": 1.5},
    "kimi-k2.5:cloud":              {"input": 2.0,  "output": 6.0},
    "minimax-m2.5:cloud":           {"input": 3.5,  "output": 10.5},
    "gemma3:12b-cloud":             {"input": 0.3,  "output": 0.9},
}

def estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for a model call."""
    costs = MODEL_COSTS.get(model_id, {"input": 1.0, "output": 3.0})
    return (
        input_tokens  / 1_000_000 * costs["input"] +
        output_tokens / 1_000_000 * costs["output"]
    )

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars/token."""
    return max(1, len(text) // 4)


# ══════════════════════════════════════════════════════════════════════════════
# SPAN
# ══════════════════════════════════════════════════════════════════════════════

class SpanKind(str, Enum):
    LLM      = "llm"
    TOOL     = "tool"
    PIPELINE = "pipeline"
    RAG      = "rag"
    CACHE    = "cache"
    INTERNAL = "internal"


class SpanStatus(str, Enum):
    OK      = "ok"
    ERROR   = "error"
    PENDING = "pending"


@dataclass
class Span:
    """A single traced operation."""
    span_id: str
    trace_id: str
    name: str
    kind: SpanKind
    started_at: float
    parent_id: Optional[str] = None
    ended_at: Optional[float] = None
    status: SpanStatus = SpanStatus.PENDING
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict] = field(default_factory=list)
    error: str = ""

    @property
    def duration_ms(self) -> float:
        if self.ended_at is None:
            return (time.time() - self.started_at) * 1000
        return (self.ended_at - self.started_at) * 1000

    def end(self, status: SpanStatus = SpanStatus.OK, error: str = ""):
        self.ended_at = time.time()
        self.status = status
        self.error = error

    def add_event(self, name: str, attrs: Dict = None):
        self.events.append({
            "name": name,
            "ts": time.time(),
            "attrs": attrs or {},
        })

    def set(self, key: str, value: Any):
        self.attributes[key] = value

    def to_dict(self) -> Dict:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind.value,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 2),
            "started_at": self.started_at,
            "attributes": self.attributes,
            "events": self.events,
            "error": self.error,
        }


# ══════════════════════════════════════════════════════════════════════════════
# TRACER
# ══════════════════════════════════════════════════════════════════════════════

class Tracer:
    """
    Lightweight distributed-style tracer.

    Usage:
        with tracer.span("llm.chat", SpanKind.LLM) as span:
            span.set("model", "qwen3-next:80b-cloud")
            response = await llm.chat(...)
            span.set("output_tokens", response["eval_count"])

        # Or async:
        async with tracer.async_span("tool.web_search", SpanKind.TOOL) as span:
            ...

        # Get metrics:
        tracer.summary()
    """

    def __init__(self, max_spans: int = 10_000):
        self._spans: List[Span] = []
        self._max_spans = max_spans
        self._current_trace_id: Optional[str] = None

    def new_trace(self) -> str:
        """Start a new trace context. Returns trace_id."""
        self._current_trace_id = str(uuid.uuid4())[:12]
        return self._current_trace_id

    def start_span(self, name: str, kind: SpanKind = SpanKind.INTERNAL,
                   parent_id: str = None, trace_id: str = None) -> Span:
        span = Span(
            span_id=str(uuid.uuid4())[:8],
            trace_id=trace_id or self._current_trace_id or self.new_trace(),
            name=name,
            kind=kind,
            started_at=time.time(),
            parent_id=parent_id,
        )
        self._record(span)
        return span

    def _record(self, span: Span):
        if len(self._spans) >= self._max_spans:
            self._spans = self._spans[-(self._max_spans // 2):]
        self._spans.append(span)

    @contextmanager
    def span(self, name: str, kind: SpanKind = SpanKind.INTERNAL, **attrs):
        """Synchronous context manager for a span."""
        s = self.start_span(name, kind)
        for k, v in attrs.items():
            s.set(k, v)
        try:
            yield s
            s.end(SpanStatus.OK)
        except Exception as e:
            s.end(SpanStatus.ERROR, error=str(e))
            raise

    @asynccontextmanager
    async def async_span(self, name: str, kind: SpanKind = SpanKind.INTERNAL, **attrs):
        """Async context manager for a span."""
        s = self.start_span(name, kind)
        for k, v in attrs.items():
            s.set(k, v)
        try:
            yield s
            s.end(SpanStatus.OK)
        except Exception as e:
            s.end(SpanStatus.ERROR, error=str(e))
            raise

    # ── LLM-specific helper ───────────────────────────────────────────────────

    @asynccontextmanager
    async def llm_span(self, model_id: str, session_id: str = "",
                       prompt_text: str = ""):
        """
        Specialized span for LLM calls.
        Auto-computes token estimates and cost.
        """
        s = self.start_span(f"llm.{model_id}", SpanKind.LLM)
        s.set("model", model_id)
        s.set("session_id", session_id)
        input_tokens = estimate_tokens(prompt_text)
        s.set("input_tokens_est", input_tokens)
        s.set("cost_usd_est", 0.0)

        try:
            yield s
            # Caller should set output_tokens on span if available
            output_tokens = s.attributes.get("output_tokens", 0)
            if not output_tokens:
                output_tokens = s.attributes.get("output_tokens_est", 50)
            cost = estimate_cost(model_id, input_tokens, output_tokens)
            s.set("cost_usd_est", round(cost, 6))
            s.end(SpanStatus.OK)
        except Exception as e:
            s.end(SpanStatus.ERROR, error=str(e))
            raise

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_spans(self, kind: SpanKind = None,
                  trace_id: str = None,
                  last_n: int = None) -> List[Span]:
        spans = list(self._spans)
        if kind:
            spans = [s for s in spans if s.kind == kind]
        if trace_id:
            spans = [s for s in spans if s.trace_id == trace_id]
        if last_n:
            spans = spans[-last_n:]
        return spans

    def get_trace(self, trace_id: str) -> List[Span]:
        return [s for s in self._spans if s.trace_id == trace_id]

    def summary(self) -> Dict:
        """Aggregate metrics across all spans."""
        if not self._spans:
            return {"total_spans": 0}

        completed = [s for s in self._spans if s.ended_at is not None]
        errors = [s for s in completed if s.status == SpanStatus.ERROR]

        llm_spans = [s for s in completed if s.kind == SpanKind.LLM]
        total_cost = sum(s.attributes.get("cost_usd_est", 0) for s in llm_spans)
        total_input_tokens = sum(s.attributes.get("input_tokens_est", 0) for s in llm_spans)
        total_output_tokens = sum(s.attributes.get("output_tokens", 0) for s in llm_spans)

        by_model: Dict[str, Dict] = {}
        for s in llm_spans:
            m = s.attributes.get("model", "unknown")
            if m not in by_model:
                by_model[m] = {"calls": 0, "cost_usd": 0.0,
                               "total_tokens": 0, "latencies_ms": []}
            by_model[m]["calls"] += 1
            by_model[m]["cost_usd"] += s.attributes.get("cost_usd_est", 0)
            by_model[m]["total_tokens"] += (
                s.attributes.get("input_tokens_est", 0) +
                s.attributes.get("output_tokens", 0)
            )
            by_model[m]["latencies_ms"].append(s.duration_ms)

        # Compute p50/p95 per model
        for m, data in by_model.items():
            lats = sorted(data.pop("latencies_ms", []))
            data["p50_ms"] = round(lats[len(lats)//2], 1) if lats else 0
            data["p95_ms"] = round(lats[int(len(lats)*0.95)], 1) if lats else 0
            data["avg_ms"] = round(sum(lats)/len(lats), 1) if lats else 0
            data["cost_usd"] = round(data["cost_usd"], 6)

        # Overall latency histogram
        all_lats = sorted([s.duration_ms for s in completed])
        p50 = all_lats[len(all_lats)//2] if all_lats else 0
        p95 = all_lats[int(len(all_lats)*0.95)] if all_lats else 0

        return {
            "total_spans": len(self._spans),
            "completed_spans": len(completed),
            "error_spans": len(errors),
            "error_rate": round(len(errors)/max(len(completed),1), 3),
            "llm_calls": len(llm_spans),
            "total_cost_usd": round(total_cost, 6),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "p50_latency_ms": round(p50, 1),
            "p95_latency_ms": round(p95, 1),
            "by_model": by_model,
        }

    def model_leaderboard(self) -> List[Dict]:
        """Rank models by usage, cost, and latency."""
        summary = self.summary()
        models = []
        for m, data in summary.get("by_model", {}).items():
            models.append({
                "model": m,
                **data,
            })
        return sorted(models, key=lambda x: x["calls"], reverse=True)

    def recent_errors(self, limit: int = 10) -> List[Dict]:
        errors = [s for s in self._spans if s.status == SpanStatus.ERROR]
        return [s.to_dict() for s in errors[-limit:]]

    def export_jsonl(self, path: str):
        """Export all spans to a JSONL file for external analysis."""
        with open(path, "w") as f:
            for span in self._spans:
                f.write(json.dumps(span.to_dict()) + "\n")
        logger.info(f"Exported {len(self._spans)} spans to {path}")

    def clear(self):
        self._spans.clear()

    def __len__(self) -> int:
        return len(self._spans)


# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL TRACER INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

tracer = Tracer()
