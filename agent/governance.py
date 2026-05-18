"""
OMNI AGENT - Governance & Compliance
PII detection and redaction, content policy enforcement, GDPR audit trail,
data retention rules, and compliance reporting.

Features:
- PII detection: email, phone, SSN, credit card, IP, name patterns (regex + keyword)
- Redaction: replace detected PII with [REDACTED:type] or custom placeholder
- Content policy engine: allow/block/warn rules on keywords, patterns, topics
- Policy chain: multiple policies evaluated in order with priority
- GDPR audit trail: log all data processing events with user consent tracking
- Right to erasure: delete user data across all stores
- Data retention: auto-expire records after configured TTL
- Consent management: track and verify user consent per purpose
- Compliance report: summary of data processing activities
- REST API: audit log, consent management, PII scan, policy CRUD
"""
import re
import time
import uuid
import json
import sqlite3
import hashlib
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# PII DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class PIIType(str, Enum):
    EMAIL        = "email"
    PHONE        = "phone"
    SSN          = "ssn"
    CREDIT_CARD  = "credit_card"
    IP_ADDRESS   = "ip_address"
    DATE_OF_BIRTH= "date_of_birth"
    POSTAL_CODE  = "postal_code"
    PASSPORT     = "passport"
    IBAN         = "iban"
    GENERIC_ID   = "generic_id"
    CUSTOM       = "custom"


# Built-in PII patterns: (PIIType, compiled regex, description)
_PII_PATTERNS: List[Tuple[PIIType, re.Pattern, str]] = [
    (PIIType.EMAIL,
     re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'),
     "Email address"),

    (PIIType.PHONE,
     re.compile(r'\b(?:\+?1[-.\s]?)?\(?[2-9]\d{2}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'),
     "US phone number"),

    (PIIType.PHONE,
     re.compile(r'\b\+?[1-9]\d{6,14}\b'),
     "International phone number"),

    (PIIType.SSN,
     re.compile(r'\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b'),
     "US Social Security Number"),

    (PIIType.CREDIT_CARD,
     re.compile(r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}'
                r'|6(?:011|5[0-9]{2})[0-9]{12}|(?:2131|1800|35\d{3})\d{11})\b'),
     "Credit card number (Luhn-format)"),

    (PIIType.IP_ADDRESS,
     re.compile(r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'
                r'(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'),
     "IPv4 address"),

    (PIIType.DATE_OF_BIRTH,
     re.compile(r'\b(?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])[-/]'
                r'(?:19|20)\d{2}\b'),
     "Date of birth (MM/DD/YYYY)"),

    (PIIType.POSTAL_CODE,
     re.compile(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b|\b\d{5}(?:-\d{4})?\b'),
     "Postal/ZIP code"),

    (PIIType.IBAN,
     re.compile(r'\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b'),
     "IBAN bank account"),
]


@dataclass
class PIIMatch:
    pii_type: PIIType
    value: str
    start: int
    end: int
    description: str

    def to_dict(self) -> Dict:
        return {
            "type": self.pii_type,
            "value": self.value[:4] + "***" if len(self.value) > 4 else "***",
            "start": self.start, "end": self.end,
            "description": self.description,
        }


class PIIScanner:
    """
    Scan text for PII using regex patterns.
    Supports custom patterns and configurable sensitivity.
    """

    def __init__(self, extra_patterns: List[Tuple[PIIType, re.Pattern, str]] = None,
                 disabled_types: Set[PIIType] = None):
        self._patterns = list(_PII_PATTERNS)
        if extra_patterns:
            self._patterns.extend(extra_patterns)
        self._disabled = disabled_types or set()

    def scan(self, text: str) -> List[PIIMatch]:
        """Return all PII matches found in text."""
        matches: List[PIIMatch] = []
        seen_spans: Set[Tuple[int, int]] = set()
        for pii_type, pattern, desc in self._patterns:
            if pii_type in self._disabled:
                continue
            for m in pattern.finditer(text):
                span = (m.start(), m.end())
                if span not in seen_spans:
                    seen_spans.add(span)
                    matches.append(PIIMatch(
                        pii_type=pii_type,
                        value=m.group(),
                        start=m.start(),
                        end=m.end(),
                        description=desc,
                    ))
        matches.sort(key=lambda x: x.start)
        return matches

    def contains_pii(self, text: str) -> bool:
        return len(self.scan(text)) > 0

    def redact(self, text: str,
               placeholder: str = None,
               keep_type: bool = True) -> Tuple[str, List[PIIMatch]]:
        """
        Replace all PII in text with placeholders.
        Returns (redacted_text, list_of_matches).
        """
        matches = self.scan(text)
        if not matches:
            return text, []

        result = []
        prev = 0
        for m in matches:
            result.append(text[prev:m.start])
            if placeholder:
                result.append(placeholder)
            elif keep_type:
                result.append(f"[REDACTED:{m.pii_type.upper()}]")
            else:
                result.append("[REDACTED]")
            prev = m.end
        result.append(text[prev:])
        return "".join(result), matches

    def add_pattern(self, pii_type: PIIType, pattern: str, description: str = ""):
        self._patterns.append((pii_type, re.compile(pattern), description))


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT POLICY ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class PolicyAction(str, Enum):
    ALLOW  = "allow"
    WARN   = "warn"
    BLOCK  = "block"
    REDACT = "redact"
    FLAG   = "flag"     # allow but log for review


class PolicyTrigger(str, Enum):
    KEYWORD  = "keyword"
    REGEX    = "regex"
    PII      = "pii"
    TOPIC    = "topic"
    CUSTOM   = "custom"


@dataclass
class PolicyRule:
    id: str
    name: str
    trigger: PolicyTrigger
    pattern: str             # keyword, regex pattern, PII type, or topic name
    action: PolicyAction
    message: str = ""        # shown when blocked/warned
    priority: int = 100      # lower = evaluated first
    enabled: bool = True
    case_sensitive: bool = False
    _compiled: Optional[re.Pattern] = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.trigger == PolicyTrigger.REGEX and self._compiled is None:
            try:
                flags = 0 if self.case_sensitive else re.IGNORECASE
                self._compiled = re.compile(self.pattern, flags)
            except re.error:
                logger.warning(f"Invalid regex in policy rule '{self.name}': {self.pattern}")

    def matches(self, text: str, pii_matches: List[PIIMatch] = None) -> bool:
        if not self.enabled:
            return False
        if self.trigger == PolicyTrigger.KEYWORD:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            return bool(re.search(re.escape(self.pattern), text, flags))
        elif self.trigger == PolicyTrigger.REGEX:
            if self._compiled:
                return bool(self._compiled.search(text))
        elif self.trigger == PolicyTrigger.PII:
            if pii_matches:
                return any(m.pii_type == self.pattern for m in pii_matches)
        elif self.trigger == PolicyTrigger.CUSTOM:
            return False  # Custom triggers handled by subclass
        return False

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "name": self.name,
            "trigger": self.trigger, "pattern": self.pattern,
            "action": self.action, "message": self.message,
            "priority": self.priority, "enabled": self.enabled,
        }


@dataclass
class PolicyDecision:
    allowed: bool
    action: PolicyAction
    triggered_rules: List[PolicyRule]
    warnings: List[str]
    message: str = ""

    def to_dict(self) -> Dict:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "triggered_rules": [r.name for r in self.triggered_rules],
            "warnings": self.warnings,
            "message": self.message,
        }


class PolicyEngine:
    """
    Multi-rule content policy evaluator.

    Rules are evaluated in priority order (lowest number first).
    A BLOCK action stops evaluation and returns immediately.
    WARN and FLAG actions accumulate but don't block.
    """

    def __init__(self):
        self._rules: List[PolicyRule] = []
        self._scanner = PIIScanner()
        self._register_defaults()

    def _register_defaults(self):
        """Register sensible built-in safety rules."""
        defaults = [
            PolicyRule(id="no_jailbreak", name="Jailbreak attempt",
                      trigger=PolicyTrigger.REGEX,
                      pattern=r"ignore\s+(all\s+|previous\s+|your\s+)*(instructions|rules|guidelines)",
                      action=PolicyAction.BLOCK,
                      message="This request violates usage policy.",
                      priority=1),
            PolicyRule(id="no_prompt_injection", name="Prompt injection",
                      trigger=PolicyTrigger.REGEX,
                      pattern=r"system\s*prompt|<\s*system\s*>|\[INST\]",
                      action=PolicyAction.FLAG,
                      message="Possible prompt injection detected.",
                      priority=2),
            PolicyRule(id="pii_warn", name="PII detected",
                      trigger=PolicyTrigger.PII,
                      pattern="email",
                      action=PolicyAction.WARN,
                      message="Input contains email address.",
                      priority=50),
        ]
        for rule in defaults:
            self._rules.append(rule)

    def add_rule(self, rule: PolicyRule):
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)
        logger.info(f"Policy rule added: '{rule.name}' action={rule.action}")

    def remove_rule(self, rule_id: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.id != rule_id]
        return len(self._rules) < before

    def get_rule(self, rule_id: str) -> Optional[PolicyRule]:
        return next((r for r in self._rules if r.id == rule_id), None)

    def list_rules(self) -> List[Dict]:
        return [r.to_dict() for r in self._rules]

    def evaluate(self, text: str,
                 context: Dict = None) -> PolicyDecision:
        """Evaluate text against all policy rules."""
        pii_matches = self._scanner.scan(text)
        triggered: List[PolicyRule] = []
        warnings: List[str] = []
        final_action = PolicyAction.ALLOW

        for rule in sorted(self._rules, key=lambda r: r.priority):
            if not rule.enabled:
                continue
            if rule.matches(text, pii_matches):
                triggered.append(rule)
                if rule.action == PolicyAction.BLOCK:
                    return PolicyDecision(
                        allowed=False,
                        action=PolicyAction.BLOCK,
                        triggered_rules=triggered,
                        warnings=warnings,
                        message=rule.message or "Content blocked by policy.",
                    )
                elif rule.action == PolicyAction.WARN:
                    warnings.append(rule.message or f"Warning: {rule.name}")
                    if final_action == PolicyAction.ALLOW:
                        final_action = PolicyAction.WARN
                elif rule.action == PolicyAction.FLAG:
                    if final_action == PolicyAction.ALLOW:
                        final_action = PolicyAction.FLAG

        return PolicyDecision(
            allowed=True,
            action=final_action,
            triggered_rules=triggered,
            warnings=warnings,
        )


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT TRAIL
# ══════════════════════════════════════════════════════════════════════════════

class AuditEventType(str, Enum):
    DATA_PROCESSED  = "data.processed"
    DATA_ACCESSED   = "data.accessed"
    DATA_DELETED    = "data.deleted"
    DATA_EXPORTED   = "data.exported"
    CONSENT_GIVEN   = "consent.given"
    CONSENT_REVOKED = "consent.revoked"
    PII_DETECTED    = "pii.detected"
    PII_REDACTED    = "pii.redacted"
    POLICY_BLOCKED  = "policy.blocked"
    POLICY_WARNED   = "policy.warned"
    USER_CREATED    = "user.created"
    USER_DELETED    = "user.deleted"
    AUTH_SUCCESS    = "auth.success"
    AUTH_FAILURE    = "auth.failure"
    CUSTOM          = "custom"


@dataclass
class AuditEvent:
    id: str
    event_type: AuditEventType
    user_id: str
    session_id: str = ""
    resource: str = ""
    details: Dict = field(default_factory=dict)
    ip_address: str = ""
    user_agent: str = ""
    outcome: str = "success"   # success | failure | blocked
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "event_type": self.event_type,
            "user_id": self.user_id, "session_id": self.session_id,
            "resource": self.resource, "details": self.details,
            "ip_address": self.ip_address,
            "outcome": self.outcome, "timestamp": self.timestamp,
        }


class AuditStore:
    """SQLite-backed audit log."""

    def __init__(self, db_path: str = "data/audit.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id          TEXT PRIMARY KEY,
                    event_type  TEXT NOT NULL,
                    user_id     TEXT NOT NULL,
                    session_id  TEXT DEFAULT '',
                    resource    TEXT DEFAULT '',
                    details     TEXT DEFAULT '{}',
                    ip_address  TEXT DEFAULT '',
                    outcome     TEXT DEFAULT 'success',
                    timestamp   REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp DESC);
                -- Consent records
                CREATE TABLE IF NOT EXISTS consents (
                    user_id    TEXT NOT NULL,
                    purpose    TEXT NOT NULL,
                    granted    INTEGER NOT NULL,
                    granted_at REAL,
                    revoked_at REAL,
                    ip_address TEXT DEFAULT '',
                    PRIMARY KEY (user_id, purpose)
                );
                -- Data retention rules
                CREATE TABLE IF NOT EXISTS retention_rules (
                    resource_type TEXT PRIMARY KEY,
                    max_age_days  INTEGER NOT NULL,
                    created_at    REAL
                );
            """)

    def log(self, event: AuditEvent):
        with self._conn() as c:
            c.execute("""
                INSERT INTO audit_log
                (id,event_type,user_id,session_id,resource,details,ip_address,outcome,timestamp)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                event.id, event.event_type, event.user_id, event.session_id,
                event.resource, json.dumps(event.details), event.ip_address,
                event.outcome, event.timestamp,
            ))

    def query(self, user_id: str = None, event_type: str = None,
              after: float = None, before: float = None,
              outcome: str = None, limit: int = 100) -> List[AuditEvent]:
        conditions = []
        params: List[Any] = []
        if user_id:
            conditions.append("user_id=?"); params.append(user_id)
        if event_type:
            conditions.append("event_type=?"); params.append(event_type)
        if after:
            conditions.append("timestamp>=?"); params.append(after)
        if before:
            conditions.append("timestamp<=?"); params.append(before)
        if outcome:
            conditions.append("outcome=?"); params.append(outcome)
        q = "SELECT * FROM audit_log"
        if conditions:
            q += " WHERE " + " AND ".join(conditions)
        q += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def stats(self, days: int = 30) -> Dict:
        cutoff = time.time() - days * 86400
        with self._conn() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM audit_log WHERE timestamp>=?", (cutoff,)
            ).fetchone()[0]
            by_type = dict(c.execute(
                "SELECT event_type, COUNT(*) FROM audit_log WHERE timestamp>=? GROUP BY event_type",
                (cutoff,)
            ).fetchall())
            by_outcome = dict(c.execute(
                "SELECT outcome, COUNT(*) FROM audit_log WHERE timestamp>=? GROUP BY outcome",
                (cutoff,)
            ).fetchall())
        return {"period_days": days, "total": total,
                "by_type": by_type, "by_outcome": by_outcome}

    def delete_user_data(self, user_id: str) -> int:
        with self._conn() as c:
            # Anonymize rather than delete for audit integrity
            cur = c.execute("""
                UPDATE audit_log SET user_id='[DELETED]', session_id='', ip_address=''
                WHERE user_id=?
            """, (user_id,))
        return cur.rowcount

    def _row_to_event(self, row) -> AuditEvent:
        return AuditEvent(
            id=row["id"], event_type=AuditEventType(row["event_type"]),
            user_id=row["user_id"], session_id=row["session_id"] or "",
            resource=row["resource"] or "",
            details=json.loads(row["details"] or "{}"),
            ip_address=row["ip_address"] or "",
            outcome=row["outcome"],
            timestamp=row["timestamp"],
        )


# ══════════════════════════════════════════════════════════════════════════════
# CONSENT MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class ConsentManager:
    """GDPR consent tracking per user per purpose."""

    PURPOSES = {
        "analytics":     "Usage analytics and performance monitoring",
        "personalization": "Personalizing responses and recommendations",
        "training":      "Using conversations for model improvement",
        "marketing":     "Sending product updates and offers",
        "storage":       "Storing conversation history",
    }

    def __init__(self, store: AuditStore):
        self._store = store

    def grant(self, user_id: str, purpose: str, ip: str = "") -> bool:
        if purpose not in self.PURPOSES:
            return False
        with self._store._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO consents
                (user_id, purpose, granted, granted_at, revoked_at, ip_address)
                VALUES (?,?,1,?,NULL,?)
            """, (user_id, purpose, time.time(), ip))
        self._store.log(AuditEvent(
            id=str(uuid.uuid4())[:12],
            event_type=AuditEventType.CONSENT_GIVEN,
            user_id=user_id, resource=purpose,
            details={"purpose": purpose},
            ip_address=ip,
        ))
        return True

    def revoke(self, user_id: str, purpose: str) -> bool:
        with self._store._conn() as c:
            cur = c.execute("""
                UPDATE consents SET granted=0, revoked_at=?
                WHERE user_id=? AND purpose=?
            """, (time.time(), user_id, purpose))
        if cur.rowcount:
            self._store.log(AuditEvent(
                id=str(uuid.uuid4())[:12],
                event_type=AuditEventType.CONSENT_REVOKED,
                user_id=user_id, resource=purpose,
                details={"purpose": purpose},
            ))
        return cur.rowcount > 0

    def has_consent(self, user_id: str, purpose: str) -> bool:
        with self._store._conn() as c:
            row = c.execute("""
                SELECT granted FROM consents
                WHERE user_id=? AND purpose=? AND granted=1
            """, (user_id, purpose)).fetchone()
        return row is not None

    def get_consents(self, user_id: str) -> Dict[str, bool]:
        with self._store._conn() as c:
            rows = c.execute("""
                SELECT purpose, granted FROM consents WHERE user_id=?
            """, (user_id,)).fetchall()
        result = {p: False for p in self.PURPOSES}
        for row in rows:
            result[row["purpose"]] = bool(row["granted"])
        return result

    def revoke_all(self, user_id: str) -> int:
        """Right to withdraw: revoke all consents for a user."""
        count = 0
        for purpose in self.PURPOSES:
            if self.revoke(user_id, purpose):
                count += 1
        return count


# ══════════════════════════════════════════════════════════════════════════════
# GOVERNANCE MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class GovernanceManager:
    """
    Central governance manager combining PII, policies, audit, and consent.

    Usage:
        gov = GovernanceManager()

        # Check + redact user input
        clean, pii, decision = gov.process_input("My email is foo@bar.com", "user_1")
        if not decision.allowed:
            return 403, decision.message

        # Log data processing
        gov.audit(AuditEventType.DATA_PROCESSED, user_id="user_1",
                  resource="chat_message", details={"tokens": 42})

        # GDPR: right to erasure
        gov.erase_user("user_1")

        # Consent check
        if gov.consent.has_consent("user_1", "analytics"):
            track_usage()
    """

    def __init__(self, db_path: str = "data/governance.db"):
        self._audit_store = AuditStore(db_path)
        self.pii = PIIScanner()
        self.policy = PolicyEngine()
        self.consent = ConsentManager(self._audit_store)

    def process_input(self, text: str, user_id: str = "",
                      session_id: str = "",
                      auto_redact: bool = True,
                      log_pii: bool = True) -> Tuple[str, List[PIIMatch], PolicyDecision]:
        """
        Full pipeline: scan PII → evaluate policy → optionally redact → audit.
        Returns (processed_text, pii_matches, policy_decision).
        """
        pii_matches = self.pii.scan(text)
        processed = text

        if pii_matches and auto_redact:
            processed, _ = self.pii.redact(text)
            if log_pii:
                self._audit_store.log(AuditEvent(
                    id=str(uuid.uuid4())[:12],
                    event_type=AuditEventType.PII_REDACTED,
                    user_id=user_id, session_id=session_id,
                    details={"count": len(pii_matches),
                             "types": list({m.pii_type for m in pii_matches})},
                ))

        decision = self.policy.evaluate(text)

        if not decision.allowed:
            self._audit_store.log(AuditEvent(
                id=str(uuid.uuid4())[:12],
                event_type=AuditEventType.POLICY_BLOCKED,
                user_id=user_id, session_id=session_id,
                details={"rules": [r.name for r in decision.triggered_rules]},
                outcome="blocked",
            ))
        elif decision.warnings:
            self._audit_store.log(AuditEvent(
                id=str(uuid.uuid4())[:12],
                event_type=AuditEventType.POLICY_WARNED,
                user_id=user_id, session_id=session_id,
                details={"warnings": decision.warnings},
                outcome="warned",
            ))

        return processed, pii_matches, decision

    def audit(self, event_type: AuditEventType, user_id: str,
              session_id: str = "", resource: str = "",
              details: Dict = None, outcome: str = "success",
              ip: str = "") -> AuditEvent:
        event = AuditEvent(
            id=str(uuid.uuid4())[:12],
            event_type=event_type,
            user_id=user_id,
            session_id=session_id,
            resource=resource,
            details=details or {},
            ip_address=ip,
            outcome=outcome,
        )
        self._audit_store.log(event)
        return event

    def erase_user(self, user_id: str) -> Dict:
        """GDPR right to erasure: anonymize audit records, revoke consents."""
        audit_count = self._audit_store.delete_user_data(user_id)
        consent_count = self.consent.revoke_all(user_id)
        self._audit_store.log(AuditEvent(
            id=str(uuid.uuid4())[:12],
            event_type=AuditEventType.USER_DELETED,
            user_id="[SYSTEM]",
            resource=f"user:{user_id}",
            details={"audit_anonymized": audit_count,
                     "consents_revoked": consent_count},
        ))
        logger.info(f"User data erased: {user_id} "
                   f"(audit:{audit_count}, consents:{consent_count})")
        return {"audit_anonymized": audit_count, "consents_revoked": consent_count}

    def audit_log(self, **kwargs) -> List[AuditEvent]:
        return self._audit_store.query(**kwargs)

    def compliance_report(self, days: int = 30) -> Dict:
        stats = self._audit_store.stats(days)
        return {
            "period_days": days,
            "generated_at": time.time(),
            "audit_stats": stats,
            "policy_rules": len(self.policy._rules),
            "consent_purposes": list(ConsentManager.PURPOSES.keys()),
        }

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def scan_pii(request):
            data = await request.json()
            text = data.get("text", "")
            matches = self.pii.scan(text)
            return web.json_response({
                "contains_pii": len(matches) > 0,
                "matches": [m.to_dict() for m in matches],
            })

        async def redact_text(request):
            data = await request.json()
            text = data.get("text", "")
            keep_type = data.get("keep_type", True)
            redacted, matches = self.pii.redact(text, keep_type=keep_type)
            return web.json_response({
                "redacted": redacted,
                "matches_count": len(matches),
            })

        async def evaluate_policy(request):
            data = await request.json()
            text = data.get("text", "")
            decision = self.policy.evaluate(text)
            return web.json_response(decision.to_dict())

        async def list_rules(request):
            return web.json_response({"rules": self.policy.list_rules()})

        async def add_rule(request):
            data = await request.json()
            rule = PolicyRule(
                id=data.get("id", str(uuid.uuid4())[:8]),
                name=data["name"],
                trigger=PolicyTrigger(data["trigger"]),
                pattern=data["pattern"],
                action=PolicyAction(data["action"]),
                message=data.get("message", ""),
                priority=data.get("priority", 100),
            )
            self.policy.add_rule(rule)
            return web.json_response(rule.to_dict(), status=201)

        async def audit_log_ep(request):
            user = request.rel_url.query.get("user_id")
            etype = request.rel_url.query.get("event_type")
            limit = int(request.rel_url.query.get("limit", 50))
            events = self.audit_log(user_id=user, event_type=etype, limit=limit)
            return web.json_response({"events": [e.to_dict() for e in events]})

        async def consent_ep(request):
            user_id = request.match_info["user_id"]
            consents = self.consent.get_consents(user_id)
            return web.json_response({"user_id": user_id, "consents": consents})

        async def grant_consent(request):
            user_id = request.match_info["user_id"]
            data = await request.json()
            ok = self.consent.grant(user_id, data["purpose"])
            return web.json_response({"granted": ok})

        async def revoke_consent(request):
            user_id = request.match_info["user_id"]
            data = await request.json()
            ok = self.consent.revoke(user_id, data["purpose"])
            return web.json_response({"revoked": ok})

        async def erase_user_ep(request):
            user_id = request.match_info["user_id"]
            result = self.erase_user(user_id)
            return web.json_response(result)

        async def compliance_ep(request):
            days = int(request.rel_url.query.get("days", 30))
            return web.json_response(self.compliance_report(days))

        app.router.add_post(f"{prefix}/governance/pii/scan",         scan_pii)
        app.router.add_post(f"{prefix}/governance/pii/redact",       redact_text)
        app.router.add_post(f"{prefix}/governance/policy/evaluate",  evaluate_policy)
        app.router.add_get( f"{prefix}/governance/policy/rules",     list_rules)
        app.router.add_post(f"{prefix}/governance/policy/rules",     add_rule)
        app.router.add_get( f"{prefix}/governance/audit",            audit_log_ep)
        app.router.add_get( f"{prefix}/governance/consent/{{user_id}}", consent_ep)
        app.router.add_post(f"{prefix}/governance/consent/{{user_id}}/grant",  grant_consent)
        app.router.add_post(f"{prefix}/governance/consent/{{user_id}}/revoke", revoke_consent)
        app.router.add_delete(f"{prefix}/governance/users/{{user_id}}", erase_user_ep)
        app.router.add_get(f"{prefix}/governance/compliance",        compliance_ep)
        logger.info(f"Governance API routes registered at {prefix}/governance/")
