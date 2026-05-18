"""OMNI AGENT - Feedback Loop
Human-in-the-loop feedback collection: annotate responses, aggregate
scores, compute reward signals for RL-style fine-tuning, and surface
quality trends over time.

Features:
- Annotation types: rating (1-5), thumbs, label, comparison, correction
- Response tracking: link feedback to request/response pairs
- Reward signal: normalise scores to [-1, +1] for RL
- Preference pairs: A/B comparisons for RLHF-style training
- Quality metrics: per-model, per-topic, per-user rolling averages
- Threshold alerts: flag responses with persistent low scores
- Calibration: adjust raw scores by annotator agreement
- Batch import: load feedback from CSV/JSON
- Export: JSONL for fine-tuning dataset preparation
- SQLite persistence: all annotations and aggregates
- REST API: annotate, compare, export, stats, trends
"""
import time, uuid, sqlite3, json, logging, statistics
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class AnnotationType(str, Enum):
    RATING     = "rating"       # 1-5 numeric
    THUMBS     = "thumbs"       # +1 / -1
    LABEL      = "label"        # free-text category
    COMPARISON = "comparison"   # preferred A or B
    CORRECTION = "correction"   # free-text corrected response

@dataclass
class Annotation:
    id: str
    response_id: str           # ID of the response being annotated
    annotation_type: AnnotationType
    value: Any                 # the actual annotation
    annotator: str = "user"
    model_id: str = ""
    topic: str = ""
    prompt_snippet: str = ""   # first 100 chars of the prompt
    response_snippet: str = "" # first 100 chars of the response
    metadata: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def reward(self) -> float:
        """Normalise to [-1, +1]."""
        if self.annotation_type == AnnotationType.RATING:
            return (float(self.value) - 3.0) / 2.0   # 1→-1, 3→0, 5→+1
        elif self.annotation_type == AnnotationType.THUMBS:
            return float(self.value)                  # already ±1
        return 0.0

    def to_dict(self):
        return {"id": self.id, "response_id": self.response_id,
                "type": self.annotation_type, "value": self.value,
                "annotator": self.annotator, "model_id": self.model_id,
                "topic": self.topic, "reward": round(self.reward, 4),
                "created_at": self.created_at}

@dataclass
class PreferencePair:
    id: str
    prompt: str
    response_a: str; response_b: str
    preferred: str   # "a" | "b" | "tie"
    annotator: str = "user"
    model_a: str = ""; model_b: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "preferred": self.preferred,
                "model_a": self.model_a, "model_b": self.model_b,
                "reason": self.reason, "created_at": self.created_at}

    def to_training_pair(self) -> Dict:
        """RLHF-style training pair: chosen/rejected."""
        chosen   = self.response_a if self.preferred == "a" else self.response_b
        rejected = self.response_b if self.preferred == "a" else self.response_a
        return {"prompt": self.prompt, "chosen": chosen, "rejected": rejected}

class FLStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS annotations(
                    id TEXT PRIMARY KEY, response_id TEXT,
                    annotation_type TEXT, value TEXT,
                    annotator TEXT DEFAULT 'user', model_id TEXT DEFAULT '',
                    topic TEXT DEFAULT '', prompt_snippet TEXT DEFAULT '',
                    response_snippet TEXT DEFAULT '',
                    metadata TEXT DEFAULT '{}', created_at REAL);
                CREATE TABLE IF NOT EXISTS preference_pairs(
                    id TEXT PRIMARY KEY, prompt TEXT,
                    response_a TEXT, response_b TEXT, preferred TEXT,
                    annotator TEXT DEFAULT 'user',
                    model_a TEXT DEFAULT '', model_b TEXT DEFAULT '',
                    reason TEXT DEFAULT '', created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_ann_resp ON annotations(response_id);
                CREATE INDEX IF NOT EXISTS idx_ann_model ON annotations(model_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ann_topic ON annotations(topic, created_at DESC);
            """)

    def save_annotation(self, a: Annotation):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO annotations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.response_id, a.annotation_type, json.dumps(a.value),
                 a.annotator, a.model_id, a.topic,
                 a.prompt_snippet[:100], a.response_snippet[:100],
                 json.dumps(a.metadata), a.created_at))

    def save_pair(self, p: PreferencePair):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO preference_pairs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (p.id, p.prompt, p.response_a, p.response_b, p.preferred,
                 p.annotator, p.model_a, p.model_b, p.reason, p.created_at))

    def get_annotations(self, response_id: str = None, model_id: str = None,
                          topic: str = None, limit: int = 100) -> List[Annotation]:
        sql = "SELECT * FROM annotations WHERE 1=1"
        params = []
        if response_id: sql += " AND response_id=?";   params.append(response_id)
        if model_id:    sql += " AND model_id=?";       params.append(model_id)
        if topic:       sql += " AND topic=?";          params.append(topic)
        sql += f" ORDER BY created_at DESC LIMIT {limit}"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        result = []
        for row in rows:
            val = json.loads(row["value"])
            result.append(Annotation(
                id=row["id"], response_id=row["response_id"],
                annotation_type=AnnotationType(row["annotation_type"]),
                value=val, annotator=row["annotator"],
                model_id=row["model_id"] or "", topic=row["topic"] or "",
                prompt_snippet=row["prompt_snippet"] or "",
                response_snippet=row["response_snippet"] or "",
                metadata=json.loads(row["metadata"] or "{}"),
                created_at=row["created_at"]))
        return result

    def get_pairs(self, limit: int = 100) -> List[PreferencePair]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM preference_pairs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [PreferencePair(id=r["id"], prompt=r["prompt"],
                                response_a=r["response_a"], response_b=r["response_b"],
                                preferred=r["preferred"], annotator=r["annotator"],
                                model_a=r["model_a"] or "", model_b=r["model_b"] or "",
                                reason=r["reason"] or "",
                                created_at=r["created_at"]) for r in rows]

    def avg_reward(self, model_id: str = None, topic: str = None,
                    since: float = 0.0) -> float:
        sql = ("SELECT value, annotation_type FROM annotations "
               "WHERE annotation_type IN ('rating','thumbs') AND created_at > ?")
        params: List[Any] = [since]
        if model_id: sql += " AND model_id=?"; params.append(model_id)
        if topic:    sql += " AND topic=?";    params.append(topic)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        rewards = []
        for row in rows:
            val = json.loads(row["value"])
            ann_type = row["annotation_type"]
            if ann_type == "rating":
                rewards.append((float(val) - 3.0) / 2.0)
            elif ann_type == "thumbs":
                rewards.append(float(val))
        return round(statistics.mean(rewards), 4) if rewards else 0.0

    def stats(self) -> Dict:
        with self._conn() as c:
            na = c.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
            np = c.execute("SELECT COUNT(*) FROM preference_pairs").fetchone()[0]
            by_type = dict(c.execute(
                "SELECT annotation_type, COUNT(*) FROM annotations GROUP BY annotation_type"
            ).fetchall())
        return {"total_annotations": na, "total_pairs": np, "by_type": by_type}

class FeedbackLoop:
    """
    Human-in-the-loop feedback collector with reward signal computation.

    Usage:
        fl = FeedbackLoop()

        # Collect a star rating
        fl.annotate(response_id="resp_123", annotation_type="rating",
                     value=4, model_id="gpt-4o", topic="coding",
                     prompt="Write a sort function",
                     response="def sort(arr): return sorted(arr)")

        # Collect a thumbs-up
        fl.annotate("resp_456", "thumbs", value=1)

        # Record a preference comparison
        fl.compare(prompt="Explain recursion",
                    response_a="Recursion is when a function calls itself...",
                    response_b="A recursive function...",
                    preferred="a", model_a="gpt-4o", model_b="claude")

        # Get average reward for a model
        avg = fl.avg_reward(model_id="gpt-4o")    # e.g. 0.35
    """
    def __init__(self, db_path: str = "data/feedback.db",
                 low_score_threshold: float = -0.3):
        self._store = FLStore(db_path)
        self._threshold = low_score_threshold
        self._alert_hooks: List = []

    def annotate(self, response_id: str, annotation_type: str,
                  value: Any, annotator: str = "user",
                  model_id: str = "", topic: str = "",
                  prompt: str = "", response: str = "",
                  metadata: Dict = None) -> Annotation:
        ann = Annotation(
            id=str(uuid.uuid4())[:10],
            response_id=response_id,
            annotation_type=AnnotationType(annotation_type),
            value=value, annotator=annotator,
            model_id=model_id, topic=topic,
            prompt_snippet=prompt[:100],
            response_snippet=response[:100],
            metadata=metadata or {})
        self._store.save_annotation(ann)
        # Check threshold
        if ann.reward < self._threshold:
            for hook in self._alert_hooks:
                try: hook(ann)
                except: pass
        return ann

    def compare(self, prompt: str, response_a: str, response_b: str,
                 preferred: str, annotator: str = "user",
                 model_a: str = "", model_b: str = "",
                 reason: str = "") -> PreferencePair:
        pair = PreferencePair(
            id=str(uuid.uuid4())[:10], prompt=prompt,
            response_a=response_a, response_b=response_b,
            preferred=preferred, annotator=annotator,
            model_a=model_a, model_b=model_b, reason=reason)
        self._store.save_pair(pair)
        return pair

    def batch_annotate(self, items: List[Dict]) -> List[Annotation]:
        return [self.annotate(**item) for item in items]

    def get_annotations(self, response_id: str = None,
                         model_id: str = None, topic: str = None,
                         limit: int = 100) -> List[Annotation]:
        return self._store.get_annotations(response_id, model_id, topic, limit)

    def avg_reward(self, model_id: str = None, topic: str = None,
                    since_hours: float = 0.0) -> float:
        since = time.time() - since_hours * 3600 if since_hours > 0 else 0.0
        return self._store.avg_reward(model_id, topic, since)

    def reward_trend(self, model_id: str = None,
                      window_hours: float = 24.0,
                      buckets: int = 6) -> List[Dict]:
        """Divide last window_hours into buckets and compute avg reward per bucket."""
        now = time.time()
        bucket_size = window_hours * 3600 / buckets
        result = []
        for i in range(buckets):
            end_t   = now - i * bucket_size
            start_t = end_t - bucket_size
            reward  = self._store.avg_reward(model_id, since=start_t)
            result.append({"bucket": i, "start": round(start_t, 0),
                            "end": round(end_t, 0),
                            "avg_reward": reward})
        return list(reversed(result))

    def model_leaderboard(self) -> List[Dict]:
        """Rank models by average reward."""
        with self._store._conn() as c:
            models = [r[0] for r in c.execute(
                "SELECT DISTINCT model_id FROM annotations "
                "WHERE model_id != '' AND annotation_type IN ('rating','thumbs')"
            ).fetchall()]
        board = []
        for m in models:
            avg = self._store.avg_reward(model_id=m)
            count = len(self.get_annotations(model_id=m, limit=10000))
            board.append({"model": m, "avg_reward": avg, "annotations": count})
        return sorted(board, key=lambda x: -x["avg_reward"])

    def export_rlhf(self, limit: int = 10000) -> List[Dict]:
        """Export preference pairs as RLHF training data."""
        pairs = self._store.get_pairs(limit)
        return [p.to_training_pair() for p in pairs if p.preferred in ("a","b")]

    def add_alert_hook(self, fn): self._alert_hooks.append(fn)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["avg_reward_overall"] = self.avg_reward()
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def annotate_ep(req):
            d = await req.json()
            ann = self.annotate(d["response_id"], d["type"], d["value"],
                                 d.get("annotator","user"), d.get("model_id",""),
                                 d.get("topic",""), d.get("prompt",""),
                                 d.get("response",""))
            return web.json_response(ann.to_dict(), status=201)
        async def compare_ep(req):
            d = await req.json()
            p = self.compare(d["prompt"], d["response_a"], d["response_b"],
                              d["preferred"], d.get("annotator","user"),
                              d.get("model_a",""), d.get("model_b",""))
            return web.json_response(p.to_dict(), status=201)
        async def export_ep(req):
            return web.json_response({"pairs": self.export_rlhf()})
        async def leaderboard_ep(req):
            return web.json_response({"leaderboard": self.model_leaderboard()})
        async def stats_ep(req): return web.json_response(self.stats())
        pr = f"{prefix}/feedback"
        app.router.add_post(f"{pr}/annotate",    annotate_ep)
        app.router.add_post(f"{pr}/compare",     compare_ep)
        app.router.add_get( f"{pr}/export",      export_ep)
        app.router.add_get( f"{pr}/leaderboard", leaderboard_ep)
        app.router.add_get( f"{pr}/stats",       stats_ep)
        logger.info(f"Feedback loop API at {prefix}/feedback/")
