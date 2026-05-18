"""OMNI AGENT - Response Ranker
Rank multiple LLM responses by quality: coherence, relevance, length
balance, deduplication, toxicity proxy, and ensemble scoring.

Features:
- Coherence score: sentence-pair cosine similarity (BOW), flow check
- Relevance score: query-response BOW cosine vs prompt
- Length score: penalise too-short or too-long responses (Gaussian)
- Vocabulary richness: type-token ratio as diversity proxy
- Deduplication: near-duplicate detection via Jaccard on trigrams
- Toxicity proxy: keyword-based heuristic (fast, no external model)
- Format bonus: reward JSON, code blocks, lists as structural quality
- Ensemble: weighted sum of all scores → final rank
- Calibration: per-criterion weight config
- Batch ranking: rank N candidates with one call
- Explanation: per-criterion breakdown for every response
- SQLite persistence: ranking sessions and score distributions
- REST API: rank, calibrate, stats
"""
import math, re, sqlite3, time, uuid, json, logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _tokenize(text: str) -> List[str]:
    return re.findall(r'\b\w+\b', text.lower())

def _bow(tokens: List[str]) -> Dict[str, float]:
    d: Dict[str, float] = {}
    for t in tokens: d[t] = d.get(t, 0) + 1
    n = max(1, len(tokens))
    return {k: v / n for k, v in d.items()}

def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    keys = set(a) & set(b)
    dot  = sum(a[k] * b[k] for k in keys)
    na   = math.sqrt(sum(v*v for v in a.values()))
    nb   = math.sqrt(sum(v*v for v in b.values()))
    return dot / max(1e-12, na * nb)

def _trigrams(text: str) -> set:
    t = text.lower()
    return {t[i:i+3] for i in range(len(t) - 2)}

def _jaccard(a: str, b: str) -> float:
    sa = _trigrams(a); sb = _trigrams(b)
    if not sa and not sb: return 1.0
    return len(sa & sb) / max(1, len(sa | sb))

_TOXIC_WORDS = frozenset([
    "kill", "hate", "stupid", "idiot", "moron", "dumb",
    "offensive", "violence", "threat", "abuse", "harassment"
])

def _toxicity_score(text: str) -> float:
    """Returns 0 (clean) to 1 (toxic proxy) based on keyword density."""
    tokens = _tokenize(text)
    if not tokens: return 0.0
    hits = sum(1 for t in tokens if t in _TOXIC_WORDS)
    return min(1.0, hits / max(1, len(tokens)) * 20)

def _length_score(text: str, target: int = 300, sigma: float = 200) -> float:
    """Gaussian reward centered on target length (chars)."""
    n = len(text)
    return math.exp(-((n - target) ** 2) / (2 * sigma ** 2))

def _richness(text: str) -> float:
    """Type-token ratio (vocabulary diversity)."""
    tokens = _tokenize(text)
    if not tokens: return 0.0
    return len(set(tokens)) / len(tokens)

def _coherence(text: str) -> float:
    """Average cosine similarity between consecutive sentences."""
    sents = re.split(r'(?<=[.!?])\s+', text.strip())
    sents = [s for s in sents if len(s.split()) >= 3]
    if len(sents) < 2: return 1.0
    sims = []
    for i in range(len(sents) - 1):
        a = _bow(_tokenize(sents[i]))
        b = _bow(_tokenize(sents[i+1]))
        sims.append(_cosine(a, b))
    return sum(sims) / len(sims)

def _format_bonus(text: str) -> float:
    """Bonus for structural formatting cues."""
    score = 0.0
    if re.search(r'```', text): score += 0.3          # code block
    if re.search(r'^\s*[-*]\s', text, re.M): score += 0.2  # bullets
    if re.search(r'^\s*\d+\.\s', text, re.M): score += 0.2  # numbered
    try: json.loads(text); score += 0.4               # valid JSON
    except: pass
    return min(1.0, score)

# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class RankingWeights:
    relevance:  float = 0.30
    coherence:  float = 0.20
    length:     float = 0.15
    richness:   float = 0.10
    toxicity:   float = 0.15   # subtracted
    format:     float = 0.10

    def to_dict(self): return self.__dict__

@dataclass
class ResponseScore:
    response_id: str; text: str
    relevance:   float = 0.0
    coherence:   float = 0.0
    length:      float = 0.0
    richness:    float = 0.0
    toxicity:    float = 0.0
    format_bonus: float = 0.0
    final_score: float = 0.0
    rank: int = 0
    duplicate_of: Optional[str] = None

    def breakdown(self) -> Dict:
        return {"relevance": round(self.relevance, 4),
                "coherence": round(self.coherence, 4),
                "length": round(self.length, 4),
                "richness": round(self.richness, 4),
                "toxicity": round(self.toxicity, 4),
                "format_bonus": round(self.format_bonus, 4),
                "final": round(self.final_score, 4),
                "rank": self.rank,
                "duplicate_of": self.duplicate_of}

    def to_dict(self):
        return {"id": self.response_id,
                "text_preview": self.text[:150],
                **self.breakdown()}

class RRStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY, prompt TEXT DEFAULT '',
                    candidate_count INTEGER DEFAULT 0,
                    best_score REAL DEFAULT 0, created_at REAL);
                CREATE TABLE IF NOT EXISTS scores(
                    id TEXT PRIMARY KEY, session_id TEXT,
                    response_id TEXT, final_score REAL DEFAULT 0,
                    rank INTEGER DEFAULT 0, created_at REAL);
            """)

    def log_session(self, session_id: str, prompt: str,
                     count: int, best: float):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)",
                (session_id, prompt[:200], count, round(best, 4), time.time()))

    def log_scores(self, session_id: str, scores: List[ResponseScore]):
        with self._conn() as c:
            for s in scores:
                c.execute("INSERT INTO scores VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4())[:8], session_id, s.response_id,
                     s.final_score, s.rank, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            ns = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            avg = c.execute(
                "SELECT AVG(best_score) FROM sessions").fetchone()[0] or 0
        return {"sessions": ns, "avg_best_score": round(avg, 4)}

class ResponseRanker:
    """
    Multi-criterion LLM response ranker with dedup and weighted ensemble.

    Usage:
        ranker = ResponseRanker()
        ranker.weights.relevance = 0.4
        ranker.weights.toxicity  = 0.2

        candidates = ["Answer A text...", "Answer B text...", "Answer C text..."]
        ranked = ranker.rank(candidates, prompt="Explain Python asyncio")

        for r in ranked:
            print(r.rank, r.final_score, r.text[:60])
    """
    def __init__(self, db_path: str = "data/ranker.db",
                 dedup_threshold: float = 0.85,
                 length_target: int = 300,
                 length_sigma: float = 200):
        self._store = RRStore(db_path)
        self.weights = RankingWeights()
        self._dedup_threshold = dedup_threshold
        self._length_target = length_target
        self._length_sigma = length_sigma

    def _score_one(self, text: str, prompt: str) -> Tuple[float,...]:
        prompt_bow = _bow(_tokenize(prompt)) if prompt else {}
        resp_bow   = _bow(_tokenize(text))
        rel  = _cosine(prompt_bow, resp_bow) if prompt_bow else 0.5
        coh  = _coherence(text)
        lng  = _length_score(text, self._length_target, self._length_sigma)
        rich = _richness(text)
        tox  = _toxicity_score(text)
        fmt  = _format_bonus(text)
        w = self.weights
        final = (w.relevance * rel + w.coherence * coh + w.length * lng
                 + w.richness * rich - w.toxicity * tox + w.format * fmt)
        final = max(0.0, min(1.0, final))
        return rel, coh, lng, rich, tox, fmt, final

    def rank(self, candidates: List[str], prompt: str = "",
              session_id: str = None) -> List[ResponseScore]:
        if not candidates: return []
        session_id = session_id or str(uuid.uuid4())[:10]
        scores: List[ResponseScore] = []

        for i, text in enumerate(candidates):
            rid = f"r{i}"
            rel, coh, lng, rich, tox, fmt, final = self._score_one(text, prompt)
            s = ResponseScore(response_id=rid, text=text,
                               relevance=rel, coherence=coh, length=lng,
                               richness=rich, toxicity=tox,
                               format_bonus=fmt, final_score=final)
            scores.append(s)

        # Dedup: mark near-duplicates
        for i in range(len(scores)):
            if scores[i].duplicate_of: continue
            for j in range(i+1, len(scores)):
                if scores[j].duplicate_of: continue
                sim = _jaccard(scores[i].text, scores[j].text)
                if sim >= self._dedup_threshold:
                    # Keep higher-scoring one
                    if scores[i].final_score >= scores[j].final_score:
                        scores[j].duplicate_of = scores[i].response_id
                        scores[j].final_score *= 0.1
                    else:
                        scores[i].duplicate_of = scores[j].response_id
                        scores[i].final_score *= 0.1

        # Sort and assign ranks
        scores.sort(key=lambda s: -s.final_score)
        for rank, s in enumerate(scores):
            s.rank = rank + 1

        best = scores[0].final_score if scores else 0.0
        self._store.log_session(session_id, prompt, len(candidates), best)
        self._store.log_scores(session_id, scores)
        return scores

    def best(self, candidates: List[str], prompt: str = "") -> Optional[str]:
        ranked = self.rank(candidates, prompt)
        return ranked[0].text if ranked else None

    def filter_duplicates(self, candidates: List[str]) -> List[str]:
        unique = []
        for text in candidates:
            if all(_jaccard(text, u) < self._dedup_threshold for u in unique):
                unique.append(text)
        return unique

    def score_one(self, text: str, prompt: str = "") -> ResponseScore:
        rel, coh, lng, rich, tox, fmt, final = self._score_one(text, prompt)
        return ResponseScore(response_id="single", text=text,
                              relevance=rel, coherence=coh, length=lng,
                              richness=rich, toxicity=tox,
                              format_bonus=fmt, final_score=final)

    def calibrate(self, weights: Dict[str, float]):
        for k, v in weights.items():
            if hasattr(self.weights, k):
                setattr(self.weights, k, float(v))

    def stats(self) -> Dict:
        s = self._store.stats()
        s["weights"] = self.weights.to_dict()
        s["dedup_threshold"] = self._dedup_threshold
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def rank_ep(req):
            d = await req.json()
            ranked = self.rank(d["candidates"], d.get("prompt",""),
                                d.get("session_id"))
            return web.json_response({"ranked": [r.to_dict() for r in ranked]})
        async def calibrate_ep(req):
            d = await req.json()
            self.calibrate(d.get("weights",{}))
            return web.json_response({"weights": self.weights.to_dict()})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/ranker"
        app.router.add_post(f"{p}/rank",      rank_ep)
        app.router.add_post(f"{p}/calibrate", calibrate_ep)
        app.router.add_get( f"{p}/stats",     stats_ep)
        logger.info(f"Response ranker API at {prefix}/ranker/")
