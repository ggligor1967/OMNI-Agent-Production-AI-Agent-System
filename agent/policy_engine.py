"""OMNI Agent — Policy Engine: rule-based policy evaluation with conditions and audit trail."""
from __future__ import annotations
import json, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class Effect(str, Enum):
    ALLOW = "allow"
    DENY  = "deny"
    AUDIT = "audit"      # allow but log
    WARN  = "warn"       # allow but warn
    REQUIRE_MFA = "require_mfa"


class PolicyStatus(str, Enum):
    ACTIVE   = "active"
    INACTIVE = "inactive"
    TESTING  = "testing"   # evaluate but don't enforce


class ConditionOp(str, Enum):
    EQ        = "eq"
    NEQ       = "neq"
    GT        = "gt"
    LT        = "lt"
    GTE       = "gte"
    LTE       = "lte"
    IN        = "in"
    NOT_IN    = "not_in"
    CONTAINS  = "contains"
    REGEX     = "regex"
    EXISTS    = "exists"
    NOT_EXISTS = "not_exists"


@dataclass
class Condition:
    field: str             # dot-path like "user.role" or "request.ip"
    op: ConditionOp
    value: Any = None

    def evaluate(self, context: Dict[str, Any]) -> bool:
        val = _get_field(context, self.field)
        try:
            if self.op == ConditionOp.EQ:        return val == self.value
            if self.op == ConditionOp.NEQ:       return val != self.value
            if self.op == ConditionOp.GT:        return val > self.value
            if self.op == ConditionOp.LT:        return val < self.value
            if self.op == ConditionOp.GTE:       return val >= self.value
            if self.op == ConditionOp.LTE:       return val <= self.value
            if self.op == ConditionOp.IN:        return val in self.value
            if self.op == ConditionOp.NOT_IN:    return val not in self.value
            if self.op == ConditionOp.CONTAINS:  return str(self.value) in str(val)
            if self.op == ConditionOp.REGEX:
                return bool(re.search(str(self.value), str(val)))
            if self.op == ConditionOp.EXISTS:    return val is not None
            if self.op == ConditionOp.NOT_EXISTS: return val is None
        except (TypeError, AttributeError):
            return False
        return False


def _get_field(obj: Dict, path: str) -> Any:
    """Resolve dot-notation path: 'user.role' → obj['user']['role']"""
    parts = path.split(".")
    cur = obj
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


@dataclass
class PolicyRule:
    rule_id: str
    name: str
    effect: Effect
    conditions: List[Condition] = field(default_factory=list)
    condition_logic: str = "AND"    # AND | OR
    priority: int = 0               # higher = evaluated first
    status: PolicyStatus = PolicyStatus.ACTIVE
    description: str = ""
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def matches(self, context: Dict[str, Any]) -> bool:
        if not self.conditions:
            return True
        results = [c.evaluate(context) for c in self.conditions]
        if self.condition_logic == "OR":
            return any(results)
        return all(results)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "effect": self.effect.value,
            "priority": self.priority,
            "status": self.status.value,
            "condition_count": len(self.conditions),
            "logic": self.condition_logic,
        }


@dataclass
class EvaluationResult:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    allowed: bool = True
    effect: Effect = Effect.ALLOW
    matched_rules: List[str] = field(default_factory=list)
    denied_by: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    audit_notes: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "allowed": self.allowed,
            "effect": self.effect.value,
            "matched_rules": self.matched_rules,
            "denied_by": self.denied_by,
            "warnings": self.warnings,
            "duration_ms": round(self.duration_ms, 2),
        }


class PolicyEngine:
    """
    Rule-based policy engine supporting:
    - ALLOW / DENY / AUDIT / WARN / REQUIRE_MFA effects
    - Condition evaluation with dot-path field access
    - AND / OR condition logic per rule
    - Priority-ordered evaluation (DENY wins over ALLOW at same priority)
    - Testing mode (evaluate without enforcing)
    - Policy sets (group rules)
    - Audit trail in SQLite
    - Custom evaluator hooks
    """

    def __init__(self, default_effect: Effect = Effect.ALLOW,
                 db_path: str = ":memory:"):
        self.default_effect = default_effect
        self._rules: Dict[str, PolicyRule] = {}
        self._sets: Dict[str, List[str]] = {}        # set_name → [rule_ids]
        self._pre_hooks:  List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._eval_count = 0
        self._deny_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS pe_evals (
                request_id TEXT PRIMARY KEY, allowed INTEGER,
                effect TEXT, matched_rules TEXT, denied_by TEXT,
                duration_ms REAL, ts REAL
            );
        """)
        self._db.commit()

    # ── RULE MANAGEMENT ───────────────────────────────────────────────

    def add_rule(self, name: str, effect: Effect,
                 conditions: Optional[List[Dict]] = None,
                 condition_logic: str = "AND",
                 priority: int = 0,
                 status: PolicyStatus = PolicyStatus.ACTIVE,
                 description: str = "",
                 tags: Optional[List[str]] = None,
                 policy_set: Optional[str] = None,
                 rule_id: Optional[str] = None) -> PolicyRule:
        rid = rule_id or str(uuid.uuid4())[:8]
        parsed_conds = []
        for c in (conditions or []):
            parsed_conds.append(Condition(
                field=c["field"],
                op=ConditionOp(c["op"]),
                value=c.get("value")))
        rule = PolicyRule(
            rule_id=rid, name=name, effect=effect,
            conditions=parsed_conds, condition_logic=condition_logic,
            priority=priority, status=status,
            description=description, tags=list(tags or []))
        self._rules[rid] = rule
        if policy_set:
            self._sets.setdefault(policy_set, []).append(rid)
        return rule

    def remove_rule(self, rule_id: str):
        self._rules.pop(rule_id, None)
        for sids in self._sets.values():
            if rule_id in sids:
                sids.remove(rule_id)

    def activate(self, rule_id: str):
        if rule_id in self._rules:
            self._rules[rule_id].status = PolicyStatus.ACTIVE

    def deactivate(self, rule_id: str):
        if rule_id in self._rules:
            self._rules[rule_id].status = PolicyStatus.INACTIVE

    def set_testing(self, rule_id: str):
        if rule_id in self._rules:
            self._rules[rule_id].status = PolicyStatus.TESTING

    # ── POLICY SETS ───────────────────────────────────────────────────

    def create_set(self, name: str, rule_ids: Optional[List[str]] = None):
        self._sets[name] = list(rule_ids or [])

    def add_to_set(self, set_name: str, rule_id: str):
        self._sets.setdefault(set_name, []).append(rule_id)

    # ── EVALUATION ────────────────────────────────────────────────────

    def evaluate(self, context: Dict[str, Any],
                 policy_set: Optional[str] = None) -> EvaluationResult:
        t0 = time.time()
        self._eval_count += 1

        result = EvaluationResult(context=context)

        # Pre-hooks
        for fn in self._pre_hooks:
            try: fn(context)
            except Exception: pass

        # Select rules
        if policy_set:
            rule_ids = self._sets.get(policy_set, [])
            rules = [self._rules[rid] for rid in rule_ids if rid in self._rules]
        else:
            rules = list(self._rules.values())

        # Filter active/testing only
        active_rules = [r for r in rules
                        if r.status in (PolicyStatus.ACTIVE, PolicyStatus.TESTING)]

        # Sort by priority descending, then DENY before ALLOW
        def sort_key(r: PolicyRule):
            effect_order = {Effect.DENY: 0, Effect.REQUIRE_MFA: 1,
                            Effect.WARN: 2, Effect.AUDIT: 3, Effect.ALLOW: 4}
            return (-r.priority, effect_order.get(r.effect, 5))

        active_rules.sort(key=sort_key)

        matched_effects: List[Tuple[PolicyRule, Effect]] = []
        for rule in active_rules:
            if rule.matches(context):
                result.matched_rules.append(rule.rule_id)
                matched_effects.append((rule, rule.effect))

        # Determine final effect
        # DENY wins, then REQUIRE_MFA, then WARN/AUDIT, then ALLOW
        final_effect = self.default_effect
        denied_by    = None
        for rule, eff in matched_effects:
            if rule.status == PolicyStatus.TESTING:
                continue   # Testing rules don't enforce
            if eff == Effect.DENY:
                final_effect = Effect.DENY
                denied_by = rule.rule_id
                break
            if eff == Effect.REQUIRE_MFA and final_effect != Effect.DENY:
                final_effect = Effect.REQUIRE_MFA
                denied_by = rule.rule_id
            if eff == Effect.WARN:
                result.warnings.append(f"Rule '{rule.name}' warned")
            if eff == Effect.AUDIT:
                result.audit_notes.append(f"Rule '{rule.name}' audited")

        if final_effect == Effect.ALLOW and not matched_effects:
            final_effect = self.default_effect

        result.allowed  = final_effect not in (Effect.DENY, Effect.REQUIRE_MFA)
        result.effect   = final_effect
        result.denied_by = denied_by
        result.duration_ms = (time.time() - t0) * 1000

        if not result.allowed:
            self._deny_count += 1

        self._db.execute(
            "INSERT INTO pe_evals VALUES (?,?,?,?,?,?,?)",
            (result.request_id, int(result.allowed), final_effect.value,
             ",".join(result.matched_rules), denied_by,
             result.duration_ms, result.ts))
        self._db.commit()

        for fn in self._post_hooks:
            try: fn(result)
            except Exception: pass

        return result

    def is_allowed(self, context: Dict[str, Any],
                   policy_set: Optional[str] = None) -> bool:
        return self.evaluate(context, policy_set).allowed

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_pre_eval(self, fn: Callable): self._pre_hooks.append(fn)
    def on_post_eval(self, fn: Callable): self._post_hooks.append(fn)

    # ── QUERY ─────────────────────────────────────────────────────────

    def list_rules(self, status: Optional[PolicyStatus] = None,
                   tag: Optional[str] = None) -> List[Dict]:
        rules = list(self._rules.values())
        if status:
            rules = [r for r in rules if r.status == status]
        if tag:
            rules = [r for r in rules if tag in r.tags]
        return [r.to_dict() for r in rules]

    def eval_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT request_id,allowed,effect,denied_by,duration_ms,ts "
            "FROM pe_evals ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "allowed": bool(r[1]), "effect": r[2],
                 "denied_by": r[3], "ms": r[4]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        total = self._eval_count
        return {
            "rules": len(self._rules),
            "policy_sets": len(self._sets),
            "evaluations": total,
            "denied": self._deny_count,
            "deny_rate": round(self._deny_count / total, 4) if total else 0.0,
            "default_effect": self.default_effect.value,
        }
