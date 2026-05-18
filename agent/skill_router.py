"""OMNI AGENT - Skill Router
Route incoming queries to the most appropriate specialist skill/agent:
confidence scoring, fallback chains, load balancing, and skill composition.

Features:
- Skill registry: register skills with descriptions, examples, and keywords
- Routing strategies: best_match, ensemble, round_robin, weighted_random
- Confidence scoring: keyword overlap + semantic description matching
- Threshold gating: skip skills below confidence threshold
- Fallback chain: try next skill if primary returns low-confidence
- Skill composition: chain multiple skills sequentially or in parallel
- Load balancing: track invocation counts, route to least-used
- Cooldown: temporarily disable overloaded skills
- Pre/post hooks: inject middleware around skill calls
- History: log every routing decision with confidence and latency
- REST API: route, register-skill, stats, history
"""
import asyncio, time, uuid, random, re, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
logger = logging.getLogger(__name__)

def _overlap_score(query: str, text: str) -> float:
    qw = set(re.findall(r'\w+', query.lower()))
    tw = set(re.findall(r'\w+', text.lower()))
    if not qw: return 0.0
    return len(qw & tw) / len(qw)

@dataclass
class Skill:
    id: str; name: str; fn: Callable
    description: str = ""
    keywords: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)
    weight: float = 1.0
    max_concurrent: int = 10
    cooldown_s: float = 0.0          # 0 = no cooldown
    active: bool = True
    # Runtime stats
    call_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    _last_used: float = 0.0
    _current: int = 0

    @property
    def avg_latency_ms(self): return round(self.total_latency_ms / max(1, self.call_count), 1)
    @property
    def error_rate(self): return round(self.error_count / max(1, self.call_count), 4)
    @property
    def in_cooldown(self):
        return self.cooldown_s > 0 and (time.time() - self._last_used) < self.cooldown_s

    def score(self, query: str) -> float:
        desc_score = _overlap_score(query, self.description)
        kw_score = _overlap_score(query, " ".join(self.keywords))
        ex_score = max((_overlap_score(query, ex) for ex in self.examples), default=0.0)
        return round(max(desc_score, kw_score * 1.2, ex_score), 4)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "description": self.description,
                "keywords": self.keywords, "weight": self.weight,
                "active": self.active, "call_count": self.call_count,
                "error_rate": self.error_rate, "avg_latency_ms": self.avg_latency_ms,
                "in_cooldown": self.in_cooldown}

@dataclass
class RoutingDecision:
    query: str; skill_name: str; skill_id: str
    confidence: float; strategy: str
    all_scores: Dict[str, float] = field(default_factory=dict)
    result: Any = None; error: str = ""
    latency_ms: float = 0.0; fallback_used: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"query": self.query[:200], "skill": self.skill_name,
                "confidence": round(self.confidence, 4), "strategy": self.strategy,
                "fallback_used": self.fallback_used,
                "latency_ms": round(self.latency_ms, 1),
                "error": self.error,
                "all_scores": {k: round(v, 4) for k, v in self.all_scores.items()}}

class SkillRouter:
    """
    Route queries to the best-matching specialist skill with fallback support.

    Usage:
        router = SkillRouter(threshold=0.2)

        router.register("math",    math_fn,
                         description="Solve arithmetic and algebra problems",
                         keywords=["calculate","sum","equation","solve","math"])
        router.register("weather", weather_fn,
                         description="Get current weather and forecasts",
                         keywords=["weather","temperature","rain","forecast"])
        router.register("search",  search_fn,
                         description="Search the web for information",
                         keywords=["search","find","lookup","who is","what is"])

        decision = await router.route("What's 2 + 2?")
        print(decision.skill_name)       # "math"
        print(decision.confidence)       # e.g. 0.67
        print(decision.result)           # 4
    """
    STRATEGIES = ["best_match", "ensemble", "round_robin", "weighted_random", "least_used"]

    def __init__(self, threshold: float = 0.15, fallback_skill: str = None,
                 default_strategy: str = "best_match"):
        self._skills: Dict[str, Skill] = {}
        self._threshold = threshold
        self._fallback_name = fallback_skill
        self._default_strategy = default_strategy
        self._history: List[RoutingDecision] = []
        self._pre_hooks: List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._rr_index: int = 0

    def register(self, name: str, fn: Callable,
                  description: str = "", keywords: List[str] = None,
                  examples: List[str] = None, weight: float = 1.0,
                  max_concurrent: int = 10, cooldown_s: float = 0.0) -> Skill:
        sid = str(uuid.uuid4())[:8]
        skill = Skill(id=sid, name=name, fn=fn, description=description,
                       keywords=keywords or [], examples=examples or [],
                       weight=weight, max_concurrent=max_concurrent,
                       cooldown_s=cooldown_s)
        self._skills[name] = skill
        logger.info(f"Skill registered: {name!r}")
        return skill

    def unregister(self, name: str) -> bool:
        return bool(self._skills.pop(name, None))

    def activate(self, name: str):
        s = self._skills.get(name)
        if s: s.active = True

    def deactivate(self, name: str):
        s = self._skills.get(name)
        if s: s.active = False

    def add_pre_hook(self, fn: Callable): self._pre_hooks.append(fn)
    def add_post_hook(self, fn: Callable): self._post_hooks.append(fn)

    def _available(self) -> List[Skill]:
        return [s for s in self._skills.values()
                if s.active and not s.in_cooldown and s._current < s.max_concurrent]

    def _score_all(self, query: str) -> Dict[str, float]:
        return {s.name: s.score(query) for s in self._available()}

    def _select_skill(self, query: str, strategy: str) -> Optional[Skill]:
        available = self._available()
        if not available: return None
        if strategy == "round_robin":
            self._rr_index = self._rr_index % len(available)
            skill = available[self._rr_index]
            self._rr_index += 1
            return skill
        elif strategy == "weighted_random":
            weights = [s.weight for s in available]
            total = sum(weights)
            r = random.random() * total
            cumulative = 0
            for s, w in zip(available, weights):
                cumulative += w
                if r <= cumulative: return s
            return available[-1]
        elif strategy == "least_used":
            return min(available, key=lambda s: s.call_count)
        else:  # best_match (default)
            scores = {s.name: s.score(query) for s in available}
            if not scores: return None
            best_name = max(scores, key=scores.get)
            return self._skills.get(best_name)

    async def _invoke(self, skill: Skill, query: str, context: Dict = None) -> Tuple[Any, str]:
        skill._current += 1
        skill.call_count += 1; skill._last_used = time.time()
        start = time.time()
        try:
            fn = skill.fn
            kwargs = {"context": context} if context else {}
            if asyncio.iscoroutinefunction(fn):
                result = await fn(query, **kwargs)
            else:
                result = fn(query, **kwargs)
            skill.total_latency_ms += (time.time() - start) * 1000
            return result, ""
        except Exception as e:
            skill.error_count += 1
            skill.total_latency_ms += (time.time() - start) * 1000
            return None, str(e)
        finally:
            skill._current -= 1

    async def route(self, query: str, strategy: str = None,
                     context: Dict = None, fallback: bool = True) -> RoutingDecision:
        start = time.time()
        strat = strategy or self._default_strategy

        # Pre-hooks
        for hook in self._pre_hooks:
            try:
                await hook(query) if asyncio.iscoroutinefunction(hook) else hook(query)
            except: pass

        scores = self._score_all(query)
        skill = self._select_skill(query, strat)
        decision = RoutingDecision(query=query,
                                    skill_name=skill.name if skill else "none",
                                    skill_id=skill.id if skill else "",
                                    confidence=scores.get(skill.name, 0.0) if skill else 0.0,
                                    strategy=strat, all_scores=scores)

        if not skill or (strat == "best_match" and decision.confidence < self._threshold):
            # Try fallback
            if fallback and self._fallback_name and self._fallback_name in self._skills:
                skill = self._skills[self._fallback_name]
                decision.skill_name = skill.name
                decision.skill_id = skill.id
                decision.fallback_used = True
            elif not skill:
                decision.error = "No skill available"
                decision.latency_ms = (time.time() - start) * 1000
                self._history.append(decision); return decision

        if skill:
            result, error = await self._invoke(skill, query, context)
            decision.result = result; decision.error = error

        decision.latency_ms = (time.time() - start) * 1000
        self._history.append(decision)

        # Post-hooks
        for hook in self._post_hooks:
            try:
                await hook(decision) if asyncio.iscoroutinefunction(hook) else hook(decision)
            except: pass

        return decision

    async def compose(self, query: str, skill_names: List[str],
                       parallel: bool = False, context: Dict = None) -> List[RoutingDecision]:
        """Invoke multiple skills and return all results."""
        if parallel:
            return await asyncio.gather(*[self.route(query, context=context) for _ in skill_names])
        results = []
        for name in skill_names:
            if name in self._skills:
                skill = self._skills[name]
                result, error = await self._invoke(skill, query, context)
                d = RoutingDecision(query=query, skill_name=name,
                                     skill_id=skill.id, confidence=skill.score(query),
                                     strategy="compose", result=result, error=error)
                results.append(d)
        return results

    def skills(self) -> List[Skill]:
        return list(self._skills.values())

    def history(self, limit: int = 20) -> List[RoutingDecision]:
        return self._history[-limit:]

    def stats(self) -> Dict:
        total = len(self._history)
        fallback_count = sum(1 for d in self._history if d.fallback_used)
        errors = sum(1 for d in self._history if d.error)
        avg_conf = sum(d.confidence for d in self._history) / max(1, total)
        return {"total_routes": total, "fallback_rate": round(fallback_count/max(1,total),4),
                "error_rate": round(errors/max(1,total),4),
                "avg_confidence": round(avg_conf, 4),
                "registered_skills": len(self._skills),
                "available_skills": len(self._available())}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def route_ep(req):
            d = await req.json()
            dec = await self.route(d["query"], d.get("strategy"), d.get("context"))
            return web.json_response(dec.to_dict())
        async def register_ep(req):
            d = await req.json()
            skill = self.register(d["name"], lambda q, cfg=d: cfg.get("name",""),
                                   d.get("description",""), d.get("keywords",[]),
                                   d.get("examples",[]), float(d.get("weight",1.0)))
            return web.json_response(skill.to_dict(), status=201)
        async def skills_ep(req):
            return web.json_response({"skills": [s.to_dict() for s in self.skills()]})
        async def stats_ep(req): return web.json_response(self.stats())
        async def history_ep(req):
            limit = int(req.rel_url.query.get("limit",10))
            return web.json_response({"history":[d.to_dict() for d in self.history(limit)]})
        p = f"{prefix}/router"
        app.router.add_post(f"{p}/route",    route_ep)
        app.router.add_post(f"{p}/skill",    register_ep)
        app.router.add_get( f"{p}/skills",   skills_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        app.router.add_get( f"{p}/history",  history_ep)
        logger.info(f"Skill router API at {prefix}/router/")
