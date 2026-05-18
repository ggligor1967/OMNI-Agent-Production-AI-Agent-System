"""Helpers for sanitized security event auditing."""
import hashlib
import re
from typing import Any, Callable, Dict

AuditCallback = Callable[[str, str, Dict[str, Any]], None]

_EXACT_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "bearer",
    "bootstrap_token",
    "code",
    "cookie",
    "jwt",
    "password",
    "raw_key",
    "secret",
    "source_code",
    "token",
}
_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_OMNI_KEY_PATTERN = re.compile(r"\bomni_[A-Za-z0-9_-]{12,}\b")


def _is_sensitive_key(key: str) -> bool:
    normalized = (key or "").strip().lower().replace("-", "_")
    if normalized in _EXACT_SENSITIVE_KEYS:
        return True
    return normalized.endswith(("_token", "_secret", "_password", "_api_key", "_jwt", "_cookie"))


def _redact_string(value: str) -> str:
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _JWT_PATTERN.sub("[REDACTED]", redacted)
    redacted = _OMNI_KEY_PATTERN.sub("[REDACTED]", redacted)
    if len(redacted) > 500:
        return redacted[:500] + "…"
    return redacted


def sanitize_audit_details(details: Any) -> Any:
    if isinstance(details, dict):
        sanitized: Dict[str, Any] = {}
        for key, value in details.items():
            if _is_sensitive_key(key):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_audit_details(value)
        return sanitized
    if isinstance(details, list):
        return [sanitize_audit_details(item) for item in details[:50]]
    if isinstance(details, tuple):
        return [sanitize_audit_details(item) for item in details[:50]]
    if isinstance(details, str):
        return _redact_string(details)
    return details


def build_memory_audit_callback(memory: Any) -> AuditCallback:
    def _callback(action: str, actor: str, details: Dict[str, Any]) -> None:
        memory.audit(
            action,
            actor=actor or "system",
            details=sanitize_audit_details(details or {}),
        )

    return _callback


def code_fingerprint(code: str) -> Dict[str, Any]:
    payload = code or ""
    return {
        "code_chars": len(payload),
        "code_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
    }
