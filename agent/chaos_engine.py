"""OMNI Agent — Chaos Engine: controlled fault injection for resilience testing."""
from __future__ import annotations
import asyncio, functools, random, sqlite3, time, threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type


class FaultType(str, Enum):
    LATENCY     = "latency"      # add artificial delay
    EXCEPTION   = "exception"    # raise an exception
    CORRUPT     = "corrupt"      # mutate/corrupt return value
    NONE        = "none"         # no-op fault (used for testing)
    RATE_LIMIT  = "rate_limit"   # raise RateLimitError
    TIMEOUT     = "timeout"      # simulate timeout via asyncio.TimeoutError
    PARTIAL     = "partial"      # return partial/truncated result


class RateLimitError(Exception):
    pass


@dataclass
class FaultRule:
    rule_id: str
    name: str
    fault_type: FaultType
    probability: float = 1.0       # 0.0–1.0
    target: str = "*"              # function/endpoint name or "*" for all
    enabled: bool = True
    # Fault params
    latency_ms: float = 500.0
    exception_class: Type[Exception] = Exception
    exception_msg: str = "Injected fault"
    corrupt_fn: Optional[Callable] = None
    partial_slice: float = 0.5     # fraction of result to return

    hit_count: int = field(default=0, repr=False)
    miss_count: int = field(default=0, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "fault_type": self.fault_type.value,
            "probability": self.probability,
            "target": self.target,
            "enabled": self.enabled,
            "hit_count": self.hit_count,
            "miss_count": self.miss_count,
        }


class ChaosEngine:
    """
    Injects faults (latency, exceptions, corruption) into sync/async callables.
    Supports per-rule probability, targeting, enable/disable, and audit log.
    """

    def __init__(self, enabled: bool = True, seed: Optional[int] = None,
                 db_path: str = ":memory:"):
        self.enabled = enabled
        self._rules: Dict[str, FaultRule] = {}
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._total_injections = 0
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS chaos_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, rule_id TEXT, target TEXT, fault_type TEXT, outcome TEXT
            )""")
        self._db.commit()

    # ── RULES ─────────────────────────────────────────────────────────

    def add_rule(
        self,
        name: str,
        fault_type: FaultType,
        probability: float = 1.0,
        target: str = "*",
        latency_ms: float = 500.0,
        exception_class: Type[Exception] = Exception,
        exception_msg: str = "Injected fault",
        corrupt_fn: Optional[Callable] = None,
        partial_slice: float = 0.5,
        rule_id: Optional[str] = None,
    ) -> FaultRule:
        import uuid
        rid = rule_id or str(uuid.uuid4())[:8]
        rule = FaultRule(
            rule_id=rid, name=name, fault_type=fault_type,
            probability=probability, target=target,
            latency_ms=latency_ms,
            exception_class=exception_class,
            exception_msg=exception_msg,
            corrupt_fn=corrupt_fn,
            partial_slice=partial_slice,
        )
        with self._lock:
            self._rules[rid] = rule
        return rule

    def remove_rule(self, rule_id: str):
        self._rules.pop(rule_id, None)

    def enable_rule(self, rule_id: str):
        if rule_id in self._rules:
            self._rules[rule_id].enabled = True

    def disable_rule(self, rule_id: str):
        if rule_id in self._rules:
            self._rules[rule_id].enabled = False

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False

    def clear_rules(self):
        self._rules.clear()

    # ── FAULT APPLICATION ─────────────────────────────────────────────

    def _matching_rules(self, target: str) -> List[FaultRule]:
        return [
            r for r in self._rules.values()
            if r.enabled and (r.target == "*" or r.target == target)
        ]

    def _apply_rule_sync(self, rule: FaultRule, target: str):
        """Execute fault synchronously. Raises or sleeps."""
        if not self._rng.random() < rule.probability:
            rule.miss_count += 1
            return False
        rule.hit_count += 1
        self._total_injections += 1
        self._log_event(rule, target, "injected")
        if rule.fault_type == FaultType.LATENCY:
            time.sleep(rule.latency_ms / 1000)
        elif rule.fault_type == FaultType.EXCEPTION:
            raise rule.exception_class(rule.exception_msg)
        elif rule.fault_type == FaultType.RATE_LIMIT:
            raise RateLimitError(rule.exception_msg)
        elif rule.fault_type == FaultType.TIMEOUT:
            time.sleep(60)  # block effectively
        # NONE / handled downstream for CORRUPT & PARTIAL
        return True

    async def _apply_rule_async(self, rule: FaultRule, target: str):
        if not self._rng.random() < rule.probability:
            rule.miss_count += 1
            return False
        rule.hit_count += 1
        self._total_injections += 1
        self._log_event(rule, target, "injected")
        if rule.fault_type == FaultType.LATENCY:
            await asyncio.sleep(rule.latency_ms / 1000)
        elif rule.fault_type == FaultType.EXCEPTION:
            raise rule.exception_class(rule.exception_msg)
        elif rule.fault_type == FaultType.RATE_LIMIT:
            raise RateLimitError(rule.exception_msg)
        elif rule.fault_type == FaultType.TIMEOUT:
            raise asyncio.TimeoutError("Chaos timeout injected")
        return True

    def _corrupt_result(self, rule: FaultRule, result: Any) -> Any:
        if rule.fault_type == FaultType.CORRUPT:
            if rule.corrupt_fn:
                return rule.corrupt_fn(result)
            if isinstance(result, str):
                return result[::-1]  # reverse string as default corruption
            if isinstance(result, (int, float)):
                return result * -1
            if isinstance(result, list):
                return list(reversed(result))
            return None
        if rule.fault_type == FaultType.PARTIAL:
            if isinstance(result, (list, str)):
                cut = max(1, int(len(result) * rule.partial_slice))
                return result[:cut]
        return result

    def _log_event(self, rule: FaultRule, target: str, outcome: str):
        self._db.execute(
            "INSERT INTO chaos_events (ts,rule_id,target,fault_type,outcome) VALUES (?,?,?,?,?)",
            (time.time(), rule.rule_id, target, rule.fault_type.value, outcome))
        self._db.commit()

    # ── DECORATORS ────────────────────────────────────────────────────

    def inject(self, target: Optional[str] = None):
        """Decorator for sync functions."""
        def decorator(fn: Callable):
            fn_target = target or fn.__name__
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                if self.enabled:
                    for rule in self._matching_rules(fn_target):
                        self._apply_rule_sync(rule, fn_target)
                result = fn(*args, **kwargs)
                if self.enabled:
                    for rule in self._matching_rules(fn_target):
                        if rule.fault_type in (FaultType.CORRUPT, FaultType.PARTIAL):
                            if self._rng.random() < rule.probability:
                                result = self._corrupt_result(rule, result)
                return result
            return wrapper
        return decorator

    def inject_async(self, target: Optional[str] = None):
        """Decorator for async functions."""
        def decorator(fn: Callable):
            fn_target = target or fn.__name__
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                if self.enabled:
                    for rule in self._matching_rules(fn_target):
                        await self._apply_rule_async(rule, fn_target)
                result = await fn(*args, **kwargs)
                if self.enabled:
                    for rule in self._matching_rules(fn_target):
                        if rule.fault_type in (FaultType.CORRUPT, FaultType.PARTIAL):
                            if self._rng.random() < rule.probability:
                                result = self._corrupt_result(rule, result)
                return result
            return wrapper
        return decorator

    # ── MANUAL INJECTION ──────────────────────────────────────────────

    def maybe_inject(self, target: str = "*"):
        """Call from within code to maybe trigger a fault (sync)."""
        if not self.enabled:
            return
        for rule in self._matching_rules(target):
            self._apply_rule_sync(rule, target)

    async def maybe_inject_async(self, target: str = "*"):
        if not self.enabled:
            return
        for rule in self._matching_rules(target):
            await self._apply_rule_async(rule, target)

    # ── AUDIT ─────────────────────────────────────────────────────────

    def event_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT ts,rule_id,target,fault_type,outcome FROM chaos_events "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "rule_id": r[1], "target": r[2],
                 "fault_type": r[3], "outcome": r[4]} for r in rows]

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rules": len(self._rules),
            "total_injections": self._total_injections,
            "rules_detail": [r.to_dict() for r in self._rules.values()],
        }
