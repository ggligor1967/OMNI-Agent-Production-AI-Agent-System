"""OMNI Agent — Prompt Optimizer V2: variants, A/B scoring, auto-selection, history."""
from __future__ import annotations
import hashlib, json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class OptimizationStrategy(str, Enum):
    BEST_SCORE   = "best_score"    # always pick highest avg score
    UCB1         = "ucb1"          # Upper Confidence Bound
    EPSILON_GREEDY = "epsilon_greedy"
    RANDOM       = "random"
    ROUND_ROBIN  = "round_robin"


class PromptRole(str, Enum):
    SYSTEM = "system"
    USER   = "user"
    FEW_SHOT = "few_shot"
    SUFFIX = "suffix"


@dataclass
class PromptVariant:
    variant_id: str
    name: str
    template: str
    role: PromptRole = PromptRole.SYSTEM
    variables: List[str] = field(default_factory=list)  # {var} names in template
    score_sum: float = 0.0
    score_count: int = 0
    use_count: int = 0
    win_count: int = 0
    created_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def avg_score(self) -> float:
        return self.score_sum / self.score_count if self.score_count else 0.0

    @property
    def win_rate(self) -> float:
        return self.win_count / self.use_count if self.use_count else 0.0

    def render(self, **kwargs) -> str:
        text = self.template
        for k, v in kwargs.items():
            text = text.replace(f"{{{k}}}", str(v))
        return text

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "name": self.name,
            "role": self.role.value,
            "avg_score": round(self.avg_score, 4),
            "use_count": self.use_count,
            "win_rate": round(self.win_rate, 4),
        }


@dataclass
class PromptExperiment:
    experiment_id: str
    name: str
    variant_ids: List[str]
    strategy: OptimizationStrategy = OptimizationStrategy.UCB1
    epsilon: float = 0.1       # for epsilon-greedy
    ucb_c: float = 1.41        # exploration coefficient
    active: bool = True
    created_at: float = field(default_factory=time.time)
    total_trials: int = 0
    _rr_index: int = field(default=0, init=False, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "variants": len(self.variant_ids),
            "strategy": self.strategy.value,
            "active": self.active,
            "trials": self.total_trials,
        }


@dataclass
class PromptTrial:
    trial_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    experiment_id: str = ""
    variant_id: str = ""
    prompt_rendered: str = ""
    score: Optional[float] = None
    feedback: Optional[str] = None
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class PromptOptimizerV2:
    """
    Prompt optimization engine:
    - Multiple prompt variants per experiment
    - Selection strategies: BEST_SCORE, UCB1, Epsilon-Greedy, Random, Round-Robin
    - Template variable substitution
    - Score feedback loop (record outcomes)
    - Auto-winner selection after N trials
    - Few-shot example management
    - Prompt compression (remove redundant whitespace/tokens)
    - Version history per variant
    - SQLite persistence of trials and scores
    """

    def __init__(self, db_path: str = ":memory:"):
        self._variants:    Dict[str, PromptVariant] = {}
        self._experiments: Dict[str, PromptExperiment] = {}
        self._trials:      List[PromptTrial] = []
        self._few_shots:   Dict[str, List[Dict]] = {}  # tag → [{input,output}]
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS po_variants (
                variant_id TEXT PRIMARY KEY, name TEXT, template TEXT,
                role TEXT, score_sum REAL, score_count INTEGER,
                use_count INTEGER, win_count INTEGER, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS po_trials (
                trial_id TEXT PRIMARY KEY, experiment_id TEXT,
                variant_id TEXT, score REAL, ts REAL
            );
        """)
        self._db.commit()

    # ── VARIANT MANAGEMENT ────────────────────────────────────────────

    def add_variant(self, name: str, template: str,
                    role: PromptRole = PromptRole.SYSTEM,
                    tags: Optional[List[str]] = None,
                    metadata: Optional[Dict] = None,
                    variant_id: Optional[str] = None) -> PromptVariant:
        vid = variant_id or str(uuid.uuid4())[:8]
        # Detect variables in template
        import re
        variables = re.findall(r"\{(\w+)\}", template)
        v = PromptVariant(
            variant_id=vid, name=name, template=template,
            role=role, variables=variables,
            tags=list(tags or []), metadata=metadata or {})
        self._variants[vid] = v
        self._persist_variant(v)
        return v

    def update_template(self, variant_id: str, new_template: str) -> bool:
        v = self._variants.get(variant_id)
        if not v: return False
        import re
        v.template  = new_template
        v.variables = re.findall(r"\{(\w+)\}", new_template)
        self._persist_variant(v)
        return True

    def remove_variant(self, variant_id: str):
        self._variants.pop(variant_id, None)

    def render(self, variant_id: str, **kwargs) -> Optional[str]:
        v = self._variants.get(variant_id)
        return v.render(**kwargs) if v else None

    # ── FEW-SHOT MANAGEMENT ───────────────────────────────────────────

    def add_few_shot(self, tag: str, input_text: str, output_text: str):
        self._few_shots.setdefault(tag, []).append(
            {"input": input_text, "output": output_text})

    def get_few_shots(self, tag: str, max_k: int = 5) -> List[Dict]:
        return self._few_shots.get(tag, [])[:max_k]

    def build_few_shot_block(self, tag: str, max_k: int = 5) -> str:
        shots = self.get_few_shots(tag, max_k)
        lines = []
        for shot in shots:
            lines.append(f"Input: {shot['input']}")
            lines.append(f"Output: {shot['output']}")
            lines.append("")
        return "\n".join(lines)

    # ── EXPERIMENT ────────────────────────────────────────────────────

    def create_experiment(self, name: str,
                           variant_ids: List[str],
                           strategy: OptimizationStrategy = OptimizationStrategy.UCB1,
                           epsilon: float = 0.1,
                           ucb_c: float = 1.41,
                           experiment_id: Optional[str] = None) -> PromptExperiment:
        eid = experiment_id or str(uuid.uuid4())[:8]
        exp = PromptExperiment(
            experiment_id=eid, name=name,
            variant_ids=list(variant_ids),
            strategy=strategy, epsilon=epsilon, ucb_c=ucb_c)
        self._experiments[eid] = exp
        return exp

    def select_variant(self, experiment_id: str,
                        **render_kwargs) -> Optional[Tuple[PromptVariant, str]]:
        """Select best variant per strategy. Returns (variant, rendered_prompt)."""
        import math, random
        exp = self._experiments.get(experiment_id)
        if not exp or not exp.active:
            return None
        vids    = [vid for vid in exp.variant_ids if vid in self._variants]
        if not vids: return None
        variants = [self._variants[vid] for vid in vids]
        exp.total_trials += 1

        if exp.strategy == OptimizationStrategy.RANDOM:
            chosen = random.choice(variants)

        elif exp.strategy == OptimizationStrategy.ROUND_ROBIN:
            idx = exp._rr_index % len(variants)
            exp._rr_index += 1
            chosen = variants[idx]

        elif exp.strategy == OptimizationStrategy.BEST_SCORE:
            chosen = max(variants, key=lambda v: v.avg_score)

        elif exp.strategy == OptimizationStrategy.EPSILON_GREEDY:
            if random.random() < exp.epsilon:
                chosen = random.choice(variants)
            else:
                chosen = max(variants, key=lambda v: v.avg_score)

        else:  # UCB1
            total_n = sum(v.use_count for v in variants) + 1
            def ucb_score(v: PromptVariant) -> float:
                if v.use_count == 0: return float("inf")
                return v.avg_score + exp.ucb_c * math.sqrt(
                    math.log(total_n) / v.use_count)
            chosen = max(variants, key=ucb_score)

        chosen.use_count += 1
        self._persist_variant(chosen)
        rendered = chosen.render(**render_kwargs)
        return chosen, rendered

    def record_score(self, experiment_id: str, variant_id: str,
                     score: float,
                     feedback: Optional[str] = None) -> PromptTrial:
        variant = self._variants.get(variant_id)
        if variant:
            variant.score_sum   += score
            variant.score_count += 1
            self._persist_variant(variant)

        trial = PromptTrial(
            experiment_id=experiment_id,
            variant_id=variant_id,
            score=score, feedback=feedback)
        self._trials.append(trial)
        self._db.execute(
            "INSERT INTO po_trials VALUES (?,?,?,?,?)",
            (trial.trial_id, experiment_id, variant_id, score, trial.ts))
        self._db.commit()
        return trial

    def record_win(self, variant_id: str):
        v = self._variants.get(variant_id)
        if v:
            v.win_count += 1
            self._persist_variant(v)

    def auto_select_winner(self, experiment_id: str,
                            min_trials: int = 10) -> Optional[PromptVariant]:
        """Return the best variant if enough trials, else None."""
        exp = self._experiments.get(experiment_id)
        if not exp: return None
        variants = [self._variants[vid] for vid in exp.variant_ids
                    if vid in self._variants and
                    self._variants[vid].score_count >= min_trials]
        if not variants: return None
        return max(variants, key=lambda v: v.avg_score)

    def stop_experiment(self, experiment_id: str):
        exp = self._experiments.get(experiment_id)
        if exp: exp.active = False

    # ── COMPRESSION ───────────────────────────────────────────────────

    @staticmethod
    def compress(prompt: str) -> str:
        """Remove redundant whitespace and blank lines."""
        import re
        prompt = re.sub(r"\n{3,}", "\n\n", prompt)
        prompt = re.sub(r"[ \t]+", " ", prompt)
        lines  = [l.rstrip() for l in prompt.splitlines()]
        return "\n".join(lines).strip()

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_variant(self, variant_id: str) -> Optional[PromptVariant]:
        return self._variants.get(variant_id)

    def leaderboard(self, experiment_id: str) -> List[Dict]:
        exp = self._experiments.get(experiment_id)
        if not exp: return []
        variants = [self._variants[vid] for vid in exp.variant_ids
                    if vid in self._variants]
        return sorted([v.to_dict() for v in variants],
                      key=lambda d: -d["avg_score"])

    def trial_history(self, experiment_id: Optional[str] = None,
                       limit: int = 50) -> List[Dict]:
        q = ("SELECT trial_id,experiment_id,variant_id,score,ts "
             "FROM po_trials")
        params: List[Any] = []
        if experiment_id:
            q += " WHERE experiment_id=?"; params.append(experiment_id)
        q += " ORDER BY ts DESC LIMIT ?"; params.append(limit)
        rows = self._db.execute(q, params).fetchall()
        return [{"trial_id": r[0], "exp": r[1], "variant": r[2],
                 "score": r[3]} for r in rows]

    def _persist_variant(self, v: PromptVariant):
        self._db.execute(
            "INSERT OR REPLACE INTO po_variants VALUES (?,?,?,?,?,?,?,?,?)",
            (v.variant_id, v.name, v.template, v.role.value,
             v.score_sum, v.score_count, v.use_count,
             v.win_count, v.created_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "variants": len(self._variants),
            "experiments": len(self._experiments),
            "trials": len(self._trials),
            "few_shot_tags": len(self._few_shots),
        }
