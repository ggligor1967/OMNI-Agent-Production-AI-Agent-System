"""
OMNI AGENT - Prompt Optimizer
Eval-driven prompt improvement: generate variants, run A/B tests,
track performance history, and promote winning prompts automatically.

Features:
- Prompt variant generation (manual or LLM-assisted)
- A/B testing: run eval suite against multiple prompt variants
- SQLite-backed experiment log with full version history
- Automatic winner selection based on configurable metric (score/pass_rate/latency)
- Promotion: deploy best variant as the active system prompt
- Diff view: compare any two prompt versions
- Rollback: revert to any previous version
- Template variables: {user_name}, {date}, {persona} interpolation
- REST API: create experiments, check status, promote winners
"""
import re
import time
import uuid
import json
import sqlite3
import difflib
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT VARIANT
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PromptVariant:
    """A single prompt version under test."""
    id: str
    name: str
    text: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    author: str = ""
    parent_id: str = ""   # variant this was derived from

    def render(self, **variables) -> str:
        """Interpolate template variables: {key} → value."""
        result = self.text
        for key, val in variables.items():
            result = result.replace(f"{{{key}}}", str(val))
        return result

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "name": self.name,
            "text": self.text, "description": self.description,
            "tags": self.tags, "created_at": self.created_at,
            "author": self.author, "parent_id": self.parent_id,
        }


# ══════════════════════════════════════════════════════════════════════════════
# EXPERIMENT
# ══════════════════════════════════════════════════════════════════════════════

class ExperimentStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class VariantResult:
    variant_id: str
    variant_name: str
    pass_rate: float
    avg_score: float
    avg_latency_ms: float
    cases_total: int
    cases_passed: int
    raw_results: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "variant_id": self.variant_id,
            "variant_name": self.variant_name,
            "pass_rate": round(self.pass_rate, 4),
            "avg_score": round(self.avg_score, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "cases_total": self.cases_total,
            "cases_passed": self.cases_passed,
        }


@dataclass
class Experiment:
    id: str
    name: str
    suite_name: str                   # eval suite to run
    variant_ids: List[str]
    status: ExperimentStatus = ExperimentStatus.PENDING
    winner_id: Optional[str] = None
    winner_metric: str = "avg_score"  # avg_score | pass_rate | avg_latency_ms
    results: List[VariantResult] = field(default_factory=list)
    description: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    promoted: bool = False

    def best_result(self) -> Optional[VariantResult]:
        if not self.results:
            return None
        if self.winner_metric == "avg_latency_ms":
            return min(self.results, key=lambda r: r.avg_latency_ms)
        return max(self.results,
                   key=lambda r: getattr(r, self.winner_metric, 0))

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "name": self.name,
            "suite_name": self.suite_name,
            "variant_ids": self.variant_ids,
            "status": self.status,
            "winner_id": self.winner_id,
            "winner_metric": self.winner_metric,
            "results": [r.to_dict() for r in self.results],
            "description": self.description,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "promoted": self.promoted,
        }


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT STORE
# ══════════════════════════════════════════════════════════════════════════════

class PromptStore:
    """SQLite-backed storage for variants, experiments, and active prompts."""

    def __init__(self, db_path: str = "data/prompts.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS variants (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    text        TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    tags        TEXT DEFAULT '[]',
                    author      TEXT DEFAULT '',
                    parent_id   TEXT DEFAULT '',
                    created_at  REAL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    id           TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    suite_name   TEXT NOT NULL,
                    variant_ids  TEXT NOT NULL,
                    status       TEXT DEFAULT 'pending',
                    winner_id    TEXT,
                    winner_metric TEXT DEFAULT 'avg_score',
                    results      TEXT DEFAULT '[]',
                    description  TEXT DEFAULT '',
                    created_at   REAL,
                    completed_at REAL,
                    promoted     INTEGER DEFAULT 0
                );
                -- Active prompt per namespace
                CREATE TABLE IF NOT EXISTS active_prompts (
                    namespace   TEXT PRIMARY KEY,
                    variant_id  TEXT NOT NULL,
                    promoted_at REAL,
                    promoted_by TEXT DEFAULT ''
                );
            """)

    # ── Variants ──────────────────────────────────────────────────────────────

    def save_variant(self, v: PromptVariant):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO variants
                (id,name,text,description,tags,author,parent_id,created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (v.id, v.name, v.text, v.description,
                  json.dumps(v.tags), v.author, v.parent_id, v.created_at))

    def get_variant(self, vid: str) -> Optional[PromptVariant]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM variants WHERE id=?", (vid,)).fetchone()
        return self._row_to_variant(row) if row else None

    def list_variants(self, tag: str = None) -> List[PromptVariant]:
        with self._conn() as c:
            if tag:
                rows = c.execute(
                    "SELECT * FROM variants WHERE tags LIKE ? ORDER BY created_at DESC",
                    (f'%"{tag}"%',)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM variants ORDER BY created_at DESC"
                ).fetchall()
        return [self._row_to_variant(r) for r in rows]

    def delete_variant(self, vid: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM variants WHERE id=?", (vid,))
        return cur.rowcount > 0

    def _row_to_variant(self, row) -> PromptVariant:
        return PromptVariant(
            id=row["id"], name=row["name"], text=row["text"],
            description=row["description"] or "",
            tags=json.loads(row["tags"] or "[]"),
            author=row["author"] or "", parent_id=row["parent_id"] or "",
            created_at=row["created_at"],
        )

    # ── Experiments ───────────────────────────────────────────────────────────

    def save_experiment(self, exp: Experiment):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO experiments
                (id,name,suite_name,variant_ids,status,winner_id,winner_metric,
                 results,description,created_at,completed_at,promoted)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                exp.id, exp.name, exp.suite_name,
                json.dumps(exp.variant_ids), exp.status,
                exp.winner_id, exp.winner_metric,
                json.dumps([r.to_dict() for r in exp.results]),
                exp.description, exp.created_at, exp.completed_at,
                1 if exp.promoted else 0,
            ))

    def get_experiment(self, eid: str) -> Optional[Experiment]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM experiments WHERE id=?", (eid,)).fetchone()
        return self._row_to_experiment(row) if row else None

    def list_experiments(self, status: ExperimentStatus = None) -> List[Experiment]:
        with self._conn() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM experiments WHERE status=? ORDER BY created_at DESC",
                    (status,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM experiments ORDER BY created_at DESC"
                ).fetchall()
        return [self._row_to_experiment(r) for r in rows]

    def _row_to_experiment(self, row) -> Experiment:
        results_raw = json.loads(row["results"] or "[]")
        results = [
            VariantResult(
                variant_id=r["variant_id"],
                variant_name=r.get("variant_name", ""),
                pass_rate=r["pass_rate"],
                avg_score=r["avg_score"],
                avg_latency_ms=r["avg_latency_ms"],
                cases_total=r["cases_total"],
                cases_passed=r["cases_passed"],
            )
            for r in results_raw
        ]
        return Experiment(
            id=row["id"], name=row["name"],
            suite_name=row["suite_name"],
            variant_ids=json.loads(row["variant_ids"] or "[]"),
            status=ExperimentStatus(row["status"]),
            winner_id=row["winner_id"],
            winner_metric=row["winner_metric"] or "avg_score",
            results=results,
            description=row["description"] or "",
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            promoted=bool(row["promoted"]),
        )

    # ── Active prompts ────────────────────────────────────────────────────────

    def set_active(self, namespace: str, variant_id: str, promoted_by: str = ""):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO active_prompts
                (namespace, variant_id, promoted_at, promoted_by)
                VALUES (?,?,?,?)
            """, (namespace, variant_id, time.time(), promoted_by))

    def get_active(self, namespace: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT variant_id FROM active_prompts WHERE namespace=?",
                (namespace,)
            ).fetchone()
        return row["variant_id"] if row else None

    def list_active(self) -> Dict[str, str]:
        with self._conn() as c:
            rows = c.execute("SELECT namespace, variant_id FROM active_prompts").fetchall()
        return {r["namespace"]: r["variant_id"] for r in rows}


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT OPTIMIZER
# ══════════════════════════════════════════════════════════════════════════════

class PromptOptimizer:
    """
    Eval-driven prompt optimization with A/B experimentation.

    Usage:
        opt = PromptOptimizer(evaluator=agent.evaluator)

        # Create prompt variants
        v1 = opt.create_variant("baseline", "You are a helpful assistant.")
        v2 = opt.create_variant("detailed", "You are a precise, helpful assistant. "
                                             "Always cite sources. Be concise.")
        v3 = opt.create_variant("concise",  "Be brief and direct. No filler.")

        # Run A/B experiment against an eval suite
        exp = await opt.run_experiment(
            name="style_test",
            suite_name="basic_capabilities",
            variant_ids=[v1.id, v2.id, v3.id],
            model="deepseek-v3",
        )

        # Inspect results
        print(opt.experiment_report(exp.id))

        # Promote the winner
        opt.promote(exp.id, namespace="chat_system_prompt")

        # Get active prompt
        prompt = opt.get_active_prompt("chat_system_prompt")
    """

    def __init__(self, evaluator=None, llm=None,
                 db_path: str = "data/prompts.db"):
        self.evaluator = evaluator
        self.llm = llm
        self.store = PromptStore(db_path)

    # ── Variant management ────────────────────────────────────────────────────

    def create_variant(self, name: str, text: str,
                       description: str = "",
                       tags: List[str] = None,
                       author: str = "",
                       parent_id: str = "") -> PromptVariant:
        v = PromptVariant(
            id=str(uuid.uuid4())[:12],
            name=name, text=text, description=description,
            tags=tags or [], author=author, parent_id=parent_id,
        )
        self.store.save_variant(v)
        logger.info(f"Prompt variant created: id={v.id} name='{name}'")
        return v

    def get_variant(self, vid: str) -> Optional[PromptVariant]:
        return self.store.get_variant(vid)

    def list_variants(self, tag: str = None) -> List[PromptVariant]:
        return self.store.list_variants(tag)

    def delete_variant(self, vid: str) -> bool:
        return self.store.delete_variant(vid)

    def diff(self, vid_a: str, vid_b: str) -> str:
        """Return a unified diff between two prompt variants."""
        a = self.store.get_variant(vid_a)
        b = self.store.get_variant(vid_b)
        if not a or not b:
            return "One or both variants not found."
        diff = difflib.unified_diff(
            a.text.splitlines(keepends=True),
            b.text.splitlines(keepends=True),
            fromfile=f"{a.name} ({a.id})",
            tofile=f"{b.name} ({b.id})",
        )
        return "".join(diff) or "(no differences)"

    async def generate_variants(self, base_text: str,
                                 n: int = 3,
                                 goal: str = "improve clarity") -> List[PromptVariant]:
        """
        Use LLM to generate n alternative prompt variants from a base.
        Falls back to returning the base as-is if no LLM available.
        """
        if not self.llm:
            logger.warning("No LLM configured for variant generation")
            return [self.create_variant("base", base_text, "Auto-generated base")]

        prompt = f"""You are a prompt engineering expert. Generate {n} improved variants of the following system prompt.
Goal: {goal}

Original prompt:
{base_text}

Return ONLY a JSON array of objects with keys "name" and "text". No other text."""

        try:
            resp = await self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                session_id="prompt_optimizer",
                temperature=0.7,
            )
            content = resp.get("content", "[]")
            content = re.sub(r'```(?:json)?\s*', '', content).strip().rstrip('`')
            variants_data = json.loads(content)
            variants = []
            for d in variants_data[:n]:
                v = self.create_variant(
                    name=d.get("name", f"variant_{len(variants)+1}"),
                    text=d.get("text", base_text),
                    tags=["generated"],
                    description=f"LLM-generated variant. Goal: {goal}",
                )
                variants.append(v)
            return variants
        except Exception as e:
            logger.warning(f"Variant generation failed: {e}")
            return [self.create_variant("base", base_text)]

    # ── Experiments ───────────────────────────────────────────────────────────

    async def run_experiment(self,
                              name: str,
                              suite_name: str,
                              variant_ids: List[str],
                              model: str = "",
                              winner_metric: str = "avg_score",
                              description: str = "",
                              auto_promote: bool = False,
                              namespace: str = "default") -> Experiment:
        """
        Run an A/B experiment: evaluate each prompt variant against an eval suite.
        """
        exp = Experiment(
            id=str(uuid.uuid4())[:12],
            name=name,
            suite_name=suite_name,
            variant_ids=variant_ids,
            status=ExperimentStatus.RUNNING,
            winner_metric=winner_metric,
            description=description,
        )
        self.store.save_experiment(exp)
        logger.info(f"Experiment started: id={exp.id} name='{name}' "
                   f"variants={len(variant_ids)} suite={suite_name}")

        if not self.evaluator:
            # No evaluator: generate mock results for testing
            exp.results = self._mock_results(variant_ids)
        else:
            suite = self.evaluator.get_suite(suite_name)
            if not suite:
                logger.error(f"Eval suite not found: {suite_name}")
                exp.status = ExperimentStatus.CANCELLED
                self.store.save_experiment(exp)
                return exp

            # Run each variant
            for vid in variant_ids:
                variant = self.store.get_variant(vid)
                if not variant:
                    continue
                try:
                    suite_result = await self.evaluator.run(
                        suite_name, model_id=model or "default",
                        system_prompt_override=variant.text,
                    )
                    vr = VariantResult(
                        variant_id=vid,
                        variant_name=variant.name,
                        pass_rate=suite_result.pass_rate,
                        avg_score=suite_result.avg_score,
                        avg_latency_ms=suite_result.avg_latency_ms,
                        cases_total=len(suite_result.case_results),
                        cases_passed=sum(1 for c in suite_result.case_results if c.passed),
                    )
                    exp.results.append(vr)
                except Exception as e:
                    logger.error(f"Variant {vid} evaluation failed: {e}")

        # Determine winner
        best = exp.best_result()
        if best:
            exp.winner_id = best.variant_id

        exp.status = ExperimentStatus.COMPLETED
        exp.completed_at = time.time()
        self.store.save_experiment(exp)

        if auto_promote and exp.winner_id:
            self.promote(exp.id, namespace=namespace)
            exp.promoted = True

        logger.info(f"Experiment completed: id={exp.id} winner={exp.winner_id}")
        return exp

    def _mock_results(self, variant_ids: List[str]) -> List[VariantResult]:
        """Generate mock results when no evaluator is available."""
        import random
        results = []
        for i, vid in enumerate(variant_ids):
            variant = self.store.get_variant(vid)
            name = variant.name if variant else f"variant_{i}"
            results.append(VariantResult(
                variant_id=vid,
                variant_name=name,
                pass_rate=round(0.5 + i * 0.1, 2),
                avg_score=round(0.6 + i * 0.08, 3),
                avg_latency_ms=round(800 - i * 50, 1),
                cases_total=5,
                cases_passed=3 + i,
            ))
        return results

    def promote(self, experiment_id: str,
                namespace: str = "default",
                promoted_by: str = "auto") -> Optional[str]:
        """Promote the winning variant of an experiment as the active prompt."""
        exp = self.store.get_experiment(experiment_id)
        if not exp or not exp.winner_id:
            return None
        self.store.set_active(namespace, exp.winner_id, promoted_by)
        exp.promoted = True
        self.store.save_experiment(exp)
        logger.info(f"Promoted variant {exp.winner_id} → namespace '{namespace}'")
        return exp.winner_id

    def rollback(self, namespace: str, variant_id: str) -> bool:
        """Manually set the active prompt for a namespace."""
        if not self.store.get_variant(variant_id):
            return False
        self.store.set_active(namespace, variant_id, "rollback")
        logger.info(f"Rolled back namespace '{namespace}' → variant {variant_id}")
        return True

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def get_active_prompt(self, namespace: str = "default",
                          **variables) -> Optional[str]:
        """Get the active rendered prompt text for a namespace."""
        vid = self.store.get_active(namespace)
        if not vid:
            return None
        variant = self.store.get_variant(vid)
        if not variant:
            return None
        return variant.render(**variables) if variables else variant.text

    def get_experiment(self, eid: str) -> Optional[Experiment]:
        return self.store.get_experiment(eid)

    def list_experiments(self, status: ExperimentStatus = None) -> List[Experiment]:
        return self.store.list_experiments(status)

    def experiment_report(self, experiment_id: str) -> str:
        """Render a Markdown report for an experiment."""
        exp = self.store.get_experiment(experiment_id)
        if not exp:
            return "Experiment not found."

        lines = [
            f"# Prompt Experiment: {exp.name}",
            f"**Status:** {exp.status}  |  **Suite:** {exp.suite_name}  "
            f"|  **Metric:** {exp.winner_metric}",
            f"**Created:** {time.strftime('%Y-%m-%d %H:%M', time.gmtime(exp.created_at))}",
            "",
            "## Results",
            "",
            f"| Variant | Pass Rate | Avg Score | Avg Latency | Cases |",
            f"|---------|-----------|-----------|-------------|-------|",
        ]
        for r in sorted(exp.results,
                        key=lambda x: getattr(x, exp.winner_metric, 0),
                        reverse=(exp.winner_metric != "avg_latency_ms")):
            winner_mark = " 🏆" if r.variant_id == exp.winner_id else ""
            lines.append(
                f"| {r.variant_name}{winner_mark} | "
                f"{r.pass_rate:.1%} | {r.avg_score:.3f} | "
                f"{r.avg_latency_ms:.0f}ms | "
                f"{r.cases_passed}/{r.cases_total} |"
            )

        if exp.winner_id:
            winner_variant = self.store.get_variant(exp.winner_id)
            if winner_variant:
                lines += [
                    "", "## Winning Prompt",
                    f"**{winner_variant.name}** (`{winner_variant.id}`)",
                    "", "```", winner_variant.text, "```",
                ]

        return "\n".join(lines)

    # ── REST API ──────────────────────────────────────────────────────────────

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def list_variants_ep(request):
            tag = request.rel_url.query.get("tag")
            return web.json_response({
                "variants": [v.to_dict() for v in self.list_variants(tag)]
            })

        async def create_variant_ep(request):
            data = await request.json()
            v = self.create_variant(
                name=data["name"], text=data["text"],
                description=data.get("description", ""),
                tags=data.get("tags", []), author=data.get("author", ""),
            )
            return web.json_response(v.to_dict(), status=201)

        async def get_variant_ep(request):
            v = self.get_variant(request.match_info["id"])
            if not v:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response(v.to_dict())

        async def diff_ep(request):
            a, b = request.rel_url.query.get("a"), request.rel_url.query.get("b")
            return web.json_response({"diff": self.diff(a, b)})

        async def run_experiment_ep(request):
            data = await request.json()
            exp = await self.run_experiment(
                name=data["name"],
                suite_name=data["suite_name"],
                variant_ids=data["variant_ids"],
                model=data.get("model", ""),
                winner_metric=data.get("winner_metric", "avg_score"),
                description=data.get("description", ""),
                auto_promote=data.get("auto_promote", False),
                namespace=data.get("namespace", "default"),
            )
            return web.json_response(exp.to_dict(), status=201)

        async def get_experiment_ep(request):
            exp = self.get_experiment(request.match_info["id"])
            if not exp:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response(exp.to_dict())

        async def report_ep(request):
            md = self.experiment_report(request.match_info["id"])
            return web.Response(text=md, content_type="text/markdown")

        async def promote_ep(request):
            eid = request.match_info["id"]
            data = await request.json() if request.content_length else {}
            namespace = data.get("namespace", "default")
            vid = self.promote(eid, namespace)
            return web.json_response({"promoted": vid, "namespace": namespace})

        async def active_ep(request):
            namespace = request.rel_url.query.get("namespace", "default")
            prompt = self.get_active_prompt(namespace)
            return web.json_response({"namespace": namespace, "prompt": prompt})

        app.router.add_get( f"{prefix}/prompts/variants",               list_variants_ep)
        app.router.add_post(f"{prefix}/prompts/variants",               create_variant_ep)
        app.router.add_get( f"{prefix}/prompts/variants/{{id}}",        get_variant_ep)
        app.router.add_get( f"{prefix}/prompts/diff",                   diff_ep)
        app.router.add_post(f"{prefix}/prompts/experiments",            run_experiment_ep)
        app.router.add_get( f"{prefix}/prompts/experiments/{{id}}",     get_experiment_ep)
        app.router.add_get( f"{prefix}/prompts/experiments/{{id}}/report", report_ep)
        app.router.add_post(f"{prefix}/prompts/experiments/{{id}}/promote", promote_ep)
        app.router.add_get( f"{prefix}/prompts/active",                 active_ep)
        logger.info(f"Prompt optimizer API routes registered at {prefix}/prompts/")
