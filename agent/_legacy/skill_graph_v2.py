"""OMNI Agent — Skill Graph V2: typed skill registry with prerequisites, versioning, and discovery."""
from __future__ import annotations
import asyncio, inspect, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Type


class SkillCategory(str, Enum):
    REASONING  = "reasoning"
    RETRIEVAL  = "retrieval"
    GENERATION = "generation"
    EXECUTION  = "execution"
    PLANNING   = "planning"
    ANALYSIS   = "analysis"
    TOOL       = "tool"
    CUSTOM     = "custom"


class SkillStatus(str, Enum):
    ACTIVE     = "active"
    DEPRECATED = "deprecated"
    BETA       = "beta"
    DISABLED   = "disabled"


@dataclass
class SkillSpec:
    skill_id: str
    name: str
    description: str = ""
    category: SkillCategory = SkillCategory.CUSTOM
    version: str = "1.0.0"
    status: SkillStatus = SkillStatus.ACTIVE
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    prerequisites: Set[str] = field(default_factory=set)  # skill_ids
    tags: List[str] = field(default_factory=list)
    timeout_s: float = 30.0
    cacheable: bool = False
    fn: Optional[Callable] = None
    created_at: float = field(default_factory=time.time)
    call_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.call_count if self.call_count > 0 else 0.0

    @property
    def success_rate(self) -> float:
        if self.call_count == 0:
            return 1.0
        return (self.call_count - self.error_count) / self.call_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "version": self.version,
            "status": self.status.value,
            "prerequisites": list(self.prerequisites),
            "tags": self.tags,
            "call_count": self.call_count,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "success_rate": round(self.success_rate, 4),
        }


@dataclass
class SkillResult:
    skill_id: str
    success: bool
    result: Any = None
    error: Optional[str] = None
    latency_ms: float = 0.0
    cached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "success": self.success,
            "result": str(self.result)[:200] if self.result is not None else None,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 2),
            "cached": self.cached,
        }


class SkillNotFound(Exception):
    pass


class PrerequisiteNotMet(Exception):
    pass


class SkillGraphV2:
    """
    Registry and executor for typed agent skills.
    Supports prerequisites, versioning, auto-discovery from modules,
    skill chaining, result caching, and usage analytics.
    """

    def __init__(self):
        self._skills: Dict[str, SkillSpec] = {}
        self._cache: Dict[str, Any] = {}      # cache_key → result
        self._hooks_pre:  List[Callable] = []
        self._hooks_post: List[Callable] = []
        self._total_calls = 0
        self._total_errors = 0

    # ── REGISTRATION ──────────────────────────────────────────────────

    def register(self, name: str, fn: Callable,
                 description: str = "",
                 category: SkillCategory = SkillCategory.CUSTOM,
                 version: str = "1.0.0",
                 prerequisites: Optional[List[str]] = None,
                 tags: Optional[List[str]] = None,
                 timeout_s: float = 30.0,
                 cacheable: bool = False,
                 skill_id: Optional[str] = None,
                 input_schema: Optional[Dict] = None,
                 output_schema: Optional[Dict] = None) -> SkillSpec:
        sid = skill_id or str(uuid.uuid4())[:8]
        spec = SkillSpec(
            skill_id=sid, name=name, fn=fn,
            description=description or (fn.__doc__ or "").strip(),
            category=category, version=version,
            prerequisites=set(prerequisites or []),
            tags=list(tags or []),
            timeout_s=timeout_s,
            cacheable=cacheable,
            input_schema=input_schema or {},
            output_schema=output_schema or {},
        )
        self._skills[sid] = spec
        return spec

    def register_spec(self, spec: SkillSpec):
        self._skills[spec.skill_id] = spec

    def unregister(self, skill_id: str):
        self._skills.pop(skill_id, None)

    def deprecate(self, skill_id: str):
        if skill_id in self._skills:
            self._skills[skill_id].status = SkillStatus.DEPRECATED

    def disable(self, skill_id: str):
        if skill_id in self._skills:
            self._skills[skill_id].status = SkillStatus.DISABLED

    def discover(self, module: Any) -> List[SkillSpec]:
        """Auto-discover skills from a module: functions decorated with @skill or tagged."""
        discovered = []
        for name in dir(module):
            fn = getattr(module, name, None)
            if callable(fn) and hasattr(fn, "_skill_meta"):
                meta = fn._skill_meta
                spec = self.register(name=meta.get("name", name), fn=fn, **{
                    k: v for k, v in meta.items() if k != "name"})
                discovered.append(spec)
        return discovered

    # ── EXECUTION ─────────────────────────────────────────────────────

    def _check_prerequisites(self, spec: SkillSpec,
                              completed: Optional[Set[str]] = None):
        completed = completed or set()
        missing = spec.prerequisites - completed
        if missing:
            missing_names = [self._skills[s].name if s in self._skills else s
                             for s in missing]
            raise PrerequisiteNotMet(
                f"Skill '{spec.name}' requires: {missing_names}")

    def _cache_key(self, skill_id: str, args, kwargs) -> str:
        import hashlib
        raw = f"{skill_id}:{str(args)}:{str(sorted(kwargs.items()))}"
        return hashlib.md5(raw.encode()).hexdigest()

    def call(self, skill_id: str, *args,
             completed_skills: Optional[Set[str]] = None,
             **kwargs) -> SkillResult:
        spec = self._skills.get(skill_id)
        if not spec:
            raise SkillNotFound(f"Skill '{skill_id}' not found")
        if spec.status == SkillStatus.DISABLED:
            return SkillResult(skill_id=skill_id, success=False,
                               error="Skill is disabled")
        self._check_prerequisites(spec, completed_skills)
        # Cache check
        if spec.cacheable:
            ck = self._cache_key(skill_id, args, kwargs)
            if ck in self._cache:
                return SkillResult(skill_id=skill_id, success=True,
                                   result=self._cache[ck], cached=True)
        for hook in self._hooks_pre:
            try: hook(spec)
            except Exception: pass
        t0 = time.time()
        self._total_calls += 1
        spec.call_count += 1
        try:
            result = spec.fn(*args, **kwargs)
            latency = (time.time() - t0) * 1000
            spec.total_latency_ms += latency
            if spec.cacheable:
                ck = self._cache_key(skill_id, args, kwargs)
                self._cache[ck] = result
            sr = SkillResult(skill_id=skill_id, success=True,
                             result=result, latency_ms=latency)
        except Exception as exc:
            latency = (time.time() - t0) * 1000
            spec.total_latency_ms += latency
            spec.error_count += 1
            self._total_errors += 1
            sr = SkillResult(skill_id=skill_id, success=False,
                             error=str(exc), latency_ms=latency)
        for hook in self._hooks_post:
            try: hook(sr)
            except Exception: pass
        return sr

    async def call_async(self, skill_id: str, *args, **kwargs) -> SkillResult:
        spec = self._skills.get(skill_id)
        if not spec or not spec.fn:
            raise SkillNotFound(skill_id)
        t0 = time.time()
        spec.call_count += 1
        self._total_calls += 1
        try:
            if inspect.iscoroutinefunction(spec.fn):
                coro = spec.fn(*args, **kwargs)
                result = await asyncio.wait_for(coro, timeout=spec.timeout_s)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, lambda: spec.fn(*args, **kwargs))
            latency = (time.time() - t0) * 1000
            spec.total_latency_ms += latency
            return SkillResult(skill_id=skill_id, success=True,
                               result=result, latency_ms=latency)
        except Exception as exc:
            latency = (time.time() - t0) * 1000
            spec.total_latency_ms += latency
            spec.error_count += 1
            self._total_errors += 1
            return SkillResult(skill_id=skill_id, success=False,
                               error=str(exc), latency_ms=latency)

    def chain(self, skill_ids: List[str], initial_input: Any = None) -> List[SkillResult]:
        """Execute skills in sequence, piping output to next skill's input."""
        results = []
        current = initial_input
        completed: Set[str] = set()
        for sid in skill_ids:
            if current is None:
                sr = self.call(sid, completed_skills=completed)
            elif isinstance(current, dict):
                sr = self.call(sid, **current, completed_skills=completed)
            else:
                sr = self.call(sid, current, completed_skills=completed)
            results.append(sr)
            if sr.success:
                completed.add(sid)
                current = sr.result
            else:
                break
        return results

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_before_call(self, fn: Callable): self._hooks_pre.append(fn)
    def on_after_call(self, fn: Callable):  self._hooks_post.append(fn)

    # ── QUERY ─────────────────────────────────────────────────────────

    def get(self, skill_id: str) -> Optional[SkillSpec]:
        return self._skills.get(skill_id)

    def find_by_name(self, name: str) -> Optional[SkillSpec]:
        for s in self._skills.values():
            if s.name == name:
                return s
        return None

    def search(self, query: str = "",
               category: Optional[SkillCategory] = None,
               tag: Optional[str] = None,
               status: Optional[SkillStatus] = None) -> List[SkillSpec]:
        results = list(self._skills.values())
        if query:
            q = query.lower()
            results = [s for s in results
                       if q in s.name.lower() or q in s.description.lower()]
        if category:
            results = [s for s in results if s.category == category]
        if tag:
            results = [s for s in results if tag in s.tags]
        if status:
            results = [s for s in results if s.status == status]
        return results

    def top_skills(self, n: int = 5) -> List[SkillSpec]:
        return sorted(self._skills.values(),
                      key=lambda s: s.call_count, reverse=True)[:n]

    def clear_cache(self, skill_id: Optional[str] = None):
        if skill_id is None:
            self._cache.clear()
        else:
            to_del = [k for k in self._cache if k.startswith(skill_id)]
            for k in to_del:
                del self._cache[k]

    def list_skills(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self._skills.values()]

    def dependency_order(self) -> List[str]:
        """Topological sort of skills by prerequisites."""
        visited: Set[str] = set()
        order: List[str] = []
        def dfs(sid: str):
            if sid in visited:
                return
            visited.add(sid)
            spec = self._skills.get(sid)
            if spec:
                for prereq in spec.prerequisites:
                    dfs(prereq)
            order.append(sid)
        for sid in self._skills:
            dfs(sid)
        return order

    def stats(self) -> Dict[str, Any]:
        by_cat: Dict[str, int] = {}
        for s in self._skills.values():
            by_cat[s.category.value] = by_cat.get(s.category.value, 0) + 1
        return {
            "skills": len(self._skills),
            "total_calls": self._total_calls,
            "total_errors": self._total_errors,
            "cache_entries": len(self._cache),
            "by_category": by_cat,
        }


# ── DECORATOR ─────────────────────────────────────────────────────────────────

def skill(name: Optional[str] = None, category: SkillCategory = SkillCategory.CUSTOM,
          tags: Optional[List[str]] = None, cacheable: bool = False):
    """Decorator to mark a function as a discoverable skill."""
    def decorator(fn: Callable) -> Callable:
        fn._skill_meta = {
            "name": name or fn.__name__,
            "category": category,
            "tags": tags or [],
            "cacheable": cacheable,
        }
        return fn
    return decorator
