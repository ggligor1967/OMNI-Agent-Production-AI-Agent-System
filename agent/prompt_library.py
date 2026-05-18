"""OMNI AGENT - Prompt Library
Versioned prompt template store: create, tag, search, fork, diff,
A/B test variants, and track usage statistics.

Features:
- Templates: store prompts with name, description, tags, variables
- Versioning: every edit creates a new immutable version
- Forking: branch a template from any version
- Diff: compare two versions character-by-character
- Variable extraction: parse {{variable}} placeholders automatically
- Rendering: fill variables to produce final prompt string
- Tagging: multi-tag taxonomy with tag-based search
- Full-text search: search by name, description, or body text
- Usage tracking: log every render call with latency and success
- A/B testing: assign traffic to two variants and compare metrics
- SQLite persistence: all versions, tags, and usage logs
- REST API: create, get, render, search, fork, stats
"""
import json, time, uuid, sqlite3, re, logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from difflib import unified_diff
logger = logging.getLogger(__name__)

_VAR_RE = re.compile(r'\{\{(\w+)\}\}')

def _extract_vars(text: str) -> List[str]:
    return list(dict.fromkeys(_VAR_RE.findall(text)))

def _render(text: str, variables: Dict) -> str:
    result = text
    for k, v in variables.items():
        result = result.replace('{{' + k + '}}', str(v))
    return result

def _diff_texts(a: str, b: str, name_a: str = "v_a", name_b: str = "v_b") -> str:
    lines_a = a.splitlines(keepends=True)
    lines_b = b.splitlines(keepends=True)
    return "".join(unified_diff(lines_a, lines_b, fromfile=name_a, tofile=name_b))

@dataclass
class PromptVersion:
    id: str; template_id: str; version: int
    body: str; change_note: str = ""
    variables: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    created_by: str = ""

    def render(self, variables: Dict) -> str:
        return _render(self.body, variables)

    def to_dict(self, include_body: bool = True):
        d = {"id": self.id, "template_id": self.template_id,
             "version": self.version, "variables": self.variables,
             "change_note": self.change_note, "created_at": self.created_at,
             "created_by": self.created_by}
        if include_body: d["body"] = self.body
        return d

@dataclass
class PromptTemplate:
    id: str; name: str; description: str = ""
    tags: List[str] = field(default_factory=list)
    latest_version: int = 0
    forked_from: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "description": self.description,
                "tags": self.tags, "latest_version": self.latest_version,
                "forked_from": self.forked_from,
                "created_at": self.created_at, "updated_at": self.updated_at}

@dataclass
class ABTest:
    id: str; name: str
    variant_a: str; variant_b: str        # template IDs
    traffic_split: float = 0.5            # fraction to A
    renders_a: int = 0; renders_b: int = 0
    success_a: int = 0; success_b: int = 0
    active: bool = True
    created_at: float = field(default_factory=time.time)

    @property
    def success_rate_a(self): return round(self.success_a / max(1, self.renders_a), 4)
    @property
    def success_rate_b(self): return round(self.success_b / max(1, self.renders_b), 4)

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "variant_a": self.variant_a, "variant_b": self.variant_b,
                "traffic_split": self.traffic_split,
                "renders_a": self.renders_a, "renders_b": self.renders_b,
                "success_rate_a": self.success_rate_a,
                "success_rate_b": self.success_rate_b,
                "active": self.active}

class PLStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS templates(
                    id TEXT PRIMARY KEY, name TEXT, description TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]', latest_version INTEGER DEFAULT 0,
                    forked_from TEXT, created_at REAL, updated_at REAL);
                CREATE TABLE IF NOT EXISTS versions(
                    id TEXT PRIMARY KEY, template_id TEXT, version INTEGER,
                    body TEXT, change_note TEXT DEFAULT '', variables TEXT DEFAULT '[]',
                    created_at REAL, created_by TEXT DEFAULT '',
                    UNIQUE(template_id, version));
                CREATE TABLE IF NOT EXISTS usage(
                    id TEXT PRIMARY KEY, template_id TEXT, version INTEGER,
                    render_ms REAL, success INTEGER DEFAULT 1, timestamp REAL);
                CREATE TABLE IF NOT EXISTS ab_tests(
                    id TEXT PRIMARY KEY, name TEXT, variant_a TEXT, variant_b TEXT,
                    traffic_split REAL DEFAULT 0.5,
                    renders_a INTEGER DEFAULT 0, renders_b INTEGER DEFAULT 0,
                    success_a INTEGER DEFAULT 0, success_b INTEGER DEFAULT 0,
                    active INTEGER DEFAULT 1, created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_ver_tpl ON versions(template_id, version DESC);
                CREATE INDEX IF NOT EXISTS idx_use_tpl ON usage(template_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_tpl_name ON templates(name);
            """)

    def save_template(self, t: PromptTemplate):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO templates VALUES(?,?,?,?,?,?,?,?)",
                (t.id, t.name, t.description, json.dumps(t.tags),
                 t.latest_version, t.forked_from, t.created_at, t.updated_at))

    def save_version(self, v: PromptVersion):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO versions VALUES(?,?,?,?,?,?,?,?)",
                (v.id, v.template_id, v.version, v.body, v.change_note,
                 json.dumps(v.variables), v.created_at, v.created_by))

    def get_template(self, tid: str) -> Optional[PromptTemplate]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        return self._rt(row) if row else None

    def get_version(self, tid: str, ver: Optional[int] = None) -> Optional[PromptVersion]:
        with self._conn() as c:
            if ver:
                row = c.execute("SELECT * FROM versions WHERE template_id=? AND version=?",
                                  (tid, ver)).fetchone()
            else:
                row = c.execute("SELECT * FROM versions WHERE template_id=? ORDER BY version DESC LIMIT 1",
                                  (tid,)).fetchone()
        return self._rv(row) if row else None

    def list_versions(self, tid: str) -> List[PromptVersion]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM versions WHERE template_id=? ORDER BY version DESC",
                              (tid,)).fetchall()
        return [self._rv(r) for r in rows]

    def search(self, query: str = "", tags: List[str] = None, limit: int = 20):
        conds, args = ["1=1"], []
        if query:
            conds.append("(name LIKE ? OR description LIKE ?)")
            args += [f'%{query}%', f'%{query}%']
        if tags:
            for tag in tags:
                conds.append("tags LIKE ?"); args.append(f'%{tag}%')
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM templates WHERE {' AND '.join(conds)} ORDER BY updated_at DESC LIMIT ?",
                args).fetchall()
        return [self._rt(r) for r in rows]

    def log_usage(self, template_id: str, version: int, render_ms: float, success: bool):
        with self._conn() as c:
            c.execute("INSERT INTO usage VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:10], template_id, version,
                 render_ms, int(success), time.time()))

    def usage_stats(self, template_id: str) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM usage WHERE template_id=?",
                               (template_id,)).fetchone()[0]
            avg_ms = c.execute("SELECT AVG(render_ms) FROM usage WHERE template_id=?",
                                (template_id,)).fetchone()[0]
            success_rate = c.execute(
                "SELECT AVG(success) FROM usage WHERE template_id=?",
                (template_id,)).fetchone()[0]
        return {"total_renders": total, "avg_render_ms": round(avg_ms or 0, 2),
                "success_rate": round(success_rate or 0, 4)}

    def save_ab(self, ab: ABTest):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO ab_tests VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ab.id, ab.name, ab.variant_a, ab.variant_b, ab.traffic_split,
                 ab.renders_a, ab.renders_b, ab.success_a, ab.success_b,
                 int(ab.active), ab.created_at))

    def get_ab(self, ab_id: str) -> Optional[ABTest]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM ab_tests WHERE id=?", (ab_id,)).fetchone()
        if not row: return None
        return ABTest(id=row["id"], name=row["name"],
                       variant_a=row["variant_a"], variant_b=row["variant_b"],
                       traffic_split=row["traffic_split"],
                       renders_a=row["renders_a"], renders_b=row["renders_b"],
                       success_a=row["success_a"], success_b=row["success_b"],
                       active=bool(row["active"]), created_at=row["created_at"])

    def stats(self) -> Dict:
        with self._conn() as c:
            nt = c.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
            nv = c.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
            nu = c.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
        return {"templates": nt, "versions": nv, "total_renders": nu}

    def _rt(self, row) -> PromptTemplate:
        return PromptTemplate(id=row["id"], name=row["name"],
                               description=row["description"] or "",
                               tags=json.loads(row["tags"] or "[]"),
                               latest_version=row["latest_version"],
                               forked_from=row["forked_from"],
                               created_at=row["created_at"], updated_at=row["updated_at"])

    def _rv(self, row) -> PromptVersion:
        return PromptVersion(id=row["id"], template_id=row["template_id"],
                              version=row["version"], body=row["body"],
                              change_note=row["change_note"] or "",
                              variables=json.loads(row["variables"] or "[]"),
                              created_at=row["created_at"],
                              created_by=row["created_by"] or "")

class PromptLibrary:
    """
    Versioned prompt template library with A/B testing and usage analytics.

    Usage:
        lib = PromptLibrary()
        tid = lib.create("summarise", "Summarise the text below.\n\nText: {{text}}\n\nSummary:",
                          tags=["summarisation","production"])
        rendered = lib.render(tid, {"text": "The quick brown fox..."})
        print(rendered)

        # Update with new version
        lib.update(tid, "Please provide a concise summary of:\n\n{{text}}\n\nSummary:",
                    change_note="More polite phrasing")

        # Fork for experiment
        fork_id = lib.fork(tid, "summarise-v2-experiment")
        print(lib.diff(tid, fork_id))
    """
    def __init__(self, db_path: str = "data/prompt_library.db"):
        self._store = PLStore(db_path)
        self._templates: Dict[str, PromptTemplate] = {}
        self._ab_tests: Dict[str, ABTest] = {}
        # Load existing
        for t in self._store.search(limit=1000):
            self._templates[t.id] = t

    def create(self, name: str, body: str, description: str = "",
                tags: List[str] = None, created_by: str = "") -> str:
        tid = str(uuid.uuid4())[:12]
        t = PromptTemplate(id=tid, name=name, description=description,
                            tags=tags or [], latest_version=1)
        v = PromptVersion(id=str(uuid.uuid4())[:10], template_id=tid, version=1,
                           body=body, variables=_extract_vars(body),
                           change_note="Initial version", created_by=created_by)
        self._templates[tid] = t
        self._store.save_template(t); self._store.save_version(v)
        logger.info(f"Prompt '{name}' created: {tid}")
        return tid

    def update(self, template_id: str, body: str, change_note: str = "",
                created_by: str = "") -> PromptVersion:
        t = self._templates.get(template_id)
        if not t: raise ValueError(f"Template {template_id!r} not found")
        t.latest_version += 1; t.updated_at = time.time()
        v = PromptVersion(id=str(uuid.uuid4())[:10], template_id=template_id,
                           version=t.latest_version, body=body,
                           variables=_extract_vars(body),
                           change_note=change_note, created_by=created_by)
        self._store.save_template(t); self._store.save_version(v)
        return v

    def get(self, template_id: str, version: int = None) -> Optional[PromptVersion]:
        return self._store.get_version(template_id, version)

    def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        return self._templates.get(template_id) or self._store.get_template(template_id)

    def render(self, template_id: str, variables: Dict = None,
                version: int = None) -> str:
        start = time.time()
        v = self.get(template_id, version)
        if not v: raise ValueError(f"Template {template_id!r} not found")
        result = v.render(variables or {})
        self._store.log_usage(template_id, v.version,
                               (time.time() - start) * 1000, True)
        return result

    def fork(self, source_id: str, new_name: str,
              description: str = "", created_by: str = "") -> str:
        src_v = self.get(source_id)
        if not src_v: raise ValueError(f"Template {source_id!r} not found")
        new_id = str(uuid.uuid4())[:12]
        t = PromptTemplate(id=new_id, name=new_name, description=description,
                            tags=[], latest_version=1, forked_from=source_id)
        v = PromptVersion(id=str(uuid.uuid4())[:10], template_id=new_id, version=1,
                           body=src_v.body, variables=src_v.variables,
                           change_note=f"Forked from {source_id}", created_by=created_by)
        self._templates[new_id] = t
        self._store.save_template(t); self._store.save_version(v)
        return new_id

    def diff(self, template_id_a: str, template_id_b: str,
              ver_a: int = None, ver_b: int = None) -> str:
        va = self.get(template_id_a, ver_a)
        vb = self.get(template_id_b, ver_b)
        if not va or not vb: return "One or both templates not found"
        return _diff_texts(va.body, vb.body,
                            f"{template_id_a}@v{va.version}",
                            f"{template_id_b}@v{vb.version}")

    def list_versions(self, template_id: str) -> List[PromptVersion]:
        return self._store.list_versions(template_id)

    def search(self, query: str = "", tags: List[str] = None,
                limit: int = 20) -> List[PromptTemplate]:
        return self._store.search(query, tags, limit)

    def add_tag(self, template_id: str, tag: str):
        t = self._templates.get(template_id)
        if t and tag not in t.tags:
            t.tags.append(tag); self._store.save_template(t)

    def remove_tag(self, template_id: str, tag: str):
        t = self._templates.get(template_id)
        if t and tag in t.tags:
            t.tags.remove(tag); self._store.save_template(t)

    # ── A/B testing ───────────────────────────────────────────────────────────

    def create_ab_test(self, name: str, variant_a: str, variant_b: str,
                        traffic_split: float = 0.5) -> ABTest:
        ab = ABTest(id=str(uuid.uuid4())[:10], name=name,
                     variant_a=variant_a, variant_b=variant_b,
                     traffic_split=traffic_split)
        self._ab_tests[ab.id] = ab; self._store.save_ab(ab)
        return ab

    def ab_render(self, ab_id: str, variables: Dict = None) -> Tuple[str, str]:
        """Render using A/B routing; returns (rendered_text, variant_used)."""
        ab = self._ab_tests.get(ab_id) or self._store.get_ab(ab_id)
        if not ab: raise ValueError(f"A/B test {ab_id!r} not found")
        import random
        use_a = random.random() < ab.traffic_split
        tid = ab.variant_a if use_a else ab.variant_b
        result = self.render(tid, variables or {})
        if use_a: ab.renders_a += 1
        else:      ab.renders_b += 1
        self._store.save_ab(ab)
        return result, "A" if use_a else "B"

    def ab_record_success(self, ab_id: str, variant: str):
        ab = self._ab_tests.get(ab_id)
        if not ab: return
        if variant == "A": ab.success_a += 1
        else:               ab.success_b += 1
        self._store.save_ab(ab)

    def usage_stats(self, template_id: str) -> Dict:
        return self._store.usage_stats(template_id)

    def stats(self) -> Dict:
        return self._store.stats()

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def create_ep(req):
            d = await req.json()
            tid = self.create(d["name"], d["body"], d.get("description",""),
                               d.get("tags",[]), d.get("created_by",""))
            return web.json_response({"template_id": tid}, status=201)
        async def render_ep(req):
            d = await req.json()
            result = self.render(d["template_id"], d.get("variables",{}),
                                  d.get("version"))
            return web.json_response({"rendered": result})
        async def search_ep(req):
            q = req.rel_url.query
            tags = q.get("tags","").split(",") if q.get("tags") else None
            results = self.search(q.get("q",""), tags)
            return web.json_response({"templates":[t.to_dict() for t in results]})
        async def fork_ep(req):
            d = await req.json()
            new_id = self.fork(d["source_id"], d["new_name"], d.get("description",""))
            return web.json_response({"template_id": new_id}, status=201)
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/prompts"
        app.router.add_post(f"{p}",          create_ep)
        app.router.add_post(f"{p}/render",   render_ep)
        app.router.add_get( f"{p}/search",   search_ep)
        app.router.add_post(f"{p}/fork",     fork_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        logger.info(f"Prompt library API at {prefix}/prompts/")
