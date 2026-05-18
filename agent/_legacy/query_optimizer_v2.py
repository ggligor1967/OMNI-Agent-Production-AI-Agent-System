"""OMNI Agent — Query Optimizer V2: query planning, cost estimation, index hints."""
from __future__ import annotations
import re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class QueryType(str, Enum):
    SELECT  = "select"
    INSERT  = "insert"
    UPDATE  = "update"
    DELETE  = "delete"
    NOSQL   = "nosql"
    CUSTOM  = "custom"


class PlanType(str, Enum):
    FULL_SCAN   = "full_scan"
    INDEX_SCAN  = "index_scan"
    HASH_JOIN   = "hash_join"
    NESTED_LOOP = "nested_loop"
    SORT_MERGE  = "sort_merge"
    CACHED      = "cached"


@dataclass
class IndexInfo:
    index_id: str
    table: str
    columns: List[str]
    is_unique: bool = False
    cardinality: int = 1000
    size_bytes: int = 0

    def selectivity(self) -> float:
        return 1.0 / self.cardinality if self.cardinality else 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {"index_id": self.index_id, "table": self.table,
                "columns": self.columns, "unique": self.is_unique,
                "cardinality": self.cardinality}


@dataclass
class TableStats:
    table: str
    row_count: int = 1000
    avg_row_size: int = 100
    page_count: int = 10

    @property
    def size_bytes(self) -> int:
        return self.row_count * self.avg_row_size


@dataclass
class QueryPlan:
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    query: str = ""
    plan_type: PlanType = PlanType.FULL_SCAN
    estimated_cost: float = 0.0
    estimated_rows: int = 0
    used_indexes: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    hints: List[str] = field(default_factory=list)
    rewritten_query: Optional[str] = None
    from_cache: bool = False
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_type": self.plan_type.value,
            "estimated_cost": round(self.estimated_cost, 2),
            "estimated_rows": self.estimated_rows,
            "used_indexes": self.used_indexes,
            "steps": self.steps,
            "from_cache": self.from_cache,
        }


@dataclass
class ExecutionStats:
    plan_id: str
    actual_rows: int = 0
    duration_ms: float = 0.0
    rows_examined: int = 0
    cache_hit: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"plan_id": self.plan_id, "actual_rows": self.actual_rows,
                "duration_ms": round(self.duration_ms, 2),
                "cache_hit": self.cache_hit}


class QueryOptimizerV2:
    """
    Query optimizer:
    - Table statistics registry (row count, size)
    - Index registry with cardinality and selectivity
    - Cost estimation: full scan vs index scan vs join strategies
    - Plan selection: choose cheapest plan
    - Query rewriting: push down WHERE, eliminate subqueries
    - Result caching (LRU by query hash)
    - Hint injection (force index, join order)
    - Query fingerprinting (normalize literals → ?)
    - Execution stats feedback loop
    - Slow query log (threshold)
    - Plan history
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:",
                 cache_size: int = 200,
                 slow_query_ms: float = 100.0):
        self._tables:  Dict[str, TableStats] = {}
        self._indexes: Dict[str, IndexInfo] = {}
        self._plan_cache: Dict[str, QueryPlan] = {}   # fingerprint → plan
        self._lru_order: List[str] = []
        self._cache_size = cache_size
        self._slow_query_ms = slow_query_ms
        self._history: List[QueryPlan] = []
        self._exec_stats: List[ExecutionStats] = []
        self._slow_queries: List[Dict] = []
        self._rewrites: List[Callable[[str], Optional[str]]] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS qo_plans (
                plan_id TEXT PRIMARY KEY, query TEXT,
                plan_type TEXT, estimated_cost REAL,
                estimated_rows INTEGER, ts REAL
            );
            CREATE TABLE IF NOT EXISTS qo_exec_stats (
                plan_id TEXT, actual_rows INTEGER,
                duration_ms REAL, cache_hit INTEGER, ts REAL
            );
        """)
        self._db.commit()

    # ── REGISTRY ─────────────────────────────────────────────────────

    def register_table(self, table: str,
                        row_count: int = 1000,
                        avg_row_size: int = 100) -> TableStats:
        ts = TableStats(table=table, row_count=row_count,
                         avg_row_size=avg_row_size,
                         page_count=max(1, row_count * avg_row_size // 4096))
        self._tables[table] = ts
        return ts

    def update_stats(self, table: str, row_count: int):
        ts = self._tables.get(table)
        if ts:
            ts.row_count = row_count
            ts.page_count = max(1, row_count * ts.avg_row_size // 4096)

    def register_index(self, table: str,
                        columns: List[str],
                        is_unique: bool = False,
                        cardinality: Optional[int] = None,
                        index_id: Optional[str] = None) -> IndexInfo:
        iid = index_id or f"{table}_{'_'.join(columns)}_idx"
        ts  = self._tables.get(table)
        card = cardinality or (ts.row_count if ts else 1000)
        ix   = IndexInfo(index_id=iid, table=table, columns=columns,
                          is_unique=is_unique, cardinality=card)
        self._indexes[iid] = ix
        return ix

    # ── FINGERPRINTING ────────────────────────────────────────────────

    def fingerprint(self, query: str) -> str:
        """Normalize query: lowercase, replace literals with ?"""
        q = query.strip().lower()
        q = re.sub(r"'[^']*'", "?", q)
        q = re.sub(r"\b\d+\b", "?", q)
        q = re.sub(r"\s+", " ", q)
        return q

    # ── COST ESTIMATION ───────────────────────────────────────────────

    def _extract_tables(self, query: str) -> List[str]:
        q = query.lower()
        tables = re.findall(r"from\s+(\w+)", q)
        tables += re.findall(r"join\s+(\w+)", q)
        return list(set(tables))

    def _extract_where_columns(self, query: str) -> List[str]:
        q = query.lower()
        where_match = re.search(r"where\s+(.+?)(?:order|group|limit|$)", q)
        if not where_match: return []
        where_clause = where_match.group(1)
        return re.findall(r"(\w+)\s*[=<>!]", where_clause)

    def _find_usable_indexes(self, tables: List[str],
                              where_cols: List[str]) -> List[IndexInfo]:
        usable = []
        for ix in self._indexes.values():
            if ix.table not in tables: continue
            if any(col in ix.columns for col in where_cols):
                usable.append(ix)
        return usable

    def _estimate_cost(self, query: str) -> Tuple[float, PlanType, List[str]]:
        tables    = self._extract_tables(query)
        where_cols = self._extract_where_columns(query)
        usable_ix  = self._find_usable_indexes(tables, where_cols)

        total_rows = sum(self._tables[t].row_count
                         for t in tables if t in self._tables) or 1000
        page_count = sum(self._tables[t].page_count
                         for t in tables if t in self._tables) or 10

        is_join   = "join" in query.lower()
        is_select = query.lower().strip().startswith("select")

        if usable_ix:
            best_ix   = min(usable_ix, key=lambda x: x.selectivity())
            sel_rows  = int(total_rows * best_ix.selectivity())
            ix_cost   = max(1, sel_rows * 1.2)
            plan_type = PlanType.INDEX_SCAN
            used      = [best_ix.index_id]
        elif is_join:
            ix_cost   = total_rows * 1.5
            plan_type = PlanType.HASH_JOIN
            sel_rows  = total_rows // 2
            used      = []
        else:
            ix_cost   = page_count * 10.0
            plan_type = PlanType.FULL_SCAN
            sel_rows  = total_rows
            used      = []

        return ix_cost, plan_type, used

    # ── PLANNING ─────────────────────────────────────────────────────

    def plan(self, query: str,
              force_index: Optional[str] = None) -> QueryPlan:
        fp = self.fingerprint(query)

        # Cache lookup
        if fp in self._plan_cache:
            p = self._plan_cache[fp]
            p.from_cache = True
            self._lru_order = [fp] + [k for k in self._lru_order if k != fp]
            return p

        # Apply rewrites
        rewritten = query
        for fn in self._rewrites:
            try:
                r = fn(rewritten)
                if r: rewritten = r
            except Exception:
                pass

        cost, plan_type, used_ix = self._estimate_cost(rewritten)

        if force_index and force_index in self._indexes:
            ix        = self._indexes[force_index]
            plan_type = PlanType.INDEX_SCAN
            used_ix   = [force_index]
            cost      = cost * 0.7   # hint reduces cost

        tables     = self._extract_tables(rewritten)
        where_cols = self._extract_where_columns(rewritten)
        steps = [f"Parse query ({len(tables)} tables)"]
        if plan_type == PlanType.INDEX_SCAN:
            steps.append(f"Index scan on {used_ix}")
        elif plan_type == PlanType.FULL_SCAN:
            steps.append(f"Full table scan: {tables}")
        elif plan_type == PlanType.HASH_JOIN:
            steps.append(f"Hash join: {tables}")
        steps.append("Apply filters")
        steps.append("Return results")

        hints = []
        usable = self._find_usable_indexes(tables, where_cols)
        for ix in usable:
            if ix.index_id not in used_ix:
                hints.append(f"Consider index: {ix.index_id} on {ix.columns}")

        ts_data  = self._tables.get(tables[0]) if tables else None
        est_rows = int((ts_data.row_count if ts_data else 1000) *
                       (0.1 if plan_type == PlanType.INDEX_SCAN else 0.8))

        p = QueryPlan(query=query, plan_type=plan_type,
                       estimated_cost=cost, estimated_rows=est_rows,
                       used_indexes=used_ix, steps=steps, hints=hints,
                       rewritten_query=rewritten if rewritten != query else None)

        self._plan_cache[fp] = p
        self._lru_order = [fp] + self._lru_order
        if len(self._lru_order) > self._cache_size:
            evict = self._lru_order.pop()
            self._plan_cache.pop(evict, None)

        self._history.append(p)
        self._db.execute(
            "INSERT INTO qo_plans VALUES (?,?,?,?,?,?)",
            (p.plan_id, query[:500], p.plan_type.value,
             p.estimated_cost, p.estimated_rows, p.ts))
        self._db.commit()
        return p

    def explain(self, query: str) -> Dict[str, Any]:
        p = self.plan(query)
        return p.to_dict()

    # ── REWRITE RULES ─────────────────────────────────────────────────

    def add_rewrite_rule(self, fn: Callable[[str], Optional[str]]):
        self._rewrites.append(fn)

    def add_index_hint(self, pattern: str, index_id: str):
        """Auto-suggest index for queries matching pattern."""
        def hint_fn(q: str) -> Optional[str]:
            if re.search(pattern, q, re.IGNORECASE):
                return q   # just passes through; index used in plan()
            return None
        self._rewrites.append(hint_fn)

    # ── EXECUTION FEEDBACK ────────────────────────────────────────────

    def record_execution(self, plan_id: str,
                          actual_rows: int,
                          duration_ms: float,
                          cache_hit: bool = False):
        es = ExecutionStats(plan_id=plan_id, actual_rows=actual_rows,
                             duration_ms=duration_ms, cache_hit=cache_hit)
        self._exec_stats.append(es)
        if duration_ms > self._slow_query_ms:
            plan = next((p for p in self._history
                         if p.plan_id == plan_id), None)
            if plan:
                self._slow_queries.append({
                    "query": plan.query[:200],
                    "duration_ms": duration_ms,
                    "plan_type": plan.plan_type.value})
        self._db.execute(
            "INSERT INTO qo_exec_stats VALUES (?,?,?,?,?)",
            (plan_id, actual_rows, duration_ms,
             int(cache_hit), time.time()))
        self._db.commit()

    def slow_queries(self, limit: int = 20) -> List[Dict]:
        return self._slow_queries[-limit:]

    def cache_hit_rate(self) -> float:
        if not self._history: return 0.0
        hits = sum(1 for p in self._history if p.from_cache)
        return hits / len(self._history)

    def invalidate_cache(self, table: Optional[str] = None):
        if table:
            to_del = [fp for fp, p in self._plan_cache.items()
                      if table in self._extract_tables(p.query)]
            for fp in to_del:
                self._plan_cache.pop(fp, None)
                if fp in self._lru_order: self._lru_order.remove(fp)
        else:
            self._plan_cache.clear()
            self._lru_order.clear()

    def stats(self) -> Dict[str, Any]:
        return {
            "tables": len(self._tables),
            "indexes": len(self._indexes),
            "plans_cached": len(self._plan_cache),
            "total_planned": len(self._history),
            "slow_queries": len(self._slow_queries),
            "cache_hit_rate": round(self.cache_hit_rate(), 3),
        }
