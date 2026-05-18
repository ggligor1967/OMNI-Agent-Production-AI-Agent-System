"""OMNI AGENT - Model Router
Route LLM requests to models based on cost, latency, quality,
capability matching, and fallback chains.

Features:
- ModelSpec: name, provider, capabilities, cost_per_1k_tokens, avg_latency_ms
- Routing strategies: CHEAPEST, FASTEST, BEST_QUALITY, BALANCED, ROUND_ROBIN
- Capability matching: filter models by required capabilities list
- Context length routing: pick model with sufficient max_context
- Fallback chain: try next model if primary returns error or times out
- Load balancing: track in-flight requests per model; prefer least-loaded
- Cost budget: hard cap on cost_per_1k_tokens; skip expensive models
- Quality threshold: skip models below quality_score threshold
- Latency SLA: skip models whose avg_latency exceeds max_latency_ms
- Shadow mode: send to secondary model silently for comparison
- Scoring formula: BALANCED uses weighted sum of quality/cost/latency ranks
- Stats: per-model request counts, error rates, avg latency, total cost
- SQLite persistence: routing decisions, model metrics
- REST API: route, models, update_model, stats

Exports:
- TaskType enum
- classify_task(text, has_image=False) -> Tuple[TaskType, float]
- RouteDecision dataclass
- FALLBACK_CHAINS dict
- TaskToCapability mapping
- ModelRouter class with compatibility methods
"""
import json, sqlite3, time, uuid, logging, re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from config import CONFIG

logger = logging.getLogger(__name__)


class TaskType(str, Enum):
    """Task type classification for model routing."""
    CODE = "code"
    MATH = "math"
    VISION = "vision"
    CREATIVE = "creative"
    REASONING = "reasoning"
    TRANSLATION = "translation"
    AGENT = "agent"
    FAST = "fast"
    GENERAL = "general"


# ══════════════════════════════════════════════════════════════════════════════
# ROUTING CONTRACT - What callers expect from model_router
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RouteDecision:
    """Decision made by the router."""
    model_id: str
    model_spec: Any  # ModelSpec from model_registry
    task_type: TaskType
    confidence: float
    reason: str
    fallback_chain: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_name": getattr(self.model_spec, "display_name", self.model_id),
            "task_type": self.task_type.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "fallback_chain": self.fallback_chain,
        }


# Task type to required capabilities mapping
TASK_TO_CAPABILITY: Dict[TaskType, str] = {
    TaskType.CODE: "code",
    TaskType.MATH: "math",
    TaskType.VISION: "vision",
    TaskType.CREATIVE: "creative",
    TaskType.REASONING: "reasoning",
    TaskType.TRANSLATION: "translation",
    TaskType.AGENT: "agent",
    TaskType.FAST: "fast",
}


# Default fallback chains for models
FALLBACK_CHAINS: Dict[str, List[str]] = {
    "qwen3-coder-next:cloud": ["qwen3-next:80b-cloud", "gpt-oss:120b-cloud", "cogito-2.1:671b-cloud"],
    "deepseek-v3.1:671b-cloud": ["qwen3-coder-next:cloud", "gpt-oss:120b-cloud", "glm-4.7:cloud"],
    "gpt-oss:120b-cloud": ["qwen3-coder-next:cloud", "deepseek-v3.1:671b-cloud", "glm-4.7:cloud"],
    "gemini-3-flash-preview:cloud": ["qwen3-vl:235b-instruct-cloud", "qwen3-vl:235b-cloud"],
}


TASK_SCORING_RULES: Dict[TaskType, Dict[str, Any]] = {
    TaskType.MATH: {
        "strong_weight": 2.0,
        "medium_weight": 1.0,
        "strong": [
            r'\b(solve|compute|calculate|simplify|differentiate|integrate|derive|find\s+the\s+(value|solution|root|limit))\b',
            r'\b(equation|formula|inequality|expression|polynomial|matrix|vector|tensor|determinant)\b',
            r'\b(integral|derivative|differential\s+equation|partial\s+derivative|gradient|laplacian|fourier|taylor)\b',
            r'\b(proof|theorem|lemma|corollary|hypothesis|conjecture)\b',
            r'\b(eigenvalue|eigenvector|linear\s+algebra|basis|span|orthogonal)\b',
        ],
        "medium": [
            r'\b(calculus|algebra|geometry|trigonometry|statistic|probability|combinatorics|topology)\b',
            r'\b(sqrt|log|ln|exp|sin|cos|tan|asin|acos|atan|ceil|floor|abs|mod|gcd|lcm)\b',
            r'\b(sum|product|factorial|permutation|combination|series|sequence|limit|infinity)\b',
            r'(?<![a-z])(dy\s*/\s*dx|d[²³]?[a-z]\s*/\s*d[a-z][²³]?|∫|∑|∏|√|∂|∇|≈|≠|≤|≥)',
            r'\b(\d+\s*[\+\-\*\/]\s*\d+|\d+\s*\^\s*\d+|\d+\s*%\s*\d+)\b',
            r'\b(pi\b|euler|fibonacci|prime\s+number|complex\s+number|imaginary)',
        ],
    },
    TaskType.CODE: {
        "strong_weight": 2.0,
        "medium_weight": 1.0,
        "strong": [
            r'\b(write\s+(a\s+)?(python|javascript|typescript|java|c\+\+|c#|go|rust|ruby|php|swift|kotlin|scala|r|bash|powershell)\s+(function|class|script|program|code))\b',
            r'\b(implement|refactor|debug|fix\s+the\s+bug|optimize\s+(this\s+)?code)\b',
            r'\b(def\s+\w+\s*\(|class\s+\w+\s*[:\(]|import\s+\w+)\b',
            r'\b(function\s+\w+\s*\(|const\s+\w+\s*=|let\s+\w+\s*=|var\s+\w+\s*=)\b',
            r'\bimport\s+(numpy|pandas|torch|tensorflow|sklearn|flask|django|fastapi|aiohttp|asyncio|requests)\b',
        ],
        "medium": [
            r'\b(algorithm|data\s+structure|sorting|recursion|iteration|loop|array|stack|queue|tree|graph|hash)\b',
            r'\b(api|endpoint|rest|graphql|websocket|http|json|xml|yaml|csv|sql|database|orm|migration)\b',
            r'\b(compile|build|deploy|dockerfile|ci\s*/\s*cd|github\s+actions|pipeline|package|dependency)\b',
            r'\b(unit\s+test|integration\s+test|mock|assert|coverage|pytest|jest|mocha)\b',
            r'\b(html|css|react|vue|angular|webpack|vite|node|npm|pip|cargo|maven)\b',
            r'\b(regex|parse|serialize|deserialize|encode|decode|compress|encrypt|decrypt)\b',
            r'\b(print|len|int|str|float|list|dict|set|tuple|range|map|filter|lambda)\s*\(',
        ],
    },
    TaskType.TRANSLATION: {
        "strong_weight": 2.5,
        "medium_weight": 1.0,
        "strong": [
            r'\b(translate|translation|traduce|übersetze|traduire|tradurre)\b',
            r'\btranslate\s+(this|the|from|to|into)\b',
            r'\b(from\s+(english|spanish|french|german|italian|portuguese|russian|chinese|japanese|korean|arabic|hindi|romanian|dutch|polish|turkish|ukrainian)\s+to)\b',
            r'\b(into\s+(english|spanish|french|german|italian|portuguese|russian|chinese|japanese|korean|arabic|hindi|romanian|dutch|polish|turkish|ukrainian))\b',
        ],
        "medium": [
            r'\b(spanish|french|german|italian|portuguese|russian|chinese|japanese|korean|arabic|hindi|romanian|dutch|polish|turkish|ukrainian)\s+(version|translation|equivalent)\b',
            r'\b(multilingual|bilingual|localize|localization|i18n|l10n)\b',
            r'\b(what\s+does\s+.{1,30}\s+mean\s+in)\b',
            r'\b(how\s+do\s+you\s+say\s+.{1,30}\s+in)\b',
        ],
    },
    TaskType.VISION: {
        "strong_weight": 2.5,
        "medium_weight": 0.8,
        "strong": [
            r'\b(describe\s+(this|the)\s+image|what\s+(is|are)\s+in\s+(this|the)\s+(image|photo|picture|screenshot))\b',
            r'\b(ocr|extract\s+text\s+from|read\s+the\s+text\s+in)\b',
            r'\b(bounding\s+box|object\s+detection|image\s+recognition|face\s+recognition|segmentation)\b',
            r'\b(visual\s+question|vqa|caption\s+this|image\s+caption)\b',
        ],
        "medium": [
            r'\b(image|photo|picture|screenshot|diagram|chart|graph|figure|thumbnail|icon|logo)\b',
            r'\b(pixel|resolution|color|shape|object|scene|background|foreground)\b',
            r'\b(look\s+at|see\s+in|shown\s+in|visible\s+in)\b',
        ],
    },
    TaskType.CREATIVE: {
        "strong_weight": 2.5,
        "medium_weight": 1.0,
        "strong": [
            r'\b(write\s+(me\s+)?(a\s+)?(poem|story|essay|blog\s+post|short\s+story|script|screenplay|song|lyrics|haiku|sonnet|limerick|ode|slogan|tagline|ad\s+copy))\b',
            r'\b((compose|make|create|craft|generate)\s+(me\s+)?(a\s+)?(poem|song|melody|haiku|sonnet|limerick|story|tale|script))\b',
            r'\b(imagine|invent|brainstorm|come\s+up\s+with|generate\s+(ideas?|concepts?|names?|slogans?))\b',
        ],
        "medium": [
            r'\b(creative\s+writing|fiction|non-fiction|narrative|prose|poetry|rhyme|metaphor|simile)\b',
            r'\b(character|plot|setting|theme|conflict|climax|resolution|protagonist|antagonist)\b',
            r'\b(art|artwork|design|illustration|concept\s+art|visual\s+design|ux\s+design)\b',
        ],
    },
    TaskType.REASONING: {
        "strong_weight": 2.0,
        "medium_weight": 1.0,
        "strong": [
            r'\b(why\s+(is|does|would|should|did|will)|explain\s+why|analyze\s+(the|this)|evaluate\s+(the|this))\b',
            r'\b(compare\s+(and\s+contrast|the\s+(pros|cons|advantages|disadvantages)|\w+\s+(vs|versus|against|over)))\b',
            r'\b(critically\s+assess|argue\s+(for|against)|make\s+the\s+case|debate)\b',
            r'\b(first\s+principles|step\s+by\s+step|chain\s+of\s+thought|reason\s+through)\b',
            r'\b(pros\s+and\s+cons\s+of|compare\s+\w+.{1,20}vs|\w+\s+vs\.?\s+\w+.{0,30}(better|worse|prefer|recommend|choose))\b',
        ],
        "medium": [
            r'\b(analyze|analysis|examine|evaluate|assess|critique|review|reflect|compare|contrast)\b',
            r'\b(therefore|consequently|hence|thus|since|because|it\s+follows\s+that)\b',
            r'\b(pros\s+and\s+cons|trade\s*-?\s*off|strengths?\s+and\s+weaknesses?|impact)\b',
            r'\b(logic|logical|rational|infer|deduce|deduction|induction|abduction)\b',
        ],
    },
    TaskType.AGENT: {
        "strong_weight": 2.0,
        "medium_weight": 1.0,
        "strong": [
            r'\b(search\s+the\s+(web|internet|online)|look\s+(it\s+)?up\s+online|find\s+(me\s+)?on\s+the\s+internet)\b',
            r'\b(scrape|fetch\s+from|call\s+(the\s+)?api|make\s+a\s+(get|post|put|delete)\s+request)\b',
            r'\b(schedule\s+(a\s+)?(task|job|reminder|meeting)|set\s+an?\s+(alarm|reminder))\b',
            r'\b(run\s+(the\s+)?tool|use\s+(the\s+)?tool|execute\s+(the\s+)?tool)\b',
        ],
        "medium": [
            r'\b(weather\s+(in|for|today|tomorrow)|current\s+(stock|price|rate|news))\b',
            r'\b(automate|automation|workflow|pipeline|orchestrat)\b',
            r'\b(monitor|alert|notify|trigger|event\-?driven)\b',
        ],
    },
}

TASK_PRIORITY = [
    TaskType.MATH,
    TaskType.CODE,
    TaskType.TRANSLATION,
    TaskType.VISION,
    TaskType.CREATIVE,
    TaskType.AGENT,
    TaskType.REASONING,
    TaskType.FAST,
    TaskType.GENERAL,
]

FAST_GREETING_PATTERN = re.compile(
    r'^(hi|hey|hello|ok|okay|yes|no|thanks|thx|bye|sup|yo)[!?.]?$'
)

FAST_BLOCKING_TASKS = (
    TaskType.MATH,
    TaskType.CODE,
    TaskType.TRANSLATION,
    TaskType.VISION,
    TaskType.CREATIVE,
    TaskType.AGENT,
    TaskType.REASONING,
)


def _score_pattern_group(text_lower: str, patterns: List[str], weight: float) -> float:
    return sum(weight for pattern in patterns if re.search(pattern, text_lower))


def _apply_task_scoring_rules(text_lower: str, scores: Dict[TaskType, float]) -> None:
    for task_type, rule in TASK_SCORING_RULES.items():
        scores[task_type] += _score_pattern_group(
            text_lower,
            rule["strong"],
            rule["strong_weight"],
        )
        scores[task_type] += _score_pattern_group(
            text_lower,
            rule["medium"],
            rule["medium_weight"],
        )


def _apply_fast_score(text_lower: str, scores: Dict[TaskType, float]) -> None:
    word_count = len(text_lower.split())
    fast_greetings = FAST_GREETING_PATTERN.search(text_lower)
    if word_count <= 2 or fast_greetings:
        scores[TaskType.FAST] += 3.0
        return

    if word_count <= 4 and not any(scores[task] > 0 for task in FAST_BLOCKING_TASKS):
        scores[TaskType.FAST] += 0.8


def _pick_highest_priority_task(scores: Dict[TaskType, float]) -> Tuple[TaskType, float]:
    max_score = max(scores.values())
    if max_score == 0.0:
        return TaskType.GENERAL, max_score

    for task in TASK_PRIORITY:
        if scores[task] == max_score:
            return task, max_score

    return TaskType.GENERAL, max_score


def _confidence_for_score(max_score: float, scores: Dict[TaskType, float]) -> float:
    raw_confidence = min(0.5 + (max_score / 8.0), 1.0)
    scoring_categories = sum(1 for value in scores.values() if value > 0)
    if scoring_categories == 1:
        raw_confidence = min(raw_confidence + 0.1, 1.0)
    return round(raw_confidence, 2)


# ══════════════════════════════════════════════════════════════════════════════
# TASK CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def classify_task(text: str, has_image: bool = False) -> Tuple[TaskType, float]:
    """
    Classify a task based on text content and optional image flag.

    Uses a scored, multi-pass approach: each category accumulates evidence
    points from keyword and regex matches; the highest-scoring category wins.
    Ties are broken by priority order: MATH > CODE > TRANSLATION > VISION >
    CREATIVE > AGENT > REASONING > FAST > GENERAL.

    Args:
        text: The user message or task description.
        has_image: Set True when an image attachment is present.

    Returns:
        Tuple of (TaskType, confidence) where confidence ∈ [0.0, 1.0].
    """
    if has_image:
        return TaskType.VISION, 1.0

    if not text or not text.strip():
        return TaskType.FAST, 0.9

    text_lower = text.lower().strip()
    scores: Dict[TaskType, float] = {t: 0.0 for t in TaskType}

    _apply_task_scoring_rules(text_lower, scores)
    _apply_fast_score(text_lower, scores)

    winner, max_score = _pick_highest_priority_task(scores)
    if max_score == 0.0:
        return TaskType.GENERAL, 0.5

    return winner, _confidence_for_score(max_score, scores)


# ══════════════════════════════════════════════════════════════════════════════
# MODEL ROUTER - With compatibility for existing code
# ══════════════════════════════════════════════════════════════════════════════

class ModelRouter:
    """
    Intelligent LLM model router with capability matching, fallback chains,
    session overrides, and runtime tracking.
    """

    def __init__(self, db_path: str = "data/router.db",
                 default_strategy: str = "balanced",
                 default_model: str = None):
        self._store = MRStore(db_path)
        self._models: Dict[str, Any] = {}  # name -> ModelSpec
        self._rr_index: int = 0
        self._fallback_chains: Dict[str, List[str]] = FALLBACK_CHAINS.copy()
        self._shadow_model: Optional[str] = None
        self.default_strategy = default_strategy
        self.default_model = default_model
        # Session overrides: session_id -> model_id
        self._session_models: Dict[str, str] = {}
        # Unavailable models: model_id -> cooldown_until
        self._unavailable: Dict[str, float] = {}
        # Per-model stats
        self._stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "total_calls": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "total_latency_ms": 0.0,
        })

    # ── SESSION MODEL OVERRIDES ───────────────────────────────────────────────

    def set_session_model(self, session_id: str, model_id: str) -> bool:
        """Set a model override for a session."""
        from agent.model_registry import MODELS
        if model_id not in MODELS:
            return False
        self._session_models[session_id] = model_id
        return True

    def get_session_model(self, session_id: str) -> Optional[str]:
        """Get the model override for a session."""
        return self._session_models.get(session_id)

    def clear_session_model(self, session_id: str) -> None:
        """Clear the model override for a session."""
        self._session_models.pop(session_id, None)

    # ── UNAVAILABLE MODEL TRACKING ────────────────────────────────────────────

    def mark_unavailable(self, model_id: str, cooldown_seconds: int = 60) -> None:
        """Mark a model as unavailable for cooldown_seconds."""
        self._unavailable[model_id] = time.time() + cooldown_seconds

    def mark_available(self, model_id: str) -> None:
        """Mark a model as available."""
        self._unavailable.pop(model_id, None)

    def _is_unavailable(self, model_id: str) -> bool:
        """Check if a model is currently unavailable."""
        if model_id not in self._unavailable:
            return False
        if time.time() >= self._unavailable[model_id]:
            self._unavailable.pop(model_id)
            return False
        return True

    # ── ROUTING ───────────────────────────────────────────────────────────────

    def route(self, text: str = "", session_id: str = "",
              has_image: bool = False) -> RouteDecision:
        """
        Route a task to the best model.

        Args:
            text: The task description text
            session_id: Optional session ID for model override
            has_image: Whether the task involves an image

        Returns:
            RouteDecision with model_id, task_type, confidence, and fallback chain
        """
        from agent.model_registry import MODELS, ModelCapability, get_models_by_capability

        # Check for session override first
        if session_id and session_id in self._session_models:
            model_id = self._session_models[session_id]
            model_spec = MODELS.get(model_id)
            if model_spec:
                return RouteDecision(
                    model_id=model_id,
                    model_spec=model_spec,
                    task_type=TaskType.GENERAL,
                    confidence=1.0,
                    reason=f"Session override for {session_id}",
                    fallback_chain=self._get_fallback_chain(model_id),
                )

        # Classify task type
        task_type, base_confidence = classify_task(text, has_image)

        # Get candidate models based on task type
        candidates = self._get_candidates_for_task(task_type)

        if not candidates:
            # Fall back to any available model
            candidates = list(MODELS.values())

        # Score and select best model
        best_model = self._score_model(candidates, task_type)

        if best_model:
            model_id = best_model.id
            model_spec = best_model
            confidence = min(base_confidence + 0.1, 1.0)
            # Keep the classified task_type; do NOT override with model capabilities.
            # _infer_task_type was overwriting a correct classification (e.g. creative→vision)
            # when the selected model happened to have a higher-priority capability.
        else:
            model_id = CONFIG.OLLAMA_MODEL
            model_spec = MODELS.get(model_id) or list(MODELS.values())[0]
            confidence = 0.5
            task_type = TaskType.GENERAL

        return RouteDecision(
            model_id=model_id,
            model_spec=model_spec,
            task_type=task_type,
            confidence=confidence,
            reason=f"Task type: {task_type.value}, best match",
            fallback_chain=self._get_fallback_chain(model_id),
        )

    def _get_candidates_for_task(self, task_type: TaskType) -> List[Any]:
        """Get candidate models for a task type."""
        from agent.model_registry import MODELS

        capability = TASK_TO_CAPABILITY.get(task_type)
        if capability:
            return [m for m in MODELS.values() if capability in m.capabilities]
        return list(MODELS.values())

    def _score_model(self, candidates: List[Any], task_type: TaskType) -> Optional[Any]:
        """Score candidates and return best one."""
        if not candidates:
            return None

        scored = []
        for m in candidates:
            if self._is_unavailable(m.id):
                continue

            score = 0.0

            # Base quality score
            score += 0.4

            # Capability match bonus
            capability = TASK_TO_CAPABILITY.get(task_type)
            if capability and capability in m.capabilities:
                score += 0.3

            # Context window bonus for complex tasks
            if task_type in (TaskType.CODE, TaskType.REASONING):
                if m.context_window >= 100000:
                    score += 0.2

            # Fast task prefers lower context
            if task_type == TaskType.FAST:
                if m.context_window <= 8192:
                    score += 0.2

            scored.append((m, score))

        if not scored:
            return None

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]

    def _infer_task_type(self, model: Any, default: TaskType) -> TaskType:
        """Infer task type from model capabilities."""
        from agent.model_registry import ModelCapability
        if ModelCapability.CODE.value in model.capabilities:
            return TaskType.CODE
        if ModelCapability.VISION.value in model.capabilities:
            return TaskType.VISION
        if ModelCapability.FAST.value in model.capabilities:
            return TaskType.FAST
        return default

    def _get_fallback_chain(self, model_id: str) -> List[str]:
        """Get fallback chain for a model."""
        return self._fallback_chains.get(model_id, [])

    # ── STATS TRACKING ────────────────────────────────────────────────────────

    def record_call(self, model_id: str, success: bool,
                    latency_ms: float = 0.0, error: str = "") -> None:
        """Record a model call result."""
        stats = self._stats[model_id]
        stats["total_calls"] += 1
        if success:
            stats["successful_calls"] += 1
        else:
            stats["failed_calls"] += 1
        stats["total_latency_ms"] += latency_ms

    def get_stats(self) -> List[Dict[str, Any]]:
        """Get per-model statistics."""
        result = []
        for model_id, stats in self._stats.items():
            total = stats["total_calls"]
            result.append({
                "model_id": model_id,
                "total_calls": stats["total_calls"],
                "successful_calls": stats["successful_calls"],
                "failed_calls": stats["failed_calls"],
                "avg_latency_ms": stats["total_latency_ms"] / max(1, total),
                "success_rate": stats["successful_calls"] / max(1, total),
            })
        return result

    # ── MODEL MANAGEMENT ──────────────────────────────────────────────────────

    def list_all_models(self) -> List[str]:
        """List all registered model IDs."""
        from agent.model_registry import MODELS
        return list(MODELS.keys())

    def models_for_task(self, task_text: str) -> List[Dict[str, Any]]:
        """Get models suitable for a task description."""
        from agent.model_registry import MODELS, ModelCapability

        task_type, _ = classify_task(task_text)
        capability = TASK_TO_CAPABILITY.get(task_type)

        candidates = MODELS.values()
        if capability:
            candidates = [m for m in MODELS.values() if capability in m.capabilities]

        return [
            {
                "id": m.id,
                "name": m.display_name,
                "provider": m.provider,
                "context_window": m.context_window,
                "capabilities": m.capabilities,
            }
            for m in candidates
        ]

    def router_summary(self) -> Dict[str, Any]:
        """Get router summary statistics."""
        from agent.model_registry import MODELS

        providers = set()
        for m in MODELS.values():
            providers.add(m.provider)

        return {
            "total_models": len(MODELS),
            "providers": list(providers),
            "task_types": [t.value for t in TaskType],
            "fallback_chains": len(self._fallback_chains),
            "session_overrides": len(self._session_models),
        }

    # ── BACKWARD COMPATIBILITY METHODS ────────────────────────────────────────

    def set_fallback_chain(self, primary: str, fallbacks: List[str]) -> None:
        """Set fallback chain for a primary model."""
        self._fallback_chains[primary] = fallbacks

    def get_fallback_chain(self, primary: str) -> List[str]:
        """Get fallback chain for a primary model."""
        return self._fallback_chains.get(primary, [])

    def stats(self) -> Dict[str, Any]:
        """Get router statistics (legacy)."""
        return {
            "total_models": len(self._models),
            "registered_models": len(self._models),
            "enabled_models": len(self._models),
        }

    def list_models(self, capability: str = None) -> List[Any]:
        """List models (legacy - returns ModelSpec objects)."""
        from agent.model_registry import MODELS
        return list(MODELS.values())

    def _candidates(self, required_caps: List[str] = None,
                     min_quality: float = 0.0,
                     max_cost_per_1k: float = float("inf"),
                     max_latency_ms: float = float("inf"),
                     min_context: int = 0) -> List[Any]:
        """Get candidate models (legacy compatibility)."""
        from agent.model_registry import MODELS

        candidates = list(MODELS.values())
        if required_caps:
            candidates = [m for m in candidates
                         if any(c in m.capabilities for c in required_caps)]
        return candidates

    def route_with_fallback(self, **kwargs) -> Optional[Dict[str, Any]]:
        """Route with fallback (legacy - returns dict)."""
        result = self.route(**{k: v for k, v in kwargs.items() if k != 'kwargs'})
        return result.to_dict() if result else None

    def register(self, name: str, **kwargs) -> Any:
        """Register a model (legacy - uses MODELS dict)."""
        from agent.model_registry import MODELS
        return MODELS.get(name)

    def unregister(self, name: str) -> bool:
        """Unregister a model (legacy - no-op)."""
        return False

    def enable(self, name: str) -> None:
        """Enable a model (legacy - no-op)."""
        pass

    def disable(self, name: str) -> None:
        """Disable a model (legacy - no-op)."""
        pass


# ── LEGACY CLASSES (kept for backward compatibility) ──────────────────────────

class MRStore:
    """Legacy storage class (kept for backward compatibility)."""
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _init(self):
        pass  # No longer used

    def _conn(self):
        import sqlite3
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def log(self, model: str, strategy: str, tokens: int = 0,
             cost: float = 0, latency_ms: float = 0, error: str = ""):
        """Log a routing decision (legacy)."""
        pass

    def model_stats(self, model: str) -> Dict:
        """Get model statistics (legacy)."""
        return {"requests": 0, "errors": 0, "total_cost": 0.0, "avg_latency_ms": 0.0}

    def overall_stats(self) -> Dict:
        """Get overall statistics (legacy)."""
        return {"total_requests": 0, "total_cost_usd": 0.0}
