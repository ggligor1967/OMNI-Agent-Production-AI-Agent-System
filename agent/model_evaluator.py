"""OMNI Agent — Model Evaluator: benchmark harness for comparing model outputs."""
from __future__ import annotations
import json, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ScoringMethod(str, Enum):
    EXACT_MATCH  = "exact_match"
    CONTAINS     = "contains"
    REGEX        = "regex"
    RUBRIC       = "rubric"         # multi-criterion weighted scoring
    SEMANTIC     = "semantic"       # cosine similarity
    CUSTOM       = "custom"         # user-provided fn


class EvalStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class RubricCriterion:
    name: str
    weight: float = 1.0
    scorer: Optional[Callable[[str, str], float]] = None
    description: str = ""


@dataclass
class EvalCase:
    case_id: str
    prompt: str
    expected: Any
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalResult:
    case_id: str
    model_id: str
    output: Any
    score: float            # 0.0 – 1.0
    status: EvalStatus
    method: ScoringMethod
    details: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "model_id": self.model_id,
            "score": round(self.score, 4),
            "status": self.status.value,
            "method": self.method.value,
            "duration_ms": round(self.duration_ms, 2),
            "details": self.details,
        }


@dataclass
class BenchmarkReport:
    benchmark_id: str
    model_id: str
    total_cases: int
    passed: int
    failed: int
    skipped: int
    avg_score: float
    min_score: float
    max_score: float
    results: List[EvalResult]
    duration_ms: float
    ts: float = field(default_factory=time.time)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total_cases if self.total_cases else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "model_id": self.model_id,
            "total": self.total_cases,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 4),
            "avg_score": round(self.avg_score, 4),
            "min_score": round(self.min_score, 4),
            "max_score": round(self.max_score, 4),
            "duration_ms": round(self.duration_ms, 2),
        }


# ── BUILT-IN SCORERS ─────────────────────────────────────────────────────────

def _score_exact(output: Any, expected: Any) -> float:
    return 1.0 if str(output).strip() == str(expected).strip() else 0.0


def _score_contains(output: Any, expected: Any) -> float:
    return 1.0 if str(expected).lower() in str(output).lower() else 0.0


def _score_semantic(output: str, expected: str) -> float:
    """Hash-based approximate similarity (no ML)."""
    import hashlib
    def _embed(text: str, dim: int = 32) -> List[float]:
        h = hashlib.sha256(text.encode()).digest()
        raw = (list(h) * (dim // 32 + 1))[:dim]
        vec = [(b / 127.5) - 1.0 for b in raw]
        n = math.sqrt(sum(x * x for x in vec))
        return [x / n for x in vec] if n > 0 else vec
    a, b = _embed(str(output)), _embed(str(expected))
    dot = sum(x * y for x, y in zip(a, b))
    return max(0.0, min(1.0, (dot + 1.0) / 2.0))


class ModelEvaluator:
    """
    Benchmark harness for LLM output evaluation:
    - Multiple scoring methods (exact, contains, regex, rubric, semantic, custom)
    - Multi-model comparison
    - Benchmark suites (named collections of cases)
    - Pass/fail thresholds per case or globally
    - Aggregated reports per model
    - SQLite persistence of all results
    - Score aggregation across runs
    """

    def __init__(self, pass_threshold: float = 0.7,
                 db_path: str = ":memory:"):
        self.pass_threshold = pass_threshold
        self._cases: Dict[str, EvalCase] = {}
        self._suites: Dict[str, List[str]] = {}      # suite → [case_ids]
        self._models: Dict[str, Callable] = {}       # model_id → fn(prompt)→output
        self._scorers: Dict[str, Callable] = {}      # custom scorer name → fn
        self._rubrics: Dict[str, List[RubricCriterion]] = {}
        self._results: List[EvalResult] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS me_results (
                result_id TEXT PRIMARY KEY, case_id TEXT, model_id TEXT,
                score REAL, status TEXT, method TEXT, duration_ms REAL, ts REAL
            );
            CREATE TABLE IF NOT EXISTS me_benchmarks (
                benchmark_id TEXT PRIMARY KEY, model_id TEXT,
                total INTEGER, passed INTEGER, avg_score REAL,
                pass_rate REAL, ts REAL
            );
        """)
        self._db.commit()

    # ── CASE MANAGEMENT ───────────────────────────────────────────────

    def add_case(self, prompt: str, expected: Any,
                 tags: Optional[List[str]] = None,
                 metadata: Optional[Dict] = None,
                 case_id: Optional[str] = None,
                 suite: Optional[str] = None) -> EvalCase:
        cid = case_id or str(uuid.uuid4())[:8]
        case = EvalCase(case_id=cid, prompt=prompt, expected=expected,
                        tags=list(tags or []), metadata=metadata or {})
        self._cases[cid] = case
        if suite:
            self._suites.setdefault(suite, []).append(cid)
        return case

    def add_suite(self, name: str, case_ids: Optional[List[str]] = None):
        self._suites[name] = list(case_ids or [])

    def add_to_suite(self, suite: str, case_id: str):
        self._suites.setdefault(suite, []).append(case_id)

    def load_cases_from_list(self, items: List[Dict],
                              suite: Optional[str] = None) -> List[EvalCase]:
        cases = []
        for item in items:
            c = self.add_case(
                prompt=item["prompt"],
                expected=item["expected"],
                tags=item.get("tags", []),
                metadata=item.get("metadata", {}),
                suite=suite)
            cases.append(c)
        return cases

    # ── MODEL MANAGEMENT ─────────────────────────────────────────────

    def register_model(self, model_id: str,
                        fn: Callable[[str], Any]):
        self._models[model_id] = fn

    def add_rubric(self, rubric_name: str,
                   criteria: List[Dict]):
        parsed = []
        for c in criteria:
            parsed.append(RubricCriterion(
                name=c["name"],
                weight=c.get("weight", 1.0),
                scorer=c.get("scorer"),
                description=c.get("description", "")))
        self._rubrics[rubric_name] = parsed

    def add_custom_scorer(self, name: str,
                           fn: Callable[[Any, Any], float]):
        self._scorers[name] = fn

    # ── EVALUATION ────────────────────────────────────────────────────

    def eval_case(self, case_id: str, model_id: str,
                  method: ScoringMethod = ScoringMethod.EXACT_MATCH,
                  rubric_name: Optional[str] = None,
                  custom_scorer: Optional[str] = None,
                  threshold: Optional[float] = None) -> EvalResult:
        case  = self._cases.get(case_id)
        model = self._models.get(model_id)
        if not case or not model:
            return EvalResult(
                case_id=case_id, model_id=model_id, output=None,
                score=0.0, status=EvalStatus.SKIP, method=method,
                details={"error": "case or model not found"})

        t0 = time.time()
        try:
            output = model(case.prompt)
        except Exception as e:
            er = EvalResult(
                case_id=case_id, model_id=model_id, output=None,
                score=0.0, status=EvalStatus.FAIL, method=method,
                duration_ms=(time.time() - t0) * 1000,
                details={"error": str(e)})
            self._results.append(er)
            self._persist_result(er)
            return er

        score, details = self._score(output, case.expected, method,
                                     rubric_name, custom_scorer)
        thr = threshold if threshold is not None else self.pass_threshold
        status = EvalStatus.PASS if score >= thr else EvalStatus.FAIL
        result = EvalResult(
            case_id=case_id, model_id=model_id, output=output,
            score=score, status=status, method=method,
            duration_ms=(time.time() - t0) * 1000,
            details=details)
        self._results.append(result)
        self._persist_result(result)
        return result

    def _score(self, output: Any, expected: Any,
                method: ScoringMethod,
                rubric_name: Optional[str],
                custom_scorer: Optional[str]) -> Tuple[float, Dict]:
        details: Dict[str, Any] = {}
        if method == ScoringMethod.EXACT_MATCH:
            s = _score_exact(output, expected)
        elif method == ScoringMethod.CONTAINS:
            s = _score_contains(output, expected)
        elif method == ScoringMethod.REGEX:
            import re
            try:
                s = 1.0 if re.search(str(expected), str(output)) else 0.0
            except Exception as e:
                s = 0.0; details["error"] = str(e)
        elif method == ScoringMethod.SEMANTIC:
            s = _score_semantic(str(output), str(expected))
        elif method == ScoringMethod.RUBRIC and rubric_name:
            criteria = self._rubrics.get(rubric_name, [])
            if not criteria:
                s = 0.0
            else:
                total_w = sum(c.weight for c in criteria)
                weighted = 0.0
                for c in criteria:
                    if c.scorer:
                        cs = c.scorer(str(output), str(expected))
                    else:
                        cs = _score_contains(output, expected)
                    details[c.name] = round(cs, 4)
                    weighted += cs * c.weight
                s = weighted / total_w if total_w > 0 else 0.0
        elif method == ScoringMethod.CUSTOM and custom_scorer:
            fn = self._scorers.get(custom_scorer)
            s = fn(output, expected) if fn else 0.0
        else:
            s = _score_exact(output, expected)
        return min(1.0, max(0.0, s)), details

    def _persist_result(self, r: EvalResult):
        self._db.execute(
            "INSERT OR REPLACE INTO me_results VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4())[:8], r.case_id, r.model_id,
             r.score, r.status.value, r.method.value,
             r.duration_ms, r.ts))
        self._db.commit()

    # ── BENCHMARK ─────────────────────────────────────────────────────

    def run_benchmark(self, model_id: str,
                      suite: Optional[str] = None,
                      method: ScoringMethod = ScoringMethod.EXACT_MATCH,
                      **eval_kwargs) -> BenchmarkReport:
        t0 = time.time()
        bid = str(uuid.uuid4())[:8]
        if suite:
            case_ids = self._suites.get(suite, [])
        else:
            case_ids = list(self._cases.keys())

        results = [self.eval_case(cid, model_id, method, **eval_kwargs)
                   for cid in case_ids]

        scores = [r.score for r in results]
        passed  = sum(1 for r in results if r.status == EvalStatus.PASS)
        failed  = sum(1 for r in results if r.status == EvalStatus.FAIL)
        skipped = sum(1 for r in results if r.status == EvalStatus.SKIP)
        avg = sum(scores) / len(scores) if scores else 0.0

        report = BenchmarkReport(
            benchmark_id=bid, model_id=model_id,
            total_cases=len(results), passed=passed,
            failed=failed, skipped=skipped,
            avg_score=avg,
            min_score=min(scores) if scores else 0.0,
            max_score=max(scores) if scores else 0.0,
            results=results,
            duration_ms=(time.time() - t0) * 1000)

        self._db.execute(
            "INSERT INTO me_benchmarks VALUES (?,?,?,?,?,?,?)",
            (bid, model_id, len(results), passed, avg,
             report.pass_rate, report.ts))
        self._db.commit()
        return report

    def compare_models(self, model_ids: List[str],
                       suite: Optional[str] = None,
                       method: ScoringMethod = ScoringMethod.EXACT_MATCH,
                       **kwargs) -> Dict[str, BenchmarkReport]:
        return {mid: self.run_benchmark(mid, suite, method, **kwargs)
                for mid in model_ids}

    # ── QUERY ─────────────────────────────────────────────────────────

    def results_for_model(self, model_id: str) -> List[EvalResult]:
        return [r for r in self._results if r.model_id == model_id]

    def benchmark_history(self, limit: int = 20) -> List[Dict]:
        rows = self._db.execute(
            "SELECT benchmark_id,model_id,total,passed,avg_score,pass_rate,ts "
            "FROM me_benchmarks ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "model": r[1], "total": r[2], "passed": r[3],
                 "avg_score": r[4], "pass_rate": r[5]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        return {
            "cases": len(self._cases),
            "suites": len(self._suites),
            "models": len(self._models),
            "results": len(self._results),
            "pass_threshold": self.pass_threshold,
        }
