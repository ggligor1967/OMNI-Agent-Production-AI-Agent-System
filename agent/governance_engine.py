"""OMNI Agent — Governance Engine: policy evaluation, compliance rules, and audit trail."""
from __future__ import annotations
import json, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    DENY  = "deny"
    WARN  = "warn"


class PolicyScope(str, Enum):
    GLOBAL  = "global"
    USER    = "user"
    ROLE    = "role"
    RESOURCE = "resource"


class ComplianceLevel(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"
    INFO     = "info"


@dataclass
class PolicyRule:
    rule_id: str
    name: str
    description: str = ""
    effect: PolicyEffect = PolicyEffect.ALLOW
    scope: PolicyScope   = PolicyScope.GLOBAL
    scope_value: str     = "*"          # user id, role name, or resource pattern
    conditions: Dict[str, Any] = field(default_factory=dict)
    priority: int  = 0                  # higher = evaluated first
    enabled: bool  = True
    compliance_level: ComplianceLevel = ComplianceLevel.MEDIUM
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def matches_scope(self, context: Dict[str, Any]) -> bool:
        if self.scope == PolicyScope.GLOBAL:
            return True
        if self.scope == PolicyScope.USER:
            return context.get("user_id") == self.scope_value
        if self.scope == PolicyScope.ROLE:
            return self.scope_value in context.get("roles", [])
        if self.scope == PolicyScope.RESOURCE:
            resource = context.get("resource", "")
            pattern  = self.scope_value.replace("*", ".*")
            return bool(re.match(pattern, resource))
        return False

    def evaluate_conditions(self, context: Dict[str, Any]) -> bool:
        """Return True if all conditions match context."""
        for key, expected in self.conditions.items():
            actual = context.get(key)
            if isinstance(expected, dict):
                op  = expected.get("op", "eq")
                val = expected.get("value")
                if op == "eq"  and actual != val:            return False
                if op == "neq" and actual == val:            return False
                if op == "gt"  and not (actual is not None and actual > val):  return False
                if op == "lt"  and not (actual is not None and actual < val):  return False
                if op == "gte" and not (actual is not None and actual >= val): return False
                if op == "lte" and not (actual is not None and actual <= val): return False
                if op == "in"  and actual not in (val or []):                  return False
                if op == "nin" and actual in (val or []):                      return False
                if op == "contains" and str(val).lower() not in str(actual).lower(): return False
            else:
                if actual != expected:
                    return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "effect": self.effect.value,
            "scope": self.scope.value,
            "scope_value": self.scope_value,
            "priority": self.priority,
            "enabled": self.enabled,
            "compliance_level": self.compliance_level.value,
            "tags": self.tags,
        }


@dataclass
class EvaluationResult:
    allowed: bool
    effect: PolicyEffect
    matched_rules: List[str] = field(default_factory=list)
    warnings: List[str]      = field(default_factory=list)
    denials: List[str]        = field(default_factory=list)
    context: Dict[str, Any]  = field(default_factory=dict)
    evaluated_at: float       = field(default_factory=time.time)
    decision_id: str          = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "allowed": self.allowed,
            "effect": self.effect.value,
            "matched_rules": self.matched_rules,
            "warnings": self.warnings,
            "denials": self.denials,
        }


@dataclass
class ComplianceViolation:
    violation_id: str
    rule_id: str
    rule_name: str
    level: ComplianceLevel
    message: str
    context: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    resolved: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "violation_id": self.violation_id,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "level": self.level.value,
            "message": self.message,
            "resolved": self.resolved,
            "ts": self.ts,
        }


class GovernanceEngine:
    """
    Policy-based governance with:
    - Priority-ordered rule evaluation (DENY > WARN > ALLOW)
    - Scoped policies (global, user, role, resource)
    - Compliance violation tracking
    - Full audit trail in SQLite
    - Custom policy functions
    """

    def __init__(self, default_effect: PolicyEffect = PolicyEffect.ALLOW,
                 db_path: str = ":memory:"):
        self.default_effect = default_effect
        self._rules: Dict[str, PolicyRule] = {}
        self._custom_fns: Dict[str, Callable[[Dict], bool]] = {}
        self._violations: List[ComplianceViolation] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._eval_count = 0
        self._deny_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS gov_rules (
                rule_id TEXT PRIMARY KEY, name TEXT, effect TEXT,
                scope TEXT, scope_value TEXT, priority INTEGER,
                enabled INTEGER, compliance_level TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS gov_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT, ts REAL, allowed INTEGER,
                effect TEXT, context TEXT, matched_rules TEXT
            );
            CREATE TABLE IF NOT EXISTS gov_violations (
                violation_id TEXT PRIMARY KEY, rule_id TEXT,
                rule_name TEXT, level TEXT, message TEXT,
                context TEXT, ts REAL, resolved INTEGER
            );
        """)
        self._db.commit()

    # ── RULE MANAGEMENT ───────────────────────────────────────────────

    def add_rule(self, name: str, effect: PolicyEffect,
                 scope: PolicyScope = PolicyScope.GLOBAL,
                 scope_value: str = "*",
                 conditions: Optional[Dict] = None,
                 priority: int = 0,
                 description: str = "",
                 compliance_level: ComplianceLevel = ComplianceLevel.MEDIUM,
                 tags: Optional[List[str]] = None,
                 rule_id: Optional[str] = None) -> PolicyRule:
        rid = rule_id or str(uuid.uuid4())[:8]
        rule = PolicyRule(
            rule_id=rid, name=name, description=description,
            effect=effect, scope=scope, scope_value=scope_value,
            conditions=conditions or {}, priority=priority,
            compliance_level=compliance_level, tags=tags or [])
        self._rules[rid] = rule
        self._db.execute(
            "INSERT OR REPLACE INTO gov_rules VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, name, effect.value, scope.value, scope_value,
             priority, 1, compliance_level.value, rule.created_at))
        self._db.commit()
        return rule

    def remove_rule(self, rule_id: str):
        self._rules.pop(rule_id, None)
        self._db.execute("DELETE FROM gov_rules WHERE rule_id=?", (rule_id,))
        self._db.commit()

    def enable_rule(self, rule_id: str):
        if rule_id in self._rules:
            self._rules[rule_id].enabled = True

    def disable_rule(self, rule_id: str):
        if rule_id in self._rules:
            self._rules[rule_id].enabled = False

    def add_custom_fn(self, rule_id: str, fn: Callable[[Dict], bool]):
        """Register a custom condition function for a rule."""
        self._custom_fns[rule_id] = fn

    # ── EVALUATION ────────────────────────────────────────────────────

    def evaluate(self, context: Dict[str, Any]) -> EvaluationResult:
        """Evaluate all applicable rules against context. DENY wins."""
        self._eval_count += 1
        sorted_rules = sorted(
            [r for r in self._rules.values() if r.enabled],
            key=lambda r: r.priority, reverse=True)

        matched, warnings, denials = [], [], []
        final_effect = self.default_effect

        for rule in sorted_rules:
            if not rule.matches_scope(context):
                continue
            if not rule.evaluate_conditions(context):
                continue
            # Check custom function if registered
            fn = self._custom_fns.get(rule.rule_id)
            if fn:
                try:
                    if not fn(context):
                        continue
                except Exception:
                    continue
            matched.append(rule.rule_id)
            if rule.effect == PolicyEffect.DENY:
                denials.append(rule.name)
                final_effect = PolicyEffect.DENY
                self._record_violation(rule, context)
            elif rule.effect == PolicyEffect.WARN:
                warnings.append(rule.name)
                if final_effect != PolicyEffect.DENY:
                    final_effect = PolicyEffect.WARN
            elif rule.effect == PolicyEffect.ALLOW:
                if final_effect not in (PolicyEffect.DENY,):
                    final_effect = PolicyEffect.ALLOW

        allowed = final_effect != PolicyEffect.DENY
        if not allowed:
            self._deny_count += 1

        result = EvaluationResult(
            allowed=allowed,
            effect=final_effect,
            matched_rules=matched,
            warnings=warnings,
            denials=denials,
            context=context,
        )
        self._db.execute(
            "INSERT INTO gov_audit (decision_id,ts,allowed,effect,context,matched_rules) "
            "VALUES (?,?,?,?,?,?)",
            (result.decision_id, result.evaluated_at, int(allowed),
             final_effect.value, json.dumps(context), json.dumps(matched)))
        self._db.commit()
        return result

    def is_allowed(self, context: Dict[str, Any]) -> bool:
        return self.evaluate(context).allowed

    # ── COMPLIANCE ────────────────────────────────────────────────────

    def _record_violation(self, rule: PolicyRule, context: Dict[str, Any]):
        v = ComplianceViolation(
            violation_id=str(uuid.uuid4())[:8],
            rule_id=rule.rule_id,
            rule_name=rule.name,
            level=rule.compliance_level,
            message=f"Policy '{rule.name}' denied access",
            context=context)
        self._violations.append(v)
        self._db.execute(
            "INSERT INTO gov_violations VALUES (?,?,?,?,?,?,?,?)",
            (v.violation_id, v.rule_id, v.rule_name, v.level.value,
             v.message, json.dumps(context), v.ts, 0))
        self._db.commit()

    def violations(self, level: Optional[ComplianceLevel] = None,
                   resolved: Optional[bool] = None,
                   limit: int = 50) -> List[ComplianceViolation]:
        result = list(self._violations)
        if level:
            result = [v for v in result if v.level == level]
        if resolved is not None:
            result = [v for v in result if v.resolved == resolved]
        return result[-limit:]

    def resolve_violation(self, violation_id: str) -> bool:
        for v in self._violations:
            if v.violation_id == violation_id:
                v.resolved = True
                self._db.execute(
                    "UPDATE gov_violations SET resolved=1 WHERE violation_id=?",
                    (violation_id,))
                self._db.commit()
                return True
        return False

    # ── AUDIT ─────────────────────────────────────────────────────────

    def audit_log(self, limit: int = 50,
                  allowed_only: Optional[bool] = None) -> List[Dict[str, Any]]:
        q = "SELECT decision_id,ts,allowed,effect,matched_rules FROM gov_audit"
        params: List[Any] = []
        if allowed_only is not None:
            q += " WHERE allowed=?"
            params.append(int(allowed_only))
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(q, params).fetchall()
        return [{"decision_id": r[0], "ts": r[1], "allowed": bool(r[2]),
                 "effect": r[3], "matched_rules": json.loads(r[4])} for r in rows]

    # ── INTROSPECTION ─────────────────────────────────────────────────

    def list_rules(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        rules = list(self._rules.values())
        if enabled_only:
            rules = [r for r in rules if r.enabled]
        return [r.to_dict() for r in sorted(rules, key=lambda r: r.priority, reverse=True)]

    def get_rule(self, rule_id: str) -> Optional[PolicyRule]:
        return self._rules.get(rule_id)

    def stats(self) -> Dict[str, Any]:
        by_effect: Dict[str, int] = {}
        for r in self._rules.values():
            by_effect[r.effect.value] = by_effect.get(r.effect.value, 0) + 1
        open_violations = sum(1 for v in self._violations if not v.resolved)
        return {
            "rules": len(self._rules),
            "evaluations": self._eval_count,
            "denials": self._deny_count,
            "violations": len(self._violations),
            "open_violations": open_violations,
            "by_effect": by_effect,
        }
