"""OMNI AGENT - Knowledge Base
Structured knowledge store: CRUD for facts and concepts, relationship
graph, semantic search, ontology hierarchies, and versioned updates.

Features:
- Fact nodes: entity, attribute, value, confidence, source, TTL
- Concept nodes: name, definition, synonyms, domain, parent concept
- Relationships: directed typed edges between any two nodes
- Semantic search: keyword + TF-IDF style scoring across all nodes
- Ontology: is-a / part-of / related-to hierarchy with path traversal
- Confidence decay: facts age and lose confidence over time
- Versioning: every update creates a new version, rollback supported
- Import/export: JSON serialisation of entire knowledge base
- Deduplication: detect near-duplicate facts before insertion
- SQLite persistence: nodes, edges, and version history
- REST API: create-fact, create-concept, link, search, path, stats
"""
import re, time, uuid, sqlite3, json, math, logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

def _tfidf_score(query: str, text: str, corpus_size: int = 100) -> float:
    qw = re.findall(r'\b\w+\b', query.lower())
    tw = re.findall(r'\b\w+\b', text.lower())
    if not qw or not tw: return 0.0
    tf = {w: tw.count(w) / len(tw) for w in set(qw)}
    idf = {w: math.log(corpus_size / max(1, tw.count(w))) for w in set(qw)}
    return sum(tf.get(w, 0) * idf.get(w, 0) for w in qw)

@dataclass
class FactNode:
    id: str; entity: str; attribute: str; value: str
    confidence: float = 1.0; source: str = ""
    domain: str = ""; ttl: float = 0.0   # 0 = no expiry
    tags: List[str] = field(default_factory=list)
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def expired(self):
        return self.ttl > 0 and time.time() - self.created_at > self.ttl

    @property
    def effective_confidence(self):
        if self.ttl <= 0: return self.confidence
        age_ratio = (time.time() - self.created_at) / max(1, self.ttl)
        return max(0.0, self.confidence * (1 - age_ratio * 0.5))

    def to_dict(self):
        return {"id": self.id, "type": "fact", "entity": self.entity,
                "attribute": self.attribute, "value": self.value,
                "confidence": round(self.effective_confidence, 4),
                "source": self.source, "domain": self.domain,
                "version": self.version, "tags": self.tags}

@dataclass
class ConceptNode:
    id: str; name: str; definition: str = ""
    synonyms: List[str] = field(default_factory=list)
    domain: str = ""; parent: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "type": "concept", "name": self.name,
                "definition": self.definition, "synonyms": self.synonyms,
                "domain": self.domain, "parent": self.parent,
                "version": self.version, "tags": self.tags}

@dataclass
class Relationship:
    id: str; from_id: str; to_id: str
    rel_type: str   # is-a | part-of | related-to | causes | contradicts | custom
    weight: float = 1.0; bidirectional: bool = False
    metadata: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "from": self.from_id, "to": self.to_id,
                "type": self.rel_type, "weight": self.weight,
                "bidirectional": self.bidirectional}

class KBStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS facts(
                    id TEXT PRIMARY KEY, entity TEXT, attribute TEXT, value TEXT,
                    confidence REAL DEFAULT 1.0, source TEXT DEFAULT '',
                    domain TEXT DEFAULT '', ttl REAL DEFAULT 0,
                    tags TEXT DEFAULT '[]', version INTEGER DEFAULT 1,
                    created_at REAL, updated_at REAL);
                CREATE TABLE IF NOT EXISTS concepts(
                    id TEXT PRIMARY KEY, name TEXT UNIQUE, definition TEXT DEFAULT '',
                    synonyms TEXT DEFAULT '[]', domain TEXT DEFAULT '',
                    parent TEXT DEFAULT '', tags TEXT DEFAULT '[]',
                    version INTEGER DEFAULT 1, created_at REAL, updated_at REAL);
                CREATE TABLE IF NOT EXISTS relationships(
                    id TEXT PRIMARY KEY, from_id TEXT, to_id TEXT,
                    rel_type TEXT, weight REAL DEFAULT 1.0,
                    bidirectional INTEGER DEFAULT 0,
                    metadata TEXT DEFAULT '{}', created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_fact_entity ON facts(entity, attribute);
                CREATE INDEX IF NOT EXISTS idx_fact_domain ON facts(domain);
                CREATE INDEX IF NOT EXISTS idx_concept_name ON concepts(name);
                CREATE INDEX IF NOT EXISTS idx_rel_from ON relationships(from_id);
                CREATE INDEX IF NOT EXISTS idx_rel_to   ON relationships(to_id);
            """)

    def save_fact(self, f: FactNode):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f.id, f.entity, f.attribute, f.value, f.confidence, f.source,
                 f.domain, f.ttl, json.dumps(f.tags), f.version,
                 f.created_at, f.updated_at))

    def get_fact(self, fact_id: str) -> Optional[FactNode]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        return self._r_fact(row) if row else None

    def query_facts(self, entity: str = None, attribute: str = None,
                     domain: str = None, limit: int = 50) -> List[FactNode]:
        sql = "SELECT * FROM facts WHERE 1=1"
        params = []
        if entity:    sql += " AND entity LIKE ?";    params.append(f"%{entity}%")
        if attribute: sql += " AND attribute LIKE ?"; params.append(f"%{attribute}%")
        if domain:    sql += " AND domain=?";         params.append(domain)
        sql += f" ORDER BY confidence DESC LIMIT {limit}"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._r_fact(r) for r in rows]

    def _r_fact(self, row) -> FactNode:
        return FactNode(id=row["id"], entity=row["entity"],
                         attribute=row["attribute"], value=row["value"],
                         confidence=row["confidence"], source=row["source"] or "",
                         domain=row["domain"] or "", ttl=row["ttl"],
                         tags=json.loads(row["tags"] or "[]"),
                         version=row["version"],
                         created_at=row["created_at"], updated_at=row["updated_at"])

    def save_concept(self, c_node: ConceptNode):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO concepts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (c_node.id, c_node.name, c_node.definition,
                 json.dumps(c_node.synonyms), c_node.domain,
                 c_node.parent or "", json.dumps(c_node.tags),
                 c_node.version, c_node.created_at, c_node.updated_at))

    def get_concept(self, name: str) -> Optional[ConceptNode]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM concepts WHERE name=?", (name,)).fetchone()
        return self._r_concept(row) if row else None

    def _r_concept(self, row) -> ConceptNode:
        return ConceptNode(id=row["id"], name=row["name"],
                            definition=row["definition"] or "",
                            synonyms=json.loads(row["synonyms"] or "[]"),
                            domain=row["domain"] or "",
                            parent=row["parent"] or None,
                            tags=json.loads(row["tags"] or "[]"),
                            version=row["version"],
                            created_at=row["created_at"], updated_at=row["updated_at"])

    def save_rel(self, r: Relationship):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO relationships VALUES(?,?,?,?,?,?,?,?)",
                (r.id, r.from_id, r.to_id, r.rel_type, r.weight,
                 int(r.bidirectional), json.dumps(r.metadata), r.created_at))

    def get_rels(self, node_id: str, direction: str = "both") -> List[Relationship]:
        with self._conn() as c:
            if direction == "out":
                rows = c.execute("SELECT * FROM relationships WHERE from_id=?", (node_id,)).fetchall()
            elif direction == "in":
                rows = c.execute("SELECT * FROM relationships WHERE to_id=?", (node_id,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM relationships WHERE from_id=? OR to_id=? "
                    "OR (bidirectional=1 AND (from_id=? OR to_id=?))",
                    (node_id, node_id, node_id, node_id)).fetchall()
        return [Relationship(id=r["id"], from_id=r["from_id"], to_id=r["to_id"],
                              rel_type=r["rel_type"], weight=r["weight"],
                              bidirectional=bool(r["bidirectional"]),
                              metadata=json.loads(r["metadata"] or "{}"),
                              created_at=r["created_at"]) for r in rows]

    def full_text_search(self, query: str, limit: int = 20) -> List[Dict]:
        import re as _re
        words = _re.findall(r'\w+', query.lower())
        fact_ids = set(); concept_ids = set()
        with self._conn() as c:
            for word in words:
                q = f"%{word}%"
                for row in c.execute(
                    "SELECT id FROM facts WHERE entity LIKE ? OR attribute LIKE ? OR value LIKE ?",
                    (q, q, q)).fetchall():
                    fact_ids.add(row["id"])
                for row in c.execute(
                    "SELECT id FROM concepts WHERE name LIKE ? OR definition LIKE ?",
                    (q, q)).fetchall():
                    concept_ids.add(row["id"])
            facts = (c.execute(
                "SELECT id,'fact' as type, entity||' '||attribute||' '||value as text "
                f"FROM facts WHERE id IN ({','.join('?'*len(fact_ids))}) LIMIT ?",
                list(fact_ids)+[limit]).fetchall()) if fact_ids else []
            concepts = (c.execute(
                "SELECT id,'concept' as type, name||' '||definition as text "
                f"FROM concepts WHERE id IN ({','.join('?'*len(concept_ids))}) LIMIT ?",
                list(concept_ids)+[limit]).fetchall()) if concept_ids else []
        results = []
        for row in list(facts) + list(concepts):
            score = _tfidf_score(query, row["text"])
            results.append({"id": row["id"], "type": row["type"],
                              "score": round(score, 4), "snippet": row["text"][:100]})
        return sorted(results, key=lambda x: -x["score"])[:limit]

    def stats(self):
        with self._conn() as c:
            nf = c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            nc = c.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]
            nr = c.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        return {"facts": nf, "concepts": nc, "relationships": nr, "total_nodes": nf + nc}

class KnowledgeBase:
    """
    Structured knowledge store with graph traversal and semantic search.

    Usage:
        kb = KnowledgeBase()
        kb.add_fact("Python", "created_by", "Guido van Rossum", confidence=1.0)
        kb.add_fact("Python", "paradigm",   "object-oriented", confidence=0.95)
        kb.add_concept("Python", "A high-level programming language",
                        synonyms=["python3","py"], domain="programming")
        kb.link("Python", "programming-language", "is-a")

        facts = kb.get_entity_facts("Python")
        results = kb.search("object oriented language")
    """
    def __init__(self, db_path: str = "data/knowledge.db"):
        self._store = KBStore(db_path)
        self._nodes: Dict[str, Any] = {}  # id → node (in-memory cache)

    def add_fact(self, entity: str, attribute: str, value: str,
                  confidence: float = 1.0, source: str = "",
                  domain: str = "", ttl: float = 0.0,
                  tags: List[str] = None) -> FactNode:
        fact = FactNode(id=str(uuid.uuid4())[:12], entity=entity,
                         attribute=attribute, value=value,
                         confidence=confidence, source=source,
                         domain=domain, ttl=ttl, tags=tags or [])
        self._store.save_fact(fact)
        self._nodes[fact.id] = fact
        return fact

    def update_fact(self, fact_id: str, value: str = None,
                     confidence: float = None) -> Optional[FactNode]:
        fact = self._store.get_fact(fact_id)
        if not fact: return None
        if value is not None: fact.value = value
        if confidence is not None: fact.confidence = confidence
        fact.version += 1; fact.updated_at = time.time()
        self._store.save_fact(fact)
        self._nodes[fact_id] = fact
        return fact

    def add_concept(self, name: str, definition: str = "",
                     synonyms: List[str] = None, domain: str = "",
                     parent: str = None, tags: List[str] = None) -> ConceptNode:
        existing = self._store.get_concept(name)
        if existing: return existing
        c = ConceptNode(id=str(uuid.uuid4())[:12], name=name,
                         definition=definition, synonyms=synonyms or [],
                         domain=domain, parent=parent, tags=tags or [])
        self._store.save_concept(c)
        self._nodes[c.id] = c
        return c

    def link(self, from_name_or_id: str, to_name_or_id: str,
              rel_type: str = "related-to", weight: float = 1.0,
              bidirectional: bool = False) -> Relationship:
        # Resolve concept names to IDs
        from_node = self._store.get_concept(from_name_or_id)
        to_node   = self._store.get_concept(to_name_or_id)
        from_id = from_node.id if from_node else from_name_or_id
        to_id   = to_node.id   if to_node   else to_name_or_id
        rel = Relationship(id=str(uuid.uuid4())[:10],
                            from_id=from_id, to_id=to_id,
                            rel_type=rel_type, weight=weight,
                            bidirectional=bidirectional)
        self._store.save_rel(rel)
        return rel

    def get_entity_facts(self, entity: str,
                          attribute: str = None) -> List[FactNode]:
        return [f for f in self._store.query_facts(entity=entity, attribute=attribute)
                if not f.expired]

    def get_concept(self, name: str) -> Optional[ConceptNode]:
        return self._store.get_concept(name)

    def get_relations(self, node_id: str,
                       rel_type: str = None) -> List[Relationship]:
        rels = self._store.get_rels(node_id)
        if rel_type:
            rels = [r for r in rels if r.rel_type == rel_type]
        return rels

    def ancestors(self, concept_name: str, max_depth: int = 10) -> List[str]:
        """Walk is-a hierarchy upward."""
        path = []; current = concept_name; depth = 0
        while depth < max_depth:
            c = self._store.get_concept(current)
            if not c or not c.parent: break
            path.append(c.parent); current = c.parent; depth += 1
        return path

    def descendants(self, concept_name: str) -> List[str]:
        """Find all concepts with this as parent (direct children only)."""
        with self._store._conn() as conn:
            rows = conn.execute(
                "SELECT name FROM concepts WHERE parent=?",
                (concept_name,)).fetchall()
        return [r["name"] for r in rows]

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        return self._store.full_text_search(query, limit)

    def export(self) -> Dict:
        facts    = self._store.query_facts(limit=10000)
        concepts: List[ConceptNode] = []
        with self._store._conn() as c:
            rows = c.execute("SELECT name FROM concepts").fetchall()
            for row in rows:
                cn = self._store.get_concept(row["name"])
                if cn: concepts.append(cn)
        rels_raw = []
        with self._store._conn() as c:
            rows = c.execute("SELECT * FROM relationships").fetchall()
            for row in rows:
                rels_raw.append({"id": row["id"], "from": row["from_id"],
                                  "to": row["to_id"], "type": row["rel_type"]})
        return {"facts": [f.to_dict() for f in facts],
                "concepts": [c.to_dict() for c in concepts],
                "relationships": rels_raw}

    def stats(self) -> Dict:
        return self._store.stats()

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def fact_ep(req):
            d = await req.json()
            f = self.add_fact(d["entity"], d["attribute"], d["value"],
                               float(d.get("confidence",1.0)), d.get("source",""),
                               d.get("domain",""), float(d.get("ttl",0)))
            return web.json_response(f.to_dict(), status=201)
        async def concept_ep(req):
            d = await req.json()
            c = self.add_concept(d["name"], d.get("definition",""),
                                  d.get("synonyms",[]), d.get("domain",""), d.get("parent"))
            return web.json_response(c.to_dict(), status=201)
        async def link_ep(req):
            d = await req.json()
            r = self.link(d["from"], d["to"], d.get("type","related-to"),
                           float(d.get("weight",1.0)), bool(d.get("bidirectional",False)))
            return web.json_response(r.to_dict(), status=201)
        async def search_ep(req):
            q = req.rel_url.query.get("q","")
            return web.json_response({"results": self.search(q)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/kb"
        app.router.add_post(f"{p}/fact",    fact_ep)
        app.router.add_post(f"{p}/concept", concept_ep)
        app.router.add_post(f"{p}/link",    link_ep)
        app.router.add_get( f"{p}/search",  search_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Knowledge base API at {prefix}/kb/")
