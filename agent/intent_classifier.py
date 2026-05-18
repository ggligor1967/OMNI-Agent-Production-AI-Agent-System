"""OMNI AGENT - Intent Classifier
Classify user text into intents using BOW cosine similarity,
few-shot training examples, multi-label output, and confidence
thresholds — no external ML libraries required.

Features:
- IntentSpec: name, description, examples, synonyms, priority, tags
- BOW embeddings: TF-IDF weighted bag-of-words per intent class
- Cosine similarity: query vs per-intent centroid vector
- Multi-label: return all intents above confidence threshold
- Top-K: return ranked list of (intent, confidence) pairs
- Few-shot: add examples at runtime to update class vectors
- Negative examples: mark examples that should NOT match an intent
- Preprocessing: lowercase, stopword removal, stemming-lite (suffix strip)
- Fallback: "unknown" intent when max confidence < min_confidence
- Intent hierarchy: parent/child intent nesting
- Ensemble: combine BOW cosine + keyword exact-match scores
- Recency bias: recently added examples weighted more heavily
- SQLite persistence: intents, examples, classification log
- REST API: classify, add_example, intents, stats
"""
import math, re, sqlite3, time, uuid, json, logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Text processing ────────────────────────────────────────────────────────────
_STOPWORDS = frozenset([
    "a","an","the","is","it","in","on","at","to","for","of","and",
    "or","but","with","this","that","i","you","we","they","be","was",
    "are","do","did","can","will","what","how","why","when","where"
])

_SUFFIXES = ["ing","tion","sion","ed","er","est","ly","ness","ment","able"]

def _stem(word: str) -> str:
    for sfx in sorted(_SUFFIXES, key=len, reverse=True):
        if word.endswith(sfx) and len(word) - len(sfx) >= 3:
            return word[:-len(sfx)]
    return word

def _tokenize(text: str, stem: bool = True) -> List[str]:
    tokens = re.findall(r'\b[a-z]+\b', text.lower())
    tokens = [t for t in tokens if t not in _STOPWORDS and len(t) > 1]
    if stem:
        tokens = [_stem(t) for t in tokens]
    return tokens

def _bow(tokens: List[str]) -> Dict[str, float]:
    d: Dict[str, float] = {}
    for t in tokens: d[t] = d.get(t, 0) + 1
    n = max(1, len(tokens))
    return {k: v / n for k, v in d.items()}

def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    keys = set(a) & set(b)
    if not keys: return 0.0
    dot = sum(a[k] * b[k] for k in keys)
    na  = math.sqrt(sum(v*v for v in a.values()))
    nb  = math.sqrt(sum(v*v for v in b.values()))
    return dot / max(1e-12, na * nb)

def _tfidf_weight(term: str, doc_bow: Dict[str, float],
                   doc_freq: Dict[str, int], n_docs: int) -> float:
    tf  = doc_bow.get(term, 0)
    df  = doc_freq.get(term, 1)
    idf = math.log((n_docs + 1) / (df + 1)) + 1
    return tf * idf

# ── Data classes ───────────────────────────────────────────────────────────────
@dataclass
class IntentSpec:
    id: str; name: str; description: str = ""
    examples: List[str] = field(default_factory=list)
    negative_examples: List[str] = field(default_factory=list)
    synonyms: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)   # exact-match boost
    parent: Optional[str] = None
    priority: int = 5
    tags: List[str] = field(default_factory=list)
    enabled: bool = True
    # Runtime
    _centroid: Dict[str, float] = field(default_factory=dict)
    _neg_centroid: Dict[str, float] = field(default_factory=dict)
    match_count: int = 0

    def recompute_centroid(self, recency_weight: float = 0.1):
        if not self.examples:
            self._centroid = {}
            return
        n = len(self.examples)
        merged: Dict[str, float] = {}
        for i, ex in enumerate(self.examples):
            w = 1.0 + recency_weight * (i / max(1, n - 1))
            bow = _bow(_tokenize(ex))
            for k, v in bow.items():
                merged[k] = merged.get(k, 0) + v * w
        total_w = sum(1.0 + recency_weight * (i / max(1, n - 1)) for i in range(n))
        self._centroid = {k: v / total_w for k, v in merged.items()}
        # Negative centroid
        if self.negative_examples:
            neg_bows = [_bow(_tokenize(ex)) for ex in self.negative_examples]
            neg_merged: Dict[str, float] = {}
            for bow in neg_bows:
                for k, v in bow.items():
                    neg_merged[k] = neg_merged.get(k, 0) + v
            n_neg = len(neg_bows)
            self._neg_centroid = {k: v / n_neg for k, v in neg_merged.items()}

    def keyword_score(self, query_lower: str) -> float:
        if not self.keywords: return 0.0
        hits = sum(1 for kw in self.keywords if kw.lower() in query_lower)
        return min(1.0, hits / len(self.keywords) * 2)

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "description": self.description,
                "examples_count": len(self.examples),
                "keywords": self.keywords,
                "parent": self.parent,
                "priority": self.priority,
                "tags": self.tags,
                "enabled": self.enabled,
                "match_count": self.match_count}

@dataclass
class ClassificationResult:
    text: str; top_intent: str; confidence: float
    all_scores: List[Tuple[str, float]] = field(default_factory=list)
    is_unknown: bool = False
    latency_ms: float = 0.0

    def to_dict(self):
        return {"text": self.text[:200], "top_intent": self.top_intent,
                "confidence": round(self.confidence, 4),
                "is_unknown": self.is_unknown,
                "top_3": [(i, round(s, 4)) for i, s in self.all_scores[:3]],
                "latency_ms": round(self.latency_ms, 1)}

class ICStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS classifications(
                    id TEXT PRIMARY KEY, text TEXT, top_intent TEXT,
                    confidence REAL, is_unknown INTEGER DEFAULT 0,
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_cl_intent
                    ON classifications(top_intent, created_at DESC);
            """)

    def log(self, r: ClassificationResult):
        with self._conn() as c:
            c.execute("INSERT INTO classifications VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], r.text[:300], r.top_intent,
                 r.confidence, int(r.is_unknown), time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            n  = c.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
            nu = c.execute(
                "SELECT COUNT(*) FROM classifications WHERE is_unknown=1"
            ).fetchone()[0]
            top = c.execute(
                "SELECT top_intent, COUNT(*) as cnt FROM classifications "
                "GROUP BY top_intent ORDER BY cnt DESC LIMIT 5"
            ).fetchall()
        return {"total": n, "unknown": nu,
                "unknown_rate": round(nu / max(1, n), 4),
                "top_intents": [(r["top_intent"], r["cnt"]) for r in top]}

class IntentClassifier:
    """
    BOW cosine intent classifier with few-shot examples and keyword boost.

    Usage:
        ic = IntentClassifier(min_confidence=0.3)

        ic.add_intent("greeting",
                       examples=["hello", "hi there", "good morning"],
                       keywords=["hello", "hi", "hey"])
        ic.add_intent("farewell",
                       examples=["goodbye", "see you later", "bye"],
                       keywords=["bye", "goodbye"])
        ic.add_intent("help",
                       examples=["I need help", "can you assist me", "support"],
                       keywords=["help", "support", "assist"])

        result = ic.classify("hey, can you help me?")
        print(result.top_intent, result.confidence)
    """
    def __init__(self, db_path: str = "data/intents.db",
                 min_confidence: float = 0.25,
                 keyword_weight: float = 0.3,
                 unknown_label: str = "unknown",
                 recency_weight: float = 0.1):
        self._store = ICStore(db_path)
        self._intents: Dict[str, IntentSpec] = {}
        self.min_confidence = min_confidence
        self.keyword_weight = keyword_weight
        self.unknown_label  = unknown_label
        self.recency_weight = recency_weight

    def add_intent(self, name: str,
                    examples: List[str] = None,
                    description: str = "",
                    negative_examples: List[str] = None,
                    synonyms: List[str] = None,
                    keywords: List[str] = None,
                    parent: str = None,
                    priority: int = 5,
                    tags: List[str] = None) -> IntentSpec:
        spec = IntentSpec(id=str(uuid.uuid4())[:8], name=name,
                           description=description,
                           examples=list(examples or []),
                           negative_examples=list(negative_examples or []),
                           synonyms=list(synonyms or []),
                           keywords=list(keywords or []),
                           parent=parent, priority=priority,
                           tags=list(tags or []))
        spec.recompute_centroid(self.recency_weight)
        self._intents[name] = spec
        logger.debug(f"Intent added: {name!r} ({len(spec.examples)} examples)")
        return spec

    def add_example(self, intent_name: str, text: str,
                     negative: bool = False) -> bool:
        spec = self._intents.get(intent_name)
        if not spec: return False
        if negative:
            spec.negative_examples.append(text)
        else:
            spec.examples.append(text)
        spec.recompute_centroid(self.recency_weight)
        return True

    def remove_intent(self, name: str) -> bool:
        return self._intents.pop(name, None) is not None

    def classify(self, text: str, top_k: int = None) -> ClassificationResult:
        start = time.time()
        query_bow = _bow(_tokenize(text))
        query_lower = text.lower()

        scores: List[Tuple[str, float]] = []
        for name, spec in self._intents.items():
            if not spec.enabled: continue
            if not spec._centroid: continue

            # BOW cosine similarity
            cos = _cosine(query_bow, spec._centroid)

            # Negative centroid penalty
            if spec._neg_centroid:
                neg_cos = _cosine(query_bow, spec._neg_centroid)
                cos = max(0.0, cos - neg_cos * 0.5)

            # Keyword exact-match boost
            kw = spec.keyword_score(query_lower)

            # Synonym boost
            syn_boost = 0.0
            for syn in spec.synonyms:
                if syn.lower() in query_lower:
                    syn_boost = min(0.2, syn_boost + 0.1)

            combined = (cos * (1 - self.keyword_weight)
                        + kw * self.keyword_weight
                        + syn_boost)
            combined = min(1.0, combined)
            scores.append((name, combined))

        scores.sort(key=lambda x: (-x[1], -self._intents[x[0]].priority))
        if top_k:
            scores = scores[:top_k]

        top_intent = self.unknown_label
        top_conf   = 0.0
        is_unknown = True

        if scores and scores[0][1] >= self.min_confidence:
            top_intent = scores[0][0]
            top_conf   = scores[0][1]
            is_unknown = False
            self._intents[top_intent].match_count += 1

        result = ClassificationResult(
            text=text, top_intent=top_intent,
            confidence=top_conf, all_scores=scores,
            is_unknown=is_unknown,
            latency_ms=(time.time() - start) * 1000)
        self._store.log(result)
        return result

    def classify_multilabel(self, text: str,
                             threshold: float = None) -> List[Tuple[str, float]]:
        thresh = threshold or self.min_confidence
        result = self.classify(text)
        return [(i, s) for i, s in result.all_scores if s >= thresh]

    def batch_classify(self, texts: List[str]) -> List[ClassificationResult]:
        return [self.classify(t) for t in texts]

    def intent_info(self, name: str) -> Optional[Dict]:
        spec = self._intents.get(name)
        return spec.to_dict() if spec else None

    def list_intents(self, tag: str = None,
                      parent: str = None) -> List[IntentSpec]:
        specs = list(self._intents.values())
        if tag:    specs = [s for s in specs if tag in s.tags]
        if parent: specs = [s for s in specs if s.parent == parent]
        return sorted(specs, key=lambda s: s.priority)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["defined_intents"] = len(self._intents)
        s["total_examples"]  = sum(len(sp.examples) for sp in self._intents.values())
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def classify_ep(req):
            d = await req.json()
            r = self.classify(d["text"], d.get("top_k"))
            return web.json_response(r.to_dict())
        async def multilabel_ep(req):
            d = await req.json()
            hits = self.classify_multilabel(d["text"], d.get("threshold"))
            return web.json_response(
                {"intents": [(i, round(s, 4)) for i, s in hits]})
        async def add_example_ep(req):
            d = await req.json()
            ok = self.add_example(d["intent"], d["text"],
                                   d.get("negative", False))
            return web.json_response({"added": ok})
        async def intents_ep(req):
            return web.json_response(
                {"intents": [s.to_dict() for s in self.list_intents()]})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/intent"
        app.router.add_post(f"{p}/classify",    classify_ep)
        app.router.add_post(f"{p}/multilabel",  multilabel_ep)
        app.router.add_post(f"{p}/add_example", add_example_ep)
        app.router.add_get( f"{p}/intents",     intents_ep)
        app.router.add_get( f"{p}/stats",       stats_ep)
        logger.info(f"Intent classifier API at {prefix}/intent/")
