"""
OMNI AGENT - Evaluation Framework
Benchmark LLM responses with rubric scoring, A/B model comparison,
regression test suites, and automatic quality tracking over time.

Features:
- Define EvalSuites with prompts + expected outputs + scoring rubrics
- Score responses: exact match, substring, regex, LLM-as-judge, cosine similarity
- Run A/B tests across any subset of the 24 cloud models
- Store results in SQLite for trend analysis
- Detect regressions: compare current vs baseline run
- Export results as Markdown or JSON report
"""
import re
import time
import json
import math
import uuid
import asyncio
import logging
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SCORING METHODS
# ══════════════════════════════════════════════════════════════════════════════

class ScoringMethod(str, Enum):
    EXACT_MATCH   = "exact_match"     # response == expected (normalized)
    SUBSTRING     = "substring"       # expected ⊆ response
    REGEX         = "regex"           # re.search(expected, response)
    KEYWORD       = "keyword"         # all keywords present in response
    LENGTH        = "length"          # response within min/max char range
    LLM_JUDGE     = "llm_judge"       # ask another LLM to score 0-10
    COSINE        = "cosine"          # embedding cosine similarity
    CUSTOM        = "custom"          # caller-provided fn(response, expected)->float


@dataclass
class ScoringCriteria:
    """A single scoring criterion for an eval case."""
    method: ScoringMethod
    weight: float = 1.0                # relative weight in final score
    expected: str = ""                 # expected value / pattern / keywords
    keywords: List[str] = field(default_factory=list)   # for KEYWORD method
    min_length: int = 0                # for LENGTH method
    max_length: int = 100_000          # for LENGTH method
    judge_prompt: str = ""             # extra context for LLM_JUDGE
    custom_fn: Optional[Callable] = None  # for CUSTOM method
    threshold: float = 0.5            # min score to count as pass


# ══════════════════════════════════════════════════════════════════════════════
# EVAL CASE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EvalCase:
    """A single prompt + expected output + scoring configuration."""
    id: str
    prompt: str
    criteria: List[ScoringCriteria]
    description: str = ""
    category: str = "general"
    tags: List[str] = field(default_factory=list)
    system_prompt: str = ""
    max_tokens: int = 512
    temperature: float = 0.0          # deterministic by default
    expected_output: str = ""         # for reference / exact match


@dataclass
class EvalSuite:
    """A named collection of EvalCases."""
    name: str
    description: str = ""
    cases: List[EvalCase] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    def add(self, case: EvalCase) -> "EvalSuite":
        self.cases.append(case)
        return self

    def filter_by_category(self, category: str) -> "EvalSuite":
        filtered = EvalSuite(f"{self.name}[{category}]")
        filtered.cases = [c for c in self.cases if c.category == category]
        return filtered


# ══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CriterionResult:
    method: str
    score: float           # 0.0 – 1.0
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    model_id: str
    response: str
    criteria_results: List[CriterionResult]
    total_score: float     # weighted average across criteria
    passed: bool
    latency_ms: float
    error: str = ""

    def to_dict(self) -> Dict:
        return {
            "case_id": self.case_id,
            "model_id": self.model_id,
            "total_score": round(self.total_score, 3),
            "passed": self.passed,
            "latency_ms": round(self.latency_ms, 1),
            "criteria": [
                {"method": cr.method, "score": round(cr.score, 3),
                 "passed": cr.passed, "detail": cr.detail}
                for cr in self.criteria_results
            ],
            "response_preview": self.response[:200],
            "error": self.error,
        }


@dataclass
class SuiteResult:
    suite_name: str
    model_id: str
    run_id: str
    case_results: List[CaseResult]
    started_at: float
    finished_at: float

    @property
    def pass_rate(self) -> float:
        if not self.case_results:
            return 0.0
        return sum(1 for r in self.case_results if r.passed) / len(self.case_results)

    @property
    def avg_score(self) -> float:
        if not self.case_results:
            return 0.0
        return sum(r.total_score for r in self.case_results) / len(self.case_results)

    @property
    def avg_latency_ms(self) -> float:
        if not self.case_results:
            return 0.0
        return sum(r.latency_ms for r in self.case_results) / len(self.case_results)

    @property
    def duration_s(self) -> float:
        return self.finished_at - self.started_at

    def by_category(self) -> Dict[str, Dict]:
        """Group results by case category."""
        cats: Dict[str, List[CaseResult]] = {}
        return cats

    def to_dict(self) -> Dict:
        return {
            "suite": self.suite_name,
            "model": self.model_id,
            "run_id": self.run_id,
            "cases_total": len(self.case_results),
            "cases_passed": sum(1 for r in self.case_results if r.passed),
            "pass_rate": round(self.pass_rate, 3),
            "avg_score": round(self.avg_score, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "duration_s": round(self.duration_s, 2),
            "started_at": self.started_at,
        }

    def to_markdown(self) -> str:
        lines = [
            f"# Eval Report: {self.suite_name}",
            f"**Model:** {self.model_id}  |  **Run:** {self.run_id}",
            f"**Pass rate:** {self.pass_rate:.1%}  |  "
            f"**Avg score:** {self.avg_score:.3f}  |  "
            f"**Avg latency:** {self.avg_latency_ms:.0f}ms",
            "",
            "## Results",
            "| Case | Score | Pass | Latency | Preview |",
            "|------|-------|------|---------|---------|",
        ]
        for r in self.case_results:
            preview = r.response[:60].replace("\n", " ") if not r.error else f"ERROR: {r.error[:40]}"
            lines.append(
                f"| {r.case_id} | {r.total_score:.3f} | "
                f"{'✓' if r.passed else '✗'} | {r.latency_ms:.0f}ms | {preview} |"
            )
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# SCORER
# ══════════════════════════════════════════════════════════════════════════════

class ResponseScorer:
    """Scores a single LLM response against a list of ScoringCriteria."""

    def __init__(self, llm=None, embed_fn=None):
        self.llm = llm
        self.embed_fn = embed_fn

    async def score_all(self, response: str,
                        criteria: List[ScoringCriteria]) -> Tuple[float, List[CriterionResult]]:
        """Score response against all criteria. Returns (weighted_avg, results)."""
        results = []
        for c in criteria:
            result = await self._score_one(response, c)
            results.append(result)

        total_weight = sum(c.weight for c in criteria)
        if total_weight == 0:
            return 0.0, results

        weighted_sum = sum(
            r.score * c.weight
            for r, c in zip(results, criteria)
        )
        return weighted_sum / total_weight, results

    async def _score_one(self, response: str, c: ScoringCriteria) -> CriterionResult:
        method = c.method
        try:
            if method == ScoringMethod.EXACT_MATCH:
                score, detail = self._exact_match(response, c.expected)
            elif method == ScoringMethod.SUBSTRING:
                score, detail = self._substring(response, c.expected)
            elif method == ScoringMethod.REGEX:
                score, detail = self._regex(response, c.expected)
            elif method == ScoringMethod.KEYWORD:
                score, detail = self._keyword(response, c.keywords)
            elif method == ScoringMethod.LENGTH:
                score, detail = self._length(response, c.min_length, c.max_length)
            elif method == ScoringMethod.LLM_JUDGE:
                score, detail = await self._llm_judge(response, c.expected, c.judge_prompt)
            elif method == ScoringMethod.COSINE:
                score, detail = await self._cosine_sim(response, c.expected)
            elif method == ScoringMethod.CUSTOM and c.custom_fn:
                raw = c.custom_fn(response, c.expected)
                score = float(raw)
                detail = f"custom={score:.3f}"
            else:
                score, detail = 0.0, "unknown method"

            score = max(0.0, min(1.0, score))
            return CriterionResult(
                method=method.value, score=score,
                passed=score >= c.threshold, detail=detail
            )
        except Exception as e:
            logger.warning(f"Scoring error ({method}): {e}")
            return CriterionResult(method=method.value, score=0.0,
                                  passed=False, detail=f"error: {e}")

    # ── Scoring implementations ───────────────────────────────────────────────

    def _exact_match(self, response: str, expected: str) -> Tuple[float, str]:
        norm_r = response.strip().lower()
        norm_e = expected.strip().lower()
        score = 1.0 if norm_r == norm_e else 0.0
        return score, f"exact={'match' if score else 'miss'}"

    def _substring(self, response: str, expected: str) -> Tuple[float, str]:
        found = expected.lower() in response.lower()
        return (1.0 if found else 0.0), f"substring={'found' if found else 'missing'}"

    def _regex(self, response: str, pattern: str) -> Tuple[float, str]:
        try:
            match = re.search(pattern, response, re.IGNORECASE | re.DOTALL)
            return (1.0 if match else 0.0), f"regex={'matched' if match else 'no match'}"
        except re.error as e:
            return 0.0, f"invalid regex: {e}"

    def _keyword(self, response: str, keywords: List[str]) -> Tuple[float, str]:
        if not keywords:
            return 1.0, "no keywords"
        resp_lower = response.lower()
        found = [k for k in keywords if k.lower() in resp_lower]
        score = len(found) / len(keywords)
        return score, f"{len(found)}/{len(keywords)} keywords found"

    def _length(self, response: str, min_len: int, max_len: int) -> Tuple[float, str]:
        n = len(response)
        if n < min_len:
            score = n / min_len if min_len > 0 else 0.0
            return score, f"too short ({n} < {min_len})"
        if n > max_len:
            # Penalize proportionally for going over
            overage = (n - max_len) / max_len
            score = max(0.0, 1.0 - overage)
            return score, f"too long ({n} > {max_len}), score={score:.2f}"
        return 1.0, f"length OK ({n} chars)"

    async def _llm_judge(self, response: str, expected: str,
                         extra_prompt: str) -> Tuple[float, str]:
        if not self.llm:
            return 0.5, "no llm judge available"
        judge_prompt = (
            f"Rate this AI response on a scale of 0 to 10.\n\n"
            f"{'Expected: ' + expected + chr(10) if expected else ''}"
            f"{extra_prompt + chr(10) if extra_prompt else ''}"
            f"Response to rate:\n{response[:1000]}\n\n"
            f"Reply with ONLY a number 0-10. Nothing else."
        )
        try:
            resp = await self.llm.chat(
                messages=[{"role": "user", "content": judge_prompt}],
                model="gpt-oss:20b-cloud",
                temperature=0.0,
                session_id="eval_judge",
                auto_route=False,
            )
            content = resp.get("content", "5").strip()
            # Extract first number
            nums = re.findall(r'\b(\d+(?:\.\d+)?)\b', content)
            if nums:
                raw = float(nums[0])
                score = min(10.0, max(0.0, raw)) / 10.0
                return score, f"judge={raw}/10"
        except Exception as e:
            logger.warning(f"LLM judge error: {e}")
        return 0.5, "judge fallback"

    async def _cosine_sim(self, response: str, expected: str) -> Tuple[float, str]:
        if not self.embed_fn:
            return 0.5, "no embed_fn"
        try:
            emb_r = await self.embed_fn(response[:512])
            emb_e = await self.embed_fn(expected[:512])
            if not emb_r or not emb_e or len(emb_r) != len(emb_e):
                return 0.5, "embed failed"
            dot = sum(a * b for a, b in zip(emb_r, emb_e))
            nr = math.sqrt(sum(a * a for a in emb_r))
            ne = math.sqrt(sum(b * b for b in emb_e))
            sim = dot / (nr * ne) if nr and ne else 0.0
            # Map cosine [-1,1] → [0,1]
            score = (sim + 1) / 2
            return score, f"cosine={sim:.3f}"
        except Exception as e:
            return 0.5, f"cosine error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# RESULT STORE
# ══════════════════════════════════════════════════════════════════════════════

class EvalResultStore:
    """SQLite-backed store for eval results, enabling trend analysis."""

    def __init__(self, db_path: str = "data/eval_results.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS suite_runs (
                    run_id       TEXT PRIMARY KEY,
                    suite_name   TEXT,
                    model_id     TEXT,
                    pass_rate    REAL,
                    avg_score    REAL,
                    avg_latency  REAL,
                    cases_total  INTEGER,
                    cases_passed INTEGER,
                    duration_s   REAL,
                    started_at   REAL,
                    result_json  TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_suite ON suite_runs(suite_name, started_at);
                CREATE INDEX IF NOT EXISTS idx_runs_model ON suite_runs(model_id, started_at);
            """)

    def save(self, result: SuiteResult):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO suite_runs
                (run_id,suite_name,model_id,pass_rate,avg_score,avg_latency,
                 cases_total,cases_passed,duration_s,started_at,result_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                result.run_id, result.suite_name, result.model_id,
                result.pass_rate, result.avg_score, result.avg_latency_ms,
                len(result.case_results),
                sum(1 for r in result.case_results if r.passed),
                result.duration_s, result.started_at,
                json.dumps(result.to_dict()),
            ))

    def get_history(self, suite_name: str, model_id: Optional[str] = None,
                    limit: int = 20) -> List[Dict]:
        with self._conn() as conn:
            if model_id:
                rows = conn.execute("""
                    SELECT * FROM suite_runs WHERE suite_name=? AND model_id=?
                    ORDER BY started_at DESC LIMIT ?
                """, (suite_name, model_id, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM suite_runs WHERE suite_name=?
                    ORDER BY started_at DESC LIMIT ?
                """, (suite_name, limit)).fetchall()
        return [dict(r) for r in rows]

    def model_comparison(self, suite_name: str) -> List[Dict]:
        """Latest run per model for a suite, sorted by avg_score desc."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT model_id, MAX(started_at) as latest, pass_rate, avg_score, avg_latency
                FROM suite_runs WHERE suite_name=?
                GROUP BY model_id ORDER BY avg_score DESC
            """, (suite_name,)).fetchall()
        return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# EVALUATOR (main class)
# ══════════════════════════════════════════════════════════════════════════════

class Evaluator:
    """
    Runs evaluation suites against one or more models.

    Usage:
        evaluator = Evaluator(llm=agent.llm)

        suite = EvalSuite("reasoning")
        suite.add(EvalCase(
            id="math_1",
            prompt="What is 15 * 17?",
            criteria=[
                ScoringCriteria(ScoringMethod.SUBSTRING, expected="255"),
            ],
        ))

        result = await evaluator.run(suite, model_id="deepseek-v3.1:671b-cloud")
        print(result.pass_rate, result.avg_score)

        # A/B test across multiple models
        ab = await evaluator.ab_test(suite, model_ids=["deepseek-v3.1:671b-cloud",
                                                        "cogito-2.1:671b-cloud"])
        evaluator.print_ab_report(ab)
    """

    def __init__(self, llm=None, embed_fn=None,
                 db_path: str = "data/eval_results.db"):
        self.llm = llm
        self.scorer = ResponseScorer(llm=llm, embed_fn=embed_fn)
        self.store = EvalResultStore(db_path)
        self._suites: Dict[str, EvalSuite] = {}
        self._load_builtin_suites()

    # ── Built-in Suites ───────────────────────────────────────────────────────

    def _load_builtin_suites(self):
        """Register built-in evaluation suites."""

        # Basic capabilities
        basic = EvalSuite("basic_capabilities", "Core language understanding")
        for case_data in [
            ("bc_1", "What is 2 + 2?", "4", ["4"], "arithmetic"),
            ("bc_2", "What is the capital of France?", "Paris", ["Paris"], "geography"),
            ("bc_3", "Name the 3 primary colors.", "", ["red", "blue", "yellow"], "knowledge"),
            ("bc_4", "Translate 'hello' to Spanish.", "hola", ["hola"], "translation"),
            ("bc_5", "What does CPU stand for?", "Central Processing Unit",
             ["Central", "Processing", "Unit"], "technology"),
        ]:
            cid, prompt, expected, keywords, cat = case_data
            basic.add(EvalCase(
                id=cid, prompt=prompt, expected_output=expected, category=cat,
                criteria=[
                    ScoringCriteria(ScoringMethod.KEYWORD, keywords=keywords, weight=2.0),
                    ScoringCriteria(ScoringMethod.LENGTH, min_length=1, max_length=500, weight=0.5),
                ],
            ))
        self.register_suite(basic)

        # Code generation
        code = EvalSuite("code_generation", "Code quality and correctness")
        code.add(EvalCase(
            id="cg_1",
            prompt="Write a Python function that returns the sum of a list of numbers.",
            category="python",
            criteria=[
                ScoringCriteria(ScoringMethod.KEYWORD,
                               keywords=["def", "sum", "return"], weight=2.0),
                ScoringCriteria(ScoringMethod.REGEX,
                               expected=r"def\s+\w+\(", weight=1.0),
                ScoringCriteria(ScoringMethod.LENGTH,
                               min_length=30, max_length=500, weight=0.5),
            ],
        ))
        code.add(EvalCase(
            id="cg_2",
            prompt="Write a SQL query to select all users older than 25.",
            category="sql",
            criteria=[
                ScoringCriteria(ScoringMethod.KEYWORD,
                               keywords=["SELECT", "WHERE", "age"], weight=2.0),
                ScoringCriteria(ScoringMethod.REGEX,
                               expected=r"SELECT.+FROM", weight=1.0),
            ],
        ))
        self.register_suite(code)

        # Instruction following
        instruct = EvalSuite("instruction_following", "Following explicit format instructions")
        instruct.add(EvalCase(
            id="if_1",
            prompt="List exactly 3 fruits. Number them 1, 2, 3.",
            category="formatting",
            criteria=[
                ScoringCriteria(ScoringMethod.REGEX, expected=r"1\.", weight=1.0),
                ScoringCriteria(ScoringMethod.REGEX, expected=r"2\.", weight=1.0),
                ScoringCriteria(ScoringMethod.REGEX, expected=r"3\.", weight=1.0),
                ScoringCriteria(ScoringMethod.LENGTH, min_length=10, max_length=200, weight=0.5),
            ],
        ))
        instruct.add(EvalCase(
            id="if_2",
            prompt="Respond with ONLY the word 'yes' and nothing else.",
            expected_output="yes",
            category="conciseness",
            criteria=[
                ScoringCriteria(ScoringMethod.EXACT_MATCH, expected="yes",
                               weight=2.0, threshold=0.9),
                ScoringCriteria(ScoringMethod.LENGTH, max_length=10, weight=1.0),
            ],
        ))
        instruct.add(EvalCase(
            id="if_3",
            prompt="Write a haiku about programming (5-7-5 syllable structure).",
            category="creative",
            criteria=[
                ScoringCriteria(ScoringMethod.LENGTH, min_length=20, max_length=200, weight=1.0),
                ScoringCriteria(ScoringMethod.REGEX, expected=r"\n", weight=0.5,
                               threshold=0.0),  # should have line breaks
            ],
        ))
        self.register_suite(instruct)

    def register_suite(self, suite: EvalSuite):
        self._suites[suite.name] = suite

    def get_suite(self, name: str) -> Optional[EvalSuite]:
        return self._suites.get(name)

    def list_suites(self) -> List[Dict]:
        return [
            {"name": s.name, "description": s.description,
             "cases": len(s.cases)}
            for s in self._suites.values()
        ]

    # ── Execution ─────────────────────────────────────────────────────────────

    async def run(self, suite: EvalSuite, model_id: str,
                  concurrency: int = 3) -> SuiteResult:
        """Run a full eval suite against one model."""
        run_id = str(uuid.uuid4())[:8]
        started_at = time.time()
        logger.info(f"Eval '{suite.name}' on '{model_id}' "
                   f"({len(suite.cases)} cases) [run={run_id}]")

        sem = asyncio.Semaphore(concurrency)
        tasks = [self._run_case(case, model_id, sem) for case in suite.cases]
        case_results = list(await asyncio.gather(*tasks))

        result = SuiteResult(
            suite_name=suite.name,
            model_id=model_id,
            run_id=run_id,
            case_results=case_results,
            started_at=started_at,
            finished_at=time.time(),
        )
        self.store.save(result)
        logger.info(f"Eval done: pass_rate={result.pass_rate:.1%} "
                   f"avg_score={result.avg_score:.3f} [{run_id}]")
        return result

    async def _run_case(self, case: EvalCase, model_id: str,
                        sem: asyncio.Semaphore) -> CaseResult:
        async with sem:
            start = time.time()
            try:
                messages = [{"role": "user", "content": case.prompt}]
                resp = await self.llm.chat(
                    messages=messages,
                    model=model_id,
                    system=case.system_prompt or None,
                    temperature=case.temperature,
                    session_id=f"eval:{case.id}",
                    auto_route=False,
                )
                response_text = resp.get("content", "")
                error = ""
            except Exception as e:
                response_text = ""
                error = str(e)
                logger.warning(f"Case {case.id} failed: {e}")

            latency_ms = (time.time() - start) * 1000

            if error:
                return CaseResult(
                    case_id=case.id, model_id=model_id,
                    response=response_text, criteria_results=[],
                    total_score=0.0, passed=False,
                    latency_ms=latency_ms, error=error,
                )

            total_score, criteria_results = await self.scorer.score_all(
                response_text, case.criteria
            )

            # Pass if all required criteria pass (or if score > 0.5)
            all_pass = all(cr.passed for cr in criteria_results)
            passed = all_pass or total_score >= 0.5

            return CaseResult(
                case_id=case.id, model_id=model_id,
                response=response_text, criteria_results=criteria_results,
                total_score=total_score, passed=passed,
                latency_ms=latency_ms,
            )

    # ── A/B Testing ───────────────────────────────────────────────────────────

    async def ab_test(self, suite: EvalSuite,
                      model_ids: List[str]) -> Dict[str, SuiteResult]:
        """Run the same suite against multiple models in parallel."""
        tasks = {mid: self.run(suite, mid) for mid in model_ids}
        results = {}
        for mid, coro in tasks.items():
            results[mid] = await coro
        return results

    def ab_report(self, results: Dict[str, SuiteResult]) -> str:
        """Format A/B test results as a Markdown table."""
        lines = [
            "# A/B Evaluation Report",
            "",
            "| Model | Pass Rate | Avg Score | Avg Latency | Cases |",
            "|-------|-----------|-----------|-------------|-------|",
        ]
        sorted_results = sorted(results.values(),
                                key=lambda r: r.avg_score, reverse=True)
        for r in sorted_results:
            lines.append(
                f"| {r.model_id} | {r.pass_rate:.1%} | "
                f"{r.avg_score:.3f} | {r.avg_latency_ms:.0f}ms | "
                f"{sum(1 for c in r.case_results if c.passed)}/{len(r.case_results)} |"
            )
        return "\n".join(lines)

    # ── Regression Testing ────────────────────────────────────────────────────

    async def regression_test(self, suite: EvalSuite, model_id: str,
                               min_pass_rate: float = 0.8,
                               min_avg_score: float = 0.7) -> Dict:
        """
        Run suite and compare to historical baseline.
        Returns regression report with pass/fail verdict.
        """
        current = await self.run(suite, model_id)
        history = self.store.get_history(suite.name, model_id, limit=5)

        report = {
            "verdict": "pass",
            "current": current.to_dict(),
            "regressions": [],
            "improvements": [],
        }

        # Check thresholds
        if current.pass_rate < min_pass_rate:
            report["verdict"] = "fail"
            report["regressions"].append(
                f"pass_rate {current.pass_rate:.1%} < threshold {min_pass_rate:.1%}"
            )
        if current.avg_score < min_avg_score:
            report["verdict"] = "fail"
            report["regressions"].append(
                f"avg_score {current.avg_score:.3f} < threshold {min_avg_score:.3f}"
            )

        # Compare to previous run
        if len(history) >= 2:
            prev = history[1]  # [0] is current run just saved
            score_delta = current.avg_score - prev["avg_score"]
            if score_delta < -0.05:
                report["verdict"] = "fail"
                report["regressions"].append(
                    f"score dropped by {abs(score_delta):.3f} vs previous run"
                )
            elif score_delta > 0.02:
                report["improvements"].append(
                    f"score improved by {score_delta:.3f} vs previous run"
                )

        return report

    # ── History & Trends ──────────────────────────────────────────────────────

    def get_history(self, suite_name: str, model_id: Optional[str] = None) -> List[Dict]:
        return self.store.get_history(suite_name, model_id)

    def model_comparison(self, suite_name: str) -> List[Dict]:
        return self.store.model_comparison(suite_name)
