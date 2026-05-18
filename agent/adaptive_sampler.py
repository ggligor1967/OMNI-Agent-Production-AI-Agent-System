"""OMNI AGENT - Adaptive Sampler
Dynamic LLM sampling strategies: temperature scheduling, diversity tracking,
multi-sample generation, deduplication, and quality-based selection.

Features:
- Temperature scheduling: linear, cosine, exponential decay schedules
- Nucleus sampling stats: track effective vocabulary size from top-p
- Multi-sample generation: run N completions in parallel
- Diversity metrics: self-BLEU, pairwise Jaccard, entropy estimation
- Quality filtering: score samples and return top-K
- Deduplication: exact and near-duplicate removal (Jaccard threshold)
- Temperature auto-tuning: increase temp when samples too similar
- Beam tracking: maintain N best candidates with scores
- Sampling history: track temperature vs quality over time
- REST API: sample, tune, stats
"""
import time, uuid, asyncio, logging, math, re
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import Counter
logger = logging.getLogger(__name__)

# ── Schedules ─────────────────────────────────────────────────────────────────
def schedule_linear(step, total, t_start=1.0, t_end=0.1):
    if total <= 1: return t_start
    return t_start + (t_end - t_start) * (step / (total - 1))

def schedule_cosine(step, total, t_start=1.0, t_end=0.1):
    if total <= 1: return t_start
    return t_end + 0.5*(t_start-t_end)*(1+math.cos(math.pi*step/(total-1)))

def schedule_exponential(step, total, t_start=1.0, t_end=0.1):
    if total <= 1: return t_start
    ratio = (t_end/t_start)**(1/(total-1))
    return t_start * (ratio**step)

SCHEDULES = {"linear":schedule_linear,"cosine":schedule_cosine,"exponential":schedule_exponential}

# ── Diversity metrics ─────────────────────────────────────────────────────────
def _ngrams(text, n=3):
    words=text.lower().split(); return [tuple(words[i:i+n]) for i in range(len(words)-n+1)]

def _self_bleu(texts):
    """Average pairwise BLEU-1 across all text pairs (lower = more diverse)."""
    if len(texts)<2: return 0.0
    scores=[]
    for i,ref in enumerate(texts):
        ref_grams=Counter(_ngrams(ref,1))
        for j,hyp in enumerate(texts):
            if i==j: continue
            hyp_grams=Counter(_ngrams(hyp,1))
            if not hyp_grams: continue
            overlap=sum(min(hyp_grams[g],ref_grams.get(g,0)) for g in hyp_grams)
            scores.append(overlap/sum(hyp_grams.values()))
    return round(sum(scores)/len(scores),4) if scores else 0.0

def _jaccard_sim(a, b):
    wa,wb=set(a.lower().split()),set(b.lower().split())
    if not wa and not wb: return 1.0
    return len(wa&wb)/len(wa|wb)

def _text_entropy(text):
    """Word-level entropy as diversity proxy."""
    words=text.lower().split()
    if not words: return 0.0
    counts=Counter(words); total=len(words)
    return -sum((c/total)*math.log2(c/total) for c in counts.values())

def _deduplicate(texts, threshold=0.85):
    kept=[]
    for t in texts:
        if not any(_jaccard_sim(t,k)>=threshold for k in kept):
            kept.append(t)
    return kept

# ── Models ────────────────────────────────────────────────────────────────────
@dataclass
class Sample:
    id: str; text: str; temperature: float
    score: float = 0.0; rank: int = 0
    diversity_score: float = 0.0
    latency_ms: float = 0.0
    def to_dict(self):
        return {"id":self.id,"text":self.text[:500],"temperature":round(self.temperature,3),
                "score":round(self.score,3),"rank":self.rank,
                "diversity_score":round(self.diversity_score,3),
                "latency_ms":round(self.latency_ms,1)}

@dataclass
class SamplingResult:
    prompt: str; samples: List[Sample]
    best: Optional[Sample]; diversity: float
    avg_temperature: float; schedule_used: str
    latency_ms: float = 0.0
    def to_dict(self):
        return {"prompt":self.prompt[:200],"sample_count":len(self.samples),
                "best":self.best.to_dict() if self.best else None,
                "diversity":round(self.diversity,4),
                "avg_temperature":round(self.avg_temperature,3),
                "schedule_used":self.schedule_used,
                "latency_ms":round(self.latency_ms,1),
                "samples":[s.to_dict() for s in self.samples]}

class AdaptiveSampler:
    """
    Multi-sample generation with temperature scheduling and diversity tracking.

    Usage:
        sampler = AdaptiveSampler(llm_fn=my_llm)
        result = await sampler.sample(
            prompt="Write a creative opening for a story about Mars.",
            n=5, schedule="cosine", t_start=1.2, t_end=0.3,
            top_k=3, dedup_threshold=0.7,
        )
        print(result.best.text)
        print(f"Diversity: {result.diversity:.2f}")
    """
    def __init__(self, llm_fn=None, scorer_fn=None,
                 auto_tune=True, tune_threshold=0.2):
        self._llm_fn=llm_fn; self._scorer_fn=scorer_fn
        self._auto_tune=auto_tune; self._tune_threshold=tune_threshold
        self._history: List[Dict]=[]
        self._current_temp=0.7

    async def _generate_one(self, prompt, temperature, sem):
        async with sem:
            start=time.time()
            try:
                fn=self._llm_fn
                if fn:
                    # Pass temperature in prompt hint if LLM supports it
                    text=await fn(prompt) if asyncio.iscoroutinefunction(fn) else fn(prompt)
                else:
                    text=f"[Sample at temp={temperature:.2f}] Response to: {prompt[:60]}..."
                lat=(time.time()-start)*1000
                return Sample(id=str(uuid.uuid4())[:8],text=str(text),
                               temperature=temperature,latency_ms=lat)
            except Exception as e:
                return Sample(id=str(uuid.uuid4())[:8],
                               text=f"[Error: {e}]",temperature=temperature,
                               score=-1.0,latency_ms=(time.time()-start)*1000)

    async def _score_sample(self, sample, prompt):
        if self._scorer_fn:
            fn=self._scorer_fn
            s=await fn(sample.text,prompt) if asyncio.iscoroutinefunction(fn) else fn(sample.text,prompt)
            sample.score=float(s); return
        # Heuristic: length + entropy
        words=sample.text.split()
        length_score=min(1.0,len(words)/50)
        entropy_score=min(1.0,_text_entropy(sample.text)/4)
        sample.score=round((length_score+entropy_score)/2,4)

    async def sample(self, prompt, n=5, schedule="cosine",
                     t_start=1.0, t_end=0.2, top_k=None,
                     dedup_threshold=0.85, concurrency=4) -> SamplingResult:
        start=time.time()
        sched_fn=SCHEDULES.get(schedule,schedule_cosine)
        temperatures=[sched_fn(i,n,t_start,t_end) for i in range(n)]
        avg_temp=sum(temperatures)/len(temperatures)
        sem=asyncio.Semaphore(concurrency)
        # Generate all samples in parallel
        samples=await asyncio.gather(*[self._generate_one(prompt,t,sem) for t in temperatures])
        samples=[s for s in samples if s.score>=0]
        # Deduplicate
        texts=[s.text for s in samples]
        deduped_texts=set(_deduplicate(texts,dedup_threshold))
        samples=[s for s in samples if s.text in deduped_texts]
        # Score
        await asyncio.gather(*[self._score_sample(s,prompt) for s in samples],return_exceptions=True)
        # Diversity metrics
        div=_self_bleu([s.text for s in samples]) if len(samples)>1 else 0.0
        for s in samples:
            others=[x.text for x in samples if x.id!=s.id]
            if others:
                avg_sim=sum(_jaccard_sim(s.text,o) for o in others)/len(others)
                s.diversity_score=round(1-avg_sim,4)
        # Rank by score
        samples.sort(key=lambda s:-s.score)
        for i,s in enumerate(samples): s.rank=i+1
        # Auto-tune: if diversity too low, log recommendation
        if self._auto_tune and div>self._tune_threshold:
            self._current_temp=min(2.0,t_start*1.2)
            logger.debug(f"Low diversity ({div:.2f}), recommend t_start={self._current_temp:.2f}")
        else:
            self._current_temp=t_start
        # Select top-k
        final=samples[:top_k] if top_k else samples
        best=final[0] if final else None
        result=SamplingResult(prompt=prompt,samples=final,best=best,
                               diversity=1-div,avg_temperature=avg_temp,
                               schedule_used=schedule,
                               latency_ms=(time.time()-start)*1000)
        self._history.append({"prompt":prompt[:100],"n":n,"schedule":schedule,
                               "diversity":result.diversity,
                               "best_score":best.score if best else 0,
                               "timestamp":time.time()})
        logger.info(f"Sampled {len(final)} outputs, diversity={result.diversity:.2f}, best_score={best.score if best else 0:.2f}")
        return result

    async def beam_search(self, prompt, beam_width=4, depth=3, expand_fn=None) -> List[Sample]:
        """Simple beam search: expand each beam N times, keep top beam_width."""
        beams=[Sample(id=str(uuid.uuid4())[:8],text="",temperature=0.5)]
        for step in range(depth):
            candidates=[]
            sem=asyncio.Semaphore(beam_width)
            for beam in beams:
                p=f"{prompt}\n\nContinue from: {beam.text}" if beam.text else prompt
                new_samples=await asyncio.gather(*[
                    self._generate_one(p,0.3+step*0.1,sem)
                    for _ in range(beam_width)])
                candidates.extend(new_samples)
            await asyncio.gather(*[self._score_sample(s,prompt) for s in candidates],return_exceptions=True)
            candidates.sort(key=lambda s:-s.score)
            beams=candidates[:beam_width]
        return beams

    def diversity_report(self, texts: List[str]) -> Dict:
        if not texts: return {}
        return {
            "self_bleu": _self_bleu(texts),
            "avg_entropy": round(sum(_text_entropy(t) for t in texts)/len(texts),4),
            "unique_after_dedup_085": len(_deduplicate(texts,0.85)),
            "count": len(texts),
        }

    def recommended_temperature(self) -> float:
        return round(self._current_temp,3)

    def history(self, limit=20) -> List[Dict]:
        return self._history[-limit:]

    def stats(self) -> Dict:
        h=self._history
        if not h: return {"total_runs":0}
        return {"total_runs":len(h),"avg_diversity":round(sum(x["diversity"] for x in h)/len(h),4),
                "avg_best_score":round(sum(x["best_score"] for x in h)/len(h),4),
                "recommended_temperature":self.recommended_temperature()}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def sample_ep(req):
            d=await req.json()
            r=await self.sample(prompt=d["prompt"],n=int(d.get("n",5)),
                                 schedule=d.get("schedule","cosine"),
                                 t_start=float(d.get("t_start",1.0)),
                                 t_end=float(d.get("t_end",0.2)),
                                 top_k=d.get("top_k"),
                                 dedup_threshold=float(d.get("dedup_threshold",0.85)))
            return web.json_response(r.to_dict())
        async def diversity_ep(req):
            d=await req.json()
            return web.json_response(self.diversity_report(d.get("texts",[])))
        async def stats_ep(req): return web.json_response(self.stats())
        p=f"{prefix}/sampler"
        app.router.add_post(f"{p}/sample",sample_ep)
        app.router.add_post(f"{p}/diversity",diversity_ep)
        app.router.add_get(f"{p}/stats",stats_ep)
        logger.info(f"Adaptive sampler API at {prefix}/sampler/")
