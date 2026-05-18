"""
OMNI AGENT - Observability & Tracing
Lightweight in-memory tracing with optional OpenTelemetry-compatible export.

Tracing is safe-by-default:
- in-memory span recording always remains available
- OpenTelemetry export is disabled unless OTEL_ENABLED=true
- unsupported exporters or missing SDK packages degrade to a no-op backend
- sensitive attributes are redacted before they are stored or exported
"""
import hashlib
import importlib
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from config import CONFIG

logger = logging.getLogger(__name__)

_DEFAULT_SERVICE_NAME = "omni-agent"
_ALLOWED_EXPORTERS = {"none", "console", "otlp"}
_REDACTED = "[REDACTED]"
_SENSITIVE_ATTRIBUTE_HINTS = (
    "authorization",
    "api_key",
    "apikey",
    "token",
    "password",
    "secret",
    "prompt",
    "message",
    "body",
    "payload",
    "content",
    "raw_",
    "embedding",
    "document_chunk",
    "memory_content",
)
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9_\-\.=:/+]+\b"),
    re.compile(r"(?i)\b(?:api[-_ ]?key|password|token|secret)\b"),
    re.compile(r"(?i)\bsk-[a-z0-9]{10,}\b"),
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_]{10,}\b"),
    re.compile(r"(?i)\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\beyJ[a-zA-Z0-9_\-]{10,}\b"),
)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_exporter(value: Any) -> str:
    exporter = str(value or "none").strip().lower()
    return exporter if exporter in _ALLOWED_EXPORTERS else "none"


def _normalize_sample_rate(value: Any) -> float:
    try:
        sample_rate = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(sample_rate, 1.0))


def _is_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    if key_lower in {
        "input_tokens",
        "input_tokens_est",
        "output_tokens",
        "output_tokens_est",
        "cost_usd_est",
        "status_code",
        "http.status_code",
    }:
        return False
    return any(hint in key_lower for hint in _SENSITIVE_ATTRIBUTE_HINTS)


def _looks_sensitive_value(value: str) -> bool:
    compact = value.strip()
    if not compact:
        return False
    return any(pattern.search(compact) for pattern in _SENSITIVE_VALUE_PATTERNS)


def _sanitize_string_value(value: str) -> str:
    if _looks_sensitive_value(value):
        return _REDACTED
    if len(value) > 120:
        return f"<str:{len(value)}>"
    return value


def _sanitize_attribute_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return _REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):
        return _sanitize_string_value(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, Mapping):
        return f"<dict:{len(value)}>"
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"<{type(value).__name__}:{len(value)}>"
    return _sanitize_string_value(str(value))


def sanitize_attributes(attributes: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        safe_key = str(key)
        safe[safe_key] = _sanitize_attribute_value(safe_key, value)
    return safe


def _hash_identifier(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest[:16]}"


def _ns(timestamp: Optional[float]) -> Optional[int]:
    if timestamp is None:
        return None
    return int(timestamp * 1_000_000_000)


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
        input_tokens / 1_000_000 * costs["input"]
        + output_tokens / 1_000_000 * costs["output"]
    )


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars/token."""
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class TracingConfig:
    enabled: bool = False
    service_name: str = _DEFAULT_SERVICE_NAME
    exporter: str = "none"
    endpoint: str = ""
    sample_rate: float = 1.0

    @classmethod
    def from_config(cls, config_obj: Any = None) -> "TracingConfig":
        cfg = config_obj or CONFIG
        return cls(
            enabled=_coerce_bool(getattr(cfg, "OTEL_ENABLED", False)),
            service_name=(
                str(getattr(cfg, "OTEL_SERVICE_NAME", _DEFAULT_SERVICE_NAME) or _DEFAULT_SERVICE_NAME).strip()
                or _DEFAULT_SERVICE_NAME
            ),
            exporter=_normalize_exporter(getattr(cfg, "OTEL_EXPORTER", "none")),
            endpoint=str(getattr(cfg, "OTEL_ENDPOINT", "") or "").strip(),
            sample_rate=_normalize_sample_rate(getattr(cfg, "OTEL_SAMPLE_RATE", 1.0)),
        )

    @classmethod
    def from_env(cls) -> "TracingConfig":
        return cls.from_mapping(dict(os.environ))

    @classmethod
    def from_mapping(cls, mapping: Optional[Mapping[str, Any]] = None) -> "TracingConfig":
        source = mapping or {}
        return cls(
            enabled=_coerce_bool(source.get("OTEL_ENABLED", False)),
            service_name=(
                str(source.get("OTEL_SERVICE_NAME", _DEFAULT_SERVICE_NAME) or _DEFAULT_SERVICE_NAME).strip()
                or _DEFAULT_SERVICE_NAME
            ),
            exporter=_normalize_exporter(source.get("OTEL_EXPORTER", "none")),
            endpoint=str(source.get("OTEL_ENDPOINT", "") or "").strip(),
            sample_rate=_normalize_sample_rate(source.get("OTEL_SAMPLE_RATE", 1.0)),
        )


class TelemetryProviderBase:
    name = "noop"
    active = False
    reason = "OpenTelemetry export disabled"

    def attach_span(self, span: "Span", parent: Optional["Span"] = None) -> None:
        return None

    def set_attribute(self, span: "Span", key: str, value: Any) -> None:
        return None

    def add_event(self, span: "Span", name: str, attrs: Optional[Mapping[str, Any]] = None) -> None:
        return None

    def end_span(self, span: "Span") -> None:
        return None

    def shutdown(self) -> None:
        return None


class NoOpTelemetryProvider(TelemetryProviderBase):
    def __init__(self, reason: str = "OpenTelemetry export disabled"):
        self.reason = reason


class OpenTelemetryProvider(TelemetryProviderBase):
    name = "opentelemetry"
    active = True

    def __init__(self, settings: TracingConfig):
        trace_api = importlib.import_module("opentelemetry.trace")
        trace_status = importlib.import_module("opentelemetry.trace.status")
        sdk_trace = importlib.import_module("opentelemetry.sdk.trace")
        resource_mod = importlib.import_module("opentelemetry.sdk.resources")
        export_mod = importlib.import_module("opentelemetry.sdk.trace.export")
        sampling_mod = importlib.import_module("opentelemetry.sdk.trace.sampling")

        resource = resource_mod.Resource.create({"service.name": settings.service_name})
        sampler = sampling_mod.ParentBased(sampling_mod.TraceIdRatioBased(settings.sample_rate))
        self._provider = sdk_trace.TracerProvider(resource=resource, sampler=sampler)
        self._provider.add_span_processor(self._build_processor(settings, export_mod))
        self._tracer = self._provider.get_tracer("omni-agent.tracing")
        self._span_kind = trace_api.SpanKind
        self._set_span_in_context = trace_api.set_span_in_context
        self._status = trace_status.Status
        self._status_code = trace_status.StatusCode
        self.reason = f"OpenTelemetry export active via {settings.exporter}"

    def _build_processor(self, settings: TracingConfig, export_mod: Any):
        if settings.exporter == "console":
            exporter = export_mod.ConsoleSpanExporter()
            return export_mod.SimpleSpanProcessor(exporter)
        if settings.exporter == "otlp":
            try:
                otlp_mod = importlib.import_module(
                    "opentelemetry.exporter.otlp.proto.http.trace_exporter"
                )
            except ImportError as exc:
                raise RuntimeError("OTLP exporter unavailable") from exc

            kwargs = {"endpoint": settings.endpoint} if settings.endpoint else {}
            exporter = otlp_mod.OTLPSpanExporter(**kwargs)
            return export_mod.SimpleSpanProcessor(exporter)
        raise RuntimeError(f"Unsupported OTEL exporter '{settings.exporter}'")

    def _map_kind(self, kind: "SpanKind"):
        if kind == SpanKind.HTTP:
            return self._span_kind.SERVER
        return self._span_kind.INTERNAL

    def attach_span(self, span: "Span", parent: Optional["Span"] = None) -> None:
        parent_context = None
        if parent is not None and parent._provider_span is not None:
            parent_context = self._set_span_in_context(parent._provider_span)

        otel_span = self._tracer.start_span(
            name=span.name,
            context=parent_context,
            kind=self._map_kind(span.kind),
            start_time=_ns(span.started_at),
        )
        span._provider_span = otel_span

        ctx = otel_span.get_span_context()
        span.trace_id = f"{ctx.trace_id:032x}"
        span.span_id = f"{ctx.span_id:016x}"
        if parent is not None and parent._provider_span is not None:
            span.parent_id = f"{parent._provider_span.get_span_context().span_id:016x}"

    def set_attribute(self, span: "Span", key: str, value: Any) -> None:
        if span._provider_span is None:
            return
        span._provider_span.set_attribute(key, value)

    def add_event(self, span: "Span", name: str, attrs: Optional[Mapping[str, Any]] = None) -> None:
        if span._provider_span is None:
            return
        span._provider_span.add_event(name, attributes=dict(attrs or {}), timestamp=_ns(time.time()))

    def end_span(self, span: "Span") -> None:
        if span._provider_span is None:
            return

        if span.status == SpanStatus.ERROR:
            span._provider_span.set_status(
                self._status(status_code=self._status_code.ERROR, description=span.error or None)
            )
            if span.error:
                span._provider_span.add_event(
                    "error",
                    attributes={"error": span.error},
                    timestamp=_ns(span.ended_at),
                )
        else:
            span._provider_span.set_status(self._status(status_code=self._status_code.OK))

        span._provider_span.end(end_time=_ns(span.ended_at))

    def shutdown(self) -> None:
        self._provider.shutdown()


def _build_telemetry_provider(settings: TracingConfig) -> TelemetryProviderBase:
    if not settings.enabled:
        return NoOpTelemetryProvider("OpenTelemetry export disabled via OTEL_ENABLED=false")
    if settings.exporter == "none":
        return NoOpTelemetryProvider("OpenTelemetry exporter disabled via OTEL_EXPORTER=none")
    try:
        return OpenTelemetryProvider(settings)
    except Exception as exc:  # pragma: no cover - exercised via status-focused tests
        logger.warning("OpenTelemetry tracing unavailable; falling back to in-memory spans: %s", exc)
        return NoOpTelemetryProvider(f"OpenTelemetry unavailable: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# SPAN
# ══════════════════════════════════════════════════════════════════════════════


class SpanKind(str, Enum):
    LLM = "llm"
    TOOL = "tool"
    PIPELINE = "pipeline"
    RAG = "rag"
    CACHE = "cache"
    MEMORY = "memory"
    SANDBOX = "sandbox"
    HTTP = "http"
    INTERNAL = "internal"


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
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
    events: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
    _provider: Optional[TelemetryProviderBase] = field(default=None, repr=False, compare=False)
    _provider_span: Any = field(default=None, repr=False, compare=False)

    @property
    def duration_ms(self) -> float:
        if self.ended_at is None:
            return (time.time() - self.started_at) * 1000
        return (self.ended_at - self.started_at) * 1000

    def end(self, status: SpanStatus = SpanStatus.OK, error: str = ""):
        self.ended_at = time.time()
        self.status = status
        self.error = _sanitize_string_value(error) if error else ""
        if self._provider is not None:
            self._provider.end_span(self)

    def add_event(self, name: str, attrs: Optional[Mapping[str, Any]] = None):
        safe_attrs = sanitize_attributes(attrs)
        event = {
            "name": _sanitize_string_value(name),
            "ts": time.time(),
            "attrs": safe_attrs,
        }
        self.events.append(event)
        if self._provider is not None:
            self._provider.add_event(self, event["name"], safe_attrs)

    def set(self, key: str, value: Any):
        safe_key = str(key)
        safe_value = _sanitize_attribute_value(safe_key, value)
        self.attributes[safe_key] = safe_value
        if self._provider is not None:
            self._provider.set_attribute(self, safe_key, safe_value)

    def to_dict(self) -> Dict[str, Any]:
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
    Lightweight tracer that keeps an in-memory span history and can optionally
    mirror spans to an OpenTelemetry-compatible exporter.
    """

    def __init__(
        self,
        max_spans: int = 10_000,
        settings: Optional[TracingConfig] = None,
        provider: Optional[TelemetryProviderBase] = None,
    ):
        self._spans: List[Span] = []
        self._max_spans = max_spans
        self._current_trace_id: Optional[str] = None
        self._current_span: ContextVar[Optional[Span]] = ContextVar(
            f"omni_current_span_{id(self)}",
            default=None,
        )
        self._settings = settings or TracingConfig.from_config(CONFIG)
        self._provider = provider or _build_telemetry_provider(self._settings)

    def telemetry_status(self) -> Dict[str, Any]:
        return {
            "enabled": self._settings.enabled,
            "service_name": self._settings.service_name,
            "exporter": self._settings.exporter,
            "endpoint": self._settings.endpoint,
            "sample_rate": self._settings.sample_rate,
            "provider": self._provider.name,
            "active": self._provider.active,
            "reason": self._provider.reason,
        }

    def reconfigure(self, settings: Optional[TracingConfig] = None) -> Dict[str, Any]:
        try:
            self._provider.shutdown()
        except Exception as exc:  # pragma: no cover - defensive shutdown guard
            logger.debug("Telemetry provider shutdown failed during reconfigure: %s", exc)

        self._settings = settings or TracingConfig.from_config(CONFIG)
        self._provider = _build_telemetry_provider(self._settings)
        return self.telemetry_status()

    def new_trace(self) -> str:
        """Start a new trace context. Returns a 32-char trace_id."""
        self._current_trace_id = uuid.uuid4().hex
        return self._current_trace_id

    def start_span(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        parent_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Span:
        active_parent = self._current_span.get()
        resolved_trace_id = (
            trace_id
            or (active_parent.trace_id if active_parent is not None else None)
            or self._current_trace_id
            or self.new_trace()
        )
        span = Span(
            span_id=uuid.uuid4().hex[:16],
            trace_id=resolved_trace_id,
            name=name,
            kind=kind,
            started_at=time.time(),
            parent_id=parent_id or (active_parent.span_id if active_parent is not None else None),
            _provider=self._provider,
        )
        self._provider.attach_span(span, parent=active_parent)
        self._current_trace_id = span.trace_id
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
        for key, value in attrs.items():
            s.set(key, value)
        token = self._current_span.set(s)
        try:
            yield s
            s.end(SpanStatus.OK)
        except Exception as exc:
            s.end(SpanStatus.ERROR, error=str(exc))
            raise
        finally:
            self._current_span.reset(token)

    @asynccontextmanager
    async def async_span(self, name: str, kind: SpanKind = SpanKind.INTERNAL, **attrs):
        """Async context manager for a span."""
        s = self.start_span(name, kind)
        for key, value in attrs.items():
            s.set(key, value)
        token = self._current_span.set(s)
        try:
            yield s
            s.end(SpanStatus.OK)
        except Exception as exc:
            s.end(SpanStatus.ERROR, error=str(exc))
            raise
        finally:
            self._current_span.reset(token)

    @asynccontextmanager
    async def llm_span(self, model_id: str, session_id: str = "", prompt_text: str = ""):
        """
        Specialized span for LLM calls.
        Auto-computes token estimates and cost without storing raw prompt text.
        """
        s = self.start_span(f"llm.{model_id}", SpanKind.LLM)
        s.set("model", model_id)
        if session_id:
            s.set("session_id_hash", _hash_identifier(session_id))
        input_tokens = estimate_tokens(prompt_text)
        s.set("input_tokens_est", input_tokens)
        s.set("cost_usd_est", 0.0)

        token = self._current_span.set(s)
        try:
            yield s
            output_tokens = s.attributes.get("output_tokens", 0)
            if not output_tokens:
                output_tokens = s.attributes.get("output_tokens_est", 50)
            cost = estimate_cost(model_id, input_tokens, int(output_tokens))
            s.set("cost_usd_est", round(cost, 6))
            s.end(SpanStatus.OK)
        except Exception as exc:
            s.end(SpanStatus.ERROR, error=str(exc))
            raise
        finally:
            self._current_span.reset(token)

    def get_spans(
        self,
        kind: Optional[SpanKind] = None,
        trace_id: Optional[str] = None,
        last_n: Optional[int] = None,
    ) -> List[Span]:
        spans = list(self._spans)
        if kind:
            spans = [span for span in spans if span.kind == kind]
        if trace_id:
            spans = [span for span in spans if span.trace_id == trace_id]
        if last_n:
            spans = spans[-last_n:]
        return spans

    def get_trace(self, trace_id: str) -> List[Span]:
        return [span for span in self._spans if span.trace_id == trace_id]

    def summary(self) -> Dict[str, Any]:
        """Aggregate metrics across all spans."""
        if not self._spans:
            return {"total_spans": 0, "telemetry": self.telemetry_status()}

        completed = [span for span in self._spans if span.ended_at is not None]
        errors = [span for span in completed if span.status == SpanStatus.ERROR]
        llm_spans = [span for span in completed if span.kind == SpanKind.LLM]

        total_cost = sum(span.attributes.get("cost_usd_est", 0) for span in llm_spans)
        total_input_tokens = sum(span.attributes.get("input_tokens_est", 0) for span in llm_spans)
        total_output_tokens = sum(span.attributes.get("output_tokens", 0) for span in llm_spans)

        by_model: Dict[str, Dict[str, Any]] = {}
        for span in llm_spans:
            model_id = span.attributes.get("model", "unknown")
            if model_id not in by_model:
                by_model[model_id] = {
                    "calls": 0,
                    "cost_usd": 0.0,
                    "total_tokens": 0,
                    "latencies_ms": [],
                }
            by_model[model_id]["calls"] += 1
            by_model[model_id]["cost_usd"] += span.attributes.get("cost_usd_est", 0)
            by_model[model_id]["total_tokens"] += (
                span.attributes.get("input_tokens_est", 0)
                + span.attributes.get("output_tokens", 0)
            )
            by_model[model_id]["latencies_ms"].append(span.duration_ms)

        for model_id, data in by_model.items():
            latencies = sorted(data.pop("latencies_ms", []))
            data["p50_ms"] = round(latencies[len(latencies) // 2], 1) if latencies else 0
            data["p95_ms"] = round(latencies[int(len(latencies) * 0.95)], 1) if latencies else 0
            data["avg_ms"] = round(sum(latencies) / len(latencies), 1) if latencies else 0
            data["cost_usd"] = round(data["cost_usd"], 6)

        all_latencies = sorted(span.duration_ms for span in completed)
        p50 = all_latencies[len(all_latencies) // 2] if all_latencies else 0
        p95 = all_latencies[int(len(all_latencies) * 0.95)] if all_latencies else 0

        return {
            "total_spans": len(self._spans),
            "completed_spans": len(completed),
            "error_spans": len(errors),
            "error_rate": round(len(errors) / max(len(completed), 1), 3),
            "llm_calls": len(llm_spans),
            "total_cost_usd": round(total_cost, 6),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "p50_latency_ms": round(p50, 1),
            "p95_latency_ms": round(p95, 1),
            "by_model": by_model,
            "telemetry": self.telemetry_status(),
        }

    def model_leaderboard(self) -> List[Dict[str, Any]]:
        """Rank models by usage, cost, and latency."""
        summary = self.summary()
        models = []
        for model_id, data in summary.get("by_model", {}).items():
            models.append({"model": model_id, **data})
        return sorted(models, key=lambda item: item["calls"], reverse=True)

    def recent_errors(self, limit: int = 10) -> List[Dict[str, Any]]:
        errors = [span for span in self._spans if span.status == SpanStatus.ERROR]
        return [span.to_dict() for span in errors[-limit:]]

    def export_jsonl(self, path: str):
        """Export all spans to a JSONL file for external analysis."""
        with open(path, "w", encoding="utf-8") as handle:
            for span in self._spans:
                handle.write(json.dumps(span.to_dict()) + "\n")
        logger.info("Exported %s spans to %s", len(self._spans), path)

    def clear(self):
        self._spans.clear()
        self._current_trace_id = None

    def shutdown(self) -> None:
        self._provider.shutdown()

    def __len__(self) -> int:
        return len(self._spans)


tracer = Tracer()

__all__ = [
    "MODEL_COSTS",
    "TracingConfig",
    "SpanKind",
    "SpanStatus",
    "Span",
    "Tracer",
    "estimate_cost",
    "estimate_tokens",
    "tracer",
]
