"""OMNI AGENT - Feedback Collector
Collect, store, and aggregate user feedback: ratings, thumbs, free text,
per-response metrics, and trend analysis.

Features:
- Feedback types: RATING (1-5), THUMBS (up/down), TEXT, MULTI_CHOICE, COMPOSITE
- Item-level tracking: attach feedback to any response/item id
- Aggregation: avg rating, thumbs ratio, sentiment word counts per item
- Trend analysis: rolling window averages, time-series by day/hour
- Tag-based filtering: group feedback by tag or session
- Sentiment heuristic: positive/negative word list scoring on text feedback
- Threshold alerts: fire hook when avg rating drops below threshold
- Deduplication: one feedback per (user_id, item_id, feedback_type) unless allow_multiple
- Export: JSON dump of all feedback with aggregations
- SQLite persistence: all feedback entries
- REST API: submit, aggregate, trends, export, stats
"""
import json, re, sqlite3, time, uuid, logging
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class FeedbackType(str, Enum):
    RATING       = "rating"
    THUMBS       = "thumbs"
    TEXT         = "text"
    MULTI_CHOICE = "multi_choice"
    COMPOSITE    = "composite"

_POS_WORDS = frozenset([
    "good","great","excellent","helpful","love","perfect","amazing","clear",
    "accurate","fast","easy","useful","correct","best","nice","thanks","thank"
])
_NEG_WORDS = frozenset([
    "bad","wrong","slow","confusing","incorrect","useless","hate","poor",
    "broken","unclear","fail","error","worst","terrible","unhelpful","boring"
])

def _sentiment(text: str) -> float:
    words = re.findall(r'\b[a-z]+\b', text.lower())
    pos = sum(1 for w in words if w in _POS_WORDS)
    neg = sum(1 for w in words if w in _NEG_WORDS)
    total = pos + neg
    return round((pos - neg) / max(1, total), 4) if total > 0 else 0.0

@dataclass
class FeedbackEntry:
    id: str; item_id: str
    feedback_type: FeedbackType
    user_id: str = "anonymous"
    session_id: str = ""
    rating: Optional[float] = None    # 1-5 for RATING
    thumbs: Optional[bool] = None     # True=up, False=down
    text: str = ""                    # free text
    choice: str = ""                  # MULTI_CHOICE selection
    choices: List[str] = field(default_factory=list)  # available options
    tags: List[str] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)
    sentiment: float = 0.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "item_id": self.item_id,
                "type": self.feedback_type.value,
                "user_id": self.user_id, "session_id": self.session_id,
                "rating": self.rating, "thumbs": self.thumbs,
                "text": self.text[:300], "choice": self.choice,
                "tags": self.tags, "sentiment": self.sentiment,
                "created_at": round(self.created_at, 1)}

@dataclass
class FeedbackAggregate:
    item_id: str
    total: int = 0
    avg_rating: Optional[float] = None
    thumbs_up: int = 0; thumbs_down: int = 0
    thumbs_ratio: float = 0.0
    avg_sentiment: float = 0.0
    text_count: int = 0
    latest_texts: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"item_id": self.item_id, "total": self.total,
                "avg_rating": round(self.avg_rating, 3) if self.avg_rating else None,
                "thumbs_up": self.thumbs_up, "thumbs_down": self.thumbs_down,
                "thumbs_ratio": round(self.thumbs_ratio, 4),
                "avg_sentiment": round(self.avg_sentiment, 4),
                "text_count": self.text_count,
                "latest_texts": self.latest_texts[:3]}

class FCStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS feedback(
                    id TEXT PRIMARY KEY, item_id TEXT,
                    feedback_type TEXT, user_id TEXT DEFAULT 'anonymous',
                    session_id TEXT DEFAULT '',
                    rating REAL, thumbs INTEGER,
                    text TEXT DEFAULT '', choice TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    sentiment REAL DEFAULT 0,
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_fb_item ON feedback(item_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_fb_user ON feedback(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_fb_time ON feedback(created_at DESC);
            """)

    def save(self, fb: FeedbackEntry):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (fb.id, fb.item_id, fb.feedback_type.value,
                 fb.user_id, fb.session_id,
                 fb.rating, 1 if fb.thumbs is True else (0 if fb.thumbs is False else None),
                 fb.text[:500], fb.choice,
                 json.dumps(fb.tags), fb.sentiment, fb.created_at))

    def exists(self, user_id: str, item_id: str, fb_type: str) -> bool:
        with self._conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM feedback WHERE user_id=? AND item_id=? "
                "AND feedback_type=?", (user_id, item_id, fb_type)).fetchone()[0]
        return n > 0

    def get_for_item(self, item_id: str) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM feedback WHERE item_id=? ORDER BY created_at DESC",
                (item_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_recent(self, hours: float = 24, tag: str = None) -> List[Dict]:
        cutoff = time.time() - hours * 3600
        with self._conn() as c:
            if tag:
                rows = c.execute(
                    "SELECT * FROM feedback WHERE created_at>=? AND tags LIKE ? "
                    "ORDER BY created_at DESC",
                    (cutoff, f'%"{tag}"%')).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM feedback WHERE created_at>=? ORDER BY created_at DESC",
                    (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def time_series(self, bucket: str = "hour",
                     item_id: str = None, days: int = 7) -> List[Dict]:
        cutoff = time.time() - days * 86400
        fmt = "%Y-%m-%d %H" if bucket == "hour" else "%Y-%m-%d"
        with self._conn() as c:
            if item_id:
                rows = c.execute(
                    "SELECT strftime(?, datetime(created_at, 'unixepoch')) AS bucket, "
                    "COUNT(*) as count, AVG(rating) as avg_rating "
                    "FROM feedback WHERE created_at>=? AND item_id=? "
                    "GROUP BY bucket ORDER BY bucket",
                    (fmt, cutoff, item_id)).fetchall()
            else:
                rows = c.execute(
                    "SELECT strftime(?, datetime(created_at, 'unixepoch')) AS bucket, "
                    "COUNT(*) as count, AVG(rating) as avg_rating "
                    "FROM feedback WHERE created_at>=? "
                    "GROUP BY bucket ORDER BY bucket",
                    (fmt, cutoff)).fetchall()
        return [{"bucket": r["bucket"], "count": r["count"],
                  "avg_rating": round(r["avg_rating"], 3) if r["avg_rating"] else None}
                 for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            n  = c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            nr = c.execute(
                "SELECT COUNT(*) FROM feedback WHERE feedback_type='rating'").fetchone()[0]
            nt = c.execute(
                "SELECT COUNT(*) FROM feedback WHERE feedback_type='thumbs'").fetchone()[0]
            avg = c.execute(
                "SELECT AVG(rating) FROM feedback WHERE rating IS NOT NULL"
            ).fetchone()[0] or 0
        return {"total": n, "ratings": nr, "thumbs": nt,
                "overall_avg_rating": round(avg, 3)}

class FeedbackCollector:
    """
    Feedback collection and aggregation for LLM responses.

    Usage:
        fc = FeedbackCollector()

        # Submit feedback
        fc.submit("resp-42", FeedbackType.RATING, rating=4.0, user_id="alice")
        fc.submit("resp-42", FeedbackType.THUMBS, thumbs=True, user_id="bob")
        fc.submit("resp-42", FeedbackType.TEXT, text="Very helpful!", user_id="carol")

        # Aggregate
        agg = fc.aggregate("resp-42")
        print(agg.avg_rating, agg.thumbs_ratio)

        # Alerts
        fc.on_low_rating(threshold=3.0,
                          fn=lambda item_id, avg: print(f"Low rating: {item_id} = {avg}"))
    """
    def __init__(self, db_path: str = "data/feedback.db",
                 allow_multiple: bool = False):
        self._store = FCStore(db_path)
        self._hooks: Dict[str, List[Callable]] = {"low_rating": [], "any": []}
        self.allow_multiple = allow_multiple
        self._low_rating_threshold: float = 2.5

    def submit(self, item_id: str,
                feedback_type: FeedbackType = FeedbackType.RATING,
                user_id: str = "anonymous",
                session_id: str = "",
                rating: float = None,
                thumbs: bool = None,
                text: str = "",
                choice: str = "",
                tags: List[str] = None,
                metadata: Dict = None) -> Optional[FeedbackEntry]:
        # Dedup check
        if not self.allow_multiple:
            if self._store.exists(user_id, item_id, feedback_type.value):
                return None

        sent = _sentiment(text) if text else 0.0
        # Validate rating range
        if rating is not None:
            rating = max(1.0, min(5.0, float(rating)))

        fb = FeedbackEntry(
            id=str(uuid.uuid4())[:10], item_id=item_id,
            feedback_type=feedback_type, user_id=user_id,
            session_id=session_id, rating=rating,
            thumbs=thumbs, text=text, choice=choice,
            tags=list(tags or []), metadata=dict(metadata or {}),
            sentiment=sent)
        self._store.save(fb)

        # Fire hooks
        for h in self._hooks["any"]:
            try: h(fb)
            except: pass

        # Check low-rating alert
        if feedback_type == FeedbackType.RATING and rating is not None:
            agg = self.aggregate(item_id)
            if agg.avg_rating and agg.avg_rating < self._low_rating_threshold:
                for h in self._hooks["low_rating"]:
                    try: h(item_id, agg.avg_rating)
                    except: pass

        return fb

    def aggregate(self, item_id: str) -> FeedbackAggregate:
        rows = self._store.get_for_item(item_id)
        agg = FeedbackAggregate(item_id=item_id, total=len(rows))
        ratings = [r["rating"] for r in rows if r["rating"] is not None]
        thumbs_list = [r["thumbs"] for r in rows if r["thumbs"] is not None]
        texts = [r["text"] for r in rows if r["text"]]
        sentiments = [r["sentiment"] for r in rows if r["sentiment"]]

        if ratings:
            agg.avg_rating = sum(ratings) / len(ratings)
        agg.thumbs_up   = sum(1 for t in thumbs_list if t == 1)
        agg.thumbs_down = sum(1 for t in thumbs_list if t == 0)
        total_th = agg.thumbs_up + agg.thumbs_down
        agg.thumbs_ratio = agg.thumbs_up / max(1, total_th) if total_th else 0.0
        agg.avg_sentiment = sum(sentiments) / max(1, len(sentiments)) if sentiments else 0.0
        agg.text_count    = len(texts)
        agg.latest_texts  = texts[:5]
        return agg

    def aggregate_batch(self, item_ids: List[str]) -> List[FeedbackAggregate]:
        return [self.aggregate(iid) for iid in item_ids]

    def recent(self, hours: float = 24, tag: str = None) -> List[Dict]:
        return self._store.get_recent(hours, tag)

    def trends(self, bucket: str = "day", item_id: str = None,
                days: int = 7) -> List[Dict]:
        return self._store.time_series(bucket, item_id, days)

    def on_low_rating(self, threshold: float,
                       fn: Callable[[str, float], None]):
        self._low_rating_threshold = threshold
        self._hooks["low_rating"].append(fn)

    def on_feedback(self, fn: Callable[[FeedbackEntry], None]):
        self._hooks["any"].append(fn)

    def export(self, item_id: str = None) -> List[Dict]:
        rows = (self._store.get_for_item(item_id) if item_id
                else self._store.get_recent(hours=24 * 365))
        return rows

    def stats(self) -> Dict:
        return self._store.stats()

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def submit_ep(req):
            d = await req.json()
            fb = self.submit(d["item_id"],
                              FeedbackType[d.get("type","RATING").upper()],
                              d.get("user_id","anonymous"),
                              d.get("session_id",""),
                              d.get("rating"), d.get("thumbs"),
                              d.get("text",""), d.get("choice",""),
                              d.get("tags",[]))
            if not fb:
                return web.json_response({"duplicate": True}, status=409)
            return web.json_response(fb.to_dict(), status=201)
        async def agg_ep(req):
            item_id = req.match_info["item_id"]
            return web.json_response(self.aggregate(item_id).to_dict())
        async def trends_ep(req):
            q = req.rel_url.query
            return web.json_response({"trends": self.trends(
                q.get("bucket","day"), q.get("item_id"))})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/feedback"
        app.router.add_post(f"{p}/submit",              submit_ep)
        app.router.add_get( f"{p}/aggregate/{{item_id}}", agg_ep)
        app.router.add_get( f"{p}/trends",              trends_ep)
        app.router.add_get( f"{p}/stats",               stats_ep)
        logger.info(f"Feedback collector API at {prefix}/feedback/")
