"""OMNI AGENT - Model Ensemble
Combine multiple LLMs: weighted majority voting, confidence-weighted
averaging, disagreement detection, and intelligent fallback routing.

Features:
- Member registry: add LLM callables with weights and metadata
- Voting strategies: majority, weighted, confidence-weighted, unanimous
- Agreement scoring: Jaccard similarity between member outputs
- Disagreement alerts: flag high-variance outputs for human review
- Confidence extraction: parse confidence scores from LLM JSON outputs
- Best-of-N: return highest-confidence member response
- Hedge mode: return multiple outputs when members strongly disagree
- Fallback chain: try members in priority order until one succeeds
- Ensemble history: log all calls with per-member scores
- Latency budgeting: skip slow members after a timeout
- REST API: query, add-member, stats, history
"""
import asyncio, time, uuid, re, json, logging, math
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
logger = logging.getLogger(__name__)

# ── Similarity / agreement ────────────────────────────────────────────────────
def _jaccard_words(a: str, b: str) -> float:
    wa = set(re.findall(r'\w+', a.lower()))
    wb = set(re.findall(r'\w+', b.lower()))
    if not wa and not wb: return 1.0
    return len(wa & wb) / len(wa | wb)

def _agreement_score(texts: List[str]) -> float:
    """Average pairwise Jaccard similarity; 1.0 = identical, 0.0 = completely different."""
    if len(texts) < 2: return 1.0
    pairs = [(i, j) for i in range(len(texts)) for j in range(i+1, len(texts))]
    scores = [_jaccard_words(texts[i], texts[j]) for i, j in pairs]
    return round(sum(scores) / len(scores), 4)

def _extract_confidence(text: str) -> Optional[float]:
    """Try to find a confidence score in LLM JSON output."""
    try:
        m = re.search(r'"confidence"\s*:\s*([\d.]+)', text)
        if m: return min(1.0, max(0.0, float(m.group(1))))
    except: pass
    return None

# ── Models ────────────────────────────────────────────────────────────────────
@dataclass
class EnsembleMember:
    id: str; name: str; fn: Callable
    weight: float = 1.0; priority: int = 0
    model_id: str = ""; active: bool = True
    total_calls: int = 0; total_errors: int = 0
    total_latency_ms: float = 0.0

    @property
    def avg_latency_ms(self):
        return round(self.total_latency_ms / max(1, self.total_calls), 1)

    @property
    def error_rate(self):
        return round(self.total_errors / max(1, self.total_calls), 4)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "weight": self.weight,
                "priority": self.priority, "model_id": self.model_id,
                "active": self.active, "total_calls": self.total_calls,
                "error_rate": self.error_rate, "avg_latency_ms": self.avg_latency_ms}

@dataclass
class MemberOutput:
    member_id: str; member_name: str
    text: str; confidence: float = 0.5
    latency_ms: float = 0.0; error: str = ""
    success: bool = True

    def to_dict(self):
        return {"member_id": self.member_id, "member_name": self.member_name,
                "text": self.text[:300], "confidence": round(self.confidence, 4),
                "latency_ms": round(self.latency_ms, 1),
                "success": self.success, "error": self.error}

@dataclass
class EnsembleResult:
    prompt: str; strategy: str; final_answer: str
    member_outputs: List[MemberOutput]
    agreement_score: float = 1.0
    disagreement_flag: bool = False
    hedge_answers: List[str] = field(default_factory=list)
    total_ms: float = 0.0; created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"prompt": self.prompt[:200], "strategy": self.strategy,
                "final_answer": self.final_answer[:500],
                "agreement_score": round(self.agreement_score, 4),
                "disagreement_flag": self.disagreement_flag,
                "hedge_answers": self.hedge_answers[:3],
                "total_ms": round(self.total_ms, 1),
                "member_count": len(self.member_outputs),
                "success_count": sum(1 for m in self.member_outputs if m.success),
                "members": [m.to_dict() for m in self.member_outputs]}

class ModelEnsemble:
    """
    Combine multiple LLMs with voting, confidence weighting, and disagreement detection.

    Usage:
        ensemble = ModelEnsemble(disagreement_threshold=0.4)
        ensemble.add_member("gpt4",     gpt4_fn,    weight=2.0)
        ensemble.add_member("claude",   claude_fn,  weight=2.0)
        ensemble.add_member("mistral",  mistral_fn, weight=1.0)

        result = await ensemble.query("What is the capital of France?",
                                       strategy="weighted_vote")
        print(result.final_answer)          # "Paris"
        print(result.agreement_score)       # 0.85
        print(result.disagreement_flag)     # False
    """
    STRATEGIES = ["majority_vote", "weighted_vote", "confidence_weighted",
                   "best_of_n", "unanimous", "fallback_chain", "hedge"]

    def __init__(self, disagreement_threshold: float = 0.35,
                  timeout_s: float = 30.0):
        self._members: Dict[str, EnsembleMember] = {}
        self._disagreement_threshold = disagreement_threshold
        self._timeout_s = timeout_s
        self._history: List[EnsembleResult] = []

    def add_member(self, name: str, fn: Callable, weight: float = 1.0,
                    priority: int = 0, model_id: str = "") -> EnsembleMember:
        mid = str(uuid.uuid4())[:8]
        m = EnsembleMember(id=mid, name=name, fn=fn, weight=weight,
                            priority=priority, model_id=model_id)
        self._members[mid] = m
        logger.info(f"Ensemble member added: {name!r} w={weight}")
        return m

    def remove_member(self, name_or_id: str) -> bool:
        if name_or_id in self._members:
            del self._members[name_or_id]; return True
        for mid, m in list(self._members.items()):
            if m.name == name_or_id:
                del self._members[mid]; return True
        return False

    def activate(self, name_or_id: str):
        m = self._find(name_or_id)
        if m: m.active = True

    def deactivate(self, name_or_id: str):
        m = self._find(name_or_id)
        if m: m.active = False

    def _find(self, name_or_id: str) -> Optional[EnsembleMember]:
        return (self._members.get(name_or_id) or
                next((m for m in self._members.values() if m.name == name_or_id), None))

    # ── Calling individual members ────────────────────────────────────────────

    async def _call_member(self, member: EnsembleMember, prompt: str) -> MemberOutput:
        start = time.time()
        member.total_calls += 1
        try:
            async with asyncio.timeout(self._timeout_s):
                fn = member.fn
                raw = await fn(prompt) if asyncio.iscoroutinefunction(fn) else fn(prompt)
                text = str(raw)
                conf = _extract_confidence(text) or 0.7
                lat = (time.time() - start) * 1000
                member.total_latency_ms += lat
                return MemberOutput(member_id=member.id, member_name=member.name,
                                     text=text, confidence=conf, latency_ms=lat)
        except Exception as e:
            member.total_errors += 1
            lat = (time.time() - start) * 1000
            member.total_latency_ms += lat
            return MemberOutput(member_id=member.id, member_name=member.name,
                                 text="", confidence=0.0, latency_ms=lat,
                                 error=str(e)[:200], success=False)

    async def _call_all(self, prompt: str) -> List[MemberOutput]:
        active = [m for m in self._members.values() if m.active]
        if not active: return []
        return await asyncio.gather(*[self._call_member(m, prompt) for m in active],
                                      return_exceptions=False)

    # ── Aggregation strategies ────────────────────────────────────────────────

    def _majority_vote(self, outputs: List[MemberOutput],
                        weighted: bool = False) -> str:
        successful = [o for o in outputs if o.success and o.text]
        if not successful: return ""
        tally: Dict[str, float] = {}
        for o in successful:
            key = o.text.strip()[:200]
            w = (self._members[o.member_id].weight if weighted else 1.0)
            tally[key] = tally.get(key, 0) + w
        return max(tally, key=tally.get)

    def _confidence_weighted(self, outputs: List[MemberOutput]) -> str:
        successful = [o for o in outputs if o.success and o.text]
        if not successful: return ""
        # Weighted average isn't meaningful for text, so return highest-confidence
        return max(successful, key=lambda o: o.confidence * self._members[o.member_id].weight).text

    def _best_of_n(self, outputs: List[MemberOutput]) -> str:
        successful = [o for o in outputs if o.success]
        if not successful: return ""
        return max(successful, key=lambda o: o.confidence).text

    def _unanimous(self, outputs: List[MemberOutput]) -> Tuple[str, bool]:
        successful = [o for o in outputs if o.success and o.text]
        if not successful: return "", False
        texts = [o.text.strip()[:100] for o in successful]
        if len(set(texts)) == 1: return successful[0].text, True
        agreement = _agreement_score([o.text for o in successful])
        if agreement >= 0.8: return successful[0].text, True
        return self._majority_vote(outputs, weighted=True), False

    # ── Main query interface ──────────────────────────────────────────────────

    async def query(self, prompt: str, strategy: str = "weighted_vote") -> EnsembleResult:
        start = time.time()

        if strategy == "fallback_chain":
            return await self._fallback_chain(prompt, start)

        outputs = await self._call_all(prompt)
        successful = [o for o in outputs if o.success and o.text]
        agreement = _agreement_score([o.text for o in successful]) if successful else 1.0
        disagree_flag = agreement < self._disagreement_threshold

        # Choose final answer by strategy
        if strategy == "majority_vote":
            final = self._majority_vote(outputs, weighted=False)
        elif strategy == "weighted_vote":
            final = self._majority_vote(outputs, weighted=True)
        elif strategy == "confidence_weighted":
            final = self._confidence_weighted(outputs)
        elif strategy == "best_of_n":
            final = self._best_of_n(outputs)
        elif strategy == "unanimous":
            final, _ = self._unanimous(outputs)
        elif strategy == "hedge":
            # Return all distinct answers
            seen = set(); hedge = []
            for o in sorted(successful, key=lambda o: -o.confidence):
                key = o.text.strip()[:100]
                if key not in seen: seen.add(key); hedge.append(o.text)
            final = hedge[0] if hedge else ""
            result = EnsembleResult(
                prompt=prompt, strategy=strategy, final_answer=final,
                member_outputs=outputs, agreement_score=agreement,
                disagreement_flag=disagree_flag, hedge_answers=hedge,
                total_ms=(time.time()-start)*1000)
            self._history.append(result); return result
        else:
            final = self._majority_vote(outputs, weighted=True)

        result = EnsembleResult(
            prompt=prompt, strategy=strategy, final_answer=final,
            member_outputs=outputs, agreement_score=agreement,
            disagreement_flag=disagree_flag,
            total_ms=(time.time()-start)*1000)
        self._history.append(result)
        logger.info(f"Ensemble ({strategy}): agreement={agreement:.2f}, "
                     f"disagree={disagree_flag}, members={len(outputs)}")
        return result

    async def _fallback_chain(self, prompt: str, start: float) -> EnsembleResult:
        """Try members in priority order; return first success."""
        ordered = sorted([m for m in self._members.values() if m.active],
                          key=lambda m: -m.priority)
        outputs = []
        for member in ordered:
            out = await self._call_member(member, prompt)
            outputs.append(out)
            if out.success and out.text:
                result = EnsembleResult(
                    prompt=prompt, strategy="fallback_chain",
                    final_answer=out.text, member_outputs=outputs,
                    agreement_score=1.0, total_ms=(time.time()-start)*1000)
                self._history.append(result); return result
        result = EnsembleResult(
            prompt=prompt, strategy="fallback_chain", final_answer="",
            member_outputs=outputs, agreement_score=0.0,
            total_ms=(time.time()-start)*1000)
        self._history.append(result); return result

    def members(self) -> List[EnsembleMember]:
        return list(self._members.values())

    def stats(self) -> Dict:
        return {"member_count": len(self._members),
                "active_members": sum(1 for m in self._members.values() if m.active),
                "total_queries": len(self._history),
                "avg_agreement": round(sum(r.agreement_score for r in self._history) /
                                        max(1, len(self._history)), 4),
                "disagreement_rate": round(sum(1 for r in self._history if r.disagreement_flag) /
                                            max(1, len(self._history)), 4),
                "strategies": self.STRATEGIES,
                "members": [m.to_dict() for m in self._members.values()]}

    def history(self, limit: int = 20) -> List[EnsembleResult]:
        return self._history[-limit:]

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def query_ep(req):
            d = await req.json()
            result = await self.query(d["prompt"], d.get("strategy", "weighted_vote"))
            return web.json_response(result.to_dict())
        async def add_ep(req):
            d = await req.json()
            # For API use, fn is a placeholder; real use registers in code
            m = self.add_member(d["name"], lambda p, cfg=d: cfg.get("name",""),
                                  weight=float(d.get("weight", 1.0)),
                                  priority=int(d.get("priority", 0)),
                                  model_id=d.get("model_id", ""))
            return web.json_response(m.to_dict(), status=201)
        async def stats_ep(req): return web.json_response(self.stats())
        async def history_ep(req):
            limit = int(req.rel_url.query.get("limit", 10))
            return web.json_response({"history": [r.to_dict() for r in self.history(limit)]})
        p = f"{prefix}/ensemble"
        app.router.add_post(f"{p}/query", query_ep)
        app.router.add_post(f"{p}/member", add_ep)
        app.router.add_get(f"{p}/stats", stats_ep)
        app.router.add_get(f"{p}/history", history_ep)
        logger.info(f"Model ensemble API at {prefix}/ensemble/")
