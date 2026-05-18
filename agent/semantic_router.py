"""OMNI Agent — Semantic Router: intent-based routing with embedding similarity and fallbacks."""
from __future__ import annotations
import hashlib, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class RouteMatchStrategy(str, Enum):
    COSINE_NEAREST  = "cosine_nearest"
    THRESHOLD       = "threshold"          # only route if score >= threshold
    TOP_K_VOTE      = "top_k_vote"         # k nearest examples vote
    KEYWORD_FIRST   = "keyword_first"      # keyword match → embedding fallback


def _embed_hash(text: str, dim: int = 32) -> List[float]:
    """Deterministic hash-based pseudo-embedding (no ML needed)."""
    h = hashlib.sha256(text.encode()).digest()
    raw = (list(h) * (dim // 32 + 1))[:dim]
    vec = [(b / 127.5) - 1.0 for b in raw]
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n > 0 else vec


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


@dataclass
class RouteExample:
    text: str
    embedding: List[float]


@dataclass
class Route:
    route_id: str
    name: str
    handler: Callable
    examples: List[RouteExample] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    description: str = ""
    priority: int = 0
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_id": self.route_id,
            "name": self.name,
            "examples": len(self.examples),
            "keywords": self.keywords,
            "priority": self.priority,
            "enabled": self.enabled,
        }


@dataclass
class RoutingDecision:
    query: str
    route_id: Optional[str]
    route_name: Optional[str]
    score: float
    strategy_used: RouteMatchStrategy
    matched_by: str            # "keyword" | "embedding" | "default"
    duration_ms: float
    all_scores: Dict[str, float] = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return self.route_id is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query[:80],
            "route_id": self.route_id,
            "route_name": self.route_name,
            "score": round(self.score, 4),
            "strategy": self.strategy_used.value,
            "matched_by": self.matched_by,
            "duration_ms": round(self.duration_ms, 2),
        }


class SemanticRouter:
    """
    Semantic intent router:
    - Register named routes with example utterances
    - Embed queries and find nearest-matching route
    - Keyword-first fast path
    - Top-k vote for stability
    - Threshold-based confidence gating
    - Fallback route support
    - Route execution
    - SQLite routing audit
    """

    def __init__(
        self,
        strategy: RouteMatchStrategy = RouteMatchStrategy.THRESHOLD,
        threshold: float = 0.65,
        top_k: int = 3,
        embed_fn: Optional[Callable[[str], List[float]]] = None,
        fallback_route_id: Optional[str] = None,
        db_path: str = ":memory:",
    ):
        self.strategy           = strategy
        self.threshold          = threshold
        self.top_k              = top_k
        self._embed             = embed_fn or _embed_hash
        self.fallback_route_id  = fallback_route_id
        self._routes: Dict[str, Route] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._route_count = 0
        self._match_count = 0
        self._miss_count  = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sr_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT, route_id TEXT, score REAL,
                matched_by TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── REGISTRATION ──────────────────────────────────────────────────

    def add_route(self, name: str, handler: Callable,
                  examples: Optional[List[str]] = None,
                  keywords: Optional[List[str]] = None,
                  description: str = "",
                  priority: int = 0,
                  metadata: Optional[Dict] = None,
                  route_id: Optional[str] = None) -> Route:
        rid = route_id or str(uuid.uuid4())[:8]
        route_examples = [
            RouteExample(text=ex, embedding=self._embed(ex))
            for ex in (examples or [])
        ]
        route = Route(
            route_id=rid, name=name, handler=handler,
            examples=route_examples,
            keywords=list(keywords or []),
            description=description, priority=priority,
            metadata=metadata or {})
        self._routes[rid] = route
        self._route_count += 1
        return route

    def add_examples(self, route_id: str, examples: List[str]):
        route = self._routes.get(route_id)
        if route:
            for ex in examples:
                route.examples.append(
                    RouteExample(text=ex, embedding=self._embed(ex)))

    def remove_route(self, route_id: str):
        self._routes.pop(route_id, None)

    def set_fallback(self, route_id: str):
        self.fallback_route_id = route_id

    # ── ROUTING ───────────────────────────────────────────────────────

    def route(self, query: str) -> RoutingDecision:
        t0 = time.time()
        active = [r for r in self._routes.values() if r.enabled]

        # 1. Keyword fast path (if strategy includes it or KEYWORD_FIRST)
        if self.strategy in (RouteMatchStrategy.KEYWORD_FIRST,):
            kw_match = self._keyword_match(query, active)
            if kw_match:
                d = self._make_decision(query, kw_match, 1.0,
                                        "keyword", t0)
                self._log(d)
                return d

        # Always try keyword if KEYWORD_FIRST
        if self.strategy == RouteMatchStrategy.KEYWORD_FIRST:
            pass  # already tried above

        # 2. Embedding-based
        q_emb = self._embed(query)
        all_scores: Dict[str, float] = {}

        for route in active:
            if not route.examples:
                continue
            scores = [_cosine(q_emb, ex.embedding) for ex in route.examples]
            if self.strategy == RouteMatchStrategy.TOP_K_VOTE:
                # Take average of top-k scores
                scores.sort(reverse=True)
                score = sum(scores[:self.top_k]) / min(self.top_k, len(scores))
            else:
                score = max(scores)
            all_scores[route.route_id] = score

        # Also check keywords in all strategies
        for route in active:
            if route.keywords:
                q_lower = query.lower()
                if any(kw.lower() in q_lower for kw in route.keywords):
                    all_scores[route.route_id] = max(
                        all_scores.get(route.route_id, 0.0), 0.95)

        if not all_scores:
            return self._fallback_decision(query, t0)

        # Sort by priority then score
        def sort_key(item):
            rid, score = item
            r = self._routes[rid]
            return (-r.priority, -score)

        sorted_scores = sorted(all_scores.items(), key=sort_key)
        best_rid, best_score = sorted_scores[0]

        if self.strategy == RouteMatchStrategy.THRESHOLD:
            if best_score < self.threshold:
                return self._fallback_decision(query, t0, all_scores)

        matched_by = "keyword" if best_score >= 0.95 and self._routes[best_rid].keywords else "embedding"
        d = self._make_decision(query, self._routes[best_rid],
                                best_score, matched_by, t0, all_scores)
        self._log(d)
        if d.matched:
            self._match_count += 1
        else:
            self._miss_count += 1
        return d

    def _keyword_match(self, query: str,
                        routes: List[Route]) -> Optional[Route]:
        q_lower = query.lower()
        for route in sorted(routes, key=lambda r: -r.priority):
            if any(kw.lower() in q_lower for kw in route.keywords):
                return route
        return None

    def _make_decision(self, query: str, route: Route, score: float,
                        matched_by: str, t0: float,
                        all_scores: Optional[Dict] = None) -> RoutingDecision:
        return RoutingDecision(
            query=query,
            route_id=route.route_id,
            route_name=route.name,
            score=score,
            strategy_used=self.strategy,
            matched_by=matched_by,
            duration_ms=(time.time() - t0) * 1000,
            all_scores=all_scores or {})

    def _fallback_decision(self, query: str, t0: float,
                            all_scores: Optional[Dict] = None) -> RoutingDecision:
        self._miss_count += 1
        if self.fallback_route_id and self.fallback_route_id in self._routes:
            fb = self._routes[self.fallback_route_id]
            d = RoutingDecision(
                query=query, route_id=fb.route_id, route_name=fb.name,
                score=0.0, strategy_used=self.strategy,
                matched_by="fallback",
                duration_ms=(time.time() - t0) * 1000,
                all_scores=all_scores or {})
            self._log(d)
            return d
        d = RoutingDecision(
            query=query, route_id=None, route_name=None,
            score=0.0, strategy_used=self.strategy,
            matched_by="none",
            duration_ms=(time.time() - t0) * 1000,
            all_scores=all_scores or {})
        self._log(d)
        return d

    # ── EXECUTION ─────────────────────────────────────────────────────

    def dispatch(self, query: str, *args, **kwargs) -> Tuple[RoutingDecision, Any]:
        """Route and immediately call the handler."""
        decision = self.route(query)
        if decision.route_id and decision.route_id in self._routes:
            result = self._routes[decision.route_id].handler(query, *args, **kwargs)
        else:
            result = None
        return decision, result

    # ── LOGGING ───────────────────────────────────────────────────────

    def _log(self, d: RoutingDecision):
        self._db.execute(
            "INSERT INTO sr_decisions (query,route_id,score,matched_by,ts) "
            "VALUES (?,?,?,?,?)",
            (d.query[:200], d.route_id, d.score, d.matched_by, time.time()))
        self._db.commit()

    def routing_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT query,route_id,score,matched_by,ts FROM sr_decisions "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"query": r[0][:60], "route": r[1],
                 "score": r[2], "by": r[3]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        total = self._match_count + self._miss_count
        return {
            "routes": len(self._routes),
            "matched": self._match_count,
            "missed": self._miss_count,
            "match_rate": round(self._match_count / total, 4) if total else 0.0,
            "strategy": self.strategy.value,
            "threshold": self.threshold,
        }
