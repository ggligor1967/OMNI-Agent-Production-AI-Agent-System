"""OMNI Agent — Output Validator V2: schema + semantic + custom rule validation pipeline."""
from __future__ import annotations
import json, re, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union


class Severity(str, Enum):
    ERROR   = "error"
    WARNING = "warning"
    INFO    = "info"


@dataclass
class ValidationIssue:
    field: str
    message: str
    severity: Severity = Severity.ERROR
    value: Any = None
    rule: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field,
            "message": self.message,
            "severity": self.severity.value,
            "rule": self.rule,
        }


@dataclass
class ValidationResult:
    valid: bool
    issues: List[ValidationIssue] = field(default_factory=list)
    value: Any = None
    coerced: bool = False       # True if value was auto-corrected
    duration_ms: float = 0.0

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "coerced": self.coerced,
            "duration_ms": round(self.duration_ms, 2),
            "issues": [i.to_dict() for i in self.issues],
        }


# ── BUILT-IN VALIDATORS ───────────────────────────────────────────────────────

class FieldValidator:
    """Validates a single field with chained rules."""

    def __init__(self, name: str):
        self.name = name
        self._rules: List[Tuple[Callable, str, Severity]] = []

    def required(self) -> "FieldValidator":
        def check(v):
            return v is not None and v != "" and v != [] and v != {}
        self._rules.append((check, "Field is required", Severity.ERROR))
        return self

    def type_is(self, *types: Type) -> "FieldValidator":
        def check(v):
            return isinstance(v, types)
        self._rules.append((check, f"Must be of type {[t.__name__ for t in types]}", Severity.ERROR))
        return self

    def min_length(self, n: int) -> "FieldValidator":
        self._rules.append((lambda v: len(v) >= n if hasattr(v, '__len__') else True,
                            f"Min length {n}", Severity.ERROR))
        return self

    def max_length(self, n: int) -> "FieldValidator":
        self._rules.append((lambda v: len(v) <= n if hasattr(v, '__len__') else True,
                            f"Max length {n}", Severity.ERROR))
        return self

    def min_value(self, n: Union[int, float]) -> "FieldValidator":
        self._rules.append((lambda v: isinstance(v, (int, float)) and v >= n,
                            f"Min value {n}", Severity.ERROR))
        return self

    def max_value(self, n: Union[int, float]) -> "FieldValidator":
        self._rules.append((lambda v: isinstance(v, (int, float)) and v <= n,
                            f"Max value {n}", Severity.ERROR))
        return self

    def matches(self, pattern: str) -> "FieldValidator":
        compiled = re.compile(pattern)
        self._rules.append((lambda v: bool(compiled.match(str(v))),
                            f"Must match pattern {pattern}", Severity.ERROR))
        return self

    def one_of(self, choices: List[Any]) -> "FieldValidator":
        self._rules.append((lambda v: v in choices,
                            f"Must be one of {choices}", Severity.ERROR))
        return self

    def custom(self, fn: Callable[[Any], bool], message: str,
               severity: Severity = Severity.ERROR) -> "FieldValidator":
        self._rules.append((fn, message, severity))
        return self

    def warn_if(self, fn: Callable[[Any], bool], message: str) -> "FieldValidator":
        self._rules.append((lambda v: not fn(v), message, Severity.WARNING))
        return self

    def validate(self, value: Any) -> List[ValidationIssue]:
        issues = []
        for fn, msg, sev in self._rules:
            try:
                if not fn(value):
                    issues.append(ValidationIssue(
                        field=self.name, message=msg,
                        severity=sev, value=value))
            except Exception as e:
                issues.append(ValidationIssue(
                    field=self.name,
                    message=f"Validator error: {e}",
                    severity=Severity.ERROR, value=value))
        return issues


class SchemaValidator:
    """Validates a dict against a set of FieldValidator rules."""

    def __init__(self):
        self._fields: Dict[str, FieldValidator] = {}
        self._extra_forbidden = False

    def field(self, name: str) -> FieldValidator:
        fv = FieldValidator(name)
        self._fields[name] = fv
        return fv

    def no_extra_fields(self) -> "SchemaValidator":
        self._extra_forbidden = True
        return self

    def validate(self, data: Dict[str, Any]) -> ValidationResult:
        t0 = time.time()
        issues = []
        for name, fv in self._fields.items():
            value = data.get(name)
            issues.extend(fv.validate(value))
        if self._extra_forbidden:
            for key in data:
                if key not in self._fields:
                    issues.append(ValidationIssue(
                        field=key,
                        message=f"Unknown field '{key}' not allowed",
                        severity=Severity.ERROR))
        has_errors = any(i.severity == Severity.ERROR for i in issues)
        return ValidationResult(
            valid=not has_errors,
            issues=issues,
            value=data,
            duration_ms=(time.time() - t0) * 1000,
        )


class JSONValidator:
    """Validates that a string is valid JSON and optionally matches a schema."""

    def validate(self, text: str,
                 schema_validator: Optional[SchemaValidator] = None) -> ValidationResult:
        t0 = time.time()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            return ValidationResult(
                valid=False,
                issues=[ValidationIssue("root", f"Invalid JSON: {e}", Severity.ERROR)],
                duration_ms=(time.time() - t0) * 1000,
            )
        if schema_validator and isinstance(parsed, dict):
            result = schema_validator.validate(parsed)
            result.duration_ms = (time.time() - t0) * 1000
            result.value = parsed
            return result
        return ValidationResult(valid=True, value=parsed,
                                duration_ms=(time.time() - t0) * 1000)


class SemanticValidator:
    """Validates text against semantic rules (length, format, banned patterns)."""

    def __init__(self):
        self._rules: List[Tuple[Callable[[str], bool], str, Severity]] = []

    def min_words(self, n: int) -> "SemanticValidator":
        self._rules.append((lambda t: len(t.split()) >= n,
                            f"Must have at least {n} words", Severity.ERROR))
        return self

    def max_words(self, n: int) -> "SemanticValidator":
        self._rules.append((lambda t: len(t.split()) <= n,
                            f"Must have at most {n} words", Severity.ERROR))
        return self

    def no_banned_words(self, words: List[str]) -> "SemanticValidator":
        banned_lower = [w.lower() for w in words]
        def check(t):
            t_lower = t.lower()
            return not any(b in t_lower for b in banned_lower)
        self._rules.append((check, f"Contains banned words {words}", Severity.ERROR))
        return self

    def contains(self, substring: str) -> "SemanticValidator":
        self._rules.append((lambda t: substring.lower() in t.lower(),
                            f"Must contain '{substring}'", Severity.WARNING))
        return self

    def starts_with(self, prefix: str) -> "SemanticValidator":
        self._rules.append((lambda t: t.strip().startswith(prefix),
                            f"Must start with '{prefix}'", Severity.ERROR))
        return self

    def custom(self, fn: Callable[[str], bool], message: str,
               severity: Severity = Severity.ERROR) -> "SemanticValidator":
        self._rules.append((fn, message, severity))
        return self

    def validate(self, text: str) -> ValidationResult:
        t0 = time.time()
        issues = []
        for fn, msg, sev in self._rules:
            try:
                if not fn(text):
                    issues.append(ValidationIssue("text", msg, sev, value=text[:50]))
            except Exception as e:
                issues.append(ValidationIssue("text", f"Rule error: {e}", Severity.ERROR))
        has_errors = any(i.severity == Severity.ERROR for i in issues)
        return ValidationResult(
            valid=not has_errors,
            issues=issues,
            value=text,
            duration_ms=(time.time() - t0) * 1000,
        )


class ValidationPipeline:
    """
    Chains multiple validators. Stops on first failing stage or runs all.
    """

    def __init__(self, fail_fast: bool = False):
        self._stages: List[Tuple[str, Any]] = []
        self.fail_fast = fail_fast
        self._run_count = 0
        self._pass_count = 0
        self._fail_count = 0

    def add_stage(self, name: str, validator) -> "ValidationPipeline":
        self._stages.append((name, validator))
        return self

    def run(self, value: Any) -> ValidationResult:
        t0 = time.time()
        self._run_count += 1
        all_issues: List[ValidationIssue] = []
        current = value
        for stage_name, validator in self._stages:
            if isinstance(validator, (SchemaValidator,)):
                result = validator.validate(current)
            elif isinstance(validator, SemanticValidator):
                result = validator.validate(str(current))
            elif isinstance(validator, JSONValidator):
                result = validator.validate(str(current))
            elif callable(validator):
                try:
                    ok = validator(current)
                    result = ValidationResult(valid=bool(ok), value=current)
                except Exception as e:
                    result = ValidationResult(
                        valid=False,
                        issues=[ValidationIssue(stage_name, str(e), Severity.ERROR)])
            else:
                continue
            # Tag issues with stage name
            for issue in result.issues:
                issue.rule = issue.rule or stage_name
            all_issues.extend(result.issues)
            if not result.valid and self.fail_fast:
                self._fail_count += 1
                return ValidationResult(
                    valid=False, issues=all_issues,
                    value=current,
                    duration_ms=(time.time() - t0) * 1000)
        has_errors = any(i.severity == Severity.ERROR for i in all_issues)
        if has_errors:
            self._fail_count += 1
        else:
            self._pass_count += 1
        return ValidationResult(
            valid=not has_errors,
            issues=all_issues,
            value=current,
            duration_ms=(time.time() - t0) * 1000,
        )

    def stats(self) -> Dict[str, Any]:
        return {
            "stages": len(self._stages),
            "runs": self._run_count,
            "passed": self._pass_count,
            "failed": self._fail_count,
        }
