"""OMNI Agent — Feedback Analyzer: LLM quality feedback collection, scoring and trend analysis."""
from __future__ import annotations
import json, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class FeedbackType(str, Enum):
    THUMBS_UP   = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"
    RATING      = "rating"        # numeric 1–5
    FLAG        = "flag"          # moderation flag
    CORRECTION  = "correction"    # user provided correct answer
    ANNOTATION  = "annotation"    # free-form label


class FeedbackDimension(str, Enum):
    ACCURACY    = "accuracy"
    HELPFULNESS = "helpfulness"
    SAFETY      = "safety"
    FLUENCY     = "fluency"
    RELEVANCE   = "relevance"
    CREATIVITY  = "creativity"


@dataclass
class FeedbackEntry:
    feedback_id: str
    response_id: str            # ID of the LLM response being rated
    feedback_type: FeedbackType
    value: Any                  # bool for thumbs, float for rating, str for correction
    dimensions: Dict[FeedbackDimension, float] = field(default_factory=dict)
    comment: str = ""
    user_id: str = "anonymous"
    model_id: str = ""
    session_id: str = ""
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def numeric_score(self) -> Optional[float]:
        if self.feedback_type == FeedbackType.THUMBS_UP:
            return 1.0 if self.value else 0.0
        if self.feedback_type == FeedbackType.THUMBS_DOWN:
            return 0.0
        if self.feedback_type == FeedbackType.RATING:
            return float(self.value) / 5.0  # normalise to 0–1
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "response_id": self.response_id,
            "type": self.feedback_type.value,
            "value": self.value,
            "user_id": self.user_id,
            "model_id": self.model_id,
            "comment": self.comment[:100],
            "ts": self.ts,
            "numeric_score": self.numeric_score,
        }


@dataclass
class TrendPoint:
    ts: float
    avg_score: float
    count: int
    model_id: str = ""


class FeedbackAnalyzer:
    """
    Collects, stores, and analyses human/automated feedback on LLM responses.
    Supports: multi-dim scoring, trend tracking, model comparison, anomaly detection.
    """

    def __init__(self, db_path: str = ":memory:",
                 anomaly_threshold: float = 2.0):
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._anomaly_threshold = anomaly_threshold   # std devs for anomaly
        self._init_db()
        self._hooks: List[Callable] = []

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS fa_feedback (
                feedback_id TEXT PRIMARY KEY, response_id TEXT,
                feedback_type TEXT, value TEXT, dimensions TEXT,
                comment TEXT, user_id TEXT, model_id TEXT,
                session_id TEXT, ts REAL, metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS fa_aggregates (
                model_id TEXT, window TEXT,
                avg_score REAL, count INTEGER, ts REAL
            );
        """)
        self._db.commit()

    # ── SUBMIT ────────────────────────────────────────────────────────

    def submit(self, response_id: str,
               feedback_type: FeedbackType,
               value: Any,
               dimensions: Optional[Dict[FeedbackDimension, float]] = None,
               comment: str = "",
               user_id: str = "anonymous",
               model_id: str = "",
               session_id: str = "",
               metadata: Optional[Dict] = None) -> FeedbackEntry:
        entry = FeedbackEntry(
            feedback_id=str(uuid.uuid4()),
            response_id=response_id,
            feedback_type=feedback_type,
            value=value,
            dimensions=dimensions or {},
            comment=comment,
            user_id=user_id,
            model_id=model_id,
            session_id=session_id,
            metadata=metadata or {},
        )
        self._db.execute(
            "INSERT INTO fa_feedback VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (entry.feedback_id, response_id, feedback_type.value,
             json.dumps(value), json.dumps({k.value: v for k, v in (dimensions or {}).items()}),
             comment, user_id, model_id, session_id, entry.ts,
             json.dumps(metadata or {})))
        self._db.commit()
        for hook in self._hooks:
            try: hook(entry)
            except Exception: pass
        return entry

    def submit_thumbs(self, response_id: str, positive: bool,
                      **kwargs) -> FeedbackEntry:
        return self.submit(response_id,
                           FeedbackType.THUMBS_UP if positive else FeedbackType.THUMBS_DOWN,
                           positive, **kwargs)

    def submit_rating(self, response_id: str, rating: float, **kwargs) -> FeedbackEntry:
        rating = max(1.0, min(5.0, float(rating)))
        return self.submit(response_id, FeedbackType.RATING, rating, **kwargs)

    def submit_correction(self, response_id: str, correct_text: str,
                          **kwargs) -> FeedbackEntry:
        return self.submit(response_id, FeedbackType.CORRECTION,
                           correct_text, **kwargs)

    # ── QUERY ─────────────────────────────────────────────────────────

    def get(self, feedback_id: str) -> Optional[FeedbackEntry]:
        row = self._db.execute(
            "SELECT * FROM fa_feedback WHERE feedback_id=?",
            (feedback_id,)).fetchone()
        return self._row_to_entry(row) if row else None

    def for_response(self, response_id: str) -> List[FeedbackEntry]:
        rows = self._db.execute(
            "SELECT * FROM fa_feedback WHERE response_id=? ORDER BY ts",
            (response_id,)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def for_model(self, model_id: str, limit: int = 100) -> List[FeedbackEntry]:
        rows = self._db.execute(
            "SELECT * FROM fa_feedback WHERE model_id=? ORDER BY ts DESC LIMIT ?",
            (model_id, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def _row_to_entry(self, r) -> FeedbackEntry:
        dims_raw = json.loads(r[4])
        dims = {FeedbackDimension(k): v for k, v in dims_raw.items()}
        return FeedbackEntry(
            feedback_id=r[0], response_id=r[1],
            feedback_type=FeedbackType(r[2]),
            value=json.loads(r[3]),
            dimensions=dims,
            comment=r[5], user_id=r[6], model_id=r[7],
            session_id=r[8], ts=r[9],
            metadata=json.loads(r[10]))

    # ── ANALYSIS ──────────────────────────────────────────────────────

    def avg_score(self, model_id: Optional[str] = None,
                  feedback_type: Optional[FeedbackType] = None,
                  since_ts: float = 0.0) -> Optional[float]:
        rows = self._fetch_scores(model_id, feedback_type, since_ts)
        scores = [s for s in rows if s is not None]
        return sum(scores) / len(scores) if scores else None

    def _fetch_scores(self, model_id: Optional[str],
                      feedback_type: Optional[FeedbackType],
                      since_ts: float) -> List[Optional[float]]:
        q = "SELECT feedback_type, value FROM fa_feedback WHERE ts>?"
        params: List[Any] = [since_ts]
        if model_id:
            q += " AND model_id=?"; params.append(model_id)
        if feedback_type:
            q += " AND feedback_type=?"; params.append(feedback_type.value)
        rows = self._db.execute(q, params).fetchall()
        results = []
        for ft, val in rows:
            e_type = FeedbackType(ft)
            v = json.loads(val)
            if e_type == FeedbackType.THUMBS_UP:
                results.append(1.0 if v else 0.0)
            elif e_type == FeedbackType.THUMBS_DOWN:
                results.append(0.0)
            elif e_type == FeedbackType.RATING:
                results.append(float(v) / 5.0)
        return results

    def thumbs_ratio(self, model_id: Optional[str] = None,
                     since_ts: float = 0.0) -> Dict[str, float]:
        q = ("SELECT feedback_type, value FROM fa_feedback "
             "WHERE feedback_type IN (?,?) AND ts>?")
        params: List[Any] = [FeedbackType.THUMBS_UP.value,
                              FeedbackType.THUMBS_DOWN.value, since_ts]
        if model_id:
            q += " AND model_id=?"; params.append(model_id)
        rows = self._db.execute(q, params).fetchall()
        pos = sum(1 for ft, v in rows
                  if ft == FeedbackType.THUMBS_UP.value and json.loads(v))
        neg = len(rows) - pos
        total = len(rows)
        return {"positive": pos / total if total > 0 else 0.0,
                "negative": neg / total if total > 0 else 0.0,
                "total": total}

    def dimension_scores(self, model_id: Optional[str] = None,
                         since_ts: float = 0.0) -> Dict[str, float]:
        q = "SELECT dimensions FROM fa_feedback WHERE ts>?"
        params: List[Any] = [since_ts]
        if model_id:
            q += " AND model_id=?"; params.append(model_id)
        rows = self._db.execute(q, params).fetchall()
        accum: Dict[str, List[float]] = {}
        for (dims_json,) in rows:
            dims = json.loads(dims_json)
            for k, v in dims.items():
                accum.setdefault(k, []).append(float(v))
        return {k: sum(vs) / len(vs) for k, vs in accum.items() if vs}

    def trend(self, model_id: Optional[str] = None,
              bucket_s: float = 3600.0,
              since_ts: float = 0.0) -> List[TrendPoint]:
        """Bucket feedback scores into time windows."""
        q = "SELECT ts, feedback_type, value, model_id FROM fa_feedback WHERE ts>?"
        params: List[Any] = [since_ts]
        if model_id:
            q += " AND model_id=?"; params.append(model_id)
        rows = self._db.execute(q + " ORDER BY ts", params).fetchall()
        buckets: Dict[float, List[float]] = {}
        bucket_model: Dict[float, str] = {}
        for ts, ft, val, mid in rows:
            e_type = FeedbackType(ft)
            v = json.loads(val)
            score: Optional[float] = None
            if e_type == FeedbackType.THUMBS_UP:
                score = 1.0 if v else 0.0
            elif e_type == FeedbackType.THUMBS_DOWN:
                score = 0.0
            elif e_type == FeedbackType.RATING:
                score = float(v) / 5.0
            if score is not None:
                bk = math.floor(ts / bucket_s) * bucket_s
                buckets.setdefault(bk, []).append(score)
                bucket_model[bk] = mid
        return [TrendPoint(ts=bk,
                           avg_score=sum(vs) / len(vs),
                           count=len(vs),
                           model_id=bucket_model.get(bk, ""))
                for bk, vs in sorted(buckets.items())]

    def compare_models(self, model_ids: List[str],
                       since_ts: float = 0.0) -> Dict[str, Optional[float]]:
        return {mid: self.avg_score(model_id=mid, since_ts=since_ts)
                for mid in model_ids}

    def anomalies(self, model_id: Optional[str] = None,
                  since_ts: float = 0.0) -> List[FeedbackEntry]:
        """Return entries whose numeric score deviates significantly from mean."""
        scores = self._fetch_scores(model_id, None, since_ts)
        scores_clean = [s for s in scores if s is not None]
        if len(scores_clean) < 3:
            return []
        mean  = sum(scores_clean) / len(scores_clean)
        var   = sum((s - mean) ** 2 for s in scores_clean) / len(scores_clean)
        std   = math.sqrt(var) if var > 0 else 0.0
        if std == 0:
            return []
        q = "SELECT * FROM fa_feedback WHERE ts>?"
        params: List[Any] = [since_ts]
        if model_id:
            q += " AND model_id=?"; params.append(model_id)
        rows = self._db.execute(q, params).fetchall()
        result = []
        for r in rows:
            entry = self._row_to_entry(r)
            score = entry.numeric_score
            if score is not None and abs(score - mean) > self._anomaly_threshold * std:
                result.append(entry)
        return result

    def corrections(self, model_id: Optional[str] = None) -> List[FeedbackEntry]:
        q = "SELECT * FROM fa_feedback WHERE feedback_type=?"
        params: List[Any] = [FeedbackType.CORRECTION.value]
        if model_id:
            q += " AND model_id=?"; params.append(model_id)
        rows = self._db.execute(q, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_feedback(self, fn: Callable[[FeedbackEntry], None]):
        self._hooks.append(fn)

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        total = self._db.execute("SELECT COUNT(*) FROM fa_feedback").fetchone()[0]
        by_type = {}
        for row in self._db.execute(
                "SELECT feedback_type, COUNT(*) FROM fa_feedback GROUP BY feedback_type"
        ).fetchall():
            by_type[row[0]] = row[1]
        models = self._db.execute(
            "SELECT COUNT(DISTINCT model_id) FROM fa_feedback").fetchone()[0]
        return {
            "total_feedback": total,
            "by_type": by_type,
            "unique_models": models,
            "overall_avg": self.avg_score(),
        }
