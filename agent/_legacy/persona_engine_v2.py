"""OMNI Agent — Persona Engine V2: dynamic personas with trait blending and consistency."""
from __future__ import annotations
import json, random, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class TraitCategory(str, Enum):
    COMMUNICATION = "communication"   # formal/casual/technical
    PERSONALITY   = "personality"     # friendly/serious/playful
    EXPERTISE     = "expertise"       # domain knowledge areas
    TONE          = "tone"            # warm/neutral/assertive
    VERBOSITY     = "verbosity"       # concise/detailed/elaborate
    HUMOR         = "humor"           # dry/witty/none


@dataclass
class Trait:
    name: str
    category: TraitCategory
    value: float = 0.5          # 0.0–1.0 intensity
    description: str = ""
    prompt_fragment: str = ""   # snippet injected into system prompt


@dataclass
class Persona:
    persona_id: str
    name: str
    description: str = ""
    traits: List[Trait] = field(default_factory=list)
    base_prompt: str = ""
    voice: str = ""             # "assistant" | "expert" | "friend" | custom
    language: str = "en"
    custom_instructions: str = ""
    examples: List[Dict[str, str]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    active: bool = True
    version: int = 1
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_trait(self, name: str) -> Optional[Trait]:
        for t in self.traits:
            if t.name == name:
                return t
        return None

    def get_trait_value(self, name: str, default: float = 0.5) -> float:
        t = self.get_trait(name)
        return t.value if t else default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "name": self.name,
            "description": self.description,
            "voice": self.voice,
            "language": self.language,
            "traits": [{"name": t.name, "category": t.category.value,
                         "value": t.value} for t in self.traits],
            "active": self.active,
            "version": self.version,
        }

    def build_system_prompt(self) -> str:
        parts = []
        if self.base_prompt:
            parts.append(self.base_prompt)
        for trait in self.traits:
            if trait.prompt_fragment:
                parts.append(trait.prompt_fragment)
        if self.custom_instructions:
            parts.append(self.custom_instructions)
        return "\n".join(parts)


@dataclass
class PersonaBlend:
    """A weighted mix of two or more personas."""
    blend_id: str
    name: str
    components: List[Tuple[str, float]]   # [(persona_id, weight)]
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "blend_id": self.blend_id,
            "name": self.name,
            "components": [{"persona_id": p, "weight": w}
                           for p, w in self.components],
        }


class PersonaEngineV2:
    """
    Dynamic persona management engine:
    - Define personas with typed traits (0.0–1.0 intensity)
    - Automatic system prompt generation from traits
    - Persona blending (weighted interpolation of traits)
    - Persona versioning (edit → new version)
    - Per-session persona assignment
    - Consistency checks (trait conflicts detection)
    - A/B persona assignment for users
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._personas: Dict[str, Persona] = {}
        self._blends:   Dict[str, PersonaBlend] = {}
        self._sessions: Dict[str, str] = {}        # session_id → persona_id
        self._ab_assignments: Dict[str, str] = {}  # user_id → persona_id
        self._ab_pool: List[str] = []              # persona_ids in A/B pool
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS pe_personas (
                persona_id TEXT PRIMARY KEY, name TEXT, description TEXT,
                base_prompt TEXT, voice TEXT, version INTEGER,
                active INTEGER, traits TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS pe_sessions (
                session_id TEXT PRIMARY KEY, persona_id TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── PERSONA MANAGEMENT ────────────────────────────────────────────

    def create_persona(self, name: str,
                       description: str = "",
                       base_prompt: str = "",
                       voice: str = "assistant",
                       language: str = "en",
                       custom_instructions: str = "",
                       traits: Optional[List[Dict]] = None,
                       tags: Optional[List[str]] = None,
                       examples: Optional[List[Dict]] = None,
                       persona_id: Optional[str] = None,
                       metadata: Optional[Dict] = None) -> Persona:
        pid = persona_id or str(uuid.uuid4())[:8]
        parsed_traits = []
        for t in (traits or []):
            parsed_traits.append(Trait(
                name=t["name"],
                category=TraitCategory(t.get("category", "personality")),
                value=float(t.get("value", 0.5)),
                description=t.get("description", ""),
                prompt_fragment=t.get("prompt_fragment", "")))
        p = Persona(
            persona_id=pid, name=name, description=description,
            base_prompt=base_prompt, voice=voice, language=language,
            custom_instructions=custom_instructions,
            traits=parsed_traits, tags=list(tags or []),
            examples=list(examples or []),
            metadata=metadata or {})
        self._personas[pid] = p
        self._persist_persona(p)
        return p

    def update_persona(self, persona_id: str, **kwargs) -> Optional[Persona]:
        p = self._personas.get(persona_id)
        if not p: return None
        for k, v in kwargs.items():
            if hasattr(p, k):
                setattr(p, k, v)
        p.version += 1
        self._persist_persona(p)
        return p

    def set_trait(self, persona_id: str, trait_name: str,
                  value: float, category: Optional[TraitCategory] = None,
                  prompt_fragment: str = "") -> bool:
        p = self._personas.get(persona_id)
        if not p: return False
        for t in p.traits:
            if t.name == trait_name:
                t.value = max(0.0, min(1.0, value))
                if prompt_fragment:
                    t.prompt_fragment = prompt_fragment
                p.version += 1
                return True
        # Add new trait
        cat = category or TraitCategory.PERSONALITY
        p.traits.append(Trait(name=trait_name, category=cat,
                              value=max(0.0, min(1.0, value)),
                              prompt_fragment=prompt_fragment))
        p.version += 1
        return True

    def clone_persona(self, persona_id: str,
                      new_name: str) -> Optional[Persona]:
        p = self._personas.get(persona_id)
        if not p: return None
        import copy
        clone = copy.deepcopy(p)
        clone.persona_id = str(uuid.uuid4())[:8]
        clone.name       = new_name
        clone.version    = 1
        clone.created_at = time.time()
        self._personas[clone.persona_id] = clone
        self._persist_persona(clone)
        return clone

    def deactivate(self, persona_id: str):
        p = self._personas.get(persona_id)
        if p: p.active = False

    # ── BLENDING ──────────────────────────────────────────────────────

    def create_blend(self, name: str,
                     components: List[Tuple[str, float]],
                     blend_id: Optional[str] = None) -> Optional[PersonaBlend]:
        # Validate all persona_ids exist
        for pid, _ in components:
            if pid not in self._personas:
                return None
        bid = blend_id or str(uuid.uuid4())[:8]
        # Normalize weights
        total_w = sum(w for _, w in components)
        normalized = [(pid, w / total_w) for pid, w in components] \
                     if total_w > 0 else components
        blend = PersonaBlend(blend_id=bid, name=name, components=normalized)
        self._blends[bid] = blend
        return blend

    def resolve_blend(self, blend_id: str) -> Optional[Persona]:
        """Materialize a blend into a virtual Persona with interpolated traits."""
        blend = self._blends.get(blend_id)
        if not blend: return None

        # Collect all trait names
        all_traits: Dict[str, List[Tuple[float, Trait]]] = {}
        for pid, weight in blend.components:
            p = self._personas.get(pid)
            if not p: continue
            for t in p.traits:
                all_traits.setdefault(t.name, []).append((weight, t))

        # Interpolate trait values
        merged_traits = []
        for tname, weighted_traits in all_traits.items():
            blended_value = sum(w * t.value for w, t in weighted_traits)
            ref_trait = weighted_traits[0][1]
            merged_traits.append({
                "name": tname,
                "category": ref_trait.category.value,
                "value": blended_value,
                "prompt_fragment": ref_trait.prompt_fragment,
            })

        # Blend base prompts
        base_prompts = []
        for pid, weight in blend.components:
            p = self._personas.get(pid)
            if p and p.base_prompt:
                base_prompts.append(p.base_prompt)

        return self.create_persona(
            name=f"Blend:{blend.name}",
            base_prompt="\n".join(base_prompts[:1]),
            traits=merged_traits,
            persona_id=f"blend:{blend_id}")

    # ── SESSION ASSIGNMENT ────────────────────────────────────────────

    def assign_session(self, session_id: str, persona_id: str) -> bool:
        if persona_id not in self._personas:
            return False
        self._sessions[session_id] = persona_id
        self._db.execute(
            "INSERT OR REPLACE INTO pe_sessions VALUES (?,?,?)",
            (session_id, persona_id, time.time()))
        self._db.commit()
        return True

    def get_session_persona(self, session_id: str) -> Optional[Persona]:
        pid = self._sessions.get(session_id)
        return self._personas.get(pid) if pid else None

    def release_session(self, session_id: str):
        self._sessions.pop(session_id, None)

    # ── A/B ASSIGNMENT ────────────────────────────────────────────────

    def set_ab_pool(self, persona_ids: List[str]):
        self._ab_pool = [pid for pid in persona_ids
                         if pid in self._personas]

    def get_ab_persona(self, user_id: str) -> Optional[Persona]:
        if not self._ab_pool: return None
        if user_id not in self._ab_assignments:
            # Deterministic hash-based assignment
            import hashlib
            idx = int(hashlib.md5(user_id.encode()).hexdigest(), 16) % len(self._ab_pool)
            self._ab_assignments[user_id] = self._ab_pool[idx]
        pid = self._ab_assignments[user_id]
        return self._personas.get(pid)

    # ── CONSISTENCY CHECKS ────────────────────────────────────────────

    def check_consistency(self, persona_id: str) -> List[str]:
        """Detect conflicting trait combinations."""
        p = self._personas.get(persona_id)
        if not p: return ["persona not found"]
        issues = []
        formal     = p.get_trait_value("formal")
        casual     = p.get_trait_value("casual")
        if formal > 0.8 and casual > 0.8:
            issues.append("Conflict: both 'formal' and 'casual' are high")
        concise    = p.get_trait_value("concise")
        elaborate  = p.get_trait_value("elaborate")
        if concise > 0.8 and elaborate > 0.8:
            issues.append("Conflict: both 'concise' and 'elaborate' are high")
        return issues

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_persona(self, persona_id: str) -> Optional[Persona]:
        return self._personas.get(persona_id)

    def find_by_name(self, name: str) -> Optional[Persona]:
        for p in self._personas.values():
            if p.name == name:
                return p
        return None

    def list_personas(self, active_only: bool = False,
                      tag: Optional[str] = None) -> List[Dict]:
        personas = list(self._personas.values())
        if active_only:
            personas = [p for p in personas if p.active]
        if tag:
            personas = [p for p in personas if tag in p.tags]
        return [p.to_dict() for p in personas]

    def build_prompt(self, persona_id: str) -> str:
        p = self._personas.get(persona_id)
        return p.build_system_prompt() if p else ""

    # ── PERSISTENCE ───────────────────────────────────────────────────

    def _persist_persona(self, p: Persona):
        self._db.execute(
            "INSERT OR REPLACE INTO pe_personas VALUES (?,?,?,?,?,?,?,?,?)",
            (p.persona_id, p.name, p.description, p.base_prompt,
             p.voice, p.version, int(p.active),
             json.dumps([t.__dict__ for t in p.traits]),
             p.created_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "personas": len(self._personas),
            "active": sum(1 for p in self._personas.values() if p.active),
            "blends": len(self._blends),
            "sessions": len(self._sessions),
            "ab_pool": len(self._ab_pool),
        }
