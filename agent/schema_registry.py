"""OMNI Agent — Schema Registry: JSON Schema management with validation, versioning, migration."""
from __future__ import annotations
import json, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


class SchemaType(str, Enum):
    OBJECT  = "object"
    ARRAY   = "array"
    STRING  = "string"
    NUMBER  = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    NULL    = "null"


class CompatMode(str, Enum):
    FULL     = "full"          # new schema must be fully compatible (no additions/removals)
    BACKWARD = "backward"      # new schema can read data written with old schema
    FORWARD  = "forward"       # old schema can read data written with new schema
    NONE     = "none"          # no compatibility check


@dataclass
class SchemaVersion:
    version_id: str
    schema_id: str
    version: int
    schema: Dict[str, Any]
    created_at: float = field(default_factory=time.time)
    description: str = ""
    is_active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id": self.version_id,
            "schema_id": self.schema_id,
            "version": self.version,
            "description": self.description,
            "is_active": self.is_active,
            "created_at": self.created_at,
        }


@dataclass
class ValidationResult:
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    schema_id: str = ""
    version: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "schema_id": self.schema_id,
            "version": self.version,
        }


class SchemaValidationError(Exception):
    pass


def _type_check(value: Any, expected: Union[str, List[str]]) -> bool:
    type_map = {
        "string": str, "number": (int, float), "integer": int,
        "boolean": bool, "null": type(None),
        "object": dict, "array": list,
    }
    types = [expected] if isinstance(expected, str) else expected
    for t in types:
        if t == "null" and value is None:
            return True
        if t != "null" and value is not None:
            expected_type = type_map.get(t)
            if expected_type and isinstance(value, expected_type):
                if t == "integer" and isinstance(value, bool):
                    continue
                return True
    return False


def _validate_schema(data: Any, schema: Dict[str, Any],
                     path: str = "$") -> List[str]:
    """Recursive JSON Schema validator (subset of draft-07)."""
    errors: List[str] = []

    if not isinstance(schema, dict):
        return errors

    # type check
    schema_type = schema.get("type")
    if schema_type:
        if not _type_check(data, schema_type):
            errors.append(f"{path}: expected type '{schema_type}', got {type(data).__name__}")
            return errors  # no point validating further

    # enum
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: value must be one of {schema['enum']}")

    # const
    if "const" in schema and data != schema["const"]:
        errors.append(f"{path}: value must be {schema['const']!r}")

    # string checks
    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            errors.append(f"{path}: string too short (min {schema['minLength']})")
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            errors.append(f"{path}: string too long (max {schema['maxLength']})")
        if "pattern" in schema and not re.search(schema["pattern"], data):
            errors.append(f"{path}: string does not match pattern {schema['pattern']!r}")
        if "format" in schema:
            fmt = schema["format"]
            if fmt == "email" and "@" not in data:
                errors.append(f"{path}: invalid email format")
            if fmt == "uuid":
                try: uuid.UUID(data)
                except ValueError: errors.append(f"{path}: invalid UUID format")

    # number checks
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append(f"{path}: {data} < minimum {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            errors.append(f"{path}: {data} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and data <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {data} must be > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and data >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {data} must be < {schema['exclusiveMaximum']}")
        if "multipleOf" in schema and data % schema["multipleOf"] != 0:
            errors.append(f"{path}: {data} not multiple of {schema['multipleOf']}")

    # object checks
    if isinstance(data, dict):
        required = schema.get("required", [])
        for req in required:
            if req not in data:
                errors.append(f"{path}: missing required property '{req}'")
        props = schema.get("properties", {})
        for key, val in data.items():
            if key in props:
                errors.extend(_validate_schema(val, props[key], f"{path}.{key}"))
        if "additionalProperties" in schema:
            ap = schema["additionalProperties"]
            if ap is False:
                extra = set(data.keys()) - set(props.keys())
                if extra:
                    errors.append(f"{path}: additional properties not allowed: {extra}")
        if "minProperties" in schema and len(data) < schema["minProperties"]:
            errors.append(f"{path}: too few properties")
        if "maxProperties" in schema and len(data) > schema["maxProperties"]:
            errors.append(f"{path}: too many properties")

    # array checks
    if isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            errors.append(f"{path}: array too short (min {schema['minItems']})")
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            errors.append(f"{path}: array too long (max {schema['maxItems']})")
        item_schema = schema.get("items")
        if item_schema and isinstance(item_schema, dict):
            for i, item in enumerate(data):
                errors.extend(_validate_schema(item, item_schema, f"{path}[{i}]"))
        if schema.get("uniqueItems"):
            seen = []
            for item in data:
                if item in seen:
                    errors.append(f"{path}: array items must be unique")
                    break
                seen.append(item)

    # allOf / anyOf / oneOf
    if "allOf" in schema:
        for sub in schema["allOf"]:
            errors.extend(_validate_schema(data, sub, path))
    if "anyOf" in schema:
        if not any(not _validate_schema(data, sub, path) for sub in schema["anyOf"]):
            errors.append(f"{path}: does not match any of the anyOf schemas")
    if "oneOf" in schema:
        matching = sum(1 for sub in schema["oneOf"]
                       if not _validate_schema(data, sub, path))
        if matching != 1:
            errors.append(f"{path}: must match exactly one of the oneOf schemas")

    return errors


class SchemaRegistry:
    """
    Manages JSON Schema versions with validation, compatibility, and migration.
    """

    def __init__(self, compat_mode: CompatMode = CompatMode.BACKWARD,
                 db_path: str = ":memory:"):
        self.compat_mode = compat_mode
        self._schemas: Dict[str, List[SchemaVersion]] = {}   # schema_id → versions
        self._migrations: Dict[Tuple[str, int, int], Callable] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._validate_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sr_schemas (
                version_id TEXT PRIMARY KEY, schema_id TEXT,
                version INTEGER, schema TEXT, description TEXT,
                is_active INTEGER, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS sr_validate_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_id TEXT, version INTEGER, valid INTEGER, ts REAL
            );
        """)
        self._db.commit()

    # ── REGISTRATION ──────────────────────────────────────────────────

    def register(self, schema_id: str, schema: Dict[str, Any],
                 description: str = "",
                 check_compat: bool = True) -> SchemaVersion:
        versions = self._schemas.get(schema_id, [])
        new_version = len(versions) + 1

        if check_compat and versions:
            old = versions[-1].schema
            compat_errors = self._check_compat(old, schema)
            if compat_errors:
                raise SchemaValidationError(
                    f"Schema incompatible: {'; '.join(compat_errors)}")

        vid = str(uuid.uuid4())[:8]
        sv  = SchemaVersion(version_id=vid, schema_id=schema_id,
                             version=new_version, schema=schema,
                             description=description)
        if schema_id not in self._schemas:
            self._schemas[schema_id] = []
        self._schemas[schema_id].append(sv)
        self._db.execute(
            "INSERT INTO sr_schemas VALUES (?,?,?,?,?,?,?)",
            (vid, schema_id, new_version, json.dumps(schema),
             description, 1, sv.created_at))
        self._db.commit()
        return sv

    def deactivate(self, schema_id: str, version: int):
        for sv in self._schemas.get(schema_id, []):
            if sv.version == version:
                sv.is_active = False
                self._db.execute(
                    "UPDATE sr_schemas SET is_active=0 WHERE version_id=?",
                    (sv.version_id,))
                self._db.commit()

    # ── COMPATIBILITY ─────────────────────────────────────────────────

    def _check_compat(self, old: Dict, new: Dict) -> List[str]:
        if self.compat_mode == CompatMode.NONE:
            return []
        errors = []
        old_req = set(old.get("required", []))
        new_req = set(new.get("required", []))
        old_props = set(old.get("properties", {}).keys())
        new_props = set(new.get("properties", {}).keys())

        if self.compat_mode in (CompatMode.BACKWARD, CompatMode.FULL):
            # New required fields not in old → breaks old writers
            added_required = new_req - old_req
            if added_required:
                errors.append(f"New required fields added: {added_required}")
        if self.compat_mode in (CompatMode.FORWARD, CompatMode.FULL):
            # Removed properties → breaks old readers
            removed = old_props - new_props
            if removed:
                errors.append(f"Properties removed: {removed}")
        return errors

    # ── VALIDATION ────────────────────────────────────────────────────

    def validate(self, schema_id: str, data: Any,
                 version: Optional[int] = None) -> ValidationResult:
        self._validate_count += 1
        sv = self._get_version(schema_id, version)
        if sv is None:
            return ValidationResult(valid=False,
                                    errors=[f"Schema '{schema_id}' not found"])
        errors = _validate_schema(data, sv.schema)
        result = ValidationResult(
            valid=len(errors) == 0,
            errors=errors, schema_id=schema_id, version=sv.version)
        self._db.execute(
            "INSERT INTO sr_validate_log (schema_id,version,valid,ts) VALUES (?,?,?,?)",
            (schema_id, sv.version, int(result.valid), time.time()))
        self._db.commit()
        return result

    def validate_strict(self, schema_id: str, data: Any) -> Any:
        """Validate and raise on failure."""
        result = self.validate(schema_id, data)
        if not result.valid:
            raise SchemaValidationError("; ".join(result.errors))
        return data

    # ── MIGRATION ─────────────────────────────────────────────────────

    def register_migration(self, schema_id: str,
                            from_version: int, to_version: int,
                            fn: Callable[[Any], Any]):
        self._migrations[(schema_id, from_version, to_version)] = fn

    def migrate(self, schema_id: str, data: Any,
                from_version: int, to_version: int) -> Any:
        """Migrate data through registered migration functions."""
        if from_version == to_version:
            return data
        step = 1 if to_version > from_version else -1
        current = data
        v = from_version
        while v != to_version:
            nv = v + step
            fn = self._migrations.get((schema_id, v, nv))
            if fn is None:
                raise KeyError(f"No migration from v{v} to v{nv} for '{schema_id}'")
            current = fn(current)
            v = nv
        return current

    # ── QUERY ─────────────────────────────────────────────────────────

    def _get_version(self, schema_id: str,
                      version: Optional[int] = None) -> Optional[SchemaVersion]:
        versions = self._schemas.get(schema_id, [])
        if not versions:
            return None
        if version is None:
            active = [sv for sv in versions if sv.is_active]
            return active[-1] if active else versions[-1]
        for sv in versions:
            if sv.version == version:
                return sv
        return None

    def get_schema(self, schema_id: str,
                   version: Optional[int] = None) -> Optional[Dict[str, Any]]:
        sv = self._get_version(schema_id, version)
        return sv.schema if sv else None

    def list_schemas(self) -> List[str]:
        return list(self._schemas.keys())

    def schema_versions(self, schema_id: str) -> List[Dict[str, Any]]:
        return [sv.to_dict() for sv in self._schemas.get(schema_id, [])]

    def latest_version(self, schema_id: str) -> int:
        versions = self._schemas.get(schema_id, [])
        return versions[-1].version if versions else 0

    def stats(self) -> Dict[str, Any]:
        total_versions = sum(len(v) for v in self._schemas.values())
        return {
            "schemas": len(self._schemas),
            "total_versions": total_versions,
            "validations": self._validate_count,
            "migrations": len(self._migrations),
            "compat_mode": self.compat_mode.value,
        }
