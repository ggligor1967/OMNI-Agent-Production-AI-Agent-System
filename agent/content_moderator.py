"""OMNI Agent — Content Moderator: multi-layer moderation, categories, confidence scores."""
from __future__ import annotations
import re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ViolationCategory(str, Enum):
    HATE_SPEECH     = "hate_speech"
    HARASSMENT      = "harassment"
    VIOLENCE        = "violence"
    SELF_HARM       = "self_harm"
    SEXUAL_CONTENT  = "sexual_content"
    SPAM            = "spam"
    MISINFORMATION  = "misinformation"
    PROFANITY       = "profanity"
    PERSONAL_INFO   = "personal_info"
    CUSTOM          = "custom"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"


class ModerationAction(str, Enum):
    ALLOW   = "allow"
    FLAG    = "flag"       # allow but mark for review
    REDACT  = "redact"     # remove matched content
    BLOCK   = "block"      # reject entirely
    REVIEW  = "review"     # send to human review queue


@dataclass
class ModerationRule:
    rule_id: str
    category: ViolationCategory
    severity: Severity
    action: ModerationAction
    pattern: Optional[str] = None     # regex
    keywords: List[str] = field(default_factory=list)
    min_confidence: float = 0.5       # minimum score to trigger
    enabled: bool = True
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "action": self.action.value,
            "enabled": self.enabled,
        }


@dataclass
class Violation:
    rule_id: str
    category: ViolationCategory
    severity: Severity
    action: ModerationAction
    confidence: float
    matched_text: str = ""
    start: int = 0
    end: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "action": self.action.value,
            "confidence": round(self.confidence, 3),
            "matched": self.matched_text[:60],
        }


@dataclass
class ModerationResult:
    content_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str = ""
    clean_text: str = ""          # after redaction
    violations: List[Violation] = field(default_factory=list)
    action: ModerationAction = ModerationAction.ALLOW
    categories: List[str] = field(default_factory=list)
    confidence: float = 0.0       # max confidence across violations
    duration_ms: float = 0.0
    ts: float = field(default_factory=time.time)

    @property
    def safe(self) -> bool:
        return self.action == ModerationAction.ALLOW

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content_id": self.content_id,
            "safe": self.safe,
            "action": self.action.value,
            "violations": [v.to_dict() for v in self.violations],
            "categories": self.categories,
            "confidence": round(self.confidence, 3),
            "duration_ms": round(self.duration_ms, 2),
        }


# ── BUILT-IN PATTERNS ─────────────────────────────────────────────────────────

_BUILTIN_RULES = [
    # Profanity (LOW - just flag)
    {"category": ViolationCategory.PROFANITY, "severity": Severity.LOW,
     "action": ModerationAction.FLAG,
     "keywords": ["damn", "crap", "hell", "ass"],
     "description": "Mild profanity"},

    # Personal info
    {"category": ViolationCategory.PERSONAL_INFO, "severity": Severity.HIGH,
     "action": ModerationAction.REDACT,
     "pattern": r"\b\d{3}-\d{2}-\d{4}\b",
     "description": "SSN pattern"},
    {"category": ViolationCategory.PERSONAL_INFO, "severity": Severity.MEDIUM,
     "action": ModerationAction.REDACT,
     "pattern": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
     "description": "Email address"},

    # Spam signals
    {"category": ViolationCategory.SPAM, "severity": Severity.MEDIUM,
     "action": ModerationAction.FLAG,
     "keywords": ["buy now", "click here", "free money", "limited time offer",
                  "act now", "guaranteed winner"],
     "description": "Spam keywords"},

    # Self-harm
    {"category": ViolationCategory.SELF_HARM, "severity": Severity.CRITICAL,
     "action": ModerationAction.BLOCK,
     "pattern": r"\b(?:kill\s+myself|end\s+my\s+life|want\s+to\s+die)\b",
     "description": "Self-harm language"},

    # Violence
    {"category": ViolationCategory.VIOLENCE, "severity": Severity.HIGH,
     "action": ModerationAction.BLOCK,
     "pattern": r"\b(?:i(?:'ll|'m going to)\s+(?:kill|hurt|attack)\s+(?:you|them|everyone))\b",
     "description": "Explicit violence threats"},

    # Harassment
    {"category": ViolationCategory.HARASSMENT, "severity": Severity.HIGH,
     "action": ModerationAction.BLOCK,
     "keywords": ["i will find you", "you will regret this", "watch your back"],
     "description": "Harassment phrases"},
]

_ACTION_PRIORITY = {
    ModerationAction.BLOCK:  5,
    ModerationAction.REVIEW: 4,
    ModerationAction.REDACT: 3,
    ModerationAction.FLAG:   2,
    ModerationAction.ALLOW:  1,
}


class ContentModerator:
    """
    Multi-layer content moderation engine:
    - Keyword + regex pattern matching
    - Configurable rules per category
    - Confidence scoring
    - Redaction of matched content
    - Custom ML scorer hooks
    - Review queue management
    - SQLite audit log
    """

    def __init__(self, db_path: str = ":memory:",
                 load_builtins: bool = True):
        self._rules: Dict[str, ModerationRule] = {}
        self._scorers: List[Callable[[str], List[Dict]]] = []
        self._review_queue: List[ModerationResult] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._mod_count = 0
        self._violation_count = 0
        if load_builtins:
            self._load_builtins()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS cm_results (
                content_id TEXT PRIMARY KEY, action TEXT, categories TEXT,
                confidence REAL, violation_count INTEGER, ts REAL
            );
            CREATE TABLE IF NOT EXISTS cm_review_queue (
                content_id TEXT PRIMARY KEY, text TEXT, action TEXT, ts REAL
            );
        """)
        self._db.commit()

    def _load_builtins(self):
        for rule_def in _BUILTIN_RULES:
            self.add_rule(
                category=rule_def["category"],
                severity=rule_def["severity"],
                action=rule_def["action"],
                pattern=rule_def.get("pattern"),
                keywords=rule_def.get("keywords", []),
                description=rule_def.get("description", ""),
            )

    # ── RULE MANAGEMENT ───────────────────────────────────────────────

    def add_rule(self, category: ViolationCategory,
                 severity: Severity,
                 action: ModerationAction,
                 pattern: Optional[str] = None,
                 keywords: Optional[List[str]] = None,
                 min_confidence: float = 0.8,
                 description: str = "",
                 rule_id: Optional[str] = None) -> ModerationRule:
        rid = rule_id or str(uuid.uuid4())[:8]
        rule = ModerationRule(
            rule_id=rid, category=category, severity=severity,
            action=action, pattern=pattern,
            keywords=list(keywords or []),
            min_confidence=min_confidence,
            description=description)
        self._rules[rid] = rule
        return rule

    def remove_rule(self, rule_id: str):
        self._rules.pop(rule_id, None)

    def disable_rule(self, rule_id: str):
        if rule_id in self._rules:
            self._rules[rule_id].enabled = False

    def enable_rule(self, rule_id: str):
        if rule_id in self._rules:
            self._rules[rule_id].enabled = True

    def add_scorer(self, fn: Callable[[str], List[Dict]]):
        """Add ML scorer: fn(text) → [{category, confidence, action?}]"""
        self._scorers.append(fn)

    # ── MODERATION ────────────────────────────────────────────────────

    def moderate(self, text: str,
                 context: Optional[Dict] = None) -> ModerationResult:
        t0 = time.time()
        self._mod_count += 1
        result = ModerationResult(text=text, clean_text=text)
        violations: List[Violation] = []
        working = text

        for rule in self._rules.values():
            if not rule.enabled:
                continue
            found, matched, start, end = self._match_rule(rule, text)
            if found:
                conf = self._confidence(rule, matched, text)
                if conf >= rule.min_confidence:
                    v = Violation(rule_id=rule.rule_id,
                                  category=rule.category,
                                  severity=rule.severity,
                                  action=rule.action,
                                  confidence=conf,
                                  matched_text=matched,
                                  start=start, end=end)
                    violations.append(v)
                    if rule.action == ModerationAction.REDACT and matched:
                        working = working.replace(matched,
                                                  f"[REDACTED:{rule.category.value}]")

        # Custom scorers
        for scorer in self._scorers:
            try:
                findings = scorer(text)
                for f in findings:
                    cat = ViolationCategory(f.get("category", "custom"))
                    conf = float(f.get("confidence", 0.5))
                    act  = ModerationAction(f.get("action", "flag"))
                    violations.append(Violation(
                        rule_id="scorer",
                        category=cat,
                        severity=Severity.MEDIUM,
                        action=act,
                        confidence=conf))
            except Exception:
                pass

        # Determine worst action
        worst = ModerationAction.ALLOW
        for v in violations:
            if _ACTION_PRIORITY[v.action] > _ACTION_PRIORITY[worst]:
                worst = v.action

        result.violations = violations
        result.action = worst
        result.clean_text = working
        result.categories = list({v.category.value for v in violations})
        result.confidence = max((v.confidence for v in violations), default=0.0)
        result.duration_ms = (time.time() - t0) * 1000
        self._violation_count += len(violations)

        self._db.execute(
            "INSERT OR REPLACE INTO cm_results VALUES (?,?,?,?,?,?)",
            (result.content_id, worst.value,
             ",".join(result.categories),
             result.confidence, len(violations), result.ts))
        self._db.commit()

        if worst == ModerationAction.REVIEW:
            self._review_queue.append(result)
            self._db.execute(
                "INSERT OR REPLACE INTO cm_review_queue VALUES (?,?,?,?)",
                (result.content_id, text[:500], worst.value, result.ts))
            self._db.commit()

        return result

    def _match_rule(self, rule: ModerationRule,
                     text: str) -> Tuple[bool, str, int, int]:
        text_lower = text.lower()
        if rule.pattern:
            m = re.search(rule.pattern, text, re.IGNORECASE)
            if m:
                return True, m.group(0), m.start(), m.end()
        for kw in rule.keywords:
            idx = text_lower.find(kw.lower())
            if idx >= 0:
                return True, text[idx:idx + len(kw)], idx, idx + len(kw)
        return False, "", 0, 0

    def _confidence(self, rule: ModerationRule,
                     matched: str, text: str) -> float:
        base = 0.85
        if rule.severity == Severity.CRITICAL:
            base = 0.95
        elif rule.severity == Severity.HIGH:
            base = 0.9
        elif rule.severity == Severity.LOW:
            base = 0.7
        return min(1.0, base)

    # ── BULK MODERATION ───────────────────────────────────────────────

    def moderate_batch(self, texts: List[str]) -> List[ModerationResult]:
        return [self.moderate(t) for t in texts]

    def is_safe(self, text: str) -> bool:
        return self.moderate(text).safe

    # ── REVIEW QUEUE ──────────────────────────────────────────────────

    def review_queue(self, limit: int = 50) -> List[ModerationResult]:
        return self._review_queue[-limit:]

    def clear_review_queue(self):
        self._review_queue.clear()
        self._db.execute("DELETE FROM cm_review_queue")
        self._db.commit()

    # ── AUDIT ─────────────────────────────────────────────────────────

    def audit_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT content_id,action,categories,confidence,violation_count,ts "
            "FROM cm_results ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"content_id": r[0], "action": r[1], "categories": r[2],
                 "confidence": r[3], "violations": r[4], "ts": r[5]}
                for r in rows]

    def category_breakdown(self) -> Dict[str, int]:
        rows = self._db.execute(
            "SELECT categories FROM cm_results WHERE categories != ''"
        ).fetchall()
        counts: Dict[str, int] = {}
        for (cats,) in rows:
            for c in cats.split(","):
                if c:
                    counts[c] = counts.get(c, 0) + 1
        return counts

    def stats(self) -> Dict[str, Any]:
        return {
            "moderated": self._mod_count,
            "violations_found": self._violation_count,
            "rules": len(self._rules),
            "review_queue_depth": len(self._review_queue),
            "category_breakdown": self.category_breakdown(),
        }
