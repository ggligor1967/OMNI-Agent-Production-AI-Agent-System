"""OMNI AGENT - Memory Graph: entity/relationship graph with Ebbinghaus decay and semantic recall."""
import json, math, time, uuid, sqlite3, hashlib, logging
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class Entity:
    id: str; name: str; entity_type: str = "concept"
    description: str = ""; attributes: Dict = field(default_factory=dict)
    importance: float = 1.0; access_count: int = 0
    namespace: str = "default"; pinned: bool = False
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)

    def decayed_importance(self, now=None):
        if self.pinned: return self.importance
        t = ((now or time.time()) - self.last_accessed) / 86400
        stability = 1.0 + self.access_count * 0.5
        return self.importance * math.exp(-t / max(stability, 0.1))

    def to_dict(self, now=None):
        return {"id":self.id,"name":self.name,"entity_type":self.entity_type,
                "description":self.description,"attributes":self.attributes,
                "importance":round(self.importance,4),
                "decayed_importance":round(self.decayed_importance(now),4),
                "access_count":self.access_count,"namespace":self.namespace,
                "pinned":self.pinned,"created_at":self.created_at,
                "last_accessed":self.last_accessed}

@dataclass
class Relation:
    id: str; source_id: str; target_id: str; relation_type: str
    weight: float = 1.0; attributes: Dict = field(default_factory=dict)
    namespace: str = "default"; created_at: float = field(default_factory=time.time)
    def to_dict(self):
        return {"id":self.id,"source_id":self.source_id,"target_id":self.target_id,
                "relation_type":self.relation_type,"weight":self.weight,
                "attributes":self.attributes,"namespace":self.namespace}

class MemoryStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()
    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c
    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS entities(
                    id TEXT PRIMARY KEY,name TEXT,entity_type TEXT DEFAULT 'concept',
                    description TEXT DEFAULT '',attributes TEXT DEFAULT '{}',
                    importance REAL DEFAULT 1.0,access_count INTEGER DEFAULT 0,
                    namespace TEXT DEFAULT 'default',pinned INTEGER DEFAULT 0,
                    created_at REAL,last_accessed REAL);
                CREATE TABLE IF NOT EXISTS relations(
                    id TEXT PRIMARY KEY,source_id TEXT,target_id TEXT,
                    relation_type TEXT,weight REAL DEFAULT 1.0,
                    attributes TEXT DEFAULT '{}',namespace TEXT DEFAULT 'default',
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_ens ON entities(namespace,importance DESC);
                CREATE INDEX IF NOT EXISTS idx_rs ON relations(source_id);
                CREATE INDEX IF NOT EXISTS idx_rt ON relations(target_id);
            """)
    def save_entity(self, e):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (e.id,e.name,e.entity_type,e.description,json.dumps(e.attributes),
                 e.importance,e.access_count,e.namespace,int(e.pinned),
                 e.created_at,e.last_accessed))
    def get_entity(self, eid):
        with self._conn() as c:
            row = c.execute("SELECT * FROM entities WHERE id=?",(eid,)).fetchone()
        return self._re(row) if row else None
    def find_by_name(self, name, namespace=None):
        with self._conn() as c:
            q = "SELECT * FROM entities WHERE name=?"
            args = [name]
            if namespace: q += " AND namespace=?"; args.append(namespace)
            row = c.execute(q, args).fetchone()
        return self._re(row) if row else None
    def list_entities(self, namespace=None, min_importance=0.0, entity_type=None, limit=100):
        conds,args = ["importance>=?"],[min_importance]
        if namespace: conds.append("namespace=?"); args.append(namespace)
        if entity_type: conds.append("entity_type=?"); args.append(entity_type)
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(f"SELECT * FROM entities WHERE {' AND '.join(conds)} ORDER BY importance DESC LIMIT ?",args).fetchall()
        return [self._re(r) for r in rows]
    def delete_entity(self, eid):
        with self._conn() as c:
            cur = c.execute("DELETE FROM entities WHERE id=?",(eid,))
            c.execute("DELETE FROM relations WHERE source_id=? OR target_id=?",(eid,eid))
        return cur.rowcount > 0
    def save_relation(self, r):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO relations VALUES(?,?,?,?,?,?,?,?)",
                (r.id,r.source_id,r.target_id,r.relation_type,r.weight,
                 json.dumps(r.attributes),r.namespace,r.created_at))
    def get_relations(self, entity_id, direction="both"):
        with self._conn() as c:
            if direction=="out": rows=c.execute("SELECT * FROM relations WHERE source_id=?",(entity_id,)).fetchall()
            elif direction=="in": rows=c.execute("SELECT * FROM relations WHERE target_id=?",(entity_id,)).fetchall()
            else: rows=c.execute("SELECT * FROM relations WHERE source_id=? OR target_id=?",(entity_id,entity_id)).fetchall()
        return [self._rr(r) for r in rows]
    def delete_relation(self, rid):
        with self._conn() as c:
            cur = c.execute("DELETE FROM relations WHERE id=?",(rid,))
        return cur.rowcount > 0
    def forget_below(self, threshold, namespace=None):
        now = time.time()
        entities = self.list_entities(namespace=namespace, limit=10000)
        to_del = [e.id for e in entities if not e.pinned and e.decayed_importance(now) < threshold]
        with self._conn() as c:
            for eid in to_del:
                c.execute("DELETE FROM entities WHERE id=?",(eid,))
                c.execute("DELETE FROM relations WHERE source_id=? OR target_id=?",(eid,eid))
        return len(to_del)
    def stats(self, namespace=None):
        with self._conn() as c:
            q = " WHERE namespace=?" if namespace else ""
            ec = c.execute(f"SELECT COUNT(*) FROM entities{q}",(namespace,) if namespace else ()).fetchone()[0]
            rc = c.execute(f"SELECT COUNT(*) FROM relations{q}",(namespace,) if namespace else ()).fetchone()[0]
            types = dict(c.execute("SELECT entity_type,COUNT(*) FROM entities GROUP BY entity_type").fetchall())
        return {"entities":ec,"relations":rc,"entity_types":types}
    def _re(self, row):
        return Entity(id=row["id"],name=row["name"],entity_type=row["entity_type"] or "concept",
                      description=row["description"] or "",
                      attributes=json.loads(row["attributes"] or "{}"),
                      importance=row["importance"],access_count=row["access_count"],
                      namespace=row["namespace"] or "default",pinned=bool(row["pinned"]),
                      created_at=row["created_at"],last_accessed=row["last_accessed"])
    def _rr(self, row):
        return Relation(id=row["id"],source_id=row["source_id"],target_id=row["target_id"],
                        relation_type=row["relation_type"],weight=row["weight"],
                        attributes=json.loads(row["attributes"] or "{}"),
                        namespace=row["namespace"] or "default",created_at=row["created_at"])

def _embed(text, dim=128):
    vec = [0.0]*dim; text = text.lower()
    for i in range(max(1,len(text)-2)):
        idx = int(
            hashlib.md5(  # nosec B324 - deterministic graph embedding only
                text[i:i+3].encode(), usedforsecurity=False
            ).hexdigest(),
            16,
        ) % dim
        vec[idx] += 1.0
    n = math.sqrt(sum(x*x for x in vec))
    return [x/n for x in vec] if n else vec

def _cosine(a, b): return sum(x*y for x,y in zip(a,b)) if len(a)==len(b) else 0.0

class MemoryGraph:
    """Long-term associative memory with Ebbinghaus decay and semantic recall."""
    def __init__(self, db_path="data/memory_graph.db", embedder=None, default_namespace="default"):
        self._store = MemoryStore(db_path)
        self._embedder = embedder or _embed
        self._ns = default_namespace
        self._cache: Dict[str,List] = {}

    def _get_emb(self, text):
        if text not in self._cache:
            self._cache[text] = self._embedder(text)
            if len(self._cache) > 2000:
                for k in list(self._cache.keys())[:1000]: del self._cache[k]
        return self._cache[text]

    def remember(self, name, entity_type="concept", description="", attributes=None,
                 importance=1.0, namespace=None, pinned=False):
        ns = namespace or self._ns
        existing = self._store.find_by_name(name, ns)
        if existing:
            existing.description = description or existing.description
            existing.attributes.update(attributes or {})
            existing.importance = max(existing.importance, importance)
            existing.last_accessed = time.time()
            self._store.save_entity(existing); return existing
        e = Entity(id=str(uuid.uuid4())[:12],name=name,entity_type=entity_type,
                   description=description,attributes=attributes or {},
                   importance=importance,namespace=ns,pinned=pinned)
        self._store.save_entity(e); return e

    def relate(self, source_name, target_name, relation_type, weight=1.0,
               namespace=None, attributes=None):
        ns = namespace or self._ns
        src = self._store.find_by_name(source_name, ns)
        tgt = self._store.find_by_name(target_name, ns)
        if not src or not tgt: return None
        r = Relation(id=str(uuid.uuid4())[:12],source_id=src.id,target_id=tgt.id,
                     relation_type=relation_type,weight=weight,
                     attributes=attributes or {},namespace=ns)
        self._store.save_relation(r); return r

    def get_entity(self, name_or_id, namespace=None):
        e = self._store.get_entity(name_or_id)
        if e: return e
        return self._store.find_by_name(name_or_id, namespace or self._ns)

    def reinforce(self, name_or_id, boost=0.1):
        e = self.get_entity(name_or_id)
        if not e: return False
        e.importance = min(1.0, e.importance + boost)
        e.access_count += 1; e.last_accessed = time.time()
        self._store.save_entity(e); return True

    def recall(self, query, namespace=None, top_k=10, min_importance=0.0):
        ns = namespace or self._ns; q_emb = self._get_emb(query)
        entities = self._store.list_entities(namespace=ns, min_importance=min_importance, limit=500)
        now = time.time(); scored = []
        for e in entities:
            sem = _cosine(q_emb, self._get_emb(f"{e.name} {e.description}"))
            score = sem + e.decayed_importance(now) * 0.3
            scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        return scored[:top_k]

    def neighbors(self, name_or_id, direction="both", namespace=None):
        e = self.get_entity(name_or_id, namespace)
        if not e: return []
        rels = self._store.get_relations(e.id, direction)
        result = []
        for r in rels:
            nid = r.target_id if r.source_id==e.id else r.source_id
            n = self._store.get_entity(nid)
            if n: result.append((r, n))
        return result

    def forget(self, threshold=0.1, namespace=None):
        return self._store.forget_below(threshold, namespace)

    def delete(self, name_or_id, namespace=None):
        e = self.get_entity(name_or_id, namespace)
        if not e: return False
        return self._store.delete_entity(e.id)

    def working_memory(self, namespace=None, top_k=20):
        ns = namespace or self._ns
        entities = self._store.list_entities(namespace=ns, limit=200)
        now = time.time()
        pinned = [e for e in entities if e.pinned]
        others = sorted([e for e in entities if not e.pinned], key=lambda e: -e.decayed_importance(now))
        return (pinned + others)[:top_k]

    def list_entities(self, namespace=None, entity_type=None, min_importance=0.0, limit=50):
        return self._store.list_entities(namespace=namespace or self._ns,
                                          min_importance=min_importance,
                                          entity_type=entity_type, limit=limit)

    def stats(self, namespace=None):
        return self._store.stats(namespace or self._ns)

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def add_ep(req):
            d=await req.json()
            e=self.remember(name=d["name"],entity_type=d.get("entity_type","concept"),
                             description=d.get("description",""),attributes=d.get("attributes",{}),
                             importance=float(d.get("importance",1.0)),namespace=d.get("namespace"),
                             pinned=bool(d.get("pinned",False)))
            return web.json_response(e.to_dict(),status=201)
        async def relate_ep(req):
            d=await req.json()
            r=self.relate(d["source"],d["target"],d["relation_type"],
                           weight=float(d.get("weight",1.0)),namespace=d.get("namespace"))
            if not r: return web.json_response({"error":"entity not found"},status=404)
            return web.json_response(r.to_dict(),status=201)
        async def recall_ep(req):
            d=await req.json()
            results=self.recall(query=d["query"],namespace=d.get("namespace"),
                                 top_k=int(d.get("top_k",10)),
                                 min_importance=float(d.get("min_importance",0.0)))
            return web.json_response({"results":[{"score":round(s,4),"entity":e.to_dict()} for s,e in results]})
        async def get_ep(req):
            e=self.get_entity(req.match_info["name"])
            if not e: return web.json_response({"error":"not found"},status=404)
            return web.json_response(e.to_dict())
        async def reinforce_ep(req):
            return web.json_response({"ok":self.reinforce(req.match_info["name"])})
        async def forget_ep(req):
            d=await req.json() if req.content_length else {}
            n=self.forget(threshold=float(d.get("threshold",0.1)),namespace=d.get("namespace"))
            return web.json_response({"forgotten":n})
        async def stats_ep(req): return web.json_response(self.stats())
        p=f"{prefix}/memory"
        app.router.add_post(p,add_ep); app.router.add_post(f"{p}/relate",relate_ep)
        app.router.add_post(f"{p}/recall",recall_ep); app.router.add_post(f"{p}/forget",forget_ep)
        app.router.add_get(f"{p}/stats",stats_ep); app.router.add_get(f"{p}/{{name}}",get_ep)
        app.router.add_post(f"{p}/{{name}}/reinforce",reinforce_ep)
        logger.info(f"Memory graph API at {prefix}/memory/")
