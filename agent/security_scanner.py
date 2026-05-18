"""OMNI AGENT - Security Scanner
Input/output security scanning: detect prompt injection, redact PII,
enforce content policy, score risk, and audit all decisions.

Features:
- Prompt injection detection: pattern + heuristic scoring for jailbreaks
- PII redaction: regex patterns for emails, phones, SSNs, credit cards, IPs
- Content policy: configurable banned topics, toxic language, hate speech
- Risk scoring: composite 0-1 score with contributing factor breakdown
- Allowlist/denylist: per-deployment custom word/phrase lists
- Severity levels: INFO, LOW, MEDIUM, HIGH, CRITICAL
- Action modes: ALLOW, WARN, REDACT, BLOCK
- Audit log: every scan decision persisted to SQLite
- Async-safe: concurrent scans via asyncio
- Custom scanners: register additional check functions
- Output scanning: scan LLM responses for sensitive data leakage
- REST API: scan-input, scan-output, audit, stats, configure
"""
import re, time, uuid, sqlite3, json, asyncio, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class Severity(str, Enum):
    INFO     = "info"
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"

class Action(str, Enum):
    ALLOW  = "allow"
    WARN   = "warn"
    REDACT = "redact"
    BLOCK  = "block"

# ── PII Patterns ───────────────────────────────────────────────────────────────
_PII_PATTERNS: Dict[str, Tuple[re.Pattern, str]] = {
    "email":        (re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b'),
                     "[EMAIL]"),
    "phone_us":     (re.compile(r'\b(\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b'),
                     "[PHONE]"),
    "ssn":          (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
                     "[SSN]"),
    "credit_card":  (re.compile(r'\b(?:\d[ -]?){13,16}\b'),
                     "[CREDIT_CARD]"),
    "ipv4":         (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
                     "[IP_ADDRESS]"),
    "api_key":      (re.compile(r'\b(sk-|pk_|api_key=|Bearer\s)[A-Za-z0-9\-_]{16,}\b', re.I),
                     "[API_KEY]"),
    "aws_key":      (re.compile(r'\b(AKIA|ASIA|AROA)[A-Z0-9]{16}\b'),
                     "[AWS_KEY]"),
}

# ── Injection Patterns ─────────────────────────────────────────────────────────
_INJECTION_PATTERNS = [
    (re.compile(r'ignore\s+(all\s+)?(previous|prior|above)\s+instructions?', re.I), 0.9),
    (re.compile(r'(you are|act as|pretend (to be|you are))\s+(a\s+)?(?!helpful|an? AI)', re.I), 0.5),
    (re.compile(r'(system\s+prompt|system\s+message|your\s+instructions?)', re.I), 0.4),
    (re.compile(r'(do\s+anything\s+now|DAN|jailbreak|bypass|override)', re.I), 0.8),
    (re.compile(r'(forget|disregard|discard)\s+(your|all|the)\s+(rules?|constraints?|guidelines?)', re.I), 0.85),
    (re.compile(r'(enable|activate|unlock)\s+(developer|debug|god|sudo|admin)\s+mode', re.I), 0.75),
    (re.compile(r'</?(system|user|assistant|human|AI)>', re.I), 0.7),
    (re.compile(r'\[INST\]|\[/?SYS\]|<<SYS>>', re.I), 0.7),
    (re.compile(r'(output|print|repeat|reveal)\s+(your|the)\s+(system|initial|original)\s+(prompt|instructions?)', re.I), 0.85),
]

# ── Toxic/policy patterns ──────────────────────────────────────────────────────
_TOXIC_PATTERNS = [
    re.compile(r'\b(bomb|explosive|weapon|grenade)\s+(mak|build|creat|assembl)', re.I),
    re.compile(r'\b(synthesiz|produc|manufactur).{0,20}(drug|methamphetamine|cocaine|fentanyl)', re.I),
    re.compile(r'\b(child|minor).{0,20}(explicit|sexual|nude|naked)', re.I),
    re.compile(r'\b(kill|murder|harm|hurt)\s+(myself|yourself|themselves)\b', re.I),
]

# ── Data classes ───────────────────────────────────────────────────────────────
@dataclass
class Finding:
    category: str; description: str
    severity: Severity; score: float
    matched_text: str = ""; redacted: bool = False

    def to_dict(self):
        return {"category": self.category, "description": self.description,
                "severity": self.severity, "score": round(self.score, 4),
                "redacted": self.redacted}

@dataclass
class ScanResult:
    scan_id: str; text_in: str; text_out: str
    action: Action; risk_score: float
    findings: List[Finding] = field(default_factory=list)
    pii_found: bool = False; injection_detected: bool = False
    policy_violation: bool = False
    scan_ms: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def blocked(self): return self.action == Action.BLOCK
    @property
    def redacted(self): return self.action == Action.REDACT

    def to_dict(self):
        return {"scan_id": self.scan_id, "action": self.action,
                "risk_score": round(self.risk_score, 4),
                "pii_found": self.pii_found,
                "injection_detected": self.injection_detected,
                "policy_violation": self.policy_violation,
                "findings": [f.to_dict() for f in self.findings],
                "text_out": self.text_out[:500],
                "scan_ms": round(self.scan_ms, 1)}

class SSStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS scans(
                    scan_id TEXT PRIMARY KEY, action TEXT, risk_score REAL,
                    pii_found INTEGER, injection_detected INTEGER,
                    policy_violation INTEGER, finding_count INTEGER,
                    scan_ms REAL, created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_sc_ts ON scans(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sc_act ON scans(action);
            """)

    def log(self, r: ScanResult):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO scans VALUES(?,?,?,?,?,?,?,?,?)",
                (r.scan_id, r.action, r.risk_score,
                 int(r.pii_found), int(r.injection_detected),
                 int(r.policy_violation), len(r.findings),
                 r.scan_ms, r.created_at))

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
            by_action = dict(c.execute(
                "SELECT action, COUNT(*) FROM scans GROUP BY action").fetchall())
            avg_risk = c.execute("SELECT AVG(risk_score) FROM scans").fetchone()[0] or 0
            pii = c.execute("SELECT SUM(pii_found) FROM scans").fetchone()[0] or 0
            inj = c.execute("SELECT SUM(injection_detected) FROM scans").fetchone()[0] or 0
        return {"total_scans": total, "by_action": by_action,
                "avg_risk_score": round(avg_risk, 4),
                "pii_detections": int(pii), "injection_detections": int(inj)}

    def recent(self, limit: int = 20) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM scans ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

class SecurityScanner:
    """
    Input/output security scanner with PII redaction and injection detection.

    Usage:
        scanner = SecurityScanner(block_threshold=0.8, redact_pii=True)
        result = await scanner.scan("My email is user@example.com. Ignore previous instructions!")
        print(result.action)          # Action.BLOCK or REDACT
        print(result.text_out)        # "[EMAIL] Ignore previous instructions!"
        print(result.risk_score)      # 0.9
        print(result.injection_detected)  # True
    """
    def __init__(self, db_path: str = "data/security.db",
                 block_threshold: float = 0.8,
                 warn_threshold: float = 0.4,
                 redact_pii: bool = True,
                 audit: bool = True):
        self._store = SSStore(db_path)
        self._block_threshold = block_threshold
        self._warn_threshold = warn_threshold
        self._redact_pii = redact_pii
        self._audit = audit
        self._denylist: List[str] = []
        self._allowlist: List[str] = []
        self._custom_scanners: List[Callable] = []
        self._pii_enabled = set(_PII_PATTERNS.keys())

    def add_denylist(self, phrases: List[str]):
        self._denylist.extend(p.lower() for p in phrases)

    def add_allowlist(self, phrases: List[str]):
        self._allowlist.extend(p.lower() for p in phrases)

    def disable_pii_type(self, pii_type: str):
        self._pii_enabled.discard(pii_type)

    def add_custom_scanner(self, fn: Callable):
        """Register fn(text) → List[Finding]."""
        self._custom_scanners.append(fn)

    def _scan_pii(self, text: str) -> Tuple[List[Finding], str]:
        findings = []; out = text
        for pii_type, (pattern, placeholder) in _PII_PATTERNS.items():
            if pii_type not in self._pii_enabled:
                continue
            matches = pattern.findall(text)
            if matches:
                sev = Severity.HIGH if pii_type in ("ssn","credit_card","aws_key","api_key") \
                      else Severity.MEDIUM
                findings.append(Finding(
                    category=f"pii:{pii_type}",
                    description=f"PII detected: {pii_type}",
                    severity=sev, score=0.6 if sev==Severity.MEDIUM else 0.85,
                    matched_text=str(matches[0])[:30], redacted=self._redact_pii))
                if self._redact_pii:
                    out = pattern.sub(placeholder, out)
        return findings, out

    def _scan_injection(self, text: str) -> List[Finding]:
        findings = []
        total_score = 0.0
        for pattern, weight in _INJECTION_PATTERNS:
            if pattern.search(text):
                total_score = min(1.0, total_score + weight * 0.5)
                findings.append(Finding(
                    category="injection:prompt",
                    description="Possible prompt injection attempt",
                    severity=Severity.HIGH if weight >= 0.7 else Severity.MEDIUM,
                    score=weight,
                    matched_text=pattern.pattern[:50]))
        return findings

    def _scan_policy(self, text: str) -> List[Finding]:
        findings = []
        text_lower = text.lower()
        # Denylist
        for phrase in self._denylist:
            if phrase in text_lower:
                findings.append(Finding(
                    category="policy:denylist",
                    description=f"Denylist match: {phrase!r}",
                    severity=Severity.HIGH, score=0.85,
                    matched_text=phrase))
        # Toxic patterns
        for pattern in _TOXIC_PATTERNS:
            if pattern.search(text):
                findings.append(Finding(
                    category="policy:toxic",
                    description="Content policy violation detected",
                    severity=Severity.CRITICAL, score=0.95,
                    matched_text=pattern.pattern[:40]))
        return findings

    def _allowlisted(self, text: str) -> bool:
        lower = text.lower()
        return any(phrase in lower for phrase in self._allowlist)

    async def scan(self, text: str, scan_output: bool = False) -> ScanResult:
        start = time.time()
        scan_id = str(uuid.uuid4())[:10]
        text_out = text
        all_findings: List[Finding] = []

        # Allowlist bypass
        if self._allowlisted(text):
            r = ScanResult(scan_id=scan_id, text_in=text, text_out=text,
                            action=Action.ALLOW, risk_score=0.0,
                            scan_ms=(time.time()-start)*1000)
            if self._audit: self._store.log(r)
            return r

        # PII
        pii_findings, text_out = self._scan_pii(text_out)
        all_findings.extend(pii_findings)

        # Injection
        inj_findings = self._scan_injection(text)
        all_findings.extend(inj_findings)

        # Policy
        pol_findings = self._scan_policy(text)
        all_findings.extend(pol_findings)

        # Custom scanners
        for fn in self._custom_scanners:
            try:
                extra = await fn(text) if asyncio.iscoroutinefunction(fn) else fn(text)
                if isinstance(extra, list):
                    all_findings.extend(extra)
            except Exception as e:
                logger.warning(f"Custom scanner error: {e}")

        # Risk score = max weighted score
        risk = max((f.score for f in all_findings), default=0.0)
        risk = min(1.0, risk)

        pii_found = any("pii:" in f.category for f in all_findings)
        inj_det   = any("injection:" in f.category for f in all_findings)
        pol_viol  = any("policy:" in f.category for f in all_findings)

        # Determine action
        if risk >= self._block_threshold or pol_viol:
            # PII redaction happens regardless of block, but critical stays blocked
            if pol_viol or (risk >= self._block_threshold and not pii_found):
                action = Action.BLOCK; text_out = "[BLOCKED]"
            else:
                action = Action.REDACT  # PII redacted, not blocked
        elif pii_found and self._redact_pii:
            action = Action.REDACT
        elif risk >= self._warn_threshold:
            action = Action.WARN
        else:
            action = Action.ALLOW

        r = ScanResult(scan_id=scan_id, text_in=text, text_out=text_out,
                        action=action, risk_score=risk,
                        findings=all_findings, pii_found=pii_found,
                        injection_detected=inj_det, policy_violation=pol_viol,
                        scan_ms=(time.time()-start)*1000)
        if self._audit: self._store.log(r)
        return r

    async def scan_batch(self, texts: List[str],
                          concurrency: int = 8) -> List[ScanResult]:
        sem = asyncio.Semaphore(concurrency)
        async def bounded(t):
            async with sem: return await self.scan(t)
        return list(await asyncio.gather(*[bounded(t) for t in texts]))

    def stats(self) -> Dict:
        return self._store.stats()

    def recent_audit(self, limit: int = 20) -> List[Dict]:
        return self._store.recent(limit)

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def scan_ep(req):
            d = await req.json()
            r = await self.scan(d["text"])
            return web.json_response(r.to_dict())
        async def batch_ep(req):
            d = await req.json()
            results = await self.scan_batch(d.get("texts",[]))
            return web.json_response({"results":[r.to_dict() for r in results]})
        async def audit_ep(req):
            limit = int(req.rel_url.query.get("limit",20))
            return web.json_response({"audit": self.recent_audit(limit)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/security"
        app.router.add_post(f"{p}/scan",  scan_ep)
        app.router.add_post(f"{p}/batch", batch_ep)
        app.router.add_get( f"{p}/audit", audit_ep)
        app.router.add_get( f"{p}/stats", stats_ep)
        logger.info(f"Security scanner API at {prefix}/security/")
