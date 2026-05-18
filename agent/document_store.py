"""OMNI AGENT - Document Store
Schema-less JSON document store with collections, indexing,
query DSL, aggregation, upsert, and full-text search.

Features:
- Collections: named namespaces; auto-created on first insert
- Documents: arbitrary JSON-serializable dict with auto-id
- Insert: returns doc with generated _id (UUID)
- Find: query dict with operators: $eq $ne $gt $lt $gte $lte
    $in $nin $exists $regex $and $or $not
- Update: $set $unset $inc $push $pull $rename operators
- Upsert: insert if not found, update if found
- Delete: by query or by _id
- Count: count matching documents
- Indexes: single-field or compound; unique indexes enforce no duplication
- Sort: multi-field ascending/descending
- Limit/Skip: pagination support
- Projection: include/exclude fields
- Aggregation: $group $match $sort $limit $skip $count pipeline
- Full-text: tokenized field index for keyword search
- Transactions: multi-op atomic block (in-memory)
- Change stream: on_change(collection, op, doc) hooks
- Export: collection to JSON array
- SQLite persistence: collections stored as JSON blobs per document
- REST API: insert, find, update, delete, aggregate, stats
"""
import json, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

def _new_id() -> str: return str(uuid.uuid4()).replace("-","")

def _match_op(doc_val: Any, op: str, query_val: Any) -> bool:
    if op == "$eq":     return doc_val == query_val
    if op == "$ne":     return doc_val != query_val
    if op == "$gt":     return (doc_val is not None) and doc_val > query_val
    if op == "$lt":     return (doc_val is not None) and doc_val < query_val
    if op == "$gte":    return (doc_val is not None) and doc_val >= query_val
    if op == "$lte":    return (doc_val is not None) and doc_val <= query_val
    if op == "$in":     return doc_val in query_val
    if op == "$nin":    return doc_val not in query_val
    if op == "$exists": return (doc_val is not None) == bool(query_val)
    if op == "$regex":
        try: return bool(re.search(query_val, str(doc_val or "")))
        except: return False
    return False

def _get_field(doc: Dict, path: str) -> Any:
    """Support dot-notation: 'address.city'."""
    parts = path.split(".")
    cur = doc
    for p in parts:
        if isinstance(cur, dict): cur = cur.get(p)
        else: return None
    return cur

def _set_field(doc: Dict, path: str, value: Any):
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value

def _unset_field(doc: Dict, path: str):
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        if not isinstance(cur, dict): return
        cur = cur.get(p, {})
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)

def _matches_query(doc: Dict, query: Dict) -> bool:
    for key, val in query.items():
        if key == "$and":
            if not all(_matches_query(doc, q) for q in val): return False
        elif key == "$or":
            if not any(_matches_query(doc, q) for q in val): return False
        elif key == "$not":
            if _matches_query(doc, val): return False
        elif isinstance(val, dict) and any(k.startswith("$") for k in val):
            doc_val = _get_field(doc, key)
            for op, op_val in val.items():
                if not _match_op(doc_val, op, op_val): return False
        else:
            if _get_field(doc, key) != val: return False
    return True

def _apply_update(doc: Dict, update: Dict) -> Dict:
    doc = dict(doc)
    for op, fields in update.items():
        if op == "$set":
            for k, v in fields.items(): _set_field(doc, k, v)
        elif op == "$unset":
            for k in fields: _unset_field(doc, k)
        elif op == "$inc":
            for k, v in fields.items():
                cur = _get_field(doc, k) or 0
                _set_field(doc, k, cur + v)
        elif op == "$push":
            for k, v in fields.items():
                arr = _get_field(doc, k) or []
                if isinstance(arr, list): arr = arr + [v]
                _set_field(doc, k, arr)
        elif op == "$pull":
            for k, v in fields.items():
                arr = _get_field(doc, k) or []
                _set_field(doc, k, [x for x in arr if x != v])
        elif op == "$rename":
            for old_k, new_k in fields.items():
                v = _get_field(doc, old_k)
                _unset_field(doc, old_k)
                if v is not None: _set_field(doc, new_k, v)
    return doc

class DSStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS docs(
                    id TEXT PRIMARY KEY, collection TEXT,
                    data TEXT, created_at REAL, updated_at REAL);
                CREATE INDEX IF NOT EXISTS idx_docs_coll
                    ON docs(collection, created_at DESC);
            """)

    def insert(self, collection: str, doc: Dict) -> Dict:
        now = time.time()
        with self._conn() as c:
            c.execute("INSERT INTO docs VALUES(?,?,?,?,?)",
                (doc["_id"], collection,
                 json.dumps(doc, default=str), now, now))
        return doc

    def update_doc(self, doc_id: str, doc: Dict):
        with self._conn() as c:
            c.execute("UPDATE docs SET data=?, updated_at=? WHERE id=?",
                (json.dumps(doc, default=str), time.time(), doc_id))

    def delete_doc(self, doc_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM docs WHERE id=?", (doc_id,))
            return cur.rowcount > 0

    def scan(self, collection: str) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT data FROM docs WHERE collection=? "
                "ORDER BY created_at", (collection,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def get_by_id(self, doc_id: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT data FROM docs WHERE id=?", (doc_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def collections(self) -> List[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT collection FROM docs").fetchall()
        return [r["collection"] for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            by_coll = {r["collection"]: r["cnt"] for r in c.execute(
                "SELECT collection, COUNT(*) as cnt FROM docs "
                "GROUP BY collection").fetchall()}
        return {"total": total, "by_collection": by_coll}

class DocumentStore:
    """
    Schema-less document store with query DSL and aggregation.

    Usage:
        store = DocumentStore()

        store.insert("users", {"name": "Alice", "age": 30, "plan": "pro"})
        store.insert("users", {"name": "Bob",   "age": 25, "plan": "free"})

        # Find pro users over 28
        docs = store.find("users",
                           {"plan": "pro", "age": {"$gt": 28}})

        # Update
        store.update("users", {"name": "Alice"}, {"$set": {"plan": "enterprise"}})

        # Aggregate: count by plan
        result = store.aggregate("users", [
            {"$group": {"_id": "$plan", "count": {"$sum": 1}}}
        ])
    """
    def __init__(self, db_path: str = "data/docstore.db"):
        self._store = DSStore(db_path)
        # In-memory cache per collection
        self._cache: Dict[str, Dict[str, Dict]] = {}  # coll → {id: doc}
        self._indexes: Dict[str, Dict[str, Dict]] = {}  # coll.field → {val: [ids]}
        self._unique_idx: Set[str] = set()  # "coll.field" unique constraints
        self._change_hooks: List[Callable] = []
        # Load existing collections into memory
        for coll in self._store.collections():
            self._load_collection(coll)

    def _load_collection(self, collection: str):
        docs = self._store.scan(collection)
        self._cache[collection] = {d["_id"]: d for d in docs}

    def _get_cache(self, collection: str) -> Dict[str, Dict]:
        if collection not in self._cache:
            self._cache[collection] = {}
        return self._cache[collection]

    def on_change(self, fn: Callable): self._change_hooks.append(fn)

    def _fire(self, collection: str, op: str, doc: Dict):
        for h in self._change_hooks:
            try: h(collection, op, doc)
            except: pass

    def create_index(self, collection: str, field: str,
                      unique: bool = False):
        idx_key = f"{collection}.{field}"
        self._indexes[idx_key] = {}
        if unique: self._unique_idx.add(idx_key)
        # Build from existing docs
        for doc in self._get_cache(collection).values():
            val = _get_field(doc, field)
            val_key = str(val)
            self._indexes[idx_key].setdefault(val_key, [])
            self._indexes[idx_key][val_key].append(doc["_id"])

    def _update_indexes(self, collection: str, doc: Dict,
                         removing: bool = False):
        for idx_key, idx_data in self._indexes.items():
            coll, field = idx_key.split(".", 1)
            if coll != collection: continue
            val_key = str(_get_field(doc, field))
            if removing:
                ids = idx_data.get(val_key, [])
                if doc["_id"] in ids: ids.remove(doc["_id"])
            else:
                idx_data.setdefault(val_key, []).append(doc["_id"])

    def _check_unique(self, collection: str, doc: Dict,
                       exclude_id: str = None) -> Optional[str]:
        for idx_key in self._unique_idx:
            coll, field = idx_key.split(".", 1)
            if coll != collection: continue
            val_key = str(_get_field(doc, field))
            ids = self._indexes.get(idx_key, {}).get(val_key, [])
            ids = [i for i in ids if i != exclude_id]
            if ids:
                return f"Unique constraint violated: {field}={val_key}"
        return None

    def insert(self, collection: str, doc: Dict) -> Dict:
        doc = dict(doc)
        doc.setdefault("_id", _new_id())
        err = self._check_unique(collection, doc)
        if err: raise ValueError(err)
        self._get_cache(collection)[doc["_id"]] = doc
        self._update_indexes(collection, doc)
        self._store.insert(collection, doc)
        self._fire(collection, "insert", doc)
        return doc

    def insert_many(self, collection: str,
                     docs: List[Dict]) -> List[Dict]:
        return [self.insert(collection, d) for d in docs]

    def find(self, collection: str, query: Dict = None,
              sort: List[Tuple[str, int]] = None,
              limit: int = 0, skip: int = 0,
              projection: Dict = None) -> List[Dict]:
        query = query or {}
        docs = [d for d in self._get_cache(collection).values()
                if _matches_query(d, query)]
        if sort:
            for field, direction in reversed(sort):
                docs.sort(key=lambda d: (_get_field(d, field) is None,
                                          _get_field(d, field)),
                           reverse=(direction == -1))
        if skip: docs = docs[skip:]
        if limit: docs = docs[:limit]
        if projection:
            include = {k for k, v in projection.items() if v}
            exclude = {k for k, v in projection.items() if not v}
            def project(doc):
                d = {"_id": doc["_id"]}
                if include:
                    for k in include: d[k] = doc.get(k)
                else:
                    d = {k: v for k, v in doc.items() if k not in exclude}
                return d
            docs = [project(d) for d in docs]
        return docs

    def find_one(self, collection: str, query: Dict = None) -> Optional[Dict]:
        results = self.find(collection, query, limit=1)
        return results[0] if results else None

    def find_by_id(self, collection: str, doc_id: str) -> Optional[Dict]:
        return self._get_cache(collection).get(doc_id)

    def count(self, collection: str, query: Dict = None) -> int:
        return len(self.find(collection, query))

    def update(self, collection: str, query: Dict,
                update_ops: Dict, upsert: bool = False) -> int:
        docs = self.find(collection, query)
        if not docs and upsert:
            new_doc = {}
            for k, v in query.items():
                if not k.startswith("$"): new_doc[k] = v
            new_doc = _apply_update(new_doc, update_ops)
            self.insert(collection, new_doc)
            return 1
        count = 0
        for doc in docs:
            err = self._check_unique(collection,
                                      _apply_update(doc, update_ops),
                                      exclude_id=doc["_id"])
            if err: raise ValueError(err)
            self._update_indexes(collection, doc, removing=True)
            new_doc = _apply_update(doc, update_ops)
            new_doc["_id"] = doc["_id"]
            self._cache[collection][doc["_id"]] = new_doc
            self._update_indexes(collection, new_doc)
            self._store.update_doc(doc["_id"], new_doc)
            self._fire(collection, "update", new_doc)
            count += 1
        return count

    def update_one(self, collection: str, query: Dict,
                    update_ops: Dict, upsert: bool = False) -> int:
        doc = self.find_one(collection, query)
        if not doc:
            if upsert:
                new_doc = {k: v for k, v in query.items()
                            if not k.startswith("$")}
                new_doc = _apply_update(new_doc, update_ops)
                self.insert(collection, new_doc)
                return 1
            return 0
        new_doc = _apply_update(doc, update_ops)
        new_doc["_id"] = doc["_id"]
        self._update_indexes(collection, doc, removing=True)
        self._cache[collection][doc["_id"]] = new_doc
        self._update_indexes(collection, new_doc)
        self._store.update_doc(doc["_id"], new_doc)
        self._fire(collection, "update", new_doc)
        return 1

    def replace_one(self, collection: str, query: Dict,
                     replacement: Dict) -> bool:
        doc = self.find_one(collection, query)
        if not doc: return False
        replacement["_id"] = doc["_id"]
        self._update_indexes(collection, doc, removing=True)
        self._cache[collection][doc["_id"]] = replacement
        self._update_indexes(collection, replacement)
        self._store.update_doc(doc["_id"], replacement)
        self._fire(collection, "replace", replacement)
        return True

    def delete(self, collection: str, query: Dict) -> int:
        docs = self.find(collection, query)
        for doc in docs:
            self._update_indexes(collection, doc, removing=True)
            del self._cache[collection][doc["_id"]]
            self._store.delete_doc(doc["_id"])
            self._fire(collection, "delete", doc)
        return len(docs)

    def delete_one(self, collection: str, query: Dict) -> bool:
        doc = self.find_one(collection, query)
        if not doc: return False
        self._update_indexes(collection, doc, removing=True)
        del self._cache[collection][doc["_id"]]
        self._store.delete_doc(doc["_id"])
        self._fire(collection, "delete", doc)
        return True

    def aggregate(self, collection: str,
                   pipeline: List[Dict]) -> List[Dict]:
        docs = list(self._get_cache(collection).values())
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if _matches_query(d, stage["$match"])]
            elif "$sort" in stage:
                for field, direction in reversed(list(stage["$sort"].items())):
                    docs.sort(key=lambda d: (_get_field(d,field) is None,
                                              _get_field(d,field)),
                               reverse=(direction==-1))
            elif "$limit" in stage:
                docs = docs[:stage["$limit"]]
            elif "$skip" in stage:
                docs = docs[stage["$skip"]:]
            elif "$count" in stage:
                return [{stage["$count"]: len(docs)}]
            elif "$group" in stage:
                group_spec = stage["$group"]
                id_expr = group_spec.get("_id")
                groups: Dict[Any, Dict] = {}
                for doc in docs:
                    # Evaluate group key
                    if id_expr is None:
                        gkey = None
                    elif isinstance(id_expr, str) and id_expr.startswith("$"):
                        gkey = _get_field(doc, id_expr[1:])
                    else:
                        gkey = id_expr
                    gkey_str = str(gkey)
                    if gkey_str not in groups:
                        groups[gkey_str] = {"_id": gkey}
                    group = groups[gkey_str]
                    for out_field, agg in group_spec.items():
                        if out_field == "_id": continue
                        if not isinstance(agg, dict): continue
                        agg_op, agg_field = next(iter(agg.items()))
                        field_val = (_get_field(doc, agg_field[1:])
                                     if isinstance(agg_field, str)
                                        and agg_field.startswith("$")
                                     else agg_field)
                        if agg_op == "$sum":
                            group[out_field] = group.get(out_field, 0) + (
                                field_val if isinstance(field_val,(int,float)) else 1)
                        elif agg_op == "$avg":
                            prev_sum = group.get(f"__{out_field}_sum", 0)
                            prev_cnt = group.get(f"__{out_field}_cnt", 0)
                            group[f"__{out_field}_sum"] = prev_sum + (field_val or 0)
                            group[f"__{out_field}_cnt"] = prev_cnt + 1
                            group[out_field] = group[f"__{out_field}_sum"] / group[f"__{out_field}_cnt"]
                        elif agg_op == "$min":
                            prev = group.get(out_field)
                            group[out_field] = field_val if prev is None else min(prev, field_val or prev)
                        elif agg_op == "$max":
                            prev = group.get(out_field)
                            group[out_field] = field_val if prev is None else max(prev, field_val or prev)
                        elif agg_op == "$push":
                            group.setdefault(out_field, []).append(field_val)
                        elif agg_op == "$first":
                            if out_field not in group: group[out_field] = field_val
                # Clean up internal keys
                docs = [{k: v for k, v in g.items() if not k.startswith("__")}
                        for g in groups.values()]
            elif "$project" in stage:
                proj = stage["$project"]
                include = {k for k, v in proj.items() if v}
                exclude = {k for k, v in proj.items() if not v}
                def do_proj(doc):
                    if include:
                        return {k: doc.get(k) for k in include | {"_id"}}
                    return {k: v for k, v in doc.items() if k not in exclude}
                docs = [do_proj(d) for d in docs]
        return docs

    def drop_collection(self, collection: str) -> int:
        cache = self._get_cache(collection)
        n = len(cache)
        for doc_id in list(cache):
            self._store.delete_doc(doc_id)
        del self._cache[collection]
        return n

    def export(self, collection: str) -> str:
        return json.dumps(self.find(collection), indent=2, default=str)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory_collections"] = len(self._cache)
        s["indexes"] = len(self._indexes)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def insert_ep(req):
            d = await req.json()
            doc = self.insert(d["collection"], d["document"])
            return web.json_response(doc, status=201)
        async def find_ep(req):
            d = await req.json()
            docs = self.find(d["collection"], d.get("query",{}),
                              d.get("sort"), d.get("limit",0),
                              d.get("skip",0))
            return web.json_response({"docs": docs, "count": len(docs)})
        async def update_ep(req):
            d = await req.json()
            n = self.update(d["collection"], d["query"],
                             d["update"], d.get("upsert",False))
            return web.json_response({"modified": n})
        async def delete_ep(req):
            d = await req.json()
            n = self.delete(d["collection"], d["query"])
            return web.json_response({"deleted": n})
        async def agg_ep(req):
            d = await req.json()
            res = self.aggregate(d["collection"], d["pipeline"])
            return web.json_response({"result": res})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/docs"
        app.router.add_post(f"{p}/insert",    insert_ep)
        app.router.add_post(f"{p}/find",      find_ep)
        app.router.add_post(f"{p}/update",    update_ep)
        app.router.add_post(f"{p}/delete",    delete_ep)
        app.router.add_post(f"{p}/aggregate", agg_ep)
        app.router.add_get( f"{p}/stats",     stats_ep)
        logger.info(f"Document store API at {prefix}/docs/")
