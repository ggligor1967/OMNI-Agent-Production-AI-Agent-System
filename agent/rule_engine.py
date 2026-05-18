"""OMNI AGENT - Rule Engine
Forward-chaining business rule engine: define rules with conditions and
actions, evaluate against a fact context, resolve conflicts, and audit.

Features:
- Rules: name, priority (lower=first), conditions list, actions list
- Conditions: field op value — operators: ==, !=, >, <, >=, <=,
    in, not_in, contains, starts_with, ends_with, regex, exists, type
- Actions: set_fact, delete_fact, call_fn, emit_event, raise_error
- Condition logic: ALL (default) or ANY — and/or across conditions
- Priority-based ordering: rules sorted by priority ascending
- Conflict resolution: FIRST (stop at first match), ALL (run all matches)
- Forward chaining: after action modifies facts, re-evaluate from start
- Agenda: ordered list of matched rules ready to fire
- Salience: per-rule activation count limit (fire at most N times)
- Rule groups: named sets; activate/deactivate groups
- Fact types: any JSON-serializable dict
- Global functions: register Python functions callable from actions
- Hooks: on_match(rule, facts), on_action(rule, action, facts)
- Audit log: every rule fire recorded with facts snapshot
- Explain: trace which rules fired and why for a given evaluation
- SQLite persistence: rules, audit log
- REST API: add_rule, evaluate, explain, stats
"""
import json, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class ConflictStrategy(str, Enum):
    FIRST   = "first"   # stop after first matched rule fires
    ALL     = "all"     # fire all matching rules

class ConditionLogic(str, Enum):
    ALL = "all"   # all conditions must match (AND)
    ANY = "any"   # any condition must match (OR)

def _get(facts: Dict, path: str) -> Any:
    parts = path.split(".")
    cur = facts
    for p in parts:
        if isinstance(cur, dict): cur = cur.get(p)
        else: return None
    return cur

def _eval_condition(cond: Dict, facts: Dict) -> bool:
    field_path = cond.get("field","")
    op = cond.get("op","==")
    expected = cond.get("value")
    actual = _get(facts, field_path)

    if op == "exists":  return actual is not None
    if op == "type":    return type(actual).__name__ == str(expected)
    if actual is None:  return False

    if op == "==":           return actual == expected
    if op == "!=":           return actual != expected
    if op == ">":            return actual >  expected
    if op == "<":            return actual <  expected
    if op == ">=":           return actual >= expected
    if op == "<=":           return actual <= expected
    if op == "in":           return actual in (expected or [])
    if op == "not_in":       return actual not in (expected or [])
    if op == "contains":
        if isinstance(actual, (list, str)):
            return expected in actual
    if op == "starts_with":  return str(actual).startswith(str(expected))
    if op == "ends_with":    return str(actual).endswith(str(expected))
    if op == "regex":
        try: return bool(re.search(str(expected), str(actual)))
        except: return False
    return False

def _apply_action(action: Dict, facts: Dict,
                   functions: Dict[str, Callable],
                   events: List[Dict]) -> Dict:
    act = action.get("type","")
    if act == "set_fact":
        parts = action["field"].split(".")
        cur = facts
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = action.get("value")
    elif act == "delete_fact":
        parts = action["field"].split(".")
        cur = facts
        for p in parts[:-1]:
            if isinstance(cur, dict): cur = cur.get(p, {})
        if isinstance(cur, dict): cur.pop(parts[-1], None)
    elif act == "call_fn":
        fn_name = action.get("fn","")
        fn = functions.get(fn_name)
        if fn:
            try:
                result = fn(facts, **action.get("kwargs",{}))
                if action.get("result_field") and result is not None:
                    _apply_action({"type":"set_fact",
                                    "field": action["result_field"],
                                    "value": result}, facts, functions, events)
            except Exception as e:
                logger.warning(f"Rule action call_fn {fn_name} error: {e}")
    elif act == "emit_event":
        events.append({"event": action.get("event",""),
                        "data": action.get("data",{}),
                        "ts": time.time()})
    elif act == "raise_error":
        raise RuntimeError(action.get("message","Rule error"))
    return facts

@dataclass
class Rule:
    name: str
    conditions: List[Dict]
    actions: List[Dict]
    priority: int = 5
    logic: ConditionLogic = ConditionLogic.ALL
    group: str = "default"
    enabled: bool = True
    salience: int = 0           # 0 = unlimited
    _fire_count: int = field(default=0, repr=False)

    def matches(self, facts: Dict) -> bool:
        if not self.enabled: return False
        if self.salience > 0 and self._fire_count >= self.salience: return False
        results = [_eval_condition(c, facts) for c in self.conditions]
        if self.logic == ConditionLogic.ALL: return all(results)
        return any(results)

    def to_dict(self):
        return {"name": self.name, "priority": self.priority,
                "group": self.group, "enabled": self.enabled,
                "conditions": self.conditions, "actions": self.actions,
                "logic": self.logic.value, "fire_count": self._fire_count}

@dataclass
class EvalResult:
    facts: Dict
    fired: List[str]         # rule names that fired
    events: List[Dict]
    error: Optional[str]
    trace: List[Dict]        # explain trace

    def to_dict(self):
        return {"facts": self.facts, "fired": self.fired,
                "events": self.events, "error": self.error,
                "trace": self.trace}

class REStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS rules(
                    name TEXT PRIMARY KEY, data TEXT, created_at REAL);
                CREATE TABLE IF NOT EXISTS audit(
                    id TEXT PRIMARY KEY, rule_name TEXT,
                    facts_snap TEXT, events TEXT, ts REAL);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def save_rule(self, r: Rule):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO rules VALUES(?,?,?)",
                (r.name, json.dumps(r.to_dict(), default=str), time.time()))

    def delete_rule(self, name: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM rules WHERE name=?", (name,))
            return cur.rowcount > 0

    def log_fire(self, rule_name: str, facts: Dict, events: List):
        with self._conn() as c:
            c.execute("INSERT INTO audit VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], rule_name,
                 json.dumps(facts, default=str)[:500],
                 json.dumps(events, default=str)[:300],
                 time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            nr = c.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
            na = c.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            recent = [dict(r) for r in c.execute(
                "SELECT rule_name, ts FROM audit "
                "ORDER BY ts DESC LIMIT 10").fetchall()]
        return {"rules": nr, "audit_entries": na, "recent_fires": recent}

class RuleEngine:
    """
    Forward-chaining rule engine with priority and conflict resolution.

    Usage:
        engine = RuleEngine()

        engine.add_rule("flag_vip", priority=1,
            conditions=[{"field":"spend","op":">=","value":1000}],
            actions=[{"type":"set_fact","field":"tier","value":"vip"}])

        engine.add_rule("discount_vip", priority=2,
            conditions=[{"field":"tier","op":"==","value":"vip"}],
            actions=[{"type":"set_fact","field":"discount","value":0.2}])

        result = engine.evaluate({"spend": 1500})
        # result.facts["tier"]     == "vip"
        # result.facts["discount"] == 0.2
        # result.fired             == ["flag_vip", "discount_vip"]
    """
    def __init__(self, db_path: str = "data/rules.db",
                 strategy: ConflictStrategy = ConflictStrategy.ALL,
                 max_cycles: int = 20):
        self._store     = REStore(db_path)
        self._strategy  = strategy
        self._max_cycles = max_cycles
        self._rules:     Dict[str, Rule] = {}
        self._functions: Dict[str, Callable] = {}
        self._disabled_groups: Set[str] = set()
        self._hooks_match:  List[Callable] = []
        self._hooks_action: List[Callable] = []

    def on_match(self, fn):  self._hooks_match.append(fn)
    def on_action(self, fn): self._hooks_action.append(fn)

    def register_function(self, name: str, fn: Callable):
        self._functions[name] = fn

    def add_rule(self, name: str,
                  conditions: List[Dict],
                  actions: List[Dict],
                  priority: int = 5,
                  logic: ConditionLogic = ConditionLogic.ALL,
                  group: str = "default",
                  salience: int = 0) -> Rule:
        rule = Rule(name=name, conditions=conditions, actions=actions,
                     priority=priority, logic=logic, group=group,
                     salience=salience)
        self._rules[name] = rule
        self._store.save_rule(rule)
        return rule

    def remove_rule(self, name: str) -> bool:
        self._rules.pop(name, None)
        return self._store.delete_rule(name)

    def enable_rule(self, name: str):
        if name in self._rules: self._rules[name].enabled = True

    def disable_rule(self, name: str):
        if name in self._rules: self._rules[name].enabled = False

    def enable_group(self, group: str):  self._disabled_groups.discard(group)
    def disable_group(self, group: str): self._disabled_groups.add(group)

    def _sorted_rules(self) -> List[Rule]:
        return sorted(
            [r for r in self._rules.values()
             if r.group not in self._disabled_groups],
            key=lambda r: r.priority)

    def evaluate(self, facts: Dict,
                  reset_counts: bool = True) -> EvalResult:
        facts = dict(facts)
        fired: List[str] = []
        events: List[Dict] = []
        trace: List[Dict] = []
        error: Optional[str] = None

        if reset_counts:
            for r in self._rules.values():
                r._fire_count = 0

        cycle = 0
        changed = True
        while changed and cycle < self._max_cycles:
            changed = False; cycle += 1
            facts_before = json.dumps(facts, sort_keys=True, default=str)
            for rule in self._sorted_rules():
                if rule.matches(facts):
                    # Only fire if facts could change (rule not already fired this cycle)
                    trace.append({"rule": rule.name, "cycle": cycle,
                                   "matched": True,
                                   "conditions_passed": len(rule.conditions)})
                    for h in self._hooks_match:
                        try: h(rule, facts)
                        except: pass
                    try:
                        for action in rule.actions:
                            for h in self._hooks_action:
                                try: h(rule, action, facts)
                                except: pass
                            _apply_action(action, facts,
                                           self._functions, events)
                    except RuntimeError as e:
                        error = str(e); break
                    rule._fire_count += 1
                    if rule.name not in fired: fired.append(rule.name)
                    self._store.log_fire(rule.name, facts, events)
                    if self._strategy == ConflictStrategy.FIRST:
                        changed = True; break
            # Only continue if facts actually changed
            facts_after = json.dumps(facts, sort_keys=True, default=str)
            if facts_after != facts_before:
                changed = True
            if error: break

        return EvalResult(facts=facts, fired=fired,
                           events=events, error=error, trace=trace)

    def explain(self, facts: Dict) -> List[Dict]:
        """Return which conditions pass/fail for each rule."""
        explanation = []
        for rule in self._sorted_rules():
            cond_results = []
            for cond in rule.conditions:
                passed = _eval_condition(cond, facts)
                cond_results.append({**cond, "passed": passed})
            would_fire = rule.matches(facts)
            explanation.append({"rule": rule.name,
                                  "priority": rule.priority,
                                  "would_fire": would_fire,
                                  "conditions": cond_results})
        return explanation

    def get_rule(self, name: str) -> Optional[Rule]:
        return self._rules.get(name)

    def list_rules(self) -> List[Dict]:
        return [r.to_dict() for r in self._sorted_rules()]

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory"] = len(self._rules)
        s["strategy"] = self._strategy.value
        s["functions"] = list(self._functions.keys())
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def add_ep(req):
            d = await req.json()
            r = self.add_rule(d["name"], d["conditions"], d["actions"],
                               d.get("priority",5))
            return web.json_response(r.to_dict(), status=201)
        async def eval_ep(req):
            d = await req.json()
            result = self.evaluate(d.get("facts",{}))
            return web.json_response(result.to_dict())
        async def explain_ep(req):
            d = await req.json()
            return web.json_response(
                {"explanation": self.explain(d.get("facts",{}))})
        async def list_ep(req):
            return web.json_response({"rules": self.list_rules()})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/rules"
        app.router.add_post(f"{p}/add",     add_ep)
        app.router.add_post(f"{p}/evaluate",eval_ep)
        app.router.add_post(f"{p}/explain", explain_ep)
        app.router.add_get( f"{p}/list",    list_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Rule engine API at {prefix}/rules/")
