"""OMNI AGENT - Output Validator
Validate and auto-repair LLM outputs: schema checks, format
enforcement, regex patterns, semantic constraints, and repair strategies.

Features:
- Rule types: REQUIRED_FIELDS, TYPE_CHECK, REGEX, LENGTH, RANGE,
              JSON_SCHEMA, WHITELIST, BLACKLIST, CUSTOM
- Validation pipeline: ordered list of rules applied sequentially
- Severity levels: ERROR (fail), WARNING (pass but log), INFO
- Auto-repair strategies: truncate, pad, strip, json_fix, fallback
- JSON extraction: find embedded JSON in prose text
- Sanitisation: remove PII patterns, truncate, normalise whitespace
- Scoring: 0-1 validity score weighted by rule severity
- Short-circuit: stop on first ERROR or run all rules
- Batch validation: validate list of outputs, return per-item reports
- SQLite persistence: validation results and rule hit counts
- REST API: validate, batch, rules, stats
"""
import json, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class RuleType(str, Enum):
    REQUIRED_FIELDS = "required_fields"
    TYPE_CHECK      = "type_check"
    REGEX           = "regex"
    NOT_REGEX       = "not_regex"
    LENGTH          = "length"
    RANGE           = "range"
    JSON_VALID      = "json_valid"
    WHITELIST       = "whitelist"
    BLACKLIST       = "blacklist"
    CUSTOM          = "custom"

class Severity(str, Enum):
    ERROR   = "error"
    WARNING = "warning"
    INFO    = "info"

_SEV_WEIGHT = {Severity.ERROR: 1.0, Severity.WARNING: 0.3, Severity.INFO: 0.1}

@dataclass
class ValidationRule:
    id: str; name: str; rule_type: RuleType
    severity: Severity = Severity.ERROR
    # Rule parameters
    fields: List[str] = field(default_factory=list)      # REQUIRED_FIELDS
    expected_type: str = ""                               # TYPE_CHECK: str/int/float/bool/list/dict
    pattern: str = ""                                     # REGEX / NOT_REGEX
    min_len: int = 0; max_len: int = 100_000              # LENGTH
    min_val: float = None; max_val: float = None          # RANGE
    whitelist: List[str] = field(default_factory=list)    # WHITELIST
    blacklist: List[str] = field(default_factory=list)    # BLACKLIST
    custom_fn: Optional[Callable] = None                  # CUSTOM
    repair_strategy: str = ""  # truncate|strip|json_fix|fallback|none
    repair_value: Any = None   # used by 'fallback' strategy
    hit_count: int = 0
    error_count: int = 0

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "type": self.rule_type.value, "severity": self.severity.value,
                "hit_count": self.hit_count, "error_count": self.error_count}

@dataclass
class ValidationResult:
    valid: bool; score: float
    errors: List[Dict] = field(default_factory=list)
    warnings: List[Dict] = field(default_factory=list)
    repaired: bool = False
    repaired_output: Any = None
    rule_results: List[Dict] = field(default_factory=list)
    latency_ms: float = 0.0

    def to_dict(self):
        return {"valid": self.valid, "score": round(self.score, 4),
                "errors": self.errors, "warnings": self.warnings,
                "repaired": self.repaired,
                "repaired_output": str(self.repaired_output)[:500]
                                   if self.repaired_output is not None else None,
                "latency_ms": round(self.latency_ms, 1)}

def _extract_json(text: str) -> Optional[Any]:
    """Find and parse embedded JSON in prose."""
    # Try direct parse first
    try: return json.loads(text)
    except: pass
    # Find JSON object or array
    for pat in [r'\{[^{}]*\}', r'\{.*?\}', r'\[.*?\]']:
        m = re.search(pat, text, re.DOTALL)
        if m:
            try: return json.loads(m.group(0))
            except: pass
    return None

def _type_check(val: Any, expected: str) -> bool:
    t = {"str": str, "string": str, "int": int, "integer": int,
         "float": (int, float), "number": (int, float),
         "bool": bool, "boolean": bool,
         "list": list, "array": list, "dict": dict, "object": dict}
    return isinstance(val, t.get(expected, object))

class OVStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS results(
                    id TEXT PRIMARY KEY, pipeline TEXT,
                    valid INTEGER, score REAL, repaired INTEGER,
                    error_count INTEGER DEFAULT 0,
                    warning_count INTEGER DEFAULT 0,
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_res_pl ON results(pipeline, created_at DESC);
            """)

    def log(self, pipeline: str, r: ValidationResult):
        with self._conn() as c:
            c.execute("INSERT INTO results VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], pipeline, int(r.valid),
                 r.score, int(r.repaired),
                 len(r.errors), len(r.warnings), time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            n   = c.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            nv  = c.execute("SELECT COUNT(*) FROM results WHERE valid=1").fetchone()[0]
            nr  = c.execute("SELECT COUNT(*) FROM results WHERE repaired=1").fetchone()[0]
            avg = c.execute("SELECT AVG(score) FROM results").fetchone()[0] or 0
        return {"total": n, "valid": nv, "repaired": nr,
                "avg_score": round(avg, 4),
                "pass_rate": round(nv / max(1, n), 4)}

class OutputValidator:
    """
    Rule-based LLM output validator with auto-repair.

    Usage:
        validator = OutputValidator()
        validator.add_rule("has_answer", RuleType.REQUIRED_FIELDS,
                            fields=["answer", "confidence"])
        validator.add_rule("json_format", RuleType.JSON_VALID,
                            repair_strategy="json_fix")
        validator.add_rule("no_profanity", RuleType.BLACKLIST,
                            blacklist=["badword1", "badword2"],
                            severity=Severity.WARNING)

        result = validator.validate('{"answer": "42", "confidence": 0.9}')
        print(result.valid, result.score)
    """
    def __init__(self, db_path: str = "data/validator.db",
                 pipeline_name: str = "default",
                 stop_on_first_error: bool = False):
        self._store = OVStore(db_path)
        self.pipeline_name = pipeline_name
        self._rules: List[ValidationRule] = []
        self.stop_on_first_error = stop_on_first_error

    def add_rule(self, name: str, rule_type: RuleType,
                  severity: Severity = Severity.ERROR,
                  repair_strategy: str = "none",
                  repair_value: Any = None,
                  **kwargs) -> ValidationRule:
        rule = ValidationRule(
            id=str(uuid.uuid4())[:8], name=name, rule_type=rule_type,
            severity=severity, repair_strategy=repair_strategy,
            repair_value=repair_value,
            **{k: v for k, v in kwargs.items()
               if k in ValidationRule.__dataclass_fields__})
        self._rules.append(rule)
        return rule

    def remove_rule(self, name: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.name != name]
        return len(self._rules) < before

    def _apply_rule(self, rule: ValidationRule,
                     output: Any) -> Tuple[bool, str]:
        """Returns (passed, message)."""
        rt = rule.rule_type
        try:
            if rt == RuleType.REQUIRED_FIELDS:
                if not isinstance(output, dict):
                    return False, "Output is not a dict"
                missing = [f for f in rule.fields if f not in output]
                return (not missing,
                        f"Missing fields: {missing}" if missing else "")

            if rt == RuleType.TYPE_CHECK:
                return _type_check(output, rule.expected_type), \
                       f"Expected {rule.expected_type}, got {type(output).__name__}"

            if rt == RuleType.REGEX:
                text = json.dumps(output) if not isinstance(output, str) else output
                match = bool(re.search(rule.pattern, text, re.DOTALL))
                return match, "" if match else f"Pattern not matched: {rule.pattern!r}"

            if rt == RuleType.NOT_REGEX:
                text = json.dumps(output) if not isinstance(output, str) else output
                match = bool(re.search(rule.pattern, text, re.DOTALL | re.IGNORECASE))
                return not match, f"Forbidden pattern found: {rule.pattern!r}" if match else ""

            if rt == RuleType.LENGTH:
                text = str(output)
                n = len(text)
                ok = rule.min_len <= n <= rule.max_len
                return ok, "" if ok else f"Length {n} outside [{rule.min_len}, {rule.max_len}]"

            if rt == RuleType.RANGE:
                try:
                    val = float(output)
                    lo = rule.min_val if rule.min_val is not None else float('-inf')
                    hi = rule.max_val if rule.max_val is not None else float('inf')
                    ok = lo <= val <= hi
                    return ok, "" if ok else f"Value {val} outside [{lo}, {hi}]"
                except (TypeError, ValueError):
                    return False, f"Cannot coerce {output!r} to number"

            if rt == RuleType.JSON_VALID:
                extracted = _extract_json(output) if isinstance(output, str) else output
                ok = extracted is not None
                return ok, "" if ok else "Cannot parse as JSON"

            if rt == RuleType.WHITELIST:
                text = str(output).lower()
                ok = any(w.lower() in text for w in rule.whitelist)
                return ok, "" if ok else f"None of {rule.whitelist} found in output"

            if rt == RuleType.BLACKLIST:
                text = str(output).lower()
                hits = [w for w in rule.blacklist if w.lower() in text]
                return not hits, f"Blacklisted terms found: {hits}" if hits else ""

            if rt == RuleType.CUSTOM:
                if rule.custom_fn:
                    result = rule.custom_fn(output)
                    if isinstance(result, tuple): return result
                    return bool(result), ""
                return True, ""

        except Exception as e:
            return False, f"Rule evaluation error: {e}"
        return True, ""

    def _repair(self, rule: ValidationRule, output: Any) -> Any:
        strat = rule.repair_strategy
        if strat == "fallback":
            return rule.repair_value
        if strat == "truncate":
            text = str(output)
            return text[:rule.max_len] if rule.max_len else text
        if strat == "strip":
            return str(output).strip()
        if strat == "json_fix":
            extracted = _extract_json(str(output))
            return extracted if extracted is not None else rule.repair_value
        return output

    def validate(self, output: Any) -> ValidationResult:
        start = time.time()
        errors = []; warnings = []
        repaired = False; current = output
        rule_results = []
        total_weight = sum(_SEV_WEIGHT[r.severity] for r in self._rules) or 1.0
        penalty = 0.0

        for rule in self._rules:
            rule.hit_count += 1
            passed, msg = self._apply_rule(rule, current)
            rr = {"rule": rule.name, "passed": passed,
                   "severity": rule.severity.value, "message": msg}
            rule_results.append(rr)

            if not passed:
                rule.error_count += 1
                item = {"rule": rule.name, "message": msg}
                if rule.severity == Severity.ERROR:
                    errors.append(item)
                    penalty += _SEV_WEIGHT[Severity.ERROR]
                    # Attempt repair
                    if rule.repair_strategy and rule.repair_strategy != "none":
                        repaired_val = self._repair(rule, current)
                        if repaired_val != current:
                            current = repaired_val; repaired = True
                            # Re-check after repair
                            passed2, _ = self._apply_rule(rule, current)
                            if passed2:
                                errors.pop(); penalty -= _SEV_WEIGHT[Severity.ERROR]
                    if self.stop_on_first_error and errors:
                        break
                else:
                    warnings.append(item)
                    penalty += _SEV_WEIGHT[rule.severity]

        score = max(0.0, 1.0 - penalty / total_weight)
        result = ValidationResult(
            valid=len(errors) == 0, score=score,
            errors=errors, warnings=warnings,
            repaired=repaired,
            repaired_output=current if repaired else None,
            rule_results=rule_results,
            latency_ms=(time.time() - start) * 1000)
        self._store.log(self.pipeline_name, result)
        return result

    def validate_batch(self, outputs: List[Any]) -> List[ValidationResult]:
        return [self.validate(o) for o in outputs]

    def sanitize(self, text: str,
                  max_len: int = None,
                  strip_pii: bool = False,
                  normalize_whitespace: bool = True) -> str:
        if normalize_whitespace:
            text = re.sub(r'\s+', ' ', text).strip()
        if strip_pii:
            text = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '[SSN]', text)
            text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
                           '[EMAIL]', text)
            text = re.sub(r'\b\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}\b',
                           '[CARD]', text)
        if max_len and len(text) > max_len:
            text = text[:max_len]
        return text

    def rules(self) -> List[Dict]:
        return [r.to_dict() for r in self._rules]

    def stats(self) -> Dict:
        s = self._store.stats()
        s["rules_count"] = len(self._rules)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def validate_ep(req):
            d = await req.json()
            result = self.validate(d.get("output", d.get("text", "")))
            return web.json_response(result.to_dict())
        async def batch_ep(req):
            d = await req.json()
            results = self.validate_batch(d["outputs"])
            return web.json_response({"results": [r.to_dict() for r in results]})
        async def rules_ep(req):
            return web.json_response({"rules": self.rules()})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/validator"
        app.router.add_post(f"{p}/validate", validate_ep)
        app.router.add_post(f"{p}/batch",    batch_ep)
        app.router.add_get( f"{p}/rules",    rules_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        logger.info(f"Output validator API at {prefix}/validator/")
