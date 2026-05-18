"""
OMNI AGENT - Social Conversation Layer
Multi-platform conversational adapter: Telegram, CLI, REST, Discord-ready.
Handles persona management, conversation state, intent detection, and response shaping.
"""
import re
import time
import random
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATION INTENT ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class Intent(str, Enum):
    GREETING        = "greeting"
    FAREWELL        = "farewell"
    QUESTION        = "question"
    COMMAND         = "command"
    SMALL_TALK      = "small_talk"
    SEARCH_REQUEST  = "search_request"
    CODE_REQUEST    = "code_request"
    MEMORY_REQUEST  = "memory_request"
    TASK_REQUEST    = "task_request"
    FEEDBACK        = "feedback"
    COMPLAINT       = "complaint"
    UNKNOWN         = "unknown"


INTENT_PATTERNS: Dict[Intent, List[str]] = {
    Intent.GREETING:       [r"\b(hi|hello|hey|good\s+(morning|evening|afternoon)|howdy|sup|greetings)\b"],
    Intent.FAREWELL:       [r"\b(bye|goodbye|see\s+you|cya|take\s+care|quit|exit|later)\b"],
    Intent.QUESTION:       [r"^(what|who|where|when|why|how|is|are|can|could|would|do|does|did)\b", r"\?$"],
    Intent.COMMAND:        [r"^/(start|help|clear|status|exec|memory|skills)"],
    Intent.SEARCH_REQUEST: [r"\b(search|look\s+up|find|google|research|browse|check)\b"],
    Intent.CODE_REQUEST:   [r"\b(code|script|function|program|write|implement|debug|fix|python|javascript)\b"],
    Intent.MEMORY_REQUEST: [r"\b(remember|recall|forget|save|store|what\s+did\s+i|memorize)\b"],
    Intent.TASK_REQUEST:   [r"\b(schedule|remind|set\s+a|create\s+a\s+task|add\s+to)\b"],
    Intent.SMALL_TALK:     [r"\b(how\s+are\s+you|what'?s\s+up|tell\s+me\s+about\s+yourself|who\s+are\s+you)\b"],
    Intent.FEEDBACK:       [r"\b(thanks|thank\s+you|great|awesome|good\s+job|well\s+done|nice)\b"],
}


def detect_intent(text: str) -> Tuple[Intent, float]:
    """Rule-based intent detection. Returns (intent, confidence)."""
    text_lower = text.lower().strip()

    explicit_small_talk = [
        r"^how\s+are\s+you(?:\s+(?:doing|today))?\??$",
        r"^what'?s\s+up[!?., ]*$",
        r"^tell\s+me\s+about\s+yourself[.!? ]*$",
        r"^who\s+are\s+you\??$",
    ]
    if any(re.search(pattern, text_lower, re.IGNORECASE) for pattern in explicit_small_talk):
        return Intent.SMALL_TALK, 1.0

    scores: Dict[Intent, int] = {}

    for intent, patterns in INTENT_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                scores[intent] = scores.get(intent, 0) + 1

    if not scores:
        return Intent.UNKNOWN, 0.0

    best = max(scores, key=scores.get)
    confidence = min(scores[best] / 2.0, 1.0)
    return best, confidence


# ══════════════════════════════════════════════════════════════════════════════
# PERSONA ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Persona:
    name: str
    tone: str          # 'professional' | 'friendly' | 'concise' | 'playful'
    language: str = "en"
    emoji_enabled: bool = True
    max_response_length: int = 1000
    system_prompt_addon: str = ""

    def greeting_response(self, user_name: str = "") -> str:
        name_part = f", {user_name}" if user_name else ""
        responses = {
            "friendly": [
                f"Hey{name_part}! 👋 Great to hear from you. What can I do for you today?",
                f"Hello{name_part}! 😊 I'm here and ready to help!",
                f"Hi there{name_part}! What's on your mind?",
            ],
            "professional": [
                f"Hello{name_part}. How can I assist you today?",
                f"Good day{name_part}. I'm ready to help. Please go ahead.",
                f"Welcome{name_part}. What can I do for you?",
            ],
            "concise": [
                f"Hi{name_part}. Ready.",
                f"Hello. How can I help?",
            ],
            "playful": [
                f"Heyyy{name_part}! 🎉 What's the vibe today?",
                f"Oh hey{name_part}! You summoned me? 🔮",
            ],
        }
        options = responses.get(self.tone, responses["friendly"])
        return random.choice(options)

    def farewell_response(self) -> str:
        responses = {
            "friendly": ["Take care! 👋", "See you soon! 😊", "Bye for now! 🌟"],
            "professional": ["Goodbye. Have a productive day.", "Farewell. Don't hesitate to return."],
            "concise": ["Bye.", "Later."],
            "playful": ["Byeee! 🚀✨", "Peace out! 🤙"],
        }
        options = responses.get(self.tone, ["Goodbye!"])
        return random.choice(options)

    def small_talk_response(self) -> str:
        responses = [
            "I'm an AI agent, so I don't have feelings — but all my systems are running great! 😄 What can I help with?",
            "I'm doing what I love — processing information and helping out! What's on your mind?",
            "I'm OMNI Agent — your modular AI assistant. I can search the web, write code, store memories, and more. What would you like to do?",
            "Always operational and ready! I'm here to assist with research, coding, analysis, and tasks. What do you need?",
        ]
        return random.choice(responses)

    def format_response(self, text: str, intent: Intent = None) -> str:
        """Shape response to fit platform and persona constraints."""
        # Trim to length
        if len(text) > self.max_response_length:
            text = text[:self.max_response_length - 3] + "..."
        return text


# Preset personas
PERSONAS = {
    "assistant": Persona(
        name="OMNI",
        tone="friendly",
        emoji_enabled=True,
        max_response_length=2000,
        system_prompt_addon="Be warm, helpful, and concise."
    ),
    "professional": Persona(
        name="OMNI-Pro",
        tone="professional",
        emoji_enabled=False,
        max_response_length=3000,
        system_prompt_addon="Be precise, structured, and professional."
    ),
    "concise": Persona(
        name="OMNI-Lite",
        tone="concise",
        emoji_enabled=False,
        max_response_length=500,
        system_prompt_addon="Be extremely concise. One or two sentences maximum."
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATION STATE MANAGER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ConversationState:
    session_id: str
    user_id: Any
    platform: str = "unknown"
    persona_key: str = "assistant"
    message_count: int = 0
    last_intent: Optional[Intent] = None
    last_active: float = field(default_factory=time.time)
    context: Dict[str, Any] = field(default_factory=dict)
    user_name: str = ""

    @property
    def persona(self) -> Persona:
        return PERSONAS.get(self.persona_key, PERSONAS["assistant"])

    def update(self, intent: Intent):
        self.message_count += 1
        self.last_intent = intent
        self.last_active = time.time()

    def is_stale(self, timeout: int = 3600) -> bool:
        return (time.time() - self.last_active) > timeout


class ConversationManager:
    """
    Manages per-session conversation state, intent routing,
    and persona-shaped response pre-processing.
    """

    def __init__(self):
        self._states: Dict[str, ConversationState] = {}

    def get_or_create(self, session_id: str, user_id: Any,
                      platform: str = "unknown") -> ConversationState:
        if session_id not in self._states:
            self._states[session_id] = ConversationState(
                session_id=session_id,
                user_id=user_id,
                platform=platform,
            )
        return self._states[session_id]

    def process(self, session_id: str, user_id: Any,
                text: str, platform: str = "unknown") -> Dict[str, Any]:
        """
        Pre-process an incoming message.
        Returns: {intent, confidence, quick_response, persona, state}
        quick_response: if set, skip LLM and return this directly.
        """
        state = self.get_or_create(session_id, user_id, platform)
        intent, confidence = detect_intent(text)
        state.update(intent)

        quick_response: Optional[str] = None
        persona = state.persona

        # Handle social intents without LLM
        if intent == Intent.GREETING and confidence >= 0.5:
            quick_response = persona.greeting_response(state.user_name)

        elif intent == Intent.FAREWELL and confidence >= 0.5:
            quick_response = persona.farewell_response()

        # Small talk takes priority over question detection
        elif intent == Intent.SMALL_TALK and confidence >= 0.5:
            quick_response = persona.small_talk_response()

        elif intent == Intent.FEEDBACK and confidence > 0.4:
            quick_response = random.choice([
                "Glad I could help! 😊",
                "Happy to assist! Let me know if there's anything else.",
                "Thanks for the kind words!",
            ])

        # Extract user name if introduced
        name_match = re.search(
            r"(?:my name is|i'?m|call me)\s+([A-Z][a-z]+)", text, re.IGNORECASE
        )
        if name_match:
            state.user_name = name_match.group(1)

        return {
            "intent": intent,
            "confidence": confidence,
            "quick_response": quick_response,
            "persona": persona,
            "state": state,
        }

    def set_persona(self, session_id: str, persona_key: str):
        if session_id in self._states and persona_key in PERSONAS:
            self._states[session_id].persona_key = persona_key

    def prune_stale(self, timeout: int = 3600):
        stale = [sid for sid, s in self._states.items() if s.is_stale(timeout)]
        for sid in stale:
            del self._states[sid]
        if stale:
            logger.info(f"Pruned {len(stale)} stale sessions.")

    def list_active(self) -> List[Dict]:
        return [
            {
                "session_id": s.session_id,
                "platform": s.platform,
                "user_id": str(s.user_id),
                "message_count": s.message_count,
                "last_intent": s.last_intent,
                "last_active": s.last_active,
                "persona": s.persona_key,
            }
            for s in self._states.values()
        ]
