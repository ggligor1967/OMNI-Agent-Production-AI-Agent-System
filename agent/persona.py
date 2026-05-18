"""
OMNI AGENT - Persona System
Dynamic agent personas with memory-backed tone profiles and per-user customization.

Features:
- Named personas with tone, style, vocabulary, and system-prompt injection
- Per-user persona assignment persisted in memory
- Persona blending (weighted mix of two personas)
- Auto-detection: infer best persona from conversation context
- Built-in personas: assistant, tutor, coach, analyst, creative, engineer, concise
- Runtime persona registration and editing
"""
import re
import time
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# PERSONA DEFINITION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Persona:
    """
    A named behavioral profile that shapes how the agent communicates.

    Attributes:
        name:           Unique identifier
        display_name:   Human-readable label
        description:    What this persona is for
        system_inject:  Text prepended to the system prompt
        tone_words:     Adjectives describing tone (e.g. ["warm","supportive"])
        avoid_words:    Words/phrases to avoid in responses
        response_style: "concise"|"detailed"|"structured"|"conversational"
        emoji_allowed:  Whether to use emojis
        formality:      0.0 (very casual) → 1.0 (very formal)
        expertise_level: "beginner"|"intermediate"|"expert"
        language_hint:  Preferred language (e.g. "en","es","fr","zh")
        examples:       Few-shot conversation examples [[user,assistant],...]
        tags:           Categorization tags
    """
    name: str
    display_name: str
    description: str = ""
    system_inject: str = ""
    tone_words: List[str] = field(default_factory=list)
    avoid_words: List[str] = field(default_factory=list)
    response_style: str = "conversational"   # concise|detailed|structured|conversational
    emoji_allowed: bool = False
    formality: float = 0.5                   # 0.0=casual, 1.0=formal
    expertise_level: str = "intermediate"    # beginner|intermediate|expert
    language_hint: str = "en"
    examples: List[List[str]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def build_system_prompt(self, base_prompt: str = "") -> str:
        """Build the full system prompt for this persona."""
        parts = []

        if base_prompt:
            parts.append(base_prompt)

        if self.system_inject:
            parts.append(self.system_inject)

        # Style guidance
        style_map = {
            "concise":        "Be brief and to the point. Keep responses under 3 sentences unless detail is explicitly needed.",
            "detailed":       "Provide thorough, comprehensive responses with examples and context.",
            "structured":     "Organize responses with clear sections, bullet points, and headers where appropriate.",
            "conversational": "Respond naturally and conversationally, like a knowledgeable friend.",
        }
        if self.response_style in style_map:
            parts.append(style_map[self.response_style])

        # Tone
        if self.tone_words:
            parts.append(f"Maintain a {', '.join(self.tone_words)} tone throughout.")

        # Formality
        if self.formality >= 0.8:
            parts.append("Use formal, professional language. Avoid contractions and colloquialisms.")
        elif self.formality <= 0.2:
            parts.append("Use casual, relaxed language. Contractions and informal expressions are fine.")

        # Expertise level
        level_map = {
            "beginner":     "Explain concepts simply, avoid jargon, use analogies freely.",
            "intermediate": "Assume moderate familiarity with the topic. Define technical terms when first used.",
            "expert":       "Assume expert-level knowledge. Use technical terminology without over-explaining.",
        }
        if self.expertise_level in level_map:
            parts.append(level_map[self.expertise_level])

        # Emoji
        if not self.emoji_allowed:
            parts.append("Do not use emojis or emoticons.")

        # Avoid words
        if self.avoid_words:
            parts.append(f"Avoid these phrases: {', '.join(self.avoid_words)}.")

        return "\n\n".join(p for p in parts if p)

    def few_shot_messages(self) -> List[Dict[str, str]]:
        """Return few-shot examples as message dicts."""
        messages = []
        for pair in self.examples:
            if len(pair) >= 2:
                messages.append({"role": "user",    "content": pair[0]})
                messages.append({"role": "assistant","content": pair[1]})
        return messages

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "response_style": self.response_style,
            "formality": self.formality,
            "expertise_level": self.expertise_level,
            "tone_words": self.tone_words,
            "emoji_allowed": self.emoji_allowed,
            "tags": self.tags,
        }


# ══════════════════════════════════════════════════════════════════════════════
# PERSONA REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

class PersonaRegistry:
    """
    Stores and manages persona definitions.
    Supports registration, retrieval, blending, and auto-detection.
    """

    def __init__(self):
        self._personas: Dict[str, Persona] = {}
        self._load_builtins()

    def _load_builtins(self):
        builtins = [
            Persona(
                name="assistant",
                display_name="General Assistant",
                description="Balanced, helpful general-purpose assistant",
                system_inject="You are a helpful, accurate, and thoughtful AI assistant.",
                tone_words=["helpful", "clear", "friendly"],
                response_style="conversational",
                formality=0.5,
                expertise_level="intermediate",
                tags=["general", "default"],
            ),
            Persona(
                name="tutor",
                display_name="Patient Tutor",
                description="Patient teacher who breaks down complex concepts",
                system_inject=(
                    "You are a patient, encouraging tutor. Your goal is to help the "
                    "user truly understand, not just get an answer. Ask follow-up "
                    "questions to check comprehension. Use analogies and examples liberally."
                ),
                tone_words=["patient", "encouraging", "clear"],
                response_style="detailed",
                formality=0.4,
                expertise_level="beginner",
                emoji_allowed=True,
                avoid_words=["obviously", "simply", "just", "trivially"],
                tags=["education", "teaching"],
                examples=[
                    ["What is recursion?",
                     "Great question! Recursion is when a function calls itself. "
                     "Imagine Russian nesting dolls — each doll contains a smaller version "
                     "of itself. Let me show you with code...\n\nDoes that make sense so far?"],
                ],
            ),
            Persona(
                name="coach",
                display_name="Executive Coach",
                description="Motivational coach focused on goals and growth",
                system_inject=(
                    "You are an executive coach. Help the user clarify their goals, "
                    "identify obstacles, and create actionable plans. Ask powerful "
                    "questions. Be direct and action-oriented. Celebrate progress."
                ),
                tone_words=["motivating", "direct", "supportive"],
                response_style="structured",
                formality=0.5,
                expertise_level="intermediate",
                avoid_words=["maybe", "perhaps", "you could try"],
                tags=["productivity", "goals", "motivation"],
            ),
            Persona(
                name="analyst",
                display_name="Data Analyst",
                description="Rigorous, data-driven analyst who shows their reasoning",
                system_inject=(
                    "You are a rigorous analyst. Always show your reasoning. "
                    "Cite numbers when available. Express uncertainty with confidence intervals. "
                    "Challenge assumptions. Prefer data over anecdote. "
                    "Structure responses with: Observation → Analysis → Recommendation."
                ),
                tone_words=["precise", "objective", "rigorous"],
                response_style="structured",
                formality=0.8,
                expertise_level="expert",
                avoid_words=["I think", "I feel", "probably", "seems like"],
                tags=["data", "analysis", "research"],
            ),
            Persona(
                name="creative",
                display_name="Creative Partner",
                description="Imaginative creative collaborator for writing and ideas",
                system_inject=(
                    "You are a creative collaborator. Embrace imagination, metaphor, "
                    "and unconventional thinking. Build on ideas enthusiastically. "
                    "Offer vivid descriptions and unexpected angles. "
                    "Never say 'I can't be creative about that.'"
                ),
                tone_words=["imaginative", "enthusiastic", "vivid"],
                response_style="conversational",
                formality=0.2,
                expertise_level="intermediate",
                emoji_allowed=True,
                tags=["writing", "brainstorming", "art"],
            ),
            Persona(
                name="engineer",
                display_name="Senior Engineer",
                description="Pragmatic engineer focused on clean, working solutions",
                system_inject=(
                    "You are a pragmatic senior software engineer. "
                    "Prioritize working, maintainable code over theoretical elegance. "
                    "Always consider edge cases, error handling, and performance. "
                    "Prefer battle-tested solutions over novel ones unless there's a clear reason. "
                    "Call out tradeoffs explicitly."
                ),
                tone_words=["precise", "pragmatic", "direct"],
                response_style="structured",
                formality=0.6,
                expertise_level="expert",
                avoid_words=["it depends", "as an AI"],
                tags=["coding", "engineering", "technical"],
            ),
            Persona(
                name="concise",
                display_name="Concise Mode",
                description="Ultra-brief responses, no filler",
                system_inject=(
                    "Be extremely concise. One sentence when possible. "
                    "Never repeat the question. No preamble. No 'Great question!'. "
                    "No 'Certainly!'. Just the answer."
                ),
                tone_words=["direct", "brief"],
                response_style="concise",
                formality=0.5,
                expertise_level="intermediate",
                avoid_words=["certainly", "great question", "of course",
                            "absolutely", "sure", "I'd be happy to"],
                tags=["productivity", "speed"],
            ),
        ]
        for p in builtins:
            self._personas[p.name] = p

    def register(self, persona: Persona):
        self._personas[persona.name] = persona
        logger.info(f"Persona registered: '{persona.name}'")

    def get(self, name: str) -> Optional[Persona]:
        return self._personas.get(name)

    def list_personas(self, tag: Optional[str] = None) -> List[Dict]:
        personas = list(self._personas.values())
        if tag:
            personas = [p for p in personas if tag in p.tags]
        return [p.to_dict() for p in personas]

    def blend(self, name_a: str, name_b: str,
              weight_a: float = 0.5) -> Optional[Persona]:
        """
        Create a blended persona from two existing ones.
        weight_a controls the mix (0.0 = all B, 1.0 = all A).
        """
        a = self._personas.get(name_a)
        b = self._personas.get(name_b)
        if not a or not b:
            return None

        weight_b = 1.0 - weight_a
        blended_formality = a.formality * weight_a + b.formality * weight_b

        blended = Persona(
            name=f"{name_a}_{name_b}_blend",
            display_name=f"{a.display_name} / {b.display_name} Blend",
            description=f"Blend of {name_a} ({weight_a:.0%}) and {name_b} ({weight_b:.0%})",
            system_inject="\n\n".join(filter(None, [a.system_inject, b.system_inject])),
            tone_words=list(set(a.tone_words + b.tone_words)),
            avoid_words=list(set(a.avoid_words + b.avoid_words)),
            response_style=a.response_style if weight_a >= 0.5 else b.response_style,
            emoji_allowed=a.emoji_allowed if weight_a >= 0.5 else b.emoji_allowed,
            formality=blended_formality,
            expertise_level=a.expertise_level if weight_a >= 0.5 else b.expertise_level,
            examples=a.examples + b.examples,
            tags=list(set(a.tags + b.tags + ["blend"])),
        )
        return blended

    def detect_best(self, text: str) -> str:
        """
        Infer the best persona name from conversation text using keyword heuristics.
        Returns persona name string.
        """
        text_lower = text.lower()

        rules = [
            ("engineer",  ["code", "function", "bug", "error", "implement",
                          "python", "sql", "api", "algorithm", "debug"]),
            ("analyst",   ["data", "analyze", "metrics", "statistics", "trend",
                          "report", "dashboard", "forecast", "numbers"]),
            ("tutor",     ["explain", "teach", "understand", "learn", "what is",
                          "how does", "why does", "confused", "beginner"]),
            ("coach",     ["goal", "productivity", "habit", "improve", "motivation",
                          "stuck", "career", "plan", "strategy", "advice"]),
            ("creative",  ["write", "story", "poem", "creative", "imagine",
                          "brainstorm", "idea", "fiction", "novel", "art"]),
            ("concise",   ["quick", "brief", "short", "tldr", "summary", "fast"]),
        ]

        scores = {}
        for persona_name, keywords in rules:
            score = sum(1 for kw in keywords if kw in text_lower)
            if score > 0:
                scores[persona_name] = score

        if not scores:
            return "assistant"
        return max(scores, key=lambda k: scores[k])

    def search(self, query: str) -> List[Persona]:
        q = query.lower()
        return [
            p for p in self._personas.values()
            if q in p.name or q in p.description.lower()
            or any(q in t for t in p.tags)
        ]


# ══════════════════════════════════════════════════════════════════════════════
# PER-USER PERSONA MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class PersonaManager:
    """
    Manages per-user and per-session persona assignments.
    Persists assignments in the agent memory store.

    Usage:
        pm = PersonaManager(registry, memory)

        # Assign a persona to a user
        pm.set_user_persona("user_123", "engineer")

        # Get the persona for a session
        persona = pm.get_session_persona(session_id, user_id)

        # Build system prompt incorporating the persona
        system = pm.build_system_prompt(session_id, user_id, base_system)

        # Get few-shot examples for the persona
        examples = pm.get_examples(session_id, user_id)
    """

    def __init__(self, registry: PersonaRegistry, memory=None):
        self.registry = registry
        self.memory = memory
        # In-memory fallback if no memory store
        self._session_map: Dict[str, str] = {}
        self._user_map: Dict[str, str] = {}

    # ── Assignment ────────────────────────────────────────────────────────────

    def set_session_persona(self, session_id: str, persona_name: str) -> bool:
        """Assign a persona to a specific session (overrides user default)."""
        if not self.registry.get(persona_name):
            return False
        self._session_map[session_id] = persona_name
        if self.memory:
            self.memory.save_memory(
                f"persona:session:{session_id}", persona_name,
                category="persona", importance=6
            )
        logger.info(f"Session '{session_id}' → persona '{persona_name}'")
        return True

    def set_user_persona(self, user_id: str, persona_name: str) -> bool:
        """Assign a default persona to a user (used when no session override)."""
        if not self.registry.get(persona_name):
            return False
        self._user_map[str(user_id)] = persona_name
        if self.memory:
            self.memory.save_memory(
                f"persona:user:{user_id}", persona_name,
                category="persona", importance=7
            )
        logger.info(f"User '{user_id}' → persona '{persona_name}'")
        return True

    def clear_session_persona(self, session_id: str):
        self._session_map.pop(session_id, None)
        if self.memory:
            self.memory.save_memory(
                f"persona:session:{session_id}", "",
                category="persona"
            )

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def get_persona_name(self, session_id: str = "",
                         user_id: str = "") -> str:
        """
        Determine active persona name with priority:
        session override > user default > "assistant"
        """
        # Check session override
        if session_id:
            name = self._session_map.get(session_id)
            if not name and self.memory:
                name = self.memory.get_memory(f"persona:session:{session_id}")
            if name:
                return name

        # Check user default
        if user_id:
            name = self._user_map.get(str(user_id))
            if not name and self.memory:
                name = self.memory.get_memory(f"persona:user:{user_id}")
            if name:
                return name

        return "assistant"

    def get_session_persona(self, session_id: str = "",
                            user_id: str = "") -> Persona:
        """Get the active Persona object for a session."""
        name = self.get_persona_name(session_id, user_id)
        return self.registry.get(name) or self.registry.get("assistant")

    # ── System Prompt ─────────────────────────────────────────────────────────

    def build_system_prompt(self, session_id: str, user_id: str,
                             base_system: str = "") -> str:
        """Build the full system prompt with persona injection."""
        persona = self.get_session_persona(session_id, str(user_id))
        return persona.build_system_prompt(base_system)

    def get_examples(self, session_id: str,
                     user_id: str) -> List[Dict[str, str]]:
        """Get few-shot examples for the active persona."""
        persona = self.get_session_persona(session_id, str(user_id))
        return persona.few_shot_messages()

    # ── Auto-detect ───────────────────────────────────────────────────────────

    def auto_detect_and_set(self, session_id: str,
                             user_message: str) -> Optional[str]:
        """
        Auto-detect best persona from user message and set it for the session.
        Only switches if the current persona is the default 'assistant'.
        Returns new persona name if switched, else None.
        """
        current = self.get_persona_name(session_id)
        if current != "assistant":
            return None  # don't override explicit assignment

        detected = self.registry.detect_best(user_message)
        if detected != "assistant":
            self.set_session_persona(session_id, detected)
            return detected
        return None

    # ── Info ──────────────────────────────────────────────────────────────────

    def session_info(self, session_id: str, user_id: str = "") -> Dict:
        """Get info about the active persona for a session."""
        name = self.get_persona_name(session_id, user_id)
        persona = self.registry.get(name)
        source = "default"
        if session_id in self._session_map:
            source = "session"
        elif str(user_id) in self._user_map:
            source = "user"
        return {
            "persona_name": name,
            "display_name": persona.display_name if persona else name,
            "source": source,
            "style": persona.response_style if persona else "conversational",
            "formality": persona.formality if persona else 0.5,
        }
