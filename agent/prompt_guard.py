"""OMNI Agent — Prompt Guard: input sanitization, injection detection and content safety."""
from __future__ import annotations
import hashlib, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class ThreatType(str, Enum):
    PROMPT_INJECTION   = "prompt_injection"
    JAILBREAK          = "jailbreak"
    PII_LEAK           = "pii_leak"
    TOXIC_CONTENT      = "toxic_content"
    EXCESSIVE_LENGTH   = "excessive_length"
    TEMPLATE_INJECTION = "template_injection"
    ENCODING_ATTACK    = "encoding_attack"
    REPETITION_ATTACK  = "repetition_attack"
    CUSTOM             = "custom"


class ActionOnThreat(str, Enum):
    BLOCK   = "block"
    REDACT  = "redact"
    WARN    = "warn"
    LOG     = "log"
    ALLOW   = "allow"


class SeverityLevel(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"


@dataclass
class ThreatRule:
    rule_id: str
    name: str
    threat_type: ThreatType
    severity: SeverityLevel = SeverityLevel.MEDIUM
    action: ActionOnThreat  = ActionOnThreat.BLOCK
    pattern: Optional[str]  = None    # regex
    keywords: List[str]     = field(default_factory=list)
    enabled: bool           = True
    description: str        = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "threat_type": self.threat_type.value,
            "severity": self.severity.value,
            "action": self.action.value,
            "enabled": self.enabled,
        }


@dataclass
class ScanResult:
    scan_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    input_hash: str = ""
    safe: bool = True
    threats: List[Dict[str, Any]] = field(default_factory=list)
    redacted_text: Optional[str] = None
    action_taken: ActionOnThreat = ActionOnThreat.ALLOW
    duration_ms: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "safe": self.safe,
            "threats": self.threats,
            "action_taken": self.action_taken.value,
            "duration_ms": round(self.duration_ms, 2),
        }


# ── BUILT-IN PATTERNS ─────────────────────────────────────────────────────────

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions?",
    r"disregard\s+(all\s+)?prior\s+instructions?",
    r"you\s+are\s+now\s+(?:a\s+)?(?:different|new|another)",
    r"forget\s+(?:everything|all|your\s+instructions?)",
    r"act\s+as\s+(?:if\s+you\s+(?:are|were)|a\s+)",
    r"new\s+instruction[s]?\s*:",
    r"system\s*:\s*you\s+(?:are|must|should|will)",
    r"\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>",
]

_JAILBREAK_PATTERNS = [
    r"DAN\s+mode",
    r"developer\s+mode",
    r"jailbreak",
    r"do\s+anything\s+now",
    r"bypass\s+(?:your\s+)?(?:safety|filter|restriction|guideline)",
    r"without\s+(?:any\s+)?restriction[s]?",
    r"pretend\s+(?:you\s+have\s+no|there\s+are\s+no)\s+(?:rule|limit|restriction)",
]

_PII_PATTERNS = [
    (r"\b\d{3}-\d{2}-\d{4}\b",            "SSN"),
    (r"\b\d{16}\b",                         "credit_card"),
    (r"\b\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}\b", "credit_card"),
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "email"),
    (r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b", "phone"),
    (r"\b(?:password|passwd|pwd)\s*[:=]\s*\S+", "password"),
    (r"\b(?:api[_-]?key|secret|token)\s*[:=]\s*[A-Za-z0-9+/=_-]{16,}", "api_key"),
]

_ENCODING_PATTERNS = [
    r"\\u[0-9a-fA-F]{4}",     # unicode escapes
    r"&#x?[0-9a-fA-F]+;",     # HTML entities
    r"(?:[A-Za-z0-9+/]{4}){10,}={0,2}",  # long base64
]


class PromptGuard:
    """
    Multi-layer prompt safety scanner:
    - Injection/jailbreak detection
    - PII detection and redaction
    - Custom rule engine
    - Length and repetition checks
    - Audit logging
    """

    def __init__(
        self,
        max_length: int = 10_000,
        pii_redact: bool = True,
        db_path: str = ":memory:",
    ):
        self.max_length = max_length
        self.pii_redact = pii_redact
        self._rules: Dict[str, ThreatRule] = {}
        self._custom_scanners: List[Callable[[str], Optional[Dict]]] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._scan_count = 0
        self._threat_count = 0
        self._load_builtin_rules()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS pg_scans (
                scan_id TEXT PRIMARY KEY, ts REAL, safe INTEGER,
                threat_count INTEGER, action TEXT, input_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS pg_threats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT, threat_type TEXT, severity TEXT,
                rule_name TEXT, excerpt TEXT
            );
        """)
        self._db.commit()

    def _load_builtin_rules(self):
        for i, pat in enumerate(_INJECTION_PATTERNS):
            self.add_rule(f"injection_{i}", f"Injection Pattern {i}",
                          ThreatType.PROMPT_INJECTION, SeverityLevel.HIGH,
                          ActionOnThreat.BLOCK, pattern=pat,
                          rule_id=f"builtin_inj_{i}")
        for i, pat in enumerate(_JAILBREAK_PATTERNS):
            self.add_rule(f"jailbreak_{i}", f"Jailbreak Pattern {i}",
                          ThreatType.JAILBREAK, SeverityLevel.CRITICAL,
                          ActionOnThreat.BLOCK, pattern=pat,
                          rule_id=f"builtin_jb_{i}")

    # ── RULE MANAGEMENT ───────────────────────────────────────────────

    def add_rule(self, name: str, description: str,
                 threat_type: ThreatType, severity: SeverityLevel,
                 action: ActionOnThreat,
                 pattern: Optional[str] = None,
                 keywords: Optional[List[str]] = None,
                 rule_id: Optional[str] = None) -> ThreatRule:
        rid = rule_id or str(uuid.uuid4())[:8]
        rule = ThreatRule(
            rule_id=rid, name=name, description=description,
            threat_type=threat_type, severity=severity,
            action=action, pattern=pattern, keywords=keywords or [])
        self._rules[rid] = rule
        return rule

    def remove_rule(self, rule_id: str):
        self._rules.pop(rule_id, None)

    def disable_rule(self, rule_id: str):
        if rule_id in self._rules:
            self._rules[rule_id].enabled = False

    def add_scanner(self, fn: Callable[[str], Optional[Dict]]):
        """Add custom scanner: fn(text) → dict with 'threat_type','severity' or None."""
        self._custom_scanners.append(fn)

    # ── SCANNING ──────────────────────────────────────────────────────

    def scan(self, text: str) -> ScanResult:
        t0 = time.time()
        self._scan_count += 1
        input_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        result = ScanResult(input_hash=input_hash)
        working_text = text

        # 1. Length check
        if len(text) > self.max_length:
            result.threats.append({
                "threat_type": ThreatType.EXCESSIVE_LENGTH.value,
                "severity": SeverityLevel.MEDIUM.value,
                "rule": "length_check",
                "action": ActionOnThreat.WARN.value,
                "excerpt": f"Length {len(text)} > {self.max_length}",
            })

        # 2. Repetition attack (>50% repeated chars)
        if len(text) > 100:
            char_counts = {}
            for c in text:
                char_counts[c] = char_counts.get(c, 0) + 1
            max_freq = max(char_counts.values()) / len(text)
            if max_freq > 0.5:
                result.threats.append({
                    "threat_type": ThreatType.REPETITION_ATTACK.value,
                    "severity": SeverityLevel.LOW.value,
                    "rule": "repetition_check",
                    "action": ActionOnThreat.WARN.value,
                    "excerpt": f"Top char frequency: {max_freq:.2%}",
                })

        # 3. PII detection and optional redaction
        for pattern, pii_type in _PII_PATTERNS:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                if self.pii_redact:
                    working_text = re.sub(pattern, f"[REDACTED:{pii_type}]",
                                         working_text, flags=re.IGNORECASE)
                result.threats.append({
                    "threat_type": ThreatType.PII_LEAK.value,
                    "severity": SeverityLevel.HIGH.value,
                    "rule": f"pii_{pii_type}",
                    "action": (ActionOnThreat.REDACT.value if self.pii_redact
                               else ActionOnThreat.WARN.value),
                    "excerpt": f"{pii_type}: {len(matches)} instance(s)",
                })

        # 4. Encoding attack check
        for pat in _ENCODING_PATTERNS:
            if re.search(pat, text):
                result.threats.append({
                    "threat_type": ThreatType.ENCODING_ATTACK.value,
                    "severity": SeverityLevel.MEDIUM.value,
                    "rule": "encoding_check",
                    "action": ActionOnThreat.WARN.value,
                    "excerpt": "Suspicious encoding detected",
                })
                break

        # 5. Custom rules
        text_lower = text.lower()
        for rule in self._rules.values():
            if not rule.enabled:
                continue
            matched = False
            excerpt = ""
            if rule.pattern:
                m = re.search(rule.pattern, text, re.IGNORECASE)
                if m:
                    matched = True
                    excerpt = m.group(0)[:80]
            if not matched and rule.keywords:
                for kw in rule.keywords:
                    if kw.lower() in text_lower:
                        matched = True
                        excerpt = kw
                        break
            if matched:
                result.threats.append({
                    "threat_type": rule.threat_type.value,
                    "severity": rule.severity.value,
                    "rule": rule.name,
                    "action": rule.action.value,
                    "excerpt": excerpt,
                })

        # 6. Custom scanners
        for scanner in self._custom_scanners:
            try:
                finding = scanner(text)
                if finding:
                    result.threats.append({**finding,
                                           "action": ActionOnThreat.WARN.value})
            except Exception:
                pass

        # Determine overall action (most severe wins)
        action_priority = {
            ActionOnThreat.BLOCK:  4,
            ActionOnThreat.REDACT: 3,
            ActionOnThreat.WARN:   2,
            ActionOnThreat.LOG:    1,
            ActionOnThreat.ALLOW:  0,
        }
        worst_action = ActionOnThreat.ALLOW
        for threat in result.threats:
            a = ActionOnThreat(threat["action"])
            if action_priority[a] > action_priority[worst_action]:
                worst_action = a

        result.action_taken = worst_action
        result.safe = worst_action not in (ActionOnThreat.BLOCK,)
        if working_text != text:
            result.redacted_text = working_text
        result.duration_ms = (time.time() - t0) * 1000

        if result.threats:
            self._threat_count += len(result.threats)

        self._db.execute(
            "INSERT INTO pg_scans VALUES (?,?,?,?,?,?)",
            (result.scan_id, result.ts, int(result.safe),
             len(result.threats), worst_action.value, input_hash))
        for threat in result.threats:
            self._db.execute(
                "INSERT INTO pg_threats (scan_id,threat_type,severity,rule_name,excerpt) "
                "VALUES (?,?,?,?,?)",
                (result.scan_id, threat["threat_type"], threat["severity"],
                 threat.get("rule", ""), threat.get("excerpt", "")[:100]))
        self._db.commit()
        return result

    def is_safe(self, text: str) -> bool:
        return self.scan(text).safe

    def sanitize(self, text: str) -> str:
        """Scan and return redacted text if PII found, else original (or blocked → '')."""
        result = self.scan(text)
        if result.action_taken == ActionOnThreat.BLOCK:
            return ""
        return result.redacted_text or text

    # ── AUDIT ─────────────────────────────────────────────────────────

    def scan_log(self, limit: int = 50, threats_only: bool = False) -> List[Dict]:
        q = "SELECT scan_id,ts,safe,threat_count,action FROM pg_scans"
        if threats_only:
            q += " WHERE threat_count>0"
        q += " ORDER BY ts DESC LIMIT ?"
        rows = self._db.execute(q, (limit,)).fetchall()
        return [{"scan_id": r[0], "ts": r[1], "safe": bool(r[2]),
                 "threat_count": r[3], "action": r[4]} for r in rows]

    def threat_breakdown(self) -> Dict[str, int]:
        rows = self._db.execute(
            "SELECT threat_type, COUNT(*) FROM pg_threats GROUP BY threat_type"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def stats(self) -> Dict[str, Any]:
        return {
            "scans": self._scan_count,
            "threats_found": self._threat_count,
            "rules": len(self._rules),
            "custom_scanners": len(self._custom_scanners),
            "threat_breakdown": self.threat_breakdown(),
        }
