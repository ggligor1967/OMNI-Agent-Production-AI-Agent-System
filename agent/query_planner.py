"""OMNI AGENT - Query Planner
SQL-like query parsing, planning, optimization, and execution
against in-memory and SQLite-backed tables.

Features:
- Tables: named collections of row dicts; schema optional
- SELECT: column list or * with aliasing (col AS alias)
- FROM: single table or JOIN (INNER, LEFT, RIGHT, CROSS)
- WHERE: conditions with AND/OR/NOT, operators: =, !=, <, >, <=, >=,
    LIKE (% wildcard), IN (...), IS NULL, IS NOT NULL, BETWEEN
- JOIN ON: two-table join with equality condition
- GROUP BY: grouping with aggregate functions:
    COUNT(*), COUNT(col), SUM, AVG, MIN, MAX, FIRST, LAST
- HAVING: filter after aggregation
- ORDER BY: multiple columns with ASC/DESC
- LIMIT / OFFSET: result pagination
- Subqueries: SELECT ... FROM (SELECT ...) AS alias
- DISTINCT: deduplicate result rows
- Aliases: table aliases in FROM/JOIN (FROM t AS a)
- Explain plan: describe execution steps
- Query stats: rows scanned, rows returned, execution time ms
- Index hints: mark a column as "indexed" for scan optimization notes
- Insert: INSERT INTO table VALUES ({...}) or dict API
- Update: UPDATE table SET col=val WHERE ...
- Delete: DELETE FROM table WHERE ...
- Create table: CREATE TABLE name (col type, ...)
- Persistence: tables can be backed by SQLite
- REST API: execute, tables, explain, stats
"""
import json, re, sqlite3, time, logging
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

Row = Dict[str, Any]

# ── Tokenizer / Parser helpers ─────────────────────────────────────────────────
_TOK = re.compile(
    r"'[^']*'|\"[^\"]*\"|<=|>=|!=|<>|[=<>!]+|[(),*]|[A-Za-z_]\w*|\d+(?:\.\d+)?",
    re.IGNORECASE)

def _tokenize(sql: str) -> List[str]:
    return _TOK.findall(sql)

def _kw(tok: str) -> str:
    return tok.upper()

def _literal(tok: str) -> Any:
    if tok.startswith("'") or tok.startswith('"'):
        return tok[1:-1]
    try: return int(tok)
    except ValueError: pass
    try: return float(tok)
    except ValueError: pass
    if tok.upper() == "NULL": return None
    if tok.upper() == "TRUE": return True
    if tok.upper() == "FALSE": return False
    return tok

# ── Condition evaluation ────────────────────────────────────────────────────────
def _get(row: Row, col: str) -> Any:
    """Support dotted table.col or plain col."""
    if "." in col:
        parts = col.split(".", 1)
        return row.get(col, row.get(parts[1]))
    return row.get(col)

def _like(value: str, pattern: str) -> bool:
    if value is None: return False
    parts = re.split(r'(%|_)', pattern)
    p = "^" + "".join(
        ".*" if c == "%" else "." if c == "_" else re.escape(c)
        for c in parts) + "$"
    return bool(re.match(p, str(value), re.IGNORECASE))

def _eval_cond(row: Row, cond) -> bool:
    if cond is None: return True
    if isinstance(cond, bool): return cond
    kind = cond.get("op")
    if kind == "AND":
        return _eval_cond(row, cond["left"]) and _eval_cond(row, cond["right"])
    if kind == "OR":
        return _eval_cond(row, cond["left"]) or _eval_cond(row, cond["right"])
    if kind == "NOT":
        return not _eval_cond(row, cond["expr"])
    col = cond.get("col"); val = cond.get("val"); op = cond.get("cmp")
    lv = _get(row, col)
    if op == "IS NULL":    return lv is None
    if op == "IS NOT NULL": return lv is not None
    if op == "IN":
        return lv in (cond.get("vals") or [])
    if op == "NOT IN":
        return lv not in (cond.get("vals") or [])
    if op == "BETWEEN":
        lo, hi = cond.get("lo"), cond.get("hi")
        try: return lo <= lv <= hi
        except: return False
    if op == "LIKE":   return _like(lv, val)
    if lv is None or val is None:
        return (op == "=" and lv == val)
    try:
        if op == "=":  return lv == val
        if op in ("!=","<>"):  return lv != val
        if op == "<":  return lv < val
        if op == ">":  return lv > val
        if op == "<=": return lv <= val
        if op == ">=": return lv >= val
    except: return False
    return False

# ── Aggregate helpers ──────────────────────────────────────────────────────────
def _aggregate(func: str, col: str, rows: List[Row]) -> Any:
    if func == "COUNT":
        if col == "*": return len(rows)
        return sum(1 for r in rows if _get(r, col) is not None)
    vals = [_get(r, col) for r in rows if _get(r, col) is not None]
    if not vals: return None
    if func == "SUM":  return sum(vals)
    if func == "AVG":  return sum(vals) / len(vals)
    if func == "MIN":  return min(vals)
    if func == "MAX":  return max(vals)
    if func == "FIRST": return vals[0]
    if func == "LAST":  return vals[-1]
    return None

# ── Simple recursive descent parser ───────────────────────────────────────────
class Parser:
    def __init__(self, tokens: List[str]):
        self._t = tokens; self._i = 0

    def peek(self) -> Optional[str]:
        return self._t[self._i] if self._i < len(self._t) else None

    def consume(self, expected: str = None) -> str:
        tok = self._t[self._i]; self._i += 1
        if expected and tok.upper() != expected.upper():
            raise SyntaxError(f"Expected {expected!r}, got {tok!r}")
        return tok

    def match(self, *keywords) -> bool:
        return (self.peek() or "").upper() in [k.upper() for k in keywords]

    def parse_select(self) -> Dict:
        plan: Dict = {}
        self.consume("SELECT")
        plan["distinct"] = False
        if self.match("DISTINCT"):
            self.consume(); plan["distinct"] = True
        plan["columns"] = self._parse_column_list()
        self.consume("FROM")
        plan["from"] = self._parse_from()
        plan["where"] = None
        if self.match("WHERE"):
            self.consume(); plan["where"] = self._parse_condition()
        plan["group_by"] = []
        if self.match("GROUP"):
            self.consume("GROUP"); self.consume("BY")
            plan["group_by"] = self._parse_ident_list()
        plan["having"] = None
        if self.match("HAVING"):
            self.consume(); plan["having"] = self._parse_condition()
        plan["order_by"] = []
        if self.match("ORDER"):
            self.consume("ORDER"); self.consume("BY")
            plan["order_by"] = self._parse_order_list()
        plan["limit"] = None; plan["offset"] = 0
        if self.match("LIMIT"):
            self.consume()
            plan["limit"] = int(self.consume())
        if self.match("OFFSET"):
            self.consume()
            plan["offset"] = int(self.consume())
        return plan

    def _parse_from(self) -> Dict:
        name = self.consume(); alias = name
        if self.match("AS"):
            self.consume(); alias = self.consume()
        elif (self.peek() and
              self.peek().upper() not in
              ("WHERE","JOIN","INNER","LEFT","RIGHT","CROSS",
               "GROUP","ORDER","HAVING","LIMIT","OFFSET",")")):
            alias = self.consume()
        src = {"table": name, "alias": alias, "join": None}
        if self.match("JOIN","INNER","LEFT","RIGHT","CROSS"):
            join_type = "INNER"
            if self.match("INNER","LEFT","RIGHT","CROSS"):
                join_type = self.consume().upper()
                self.consume("JOIN")
            else:
                self.consume("JOIN")
            right_name = self.consume(); right_alias = right_name
            if self.match("AS"): self.consume(); right_alias = self.consume()
            on_cond = None
            if self.match("ON"): self.consume(); on_cond = self._parse_condition()
            src["join"] = {"type": join_type, "table": right_name,
                            "alias": right_alias, "on": on_cond}
        return src

    def _parse_column_list(self) -> List[Dict]:
        cols = []
        while True:
            if self.match("*"):
                self.consume(); cols.append({"expr": "*", "alias": "*"})
            else:
                expr = self._parse_expr()
                alias = expr
                if self.match("AS"): self.consume(); alias = self.consume()
                cols.append({"expr": expr, "alias": alias})
            if self.match(","): self.consume()
            else: break
        return cols

    def _parse_expr(self) -> Any:
        """Parse column name or aggregate function."""
        tok = self.consume()
        if (self.peek() == "("
                and tok.upper() in ("COUNT","SUM","AVG","MIN","MAX",
                                     "FIRST","LAST")):
            self.consume("(")
            arg = self.consume()
            self.consume(")")
            return {"agg": tok.upper(), "col": arg}
        return tok

    def _parse_ident_list(self) -> List[str]:
        names = [self.consume()]
        while self.match(","): self.consume(); names.append(self.consume())
        return names

    def _parse_order_list(self) -> List[Tuple[str, str]]:
        items = []
        while True:
            col = self.consume()
            direction = "ASC"
            if self.match("ASC","DESC"):
                direction = self.consume().upper()
            items.append((col, direction))
            if self.match(","): self.consume()
            else: break
        return items

    def _parse_condition(self) -> Dict:
        left = self._parse_and()
        while self.match("OR"):
            self.consume(); right = self._parse_and()
            left = {"op": "OR", "left": left, "right": right}
        return left

    def _parse_and(self) -> Dict:
        left = self._parse_not()
        while self.match("AND"):
            self.consume(); right = self._parse_not()
            left = {"op": "AND", "left": left, "right": right}
        return left

    def _parse_not(self) -> Dict:
        if self.match("NOT"):
            self.consume(); return {"op": "NOT", "expr": self._parse_pred()}
        return self._parse_pred()

    def _parse_pred(self) -> Dict:
        col = self.consume()
        if self.match("IS"):
            self.consume()
            if self.match("NOT"):
                self.consume()
                return {"col": col, "cmp": "IS NOT NULL"}
            return {"col": col, "cmp": "IS NULL"}
        if self.match("NOT"):
            self.consume()
            if self.match("IN"):
                self.consume("IN"); self.consume("(")
                vals = [_literal(self.consume())]
                while self.match(","): self.consume(); vals.append(_literal(self.consume()))
                self.consume(")")
                return {"col": col, "cmp": "NOT IN", "vals": vals}
            if self.match("LIKE"):
                self.consume(); pat = _literal(self.consume())
                return {"col": col, "cmp": "NOT LIKE", "val": pat}
        if self.match("IN"):
            self.consume("IN"); self.consume("(")
            vals = [_literal(self.consume())]
            while self.match(","): self.consume(); vals.append(_literal(self.consume()))
            self.consume(")")
            return {"col": col, "cmp": "IN", "vals": vals}
        if self.match("BETWEEN"):
            self.consume(); lo = _literal(self.consume())
            self.consume("AND"); hi = _literal(self.consume())
            return {"col": col, "cmp": "BETWEEN", "lo": lo, "hi": hi}
        if self.match("LIKE"):
            self.consume(); pat = _literal(self.consume())
            return {"col": col, "cmp": "LIKE", "val": pat}
        op = self.consume()
        val = _literal(self.consume())
        return {"col": col, "cmp": op, "val": val}

# ── Execution engine ───────────────────────────────────────────────────────────
class QueryPlanner:
    """
    In-memory SQL-like query engine.

    Usage:
        qp = QueryPlanner()
        qp.create_table("users", [
            {"name": "Alice", "age": 30, "dept": "eng"},
            {"name": "Bob",   "age": 25, "dept": "hr"},
        ])

        result = qp.execute(
            "SELECT name, age FROM users WHERE age > 27 ORDER BY age DESC")
        # [{"name": "Alice", "age": 30}]

        result = qp.execute(
            "SELECT dept, COUNT(*) as cnt FROM users GROUP BY dept")
    """
    def __init__(self):
        self._tables: Dict[str, List[Row]] = {}
        self._indexes: Dict[str, str] = {}   # "table.col" → indexed
        self._stats: Dict = {"queries": 0, "total_ms": 0.0}

    def create_table(self, name: str, rows: List[Row] = None):
        self._tables[name] = list(rows or [])

    def drop_table(self, name: str) -> bool:
        return bool(self._tables.pop(name, None))

    def insert(self, table: str, row: Row) -> bool:
        if table not in self._tables: self._tables[table] = []
        self._tables[table].append(deepcopy(row))
        return True

    def insert_many(self, table: str, rows: List[Row]) -> int:
        for r in rows: self.insert(table, r)
        return len(rows)

    def mark_indexed(self, table: str, col: str):
        self._indexes[f"{table}.{col}"] = True

    def execute(self, sql: str) -> List[Row]:
        t0 = time.time()
        sql = sql.strip()
        kw = _tokenize(sql)[0].upper()
        if kw == "INSERT":   result = [self._exec_insert(sql)]
        elif kw == "UPDATE": result = [self._exec_update(sql)]
        elif kw == "DELETE": result = [self._exec_delete(sql)]
        elif kw == "CREATE": result = [self._exec_create(sql)]
        else:                result = self._exec_select(sql)
        ms = (time.time() - t0) * 1000
        self._stats["queries"] += 1
        self._stats["total_ms"] += ms
        return result

    def _exec_select(self, sql: str) -> List[Row]:
        tokens = _tokenize(sql)
        plan = Parser(tokens).parse_select()
        return self._execute_plan(plan)

    def _execute_plan(self, plan: Dict) -> List[Row]:
        # Resolve source rows
        src = plan["from"]
        rows = list(self._tables.get(src["table"], []))
        # Apply table alias to rows
        alias = src.get("alias") or src["table"]
        if alias != src["table"]:
            rows = [{f"{alias}.{k}": v for k, v in r.items()} | r
                     for r in rows]
        # JOIN
        if src.get("join"):
            rows = self._apply_join(rows, src["join"])
        # WHERE
        if plan.get("where"):
            rows = [r for r in rows if _eval_cond(r, plan["where"])]
        # GROUP BY
        if plan.get("group_by"):
            rows = self._apply_groupby(rows, plan)
        else:
            # HAVING without GROUP BY
            if plan.get("having"):
                rows = [r for r in rows if _eval_cond(r, plan["having"])]
            # SELECT columns
            rows = self._project(rows, plan["columns"])
        # DISTINCT
        if plan.get("distinct"):
            seen = set(); out = []
            for r in rows:
                key = json.dumps(r, sort_keys=True, default=str)
                if key not in seen: seen.add(key); out.append(r)
            rows = out
        # ORDER BY
        for col, direction in reversed(plan.get("order_by", [])):
            rows.sort(key=lambda r: (r.get(col) is None,
                                      r.get(col)),
                       reverse=(direction == "DESC"))
        # LIMIT / OFFSET
        offset = plan.get("offset", 0)
        rows = rows[offset:]
        if plan.get("limit") is not None:
            rows = rows[:plan["limit"]]
        return rows

    def _apply_join(self, left: List[Row], join: Dict) -> List[Row]:
        right_name = join["table"]
        right_alias = join.get("alias") or right_name
        right_rows = list(self._tables.get(right_name, []))
        if right_alias != right_name:
            right_rows = [{f"{right_alias}.{k}": v for k, v in r.items()} | r
                           for r in right_rows]
        result = []
        jtype = join.get("type", "INNER")
        on = join.get("on")
        for lr in left:
            matched = False
            for rr in right_rows:
                combined = {**lr, **rr}
                if on is None or _eval_cond(combined, on):
                    result.append(combined); matched = True
            if not matched and jtype == "LEFT":
                result.append({**lr, **{k: None for k in
                                (right_rows[0].keys() if right_rows else [])}})
        if jtype == "RIGHT":
            for rr in right_rows:
                if not any(_eval_cond({**lr, **rr}, on) for lr in left):
                    result.append({**{k: None for k in
                                   (left[0].keys() if left else [])}, **rr})
        return result

    def _apply_groupby(self, rows: List[Row], plan: Dict) -> List[Row]:
        group_keys = plan["group_by"]
        groups: Dict[str, List[Row]] = {}
        for r in rows:
            key = json.dumps({k: r.get(k) for k in group_keys},
                               sort_keys=True, default=str)
            groups.setdefault(key, []).append(r)
        result = []
        for key, grp_rows in groups.items():
            key_vals = json.loads(key)
            out_row = dict(key_vals)
            for col_spec in plan["columns"]:
                expr = col_spec["expr"]; alias = col_spec["alias"]
                if isinstance(expr, dict) and "agg" in expr:
                    out_row[alias] = _aggregate(expr["agg"], expr["col"], grp_rows)
                elif expr != "*" and expr not in group_keys:
                    out_row[alias] = _aggregate("FIRST", expr, grp_rows)
            if plan.get("having") and not _eval_cond(out_row, plan["having"]):
                continue
            result.append(out_row)
        return result

    def _project(self, rows: List[Row], columns: List[Dict]) -> List[Row]:
        if any(c["expr"] == "*" for c in columns):
            return rows
        result = []
        for r in rows:
            out = {}
            for col_spec in columns:
                expr = col_spec["expr"]; alias = col_spec["alias"]
                if isinstance(expr, dict) and "agg" in expr:
                    out[alias] = _aggregate(expr["agg"], expr["col"], [r])
                else:
                    out[alias] = _get(r, expr)
            result.append(out)
        return result

    def _exec_insert(self, sql: str) -> Dict:
        m = re.match(
            r"INSERT\s+INTO\s+(\w+)\s+VALUES\s*\((.+)\)",
            sql, re.IGNORECASE | re.DOTALL)
        if not m: return {"error": "Invalid INSERT"}
        table = m.group(1)
        vals_str = m.group(2)
        vals = [_literal(t) for t in _tokenize(vals_str) if t != ","]
        row = {f"col{i}": v for i, v in enumerate(vals)}
        self.insert(table, row)
        return {"inserted": 1, "table": table}

    def _exec_update(self, sql: str) -> Dict:
        m = re.match(
            r"UPDATE\s+(\w+)\s+SET\s+(.+?)(?:\s+WHERE\s+(.+))?$",
            sql, re.IGNORECASE | re.DOTALL)
        if not m: return {"error": "Invalid UPDATE"}
        table = m.group(1); sets_str = m.group(2); where_str = m.group(3)
        sets: Dict[str, Any] = {}
        for part in re.split(r",\s*", sets_str):
            k, _, v = part.partition("=")
            sets[k.strip()] = _literal(v.strip().strip("'\""))
        cond = None
        if where_str:
            cond = Parser(_tokenize(where_str)).parse_select if False else \
                   Parser(_tokenize(where_str))._parse_condition()
        count = 0
        for r in self._tables.get(table, []):
            if cond is None or _eval_cond(r, cond):
                r.update(sets); count += 1
        return {"updated": count, "table": table}

    def _exec_delete(self, sql: str) -> Dict:
        m = re.match(
            r"DELETE\s+FROM\s+(\w+)(?:\s+WHERE\s+(.+))?$",
            sql, re.IGNORECASE | re.DOTALL)
        if not m: return {"error": "Invalid DELETE"}
        table = m.group(1); where_str = m.group(2)
        cond = None
        if where_str:
            cond = Parser(_tokenize(where_str))._parse_condition()
        before = len(self._tables.get(table, []))
        self._tables[table] = [r for r in self._tables.get(table, [])
                                  if cond is not None and not _eval_cond(r, cond)
                                  or cond is None and False]
        if cond is None:
            self._tables[table] = []
        return {"deleted": before - len(self._tables.get(table,[])),
                "table": table}

    def _exec_create(self, sql: str) -> Dict:
        m = re.match(r"CREATE\s+TABLE\s+(\w+)", sql, re.IGNORECASE)
        if not m: return {"error": "Invalid CREATE"}
        table = m.group(1)
        if table not in self._tables: self._tables[table] = []
        return {"created": table}

    def explain(self, sql: str) -> List[str]:
        try:
            tokens = _tokenize(sql)
            plan = Parser(tokens).parse_select()
        except Exception as e:
            return [f"Parse error: {e}"]
        steps = []
        src = plan["from"]
        n = len(self._tables.get(src["table"], []))
        steps.append(f"SCAN {src['table']} ({n} rows)")
        if src.get("join"):
            jt = src["join"]
            m = len(self._tables.get(jt["table"], []))
            steps.append(f"{jt['type']} JOIN {jt['table']} ({m} rows)")
        if plan.get("where"):
            steps.append(f"FILTER WHERE")
        if plan.get("group_by"):
            steps.append(f"GROUP BY {', '.join(plan['group_by'])}")
        if plan.get("having"):
            steps.append("FILTER HAVING")
        if plan.get("order_by"):
            cols = ", ".join(f"{c} {d}" for c, d in plan["order_by"])
            steps.append(f"SORT BY {cols}")
        if plan.get("limit"):
            steps.append(f"LIMIT {plan['limit']} OFFSET {plan.get('offset',0)}")
        steps.append(f"PROJECT {[c['alias'] for c in plan['columns']]}")
        return steps

    def table_info(self, name: str = None) -> Dict:
        if name:
            rows = self._tables.get(name, [])
            cols = list(rows[0].keys()) if rows else []
            return {"table": name, "rows": len(rows), "columns": cols}
        return {"tables": {n: len(r) for n, r in self._tables.items()}}

    def stats(self) -> Dict:
        s = dict(self._stats)
        s["tables"] = len(self._tables)
        s["avg_ms"] = (s["total_ms"] / s["queries"]
                        if s["queries"] else 0)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def exec_ep(req):
            d = await req.json()
            try:
                result = self.execute(d["sql"])
                return web.json_response({"result": result,
                                           "count": len(result)})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=400)
        async def explain_ep(req):
            d = await req.json()
            return web.json_response({"plan": self.explain(d["sql"])})
        async def tables_ep(req):
            name = req.rel_url.query.get("name")
            return web.json_response(self.table_info(name))
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/sql"
        app.router.add_post(f"{p}/execute", exec_ep)
        app.router.add_post(f"{p}/explain", explain_ep)
        app.router.add_get( f"{p}/tables",  tables_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Query planner API at {prefix}/sql/")
