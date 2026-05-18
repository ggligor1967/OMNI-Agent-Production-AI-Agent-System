"""OMNI AGENT - Evaluation Suite
Automated LLM output evaluation: BLEU/ROUGE-style n-gram metrics,
factuality checking, coherence scoring, rubric-based grading, and batch eval.

Features:
- BLEU-1/2/3/4: precision-recall n-gram overlap with brevity penalty
- ROUGE-1/L: recall-oriented unigram and longest-common-subsequence
- Exact match: character-normalised equality
- F1 token overlap: token-level micro-averaged F1
- Factuality check: keyword-presence proxy for fact coverage
- Coherence score: sentence-to-sentence cosine similarity (word-vector proxy)
- Fluency heuristic: average sentence length + vocabulary richness
- Rubric scoring: criterion-based weighted scoring via LLM or heuristic
- Batch evaluation: evaluate many (prediction, reference) pairs concurrently
- Leaderboard: rank multiple model outputs on composite score
- EvalResult serialisation for logging and comparison
- REST API: evaluate, batch, rubric, leaderboard
"""
import re, math, time, uuid, asyncio, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import Counter
logger = logging.getLogger(__name__)

# ── N-gram helpers ────────────────────────────────────────────────────────────

def _tokenise(text: str) -> List[str]:
    return re.findall(r'\b\w+\b', text.lower())

def _ngrams(tokens: List[str], n: int) -> Counter:
    return Counter(tuple(tokens[i:i+n]) for i in range(len(tokens)-n+1))

def _bleu_n(pred: List[str], ref: List[str], n: int) -> float:
    pred_ng = _ngrams(pred, n); ref_ng = _ngrams(ref, n)
    if not pred_ng: return 0.0
    clipped = sum(min(c, ref_ng[ng]) for ng, c in pred_ng.items())
    return clipped / sum(pred_ng.values())

def bleu(prediction: str, reference: str, max_n: int = 4) -> Dict[str, float]:
    pred_t = _tokenise(prediction); ref_t = _tokenise(reference)
    scores = {}
    for n in range(1, max_n+1):
        scores[f"bleu_{n}"] = _bleu_n(pred_t, ref_t, n) if len(pred_t) >= n else 0.0
    # Brevity penalty
    bp = math.exp(1 - len(ref_t)/max(1, len(pred_t))) if len(pred_t) < len(ref_t) else 1.0
    # Geometric mean of n-gram scores
    valid = [scores[f"bleu_{n}"] for n in range(1, max_n+1) if scores[f"bleu_{n}"] > 0]
    geo_mean = math.exp(sum(math.log(s) for s in valid)/len(valid)) if valid else 0.0
    scores["bleu"] = round(bp * geo_mean, 4)
    return {k: round(v, 4) for k, v in scores.items()}

def rouge_1(prediction: str, reference: str) -> Dict[str, float]:
    pred_t = Counter(_tokenise(prediction)); ref_t = Counter(_tokenise(reference))
    overlap = sum(min(pred_t[w], ref_t[w]) for w in pred_t)
    precision = overlap / max(1, sum(pred_t.values()))
    recall    = overlap / max(1, sum(ref_t.values()))
    f1 = 2*precision*recall / max(1e-9, precision+recall)
    return {"rouge1_p": round(precision,4), "rouge1_r": round(recall,4),
            "rouge1_f1": round(f1,4)}

def _lcs_length(a: List[str], b: List[str]) -> int:
    m, n = len(a), len(b)
    if m == 0 or n == 0: return 0
    # Space-optimised DP
    prev = [0] * (n+1)
    for i in range(1, m+1):
        curr = [0] * (n+1)
        for j in range(1, n+1):
            curr[j] = prev[j-1]+1 if a[i-1]==b[j-1] else max(prev[j], curr[j-1])
        prev = curr
    return prev[n]

def rouge_l(prediction: str, reference: str) -> Dict[str, float]:
    pred_t = _tokenise(prediction); ref_t = _tokenise(reference)
    lcs = _lcs_length(pred_t, ref_t)
    precision = lcs / max(1, len(pred_t))
    recall    = lcs / max(1, len(ref_t))
    f1 = 2*precision*recall / max(1e-9, precision+recall)
    return {"rougeL_p": round(precision,4), "rougeL_r": round(recall,4),
            "rougeL_f1": round(f1,4)}

def f1_token(prediction: str, reference: str) -> float:
    pred_t = Counter(_tokenise(prediction)); ref_t = Counter(_tokenise(reference))
    tp = sum(min(pred_t[w], ref_t[w]) for w in pred_t)
    precision = tp / max(1, sum(pred_t.values()))
    recall    = tp / max(1, sum(ref_t.values()))
    return round(2*precision*recall / max(1e-9, precision+recall), 4)

def exact_match(prediction: str, reference: str) -> bool:
    norm = lambda t: re.sub(r'\s+', ' ', t.lower().strip())
    return norm(prediction) == norm(reference)

def factuality_score(prediction: str, facts: List[str]) -> float:
    """Fraction of fact keywords present in prediction."""
    if not facts: return 1.0
    pred_lower = prediction.lower()
    covered = sum(1 for f in facts
                   if all(w in pred_lower for w in re.findall(r'\w+', f.lower())))
    return round(covered / len(facts), 4)

def coherence_score(text: str) -> float:
    """Avg cosine similarity of consecutive sentence word-vectors (proxy)."""
    sents = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(sents) < 2: return 1.0
    def vec(s): return Counter(re.findall(r'\w+', s.lower()))
    def cos(a, b):
        dot = sum(a[k]*b[k] for k in a if k in b)
        na = math.sqrt(sum(v*v for v in a.values()))
        nb = math.sqrt(sum(v*v for v in b.values()))
        return dot / max(1e-9, na*nb)
    scores = [cos(vec(sents[i]), vec(sents[i+1])) for i in range(len(sents)-1)]
    return round(sum(scores)/len(scores), 4)

def fluency_score(text: str) -> float:
    """Heuristic: avg sentence length (penalise extremes) + vocab richness."""
    sents = re.split(r'(?<=[.!?])\s+', text.strip())
    if not sents: return 0.0
    avg_len = sum(len(s.split()) for s in sents) / len(sents)
    # Penalise very short (<5) or very long (>50) sentences
    length_score = 1.0 - abs(avg_len - 20) / 40
    length_score = max(0.0, min(1.0, length_score))
    words = _tokenise(text)
    richness = len(set(words)) / max(1, len(words))
    return round(0.6 * length_score + 0.4 * richness, 4)

# ── Rubric scoring ────────────────────────────────────────────────────────────

@dataclass
class RubricCriterion:
    name: str; description: str; weight: float = 1.0
    score: Optional[float] = None; reason: str = ""

@dataclass
class EvalResult:
    id: str; prediction: str; reference: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    composite_score: float = 0.0
    rubric_scores: List[RubricCriterion] = field(default_factory=list)
    model_id: str = ""; latency_ms: float = 0.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id,
                "prediction": self.prediction[:200],
                "reference": self.reference[:200] if self.reference else "",
                "metrics": self.metrics,
                "composite_score": round(self.composite_score, 4),
                "rubric": [{"name":r.name,"score":r.score,"reason":r.reason[:100]}
                             for r in self.rubric_scores],
                "model_id": self.model_id,
                "latency_ms": round(self.latency_ms, 1)}

class EvaluationSuite:
    """
    Comprehensive LLM output evaluation with n-gram, semantic, and rubric metrics.

    Usage:
        suite = EvaluationSuite()
        result = suite.evaluate(
            prediction="The capital of France is Paris.",
            reference="Paris is the capital city of France.",
            facts=["Paris", "France", "capital"])
        print(result.composite_score)
        print(result.metrics)
    """
    def __init__(self, llm_fn: Optional[Callable] = None):
        self._llm_fn = llm_fn
        self._history: List[EvalResult] = []

    def evaluate(self, prediction: str, reference: str = "",
                  facts: List[str] = None, model_id: str = "") -> EvalResult:
        start = time.time()
        metrics: Dict[str, Any] = {}

        # N-gram metrics
        if reference:
            metrics.update(bleu(prediction, reference))
            metrics.update(rouge_1(prediction, reference))
            metrics.update(rouge_l(prediction, reference))
            metrics["f1_token"] = f1_token(prediction, reference)
            metrics["exact_match"] = exact_match(prediction, reference)

        # Factuality
        if facts:
            metrics["factuality"] = factuality_score(prediction, facts)

        # Quality metrics
        metrics["coherence"]  = coherence_score(prediction)
        metrics["fluency"]    = fluency_score(prediction)

        # Composite score
        comp_parts = [metrics.get("bleu", 0.5) if reference else 0.5,
                       metrics.get("rouge1_f1", 0.5) if reference else 0.5,
                       metrics.get("factuality", 1.0),
                       metrics["coherence"], metrics["fluency"]]
        composite = sum(comp_parts) / len(comp_parts)

        result = EvalResult(id=str(uuid.uuid4())[:10],
                             prediction=prediction, reference=reference,
                             metrics=metrics, composite_score=composite,
                             model_id=model_id,
                             latency_ms=(time.time()-start)*1000)
        self._history.append(result)
        return result

    async def rubric_evaluate(self, prediction: str,
                               criteria: List[Dict],
                               context: str = "") -> EvalResult:
        """Score prediction against named criteria (LLM or heuristic)."""
        start = time.time()
        rubric_items = []
        for c in criteria:
            crit = RubricCriterion(name=c["name"], description=c.get("description",""),
                                    weight=float(c.get("weight",1.0)))
            if self._llm_fn:
                prompt = (f"Context: {context}\n\nText to evaluate:\n{prediction}\n\n"
                           f"Criterion: {c['name']} — {c.get('description','')}\n\n"
                           "Score 0.0-1.0 and give a brief reason. JSON only:\n"
                           '{"score": 0.85, "reason": "..."}')
                fn = self._llm_fn
                raw = str(await fn(prompt) if asyncio.iscoroutinefunction(fn) else fn(prompt))
                try:
                    import json
                    m = re.search(r'\{[^}]+\}', raw)
                    if m:
                        d = json.loads(m.group(0))
                        crit.score = float(d.get("score", 0.5))
                        crit.reason = d.get("reason","")
                except:
                    crit.score = 0.5
            else:
                # Heuristic: keyword coverage of criterion description
                kw = re.findall(r'\b\w{4,}\b', c.get("description","").lower())
                pred_lower = prediction.lower()
                crit.score = round(sum(1 for k in kw if k in pred_lower) / max(1,len(kw)), 4)
            rubric_items.append(crit)

        total_weight = sum(r.weight for r in rubric_items)
        composite = sum(r.score * r.weight for r in rubric_items if r.score is not None) / max(1, total_weight)
        result = EvalResult(id=str(uuid.uuid4())[:10],
                             prediction=prediction, reference="",
                             rubric_scores=rubric_items,
                             composite_score=round(composite, 4),
                             latency_ms=(time.time()-start)*1000)
        self._history.append(result)
        return result

    async def batch_evaluate(self, pairs: List[Dict],
                               concurrency: int = 4) -> List[EvalResult]:
        sem = asyncio.Semaphore(concurrency)
        async def bounded(p):
            async with sem:
                return self.evaluate(p["prediction"], p.get("reference",""),
                                      p.get("facts"), p.get("model_id",""))
        return await asyncio.gather(*[bounded(p) for p in pairs])

    def leaderboard(self, results: List[EvalResult] = None) -> List[Dict]:
        """Rank results by composite score descending."""
        items = results or self._history
        grouped: Dict[str, List[float]] = {}
        for r in items:
            key = r.model_id or r.id
            grouped.setdefault(key, []).append(r.composite_score)
        ranked = sorted(grouped.items(),
                         key=lambda x: sum(x[1])/len(x[1]), reverse=True)
        return [{"rank": i+1, "model_id": k,
                  "avg_score": round(sum(v)/len(v), 4), "evals": len(v)}
                 for i, (k, v) in enumerate(ranked)]

    def stats(self) -> Dict:
        if not self._history: return {"total_evals": 0}
        avg_comp = sum(r.composite_score for r in self._history) / len(self._history)
        return {"total_evals": len(self._history),
                "avg_composite_score": round(avg_comp, 4),
                "avg_latency_ms": round(sum(r.latency_ms for r in self._history)/len(self._history), 1)}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def eval_ep(req):
            d = await req.json()
            r = self.evaluate(d["prediction"], d.get("reference",""),
                               d.get("facts"), d.get("model_id",""))
            return web.json_response(r.to_dict())
        async def batch_ep(req):
            d = await req.json()
            results = await self.batch_evaluate(d.get("pairs",[]))
            return web.json_response({"results":[r.to_dict() for r in results]})
        async def rubric_ep(req):
            d = await req.json()
            r = await self.rubric_evaluate(d["prediction"], d.get("criteria",[]),
                                            d.get("context",""))
            return web.json_response(r.to_dict())
        async def lb_ep(req): return web.json_response({"leaderboard": self.leaderboard()})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/eval"
        app.router.add_post(f"{p}/evaluate", eval_ep)
        app.router.add_post(f"{p}/batch",    batch_ep)
        app.router.add_post(f"{p}/rubric",   rubric_ep)
        app.router.add_get( f"{p}/leaderboard", lb_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        logger.info(f"Evaluation suite API at {prefix}/eval/")
