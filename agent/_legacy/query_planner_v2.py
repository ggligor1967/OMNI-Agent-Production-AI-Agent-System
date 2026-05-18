"""OMNI Agent — Query Planner V2: query planning, cost estimation, index hints, execution."""
from __future__ import annotations
import sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class QueryType(str, Enum):
    SELECT  = "select"
    INSERT  = "insert"
    UPDATE  = "update"
    DELETE  = "delete"
    UPSERT  = "upsert"
    SCAN    = "scan"
    LOOKUP  = "lookup"
    AGGREGATE = "aggregate"


class JoinType(str, Enum):
    INNER = "inner"
    LEFT  = "left"
    RIGHT = "right"
    FULL  = "full"
    CROSS = "cross"


class PlanNodeType(str, Enum):
    SEQ_SCAN      = "seq_scan"
    INDEX_SCAN    = "index_scan"
    INDEX_SEEK    = "index_seek"
    HASH_JOIN     = "hash_join"
    NESTED_LOOP   = "nested_loop"
    MERGE_JOIN    = "merge_join"
    SORT          = "sort"
    HASH_AGG      = "hash_agg"
    FILTER        = "filter"
    LIMIT         = "limit"
    PROJECTION    = "projection"


@dataclass
class IndexDef:
    index_id: str
    table: str
    columns: List[str]
    unique: bool = False
    covering: bool = False        # covers all queried columns
    selectivity: float = 0.1     # fraction of rows matched (lower = more selective)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index_id": self.index_id,
            "table": self.table,
            "columns": self.columns,
            "unique": self.unique,
            "selectivity": self.selectivity,
        }


@dataclass
class TableStats:
    table: str
    row_count: int = 0
    avg_row_size_b: int = 64
    pages: int = 0

    @property
    def size_mb(self) -> float:
        return self.row_count * self.avg_row_size_b / 1_048_576


@dataclass
class PlanNode:
    node_type: PlanNodeType
    table: Optional[str] = None
    index: Optional[str] = None
    cost: float = 0.0           # estimated cost units
    rows: int = 0               # estimated output rows
    children: List["PlanNode"] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def total_cost(self) -> float:
        return self.cost + sum(c.total_cost() for c in self.children)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_type": self.node_type.value,
            "table": self.table,
            "index": self.index,
            "cost": round(self.cost, 2),
            "rows": self.rows,
            "total_cost": round(self.total_cost(), 2),
            "children": [c.to_dict() for c in self.children],
            **self.details,
        }


@dataclass
class QueryPlan:
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    query_type: QueryType = QueryType.SELECT
    tables: List[str] = field(default_factory=list)
    filters: List[Dict[str, Any]] = field(default_factory=list)
    projections: List[str] = field(default_factory=list)
    order_by: List[str] = field(default_factory=list)
    limit: Optional[int] = None
    root: Optional[PlanNode] = None
    estimated_cost: float = 0.0
    estimated_rows: int = 0
    hints: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "query_type": self.query_type.value,
            "tables": self.tables,
            "estimated_cost": round(self.estimated_cost, 2),
            "estimated_rows": self.estimated_rows,
            "hints": self.hints,
            "root": self.root.to_dict() if self.root else None,
        }


@dataclass
class QueryResult:
    plan_id: str
    rows: List[Dict[str, Any]]
    execution_ms: float
    plan_cost: float
    rows_examined: int = 0
    cache_hit: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "row_count": len(self.rows),
            "execution_ms": round(self.execution_ms, 2),
            "plan_cost": round(self.plan_cost, 2),
            "rows_examined": self.rows_examined,
            "cache_hit": self.cache_hit,
        }


class QueryPlannerV2:
    """
    Query planning engine: registers table stats + indexes,
    generates cost-based query plans, and executes them via
    pluggable data source functions.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._tables: Dict[str, TableStats] = {}
        self._indexes: Dict[str, List[IndexDef]] = {}   # table → [index]
        self._sources: Dict[str, Callable] = {}          # table → fetch_fn
        self._plan_cache: Dict[str, QueryPlan] = {}
        self._query_log: List[Dict[str, Any]] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._exec_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS qp_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT, query_type TEXT, tables TEXT,
                cost REAL, exec_ms REAL, rows INTEGER, ts REAL
            );
        """)
        self._db.commit()

    # ── SCHEMA REGISTRATION ───────────────────────────────────────────

    def register_table(self, table: str, row_count: int = 1000,
                        avg_row_size_b: int = 64) -> TableStats:
        stats = TableStats(table=table, row_count=row_count,
                           avg_row_size_b=avg_row_size_b,
                           pages=max(1, row_count * avg_row_size_b // 4096))
        self._tables[table] = stats
        self._indexes.setdefault(table, [])
        return stats

    def register_index(self, table: str, columns: List[str],
                        unique: bool = False, covering: bool = False,
                        selectivity: float = 0.1) -> IndexDef:
        idx = IndexDef(
            index_id=f"idx_{table}_{'_'.join(columns)}",
            table=table, columns=columns, unique=unique,
            covering=covering, selectivity=selectivity)
        self._indexes.setdefault(table, []).append(idx)
        return idx

    def register_source(self, table: str, fetch_fn: Callable):
        """Register data fetch fn(filters, limit) → list[dict]."""
        self._sources[table] = fetch_fn

    # ── PLANNING ──────────────────────────────────────────────────────

    def plan(self, tables: List[str],
             filters: Optional[List[Dict]] = None,
             projections: Optional[List[str]] = None,
             order_by: Optional[List[str]] = None,
             limit: Optional[int] = None,
             query_type: QueryType = QueryType.SELECT,
             force_hints: Optional[List[str]] = None) -> QueryPlan:
        """Generate a cost-based query plan."""
        qp = QueryPlan(
            query_type=query_type,
            tables=list(tables),
            filters=list(filters or []),
            projections=list(projections or ["*"]),
            order_by=list(order_by or []),
            limit=limit,
            hints=list(force_hints or []),
        )

        # Build scan nodes for each table
        scan_nodes = []
        for table in tables:
            node = self._best_access_path(table, filters or [])
            scan_nodes.append(node)

        # Join multiple tables
        if len(scan_nodes) == 1:
            root = scan_nodes[0]
        else:
            root = self._build_join_tree(scan_nodes)

        # Filter node
        if filters:
            root = PlanNode(PlanNodeType.FILTER, cost=root.rows * 0.01,
                            rows=max(1, int(root.rows * 0.3)), children=[root])

        # Sort node
        if order_by:
            has_idx = any(
                any(c in order_by for c in idx.columns)
                for t in tables
                for idx in self._indexes.get(t, []))
            sort_cost = 0.0 if has_idx else root.rows * 0.05
            if sort_cost > 0:
                root = PlanNode(PlanNodeType.SORT, cost=sort_cost,
                                rows=root.rows, children=[root],
                                details={"order_by": order_by})

        # Limit node
        if limit is not None:
            root = PlanNode(PlanNodeType.LIMIT, cost=0.1, rows=min(limit, root.rows),
                            children=[root], details={"limit": limit})

        # Projection
        if projections and projections != ["*"]:
            root = PlanNode(PlanNodeType.PROJECTION, cost=root.rows * 0.001,
                            rows=root.rows, children=[root],
                            details={"columns": projections})

        qp.root = root
        qp.estimated_cost = root.total_cost()
        qp.estimated_rows = root.rows

        # Generate hints
        qp.hints.extend(self._generate_hints(qp))
        self._plan_cache[qp.plan_id] = qp
        return qp

    def _best_access_path(self, table: str,
                           filters: List[Dict]) -> PlanNode:
        stats = self._tables.get(table, TableStats(table=table, row_count=1000))
        indexes = self._indexes.get(table, [])

        # Find best index for filters
        filtered_cols = {f.get("column") for f in filters if "column" in f}
        best_idx: Optional[IndexDef] = None
        best_sel = 1.0
        for idx in indexes:
            if any(c in filtered_cols for c in idx.columns):
                if idx.selectivity < best_sel:
                    best_sel = idx.selectivity
                    best_idx = idx

        if best_idx:
            rows = max(1, int(stats.row_count * best_sel))
            node_type = (PlanNodeType.INDEX_SEEK if best_idx.unique
                         else PlanNodeType.INDEX_SCAN)
            cost = rows * 1.0 + math.log2(stats.row_count + 1) * 0.5
            return PlanNode(node_type, table=table, index=best_idx.index_id,
                            cost=cost, rows=rows)
        else:
            # Full table scan
            cost = stats.row_count * 0.5
            return PlanNode(PlanNodeType.SEQ_SCAN, table=table,
                            cost=cost, rows=stats.row_count)

    def _build_join_tree(self, nodes: List[PlanNode]) -> PlanNode:
        """Build left-deep hash join tree."""
        root = nodes[0]
        for node in nodes[1:]:
            out_rows = max(1, int(root.rows * node.rows / 1000))
            root = PlanNode(PlanNodeType.HASH_JOIN,
                            cost=root.rows * 0.1 + node.rows * 0.1,
                            rows=out_rows,
                            children=[root, node])
        return root

    def _generate_hints(self, plan: QueryPlan) -> List[str]:
        hints = []
        if plan.estimated_cost > 10_000:
            hints.append("WARN: High cost query — consider adding indexes")
        for table in plan.tables:
            if not self._indexes.get(table) and plan.filters:
                hints.append(f"HINT: No index on '{table}' for filtered columns")
        if plan.limit and plan.estimated_rows > plan.limit * 10:
            hints.append("HINT: LIMIT applied late — index could improve performance")
        return hints

    # ── EXECUTION ─────────────────────────────────────────────────────

    def execute(self, plan: QueryPlan) -> QueryResult:
        t0 = time.time()
        self._exec_count += 1
        all_rows: List[Dict[str, Any]] = []
        rows_examined = 0

        for table in plan.tables:
            fn = self._sources.get(table)
            if fn:
                try:
                    rows = fn(plan.filters, plan.limit)
                    rows_examined += len(rows)
                    all_rows.extend(rows)
                except Exception:
                    pass

        # Apply limit
        if plan.limit:
            all_rows = all_rows[:plan.limit]

        # Apply projection
        if plan.projections and plan.projections != ["*"]:
            all_rows = [{k: r[k] for k in plan.projections if k in r}
                        for r in all_rows]

        exec_ms = (time.time() - t0) * 1000
        self._db.execute(
            "INSERT INTO qp_log (plan_id,query_type,tables,cost,exec_ms,rows,ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (plan.plan_id, plan.query_type.value, ",".join(plan.tables),
             plan.estimated_cost, exec_ms, len(all_rows), time.time()))
        self._db.commit()

        return QueryResult(
            plan_id=plan.plan_id,
            rows=all_rows,
            execution_ms=exec_ms,
            plan_cost=plan.estimated_cost,
            rows_examined=rows_examined,
        )

    def plan_and_execute(self, tables: List[str], **kwargs) -> QueryResult:
        plan = self.plan(tables, **kwargs)
        return self.execute(plan)

    # ── ANALYSIS ──────────────────────────────────────────────────────

    def explain(self, plan: QueryPlan) -> str:
        lines = [f"Query Plan [{plan.plan_id}]",
                 f"  Type: {plan.query_type.value}",
                 f"  Tables: {', '.join(plan.tables)}",
                 f"  Estimated Cost: {plan.estimated_cost:.2f}",
                 f"  Estimated Rows: {plan.estimated_rows}"]
        if plan.hints:
            lines.append("  Hints:")
            for h in plan.hints:
                lines.append(f"    - {h}")
        if plan.root:
            lines.append("  Plan Tree:")
            lines.extend(self._format_node(plan.root, indent=4))
        return "\n".join(lines)

    def _format_node(self, node: PlanNode, indent: int) -> List[str]:
        prefix = " " * indent
        lines  = [f"{prefix}[{node.node_type.value}]"
                  f"  cost={node.cost:.1f}  rows={node.rows}"
                  + (f"  table={node.table}" if node.table else "")
                  + (f"  index={node.index}" if node.index else "")]
        for child in node.children:
            lines.extend(self._format_node(child, indent + 2))
        return lines

    def query_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT plan_id,query_type,tables,cost,exec_ms,rows,ts "
            "FROM qp_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"plan_id": r[0], "type": r[1], "tables": r[2],
                 "cost": r[3], "exec_ms": r[4], "rows": r[5]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        return {
            "tables": len(self._tables),
            "indexes": sum(len(v) for v in self._indexes.values()),
            "sources": len(self._sources),
            "cached_plans": len(self._plan_cache),
            "executions": self._exec_count,
        }


import math  # needed by _best_access_path
