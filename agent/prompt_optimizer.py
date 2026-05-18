"""OMNI AGENT - Prompt Optimizer
Optimize prompts via template variants, A/B scoring, few-shot assembly,
token budget management, and automatic best-selection.

Features:
- PromptTemplate: named template with {{var}} slots and metadata
- Variant management: multiple phrasings of the same prompt
- A/B scoring: attach score observations to variants; pick best by avg
- Few-shot assembler: select K examples by cosine similarity to query
- Token budget: estimate tokens, trim to max_tokens via sentence truncation
- System prompt builder: role + constraints + format instructions
- Chain-of-thought injection: prepend/append CoT scaffolding
- Persona management: swap personas in/out of system prompt
- Template registry: CRUD with version tags
- Prompt diff: compare two templates char by char for changelog
- Auto-promotion: variant promoted to default when score > threshold
- SQLite persistence: templates, variants, scores, usage log
- REST API: render, score, best, variants, stats
"""
import json, math, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Token estimation ───────────────────────────────────────────────────────────
def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def _trim_to_budget(text: str, max_tokens: int) -> str:
    if _est_tokens(text) <= max_tokens: return text
    target_chars = max_tokens * 4
    sentences = re.split(r'(?<=[.!?])\s+', text)
    result = []
    chars = 0
    for s in sentences:
        if chars + len(s) + 1 > target_chars: break
        result.append(s); chars += len(s) + 1
    return " ".join(result) if result else text[:target_chars]

def _interpolate(template: str, context: Dict) -> str:
    def _repl(m):
        key = m.group(1).strip()
        return str(context.get(key, m.group(0)))
    return re.sub(r'\{\{([^}]+)\}\}', _repl, template)

def _slot_names(template: str) -> List[str]:
    return list(dict.fromkeys(
        m.strip() for m in re.findall(r'\{\{([^}]+)\}\}', template)))

# ── BOW for few-shot retrieval ─────────────────────────────────────────────────
def _bow(text: str) -> Dict[str, float]:
    words = re.findall(r'\b\w+\b', text.lower())
    d: Dict[str, float] = {}
    for w in words: d[w] = d.get(w, 0) + 1
    n = max(1, len(words))
    return {k: v / n for k, v in d.items()}

def _cosine_bow(a: Dict[str, float], b: Dict[str, float]) -> float:
    keys = set(a) & set(b)
    if not keys: return 0.0
    dot = sum(a[k] * b[k] for k in keys)
    na  = math.sqrt(sum(v*v for v in a.values()))
    nb  = math.sqrt(sum(v*v for v in b.values()))
    return dot / max(1e-12, na * nb)

# ── Data classes ───────────────────────────────────────────────────────────────
@dataclass
class PromptVariant:
    id: str; template_id: str; text: str
    label: str = "default"
    scores: List[float] = field(default_factory=list)
    usage_count: int = 0
    is_default: bool = False
    tags: List[str] = field(default_factory=list)

    @property
    def avg_score(self) -> Optional[float]:
        return sum(self.scores) / len(self.scores) if self.scores else None

    def to_dict(self):
        return {"id": self.id, "template_id": self.template_id,
                "label": self.label, "text_preview": self.text[:150],
                "avg_score": round(self.avg_score, 4) if self.avg_score else None,
                "usage_count": self.usage_count, "is_default": self.is_default,
                "tags": self.tags}

@dataclass
class FewShotExample:
    id: str; input: str; output: str
    tags: List[str] = field(default_factory=list)
    quality_score: float = 1.0
    _bow: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        self._bow = _bow(self.input + " " + self.output)

    def to_dict(self):
        return {"id": self.id, "input": self.input[:200],
                "output": self.output[:200], "tags": self.tags,
                "quality_score": self.quality_score}

@dataclass
class RenderedPrompt:
    template_id: str; variant_id: str
    text: str; token_count: int
    slots_filled: List[str] = field(default_factory=list)
    slots_missing: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"template_id": self.template_id, "variant_id": self.variant_id,
                "text": self.text, "token_count": self.token_count,
                "slots_filled": self.slots_filled,
                "slots_missing": self.slots_missing}

class POStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS templates(
                    id TEXT PRIMARY KEY, name TEXT, description TEXT DEFAULT '',
                    default_variant TEXT DEFAULT '', tags TEXT DEFAULT '[]',
                    created_at REAL, updated_at REAL);
                CREATE TABLE IF NOT EXISTS variants(
                    id TEXT PRIMARY KEY, template_id TEXT, label TEXT,
                    text TEXT, is_default INTEGER DEFAULT 0,
                    usage_count INTEGER DEFAULT 0,
                    avg_score REAL, tags TEXT DEFAULT '[]',
                    created_at REAL);
                CREATE TABLE IF NOT EXISTS usage_log(
                    id TEXT PRIMARY KEY, template_id TEXT, variant_id TEXT,
                    score REAL, created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_var_tmpl ON variants(template_id);
                CREATE INDEX IF NOT EXISTS idx_log_var  ON usage_log(variant_id, created_at DESC);
            """)

    def save_template(self, name: str, tid: str, description: str, tags: List):
        with self._conn() as c:
            now = time.time()
            c.execute("INSERT OR REPLACE INTO templates VALUES(?,?,?,?,?,?,?)",
                (tid, name, description, "", json.dumps(tags), now, now))

    def save_variant(self, v: PromptVariant):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO variants VALUES(?,?,?,?,?,?,?,?,?)",
                (v.id, v.template_id, v.label, v.text,
                 int(v.is_default), v.usage_count,
                 v.avg_score, json.dumps(v.tags), time.time()))

    def log_usage(self, template_id: str, variant_id: str, score: float = None):
        with self._conn() as c:
            c.execute("INSERT INTO usage_log VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], template_id, variant_id, score, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            nt = c.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
            nv = c.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
            nu = c.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0]
        return {"templates": nt, "variants": nv, "usage_logs": nu}

class PromptOptimizer:
    """
    Prompt template registry with variant A/B scoring and few-shot assembly.

    Usage:
        opt = PromptOptimizer()

        # Register a template with variants
        opt.register("qa",
                      "Answer the question: {{question}}\\nBe concise.",
                      description="Basic QA prompt")
        opt.add_variant("qa", "elaborate",
                         "You are an expert. Answer thoroughly: {{question}}")

        # Render with context
        rendered = opt.render("qa", {"question": "What is AI?"})
        print(rendered.text)

        # Score a variant and auto-select best
        opt.score("qa", "default", 4.2)
        opt.score("qa", "elaborate", 3.8)
        best = opt.best_variant("qa")
    """
    def __init__(self, db_path: str = "data/prompts.db",
                 auto_promote_threshold: float = 4.5,
                 min_score_samples: int = 3):
        self._store = POStore(db_path)
        self._templates: Dict[str, str] = {}           # name -> template_id
        self._variants: Dict[str, List[PromptVariant]] = {}  # template_id -> variants
        self._few_shot: List[FewShotExample] = []
        self._personas: Dict[str, str] = {}
        self._active_persona: Optional[str] = None
        self._auto_threshold = auto_promote_threshold
        self._min_samples = min_score_samples

    # ── Templates ─────────────────────────────────────────────────────────────
    def register(self, name: str, template_text: str,
                  description: str = "",
                  tags: List[str] = None) -> str:
        tid = self._templates.get(name) or str(uuid.uuid4())[:10]
        self._templates[name] = tid
        variant = PromptVariant(id=str(uuid.uuid4())[:8], template_id=tid,
                                 text=template_text, label="default",
                                 is_default=True, tags=list(tags or []))
        if tid not in self._variants:
            self._variants[tid] = []
        # Replace existing default
        self._variants[tid] = [v for v in self._variants[tid]
                                 if v.label != "default"]
        self._variants[tid].append(variant)
        self._store.save_template(name, tid, description, tags or [])
        self._store.save_variant(variant)
        return tid

    def add_variant(self, template_name: str, label: str,
                     text: str, tags: List[str] = None) -> Optional[PromptVariant]:
        tid = self._templates.get(template_name)
        if not tid: return None
        v = PromptVariant(id=str(uuid.uuid4())[:8], template_id=tid,
                           text=text, label=label, tags=list(tags or []))
        self._variants.setdefault(tid, []).append(v)
        self._store.save_variant(v)
        return v

    def get_variant(self, template_name: str,
                     label: str = "default") -> Optional[PromptVariant]:
        tid = self._templates.get(template_name)
        if not tid: return None
        for v in self._variants.get(tid, []):
            if v.label == label: return v
        return None

    def list_variants(self, template_name: str) -> List[PromptVariant]:
        tid = self._templates.get(template_name)
        if not tid: return []
        return list(self._variants.get(tid, []))

    # ── Rendering ─────────────────────────────────────────────────────────────
    def render(self, template_name: str,
                context: Dict = None,
                variant_label: str = "default",
                max_tokens: int = 0,
                few_shot_query: str = None,
                few_shot_k: int = 3) -> Optional[RenderedPrompt]:
        ctx = dict(context or {})
        # Inject active persona
        if self._active_persona and self._active_persona in self._personas:
            ctx.setdefault("persona", self._personas[self._active_persona])

        variant = self.get_variant(template_name, variant_label)
        if not variant:
            # Fallback to best variant
            variant = self.best_variant(template_name)
        if not variant: return None

        # Few-shot injection
        prefix = ""
        if few_shot_query and self._few_shot:
            examples = self._select_few_shot(few_shot_query, few_shot_k)
            if examples:
                lines = ["Here are some examples:"]
                for ex in examples:
                    lines.append(f"Input: {ex.input}\nOutput: {ex.output}")
                lines.append("")
                prefix = "\n".join(lines)

        text = prefix + _interpolate(variant.text, ctx)

        # CoT injection
        cot = ctx.pop("_cot", None)
        if cot:
            text = text + f"\n\nLet's think step by step:\n{cot}"

        slots = _slot_names(variant.text)
        filled  = [s for s in slots if s in ctx]
        missing = [s for s in slots if s not in ctx]

        if max_tokens > 0:
            text = _trim_to_budget(text, max_tokens)

        variant.usage_count += 1
        self._store.log_usage(variant.template_id, variant.id)

        return RenderedPrompt(template_id=variant.template_id,
                               variant_id=variant.id,
                               text=text, token_count=_est_tokens(text),
                               slots_filled=filled, slots_missing=missing)

    # ── Scoring ───────────────────────────────────────────────────────────────
    def score(self, template_name: str, variant_label: str,
               score_value: float) -> bool:
        v = self.get_variant(template_name, variant_label)
        if not v: return False
        v.scores.append(max(0.0, min(5.0, float(score_value))))
        self._store.save_variant(v)
        self._store.log_usage(v.template_id, v.id, score_value)
        # Auto-promote
        if (v.avg_score is not None and v.avg_score >= self._auto_threshold
                and len(v.scores) >= self._min_samples):
            tid = v.template_id
            for other in self._variants.get(tid, []):
                other.is_default = (other.id == v.id)
                self._store.save_variant(other)
        return True

    def best_variant(self, template_name: str) -> Optional[PromptVariant]:
        variants = self.list_variants(template_name)
        if not variants: return None
        scored = [v for v in variants if v.avg_score is not None]
        if not scored:
            # Return current default
            defaults = [v for v in variants if v.is_default]
            return defaults[0] if defaults else variants[0]
        return max(scored, key=lambda v: v.avg_score)

    # ── Few-shot ──────────────────────────────────────────────────────────────
    def add_few_shot(self, input_text: str, output_text: str,
                      tags: List[str] = None,
                      quality_score: float = 1.0) -> FewShotExample:
        ex = FewShotExample(id=str(uuid.uuid4())[:8],
                             input=input_text, output=output_text,
                             tags=list(tags or []),
                             quality_score=quality_score)
        self._few_shot.append(ex)
        return ex

    def _select_few_shot(self, query: str, k: int) -> List[FewShotExample]:
        if not self._few_shot: return []
        q_bow = _bow(query)
        scored = [(ex, _cosine_bow(q_bow, ex._bow) * ex.quality_score)
                   for ex in self._few_shot]
        scored.sort(key=lambda x: -x[1])
        return [ex for ex, _ in scored[:k]]

    # ── Personas ──────────────────────────────────────────────────────────────
    def add_persona(self, name: str, description: str):
        self._personas[name] = description

    def set_persona(self, name: str) -> bool:
        if name not in self._personas: return False
        self._active_persona = name; return True

    def clear_persona(self):
        self._active_persona = None

    # ── System prompts ────────────────────────────────────────────────────────
    def build_system_prompt(self, role: str = "",
                             constraints: List[str] = None,
                             output_format: str = "",
                             cot: bool = False) -> str:
        parts = []
        if role: parts.append(f"You are {role}.")
        for c in (constraints or []):
            parts.append(f"- {c}")
        if output_format:
            parts.append(f"Output format: {output_format}")
        if cot:
            parts.append("Think step by step before answering.")
        return "\n".join(parts)

    # ── Utilities ─────────────────────────────────────────────────────────────
    def diff(self, template_name: str,
              label_a: str, label_b: str) -> Dict:
        va = self.get_variant(template_name, label_a)
        vb = self.get_variant(template_name, label_b)
        if not va or not vb:
            return {"error": "variant not found"}
        a, b = va.text, vb.text
        additions = len(b) - len(a)
        slots_a = set(_slot_names(a))
        slots_b = set(_slot_names(b))
        return {"label_a": label_a, "label_b": label_b,
                "char_diff": additions,
                "added_slots": list(slots_b - slots_a),
                "removed_slots": list(slots_a - slots_b)}

    def token_budget(self, template_name: str, context: Dict,
                      max_tokens: int) -> Dict:
        r = self.render(template_name, context, max_tokens=max_tokens)
        if not r: return {}
        return {"estimated_tokens": r.token_count, "budget": max_tokens,
                "within_budget": r.token_count <= max_tokens,
                "text_preview": r.text[:200]}

    def stats(self) -> Dict:
        s = self._store.stats()
        s["few_shot_examples"] = len(self._few_shot)
        s["personas"] = len(self._personas)
        s["active_persona"] = self._active_persona
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def render_ep(req):
            d = await req.json()
            r = self.render(d["template"], d.get("context",{}),
                             d.get("variant","default"),
                             d.get("max_tokens",0))
            if not r: return web.json_response({"error":"not found"},status=404)
            return web.json_response(r.to_dict())
        async def score_ep(req):
            d = await req.json()
            ok = self.score(d["template"], d.get("variant","default"),
                             float(d["score"]))
            return web.json_response({"scored": ok})
        async def best_ep(req):
            name = req.match_info["name"]
            v = self.best_variant(name)
            if not v: return web.json_response({"error":"not found"},status=404)
            return web.json_response(v.to_dict())
        async def variants_ep(req):
            name = req.match_info["name"]
            return web.json_response(
                {"variants": [v.to_dict() for v in self.list_variants(name)]})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/prompt"
        app.router.add_post(f"{p}/render",             render_ep)
        app.router.add_post(f"{p}/score",              score_ep)
        app.router.add_get( f"{p}/best/{{name}}",      best_ep)
        app.router.add_get( f"{p}/variants/{{name}}",  variants_ep)
        app.router.add_get( f"{p}/stats",              stats_ep)
        logger.info(f"Prompt optimizer API at {prefix}/prompt/")
