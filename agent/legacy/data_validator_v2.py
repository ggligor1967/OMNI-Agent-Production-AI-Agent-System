"""OMNI Agent — Data Validator V2: schema validation, rules, transforms, reporting."""
from __future__ import annotations
import json, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


class FieldType(str, Enum):
    STRING  = "string"
    INTEGER = "integer"
    FLOAT   = "float"
    BOOLEAN = "boolean"
    LIST    = "list"
    DICT    = "dict"
    EMAIL   = "email"
    URL     = "url"
    DATE    = "date"
    UUID    = "uuid"
    ANY     = "any"


class Severity(str, Enum):
    ERROR   = "error"
    WARNING = "warning"
    INFO    = "info"


@dataclass
class FieldDef:
    name: str
    field_type: FieldType = FieldType.ANY
    required: bool = True
    nullable: bool = False
    default: Any = None
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    min_len: Optional[int] = None
    max_len: Optional[int] = None
    pattern: Optional[str] = None
    allowed: Optional[List[Any]] = None
    transform_fn: Optional[Callable[[Any], Any]] = None
    custom_validators: List[Callable[[Any], Optional[str]]] = field(default_factory=list)
    severity: Severity = Severity.ERROR

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "type": self.field_type.value,
                "required": self.required}


@dataclass
class ValidationError:
    field: str
    message: str
    value: Any = None
    severity: Severity = Severity.ERROR
    rule: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "message": self.message,
                "severity": self.severity.value, "rule": self.rule}


@dataclass
class ValidationResult:
    result_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    valid: bool = True
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)
    transformed_data: Optional[Dict[str, Any]] = None
    fields_checked: int = 0
    duration_ms: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
            "fields_checked": self.fields_checked,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class Schema:
    schema_id: str
    name: str
    version: str = "1.0"
    fields: Dict[str, FieldDef] = field(default_factory=dict)
    cross_field_rules: List[Callable[[Dict], Optional[str]]] = field(default_factory=list)
    allow_extra: bool = False
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_id": self.schema_id, "name": self.name,
                "version": self.version, "fields": len(self.fields)}


_EMAIL_RE = re.compile(r"^[^@]+@[^@]+\.[^@]+$")
_URL_RE   = re.compile(r"^https?://\S+$")
_UUID_RE  = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_DATE_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _coerce(value: Any, ftype: FieldType) -> Tuple[Any, Optional[str]]:
    try:
        if ftype == FieldType.STRING:  return str(value), None
        if ftype == FieldType.INTEGER: return int(value), None
        if ftype == FieldType.FLOAT:   return float(value), None
        if ftype == FieldType.BOOLEAN:
            if isinstance(value, bool): return value, None
            if str(value).lower() in ("true","1","yes"): return True, None
            if str(value).lower() in ("false","0","no"): return False, None
            return None, f"Cannot coerce {value!r} to bool"
        return value, None
    except Exception as e:
        return None, str(e)


class DataValidatorV2:
    """
    Schema-based data validator:
    - Define schemas with typed FieldDef objects
    - Validate dicts against schema
    - Type coercion (string→int, etc.)
    - Required / nullable / default value handling
    - Range checks (min/max for numbers, min/max len for strings/lists)
    - Regex pattern matching
    - Allowed-values enumeration
    - Built-in format validators: email, url, uuid, date
    - Custom per-field validator functions
    - Cross-field rules
    - Transform functions applied after validation
    - Warning-severity fields (non-blocking)
    - Extra-fields handling (reject or allow)
    - Schema versioning
    - Batch validation
    - Validation history and stats
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._schemas: Dict[str, Schema] = {}
        self._history: List[ValidationResult] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS dv_results (
                result_id TEXT PRIMARY KEY, schema_id TEXT,
                valid INTEGER, errors INTEGER, warnings INTEGER,
                fields_checked INTEGER, ts REAL
            );
        """)
        self._db.commit()

    # ── SCHEMA MANAGEMENT ─────────────────────────────────────────────

    def create_schema(self, name: str,
                       version: str = "1.0",
                       allow_extra: bool = False,
                       description: str = "",
                       schema_id: Optional[str] = None) -> Schema:
        sid = schema_id or str(uuid.uuid4())[:8]
        s   = Schema(schema_id=sid, name=name, version=version,
                      allow_extra=allow_extra, description=description)
        self._schemas[sid] = s
        return s

    def add_field(self, schema_id: str,
                   name: str,
                   field_type: FieldType = FieldType.ANY,
                   required: bool = True,
                   nullable: bool = False,
                   default: Any = None,
                   min_val: Optional[float] = None,
                   max_val: Optional[float] = None,
                   min_len: Optional[int] = None,
                   max_len: Optional[int] = None,
                   pattern: Optional[str] = None,
                   allowed: Optional[List[Any]] = None,
                   transform_fn: Optional[Callable] = None,
                   custom_validators: Optional[List[Callable]] = None,
                   severity: Severity = Severity.ERROR) -> FieldDef:
        s = self._schemas.get(schema_id)
        if not s: raise KeyError(f"Schema {schema_id} not found")
        fd = FieldDef(
            name=name, field_type=field_type,
            required=required, nullable=nullable, default=default,
            min_val=min_val, max_val=max_val,
            min_len=min_len, max_len=max_len,
            pattern=pattern, allowed=allowed,
            transform_fn=transform_fn,
            custom_validators=list(custom_validators or []),
            severity=severity)
        s.fields[name] = fd
        return fd

    def add_cross_rule(self, schema_id: str,
                        fn: Callable[[Dict], Optional[str]]):
        s = self._schemas.get(schema_id)
        if s: s.cross_field_rules.append(fn)

    def get_schema(self, schema_id: str) -> Optional[Schema]:
        return self._schemas.get(schema_id)

    def find_schema(self, name: str) -> Optional[Schema]:
        return next((s for s in self._schemas.values()
                     if s.name == name), None)

    # ── VALIDATION ───────────────────────────────────────────────────

    def validate(self, data: Dict[str, Any],
                  schema_id: str,
                  coerce: bool = True) -> ValidationResult:
        s = self._schemas.get(schema_id)
        if not s: raise KeyError(f"Schema {schema_id} not found")
        t0  = time.time()
        res = ValidationResult()
        out = dict(data)

        # Extra fields
        if not s.allow_extra:
            for k in data:
                if k not in s.fields:
                    res.errors.append(ValidationError(
                        field=k, message="Unexpected field",
                        severity=Severity.ERROR, rule="no_extra"))
                    res.valid = False

        # Field-level validation
        for fname, fd in s.fields.items():
            res.fields_checked += 1
            raw = data.get(fname)

            # Missing / default
            if raw is None:
                if fd.required:
                    if fd.default is not None:
                        out[fname] = fd.default
                        raw = fd.default
                    else:
                        errs = res.errors if fd.severity == Severity.ERROR else res.warnings
                        errs.append(ValidationError(
                            field=fname, message="Required field missing",
                            severity=fd.severity, rule="required"))
                        if fd.severity == Severity.ERROR:
                            res.valid = False
                        continue
                elif fd.default is not None:
                    out[fname] = fd.default
                    continue
                else:
                    continue

            # Nullable
            if raw is None and fd.nullable:
                out[fname] = None
                continue

            # Type coercion
            if coerce and fd.field_type not in (FieldType.ANY, FieldType.LIST, FieldType.DICT):
                coerced, err = _coerce(raw, fd.field_type)
                if err:
                    res.errors.append(ValidationError(
                        field=fname, message=f"Type coercion failed: {err}",
                        value=raw, severity=fd.severity, rule="type"))
                    if fd.severity == Severity.ERROR: res.valid = False
                    continue
                raw = coerced
                out[fname] = raw

            # Format validators
            fmt_err = self._check_format(raw, fd)
            if fmt_err:
                errs = res.errors if fd.severity == Severity.ERROR else res.warnings
                errs.append(ValidationError(
                    field=fname, message=fmt_err, value=raw,
                    severity=fd.severity, rule="format"))
                if fd.severity == Severity.ERROR: res.valid = False
                continue

            # Range checks
            range_err = self._check_range(raw, fd)
            if range_err:
                errs = res.errors if fd.severity == Severity.ERROR else res.warnings
                errs.append(ValidationError(
                    field=fname, message=range_err, value=raw,
                    severity=fd.severity, rule="range"))
                if fd.severity == Severity.ERROR: res.valid = False

            # Allowed values
            if fd.allowed is not None and raw not in fd.allowed:
                errs = res.errors if fd.severity == Severity.ERROR else res.warnings
                errs.append(ValidationError(
                    field=fname, message=f"Value {raw!r} not in allowed list",
                    value=raw, severity=fd.severity, rule="allowed"))
                if fd.severity == Severity.ERROR: res.valid = False

            # Custom validators
            for cv in fd.custom_validators:
                try:
                    cv_err = cv(raw)
                    if cv_err:
                        res.errors.append(ValidationError(
                            field=fname, message=cv_err, value=raw,
                            severity=fd.severity, rule="custom"))
                        if fd.severity == Severity.ERROR: res.valid = False
                except Exception as exc:
                    res.errors.append(ValidationError(
                        field=fname, message=f"Validator exception: {exc}",
                        value=raw, severity=Severity.ERROR, rule="custom"))
                    res.valid = False

            # Transform
            if fd.transform_fn and res.valid:
                try:
                    out[fname] = fd.transform_fn(raw)
                except Exception as exc:
                    res.errors.append(ValidationError(
                        field=fname, message=f"Transform failed: {exc}",
                        value=raw, severity=Severity.ERROR, rule="transform"))
                    res.valid = False
            else:
                out[fname] = raw

        # Cross-field rules
        for rule_fn in s.cross_field_rules:
            try:
                err = rule_fn(out)
                if err:
                    res.errors.append(ValidationError(
                        field="__cross__", message=err,
                        severity=Severity.ERROR, rule="cross_field"))
                    res.valid = False
            except Exception as exc:
                res.errors.append(ValidationError(
                    field="__cross__", message=str(exc),
                    severity=Severity.ERROR, rule="cross_field"))
                res.valid = False

        res.transformed_data = out
        res.duration_ms      = (time.time() - t0) * 1000
        self._history.append(res)
        self._db.execute(
            "INSERT INTO dv_results VALUES (?,?,?,?,?,?,?)",
            (res.result_id, schema_id, int(res.valid),
             len(res.errors), len(res.warnings),
             res.fields_checked, res.ts))
        self._db.commit()
        return res

    def validate_batch(self, records: List[Dict],
                        schema_id: str) -> List[ValidationResult]:
        return [self.validate(r, schema_id) for r in records]

    # ── BUILT-IN CHECKS ──────────────────────────────────────────────

    def _check_format(self, val: Any, fd: FieldDef) -> Optional[str]:
        ft = fd.field_type
        if ft == FieldType.EMAIL:
            if not _EMAIL_RE.match(str(val)):
                return f"Invalid email: {val!r}"
        elif ft == FieldType.URL:
            if not _URL_RE.match(str(val)):
                return f"Invalid URL: {val!r}"
        elif ft == FieldType.UUID:
            if not _UUID_RE.match(str(val)):
                return f"Invalid UUID: {val!r}"
        elif ft == FieldType.DATE:
            if not _DATE_RE.match(str(val)):
                return f"Invalid date (YYYY-MM-DD): {val!r}"
        if fd.pattern:
            if not re.match(fd.pattern, str(val)):
                return f"Pattern mismatch: {val!r}"
        return None

    def _check_range(self, val: Any, fd: FieldDef) -> Optional[str]:
        if fd.min_val is not None and isinstance(val, (int, float)):
            if val < fd.min_val:
                return f"Value {val} < min {fd.min_val}"
        if fd.max_val is not None and isinstance(val, (int, float)):
            if val > fd.max_val:
                return f"Value {val} > max {fd.max_val}"
        if fd.min_len is not None and hasattr(val, "__len__"):
            if len(val) < fd.min_len:
                return f"Length {len(val)} < min_len {fd.min_len}"
        if fd.max_len is not None and hasattr(val, "__len__"):
            if len(val) > fd.max_len:
                return f"Length {len(val)} > max_len {fd.max_len}"
        return None

    # ── STATS ─────────────────────────────────────────────────────────

    def validation_history(self, limit: int = 20) -> List[Dict]:
        return [r.to_dict() for r in self._history[-limit:]]

    def stats(self) -> Dict[str, Any]:
        if not self._history:
            return {"schemas": len(self._schemas), "runs": 0}
        valid_count = sum(1 for r in self._history if r.valid)
        return {
            "schemas": len(self._schemas),
            "runs": len(self._history),
            "pass_rate": round(valid_count / len(self._history), 3),
        }
