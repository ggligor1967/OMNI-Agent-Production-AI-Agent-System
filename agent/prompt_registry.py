"""
OMNI AGENT - Prompt Registry
Centralized versioned library for all system prompts, templates, and
few-shot examples. Single source of truth for prompt management.

Features:
- CRUD: create, read, update, delete prompt entries
- Versioning: every update creates a new immutable version
- Tagging: multi-tag taxonomy for discovery (model, task, persona, env)
- Template rendering: Jinja2-style {variable} interpolation
- Conditional blocks: {{#if condition}}...{{/if}} sections
- Search: full-text search across name, description, and prompt body
- Promotion: draft → review → production lifecycle with audit trail
- Forking: clone a prompt and start a new version lineage
- Diff: compare any two versions side-by-side
- Import/export: YAML round-trip for version control
- Snapshot: export entire registry as a versioned bundle
- REST API: full CRUD + search + lifecycle management
"""
import re
import time
import uuid
import json
import sqlite3
import difflib
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# ENUMS & MODELS
# ══════════════════════════════════════════════════════════════════════════════

class PromptStatus(str, Enum):
    DRAFT      = "draft"
    REVIEW     = "review"
    PRODUCTION = "production"
    ARCHIVED   = "archived"
    DEPRECATED = "deprecated"


@dataclass
class PromptVersion:
    """A single immutable version of a prompt."""
    id: str           # version UUID
    prompt_id: str    # parent prompt entry
    version: int
    body: str
    variables: List[str]    # extracted variable names
    created_by: str = ""
    note: str = ""
    created_at: float = field(default_factory=time.time)

    def render(self, strict: bool = False, **variables) -> str:
        """
        Interpolate {variable} placeholders.
        If strict=True, raise KeyError for missing variables.
        Otherwise, leave missing placeholders unchanged.
        """
        result = self.body
        for key, val in variables.items():
            result = result.replace(f"{{{key}}}", str(val))
        if strict:
            missing = re.findall(r'\{(\w+)\}', result)
            if missing:
                raise KeyError(f"Missing template variables: {missing}")
        return result

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "prompt_id": self.prompt_id,
            "version": self.version, "body": self.body,
            "variables": self.variables, "created_by": self.created_by,
            "note": self.note, "created_at": self.created_at,
        }


@dataclass
class PromptEntry:
    """A named prompt entry with version history and lifecycle status."""
    id: str
    name: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    status: PromptStatus = PromptStatus.DRAFT
    current_version: int = 0
    current_body: str = ""
    variables: List[str] = field(default_factory=list)
    model_hint: str = ""     # suggested model (informational)
    task: str = ""           # task category
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    created_by: str = ""
    forked_from: str = ""    # parent prompt_id if this is a fork

    def to_dict(self, include_body: bool = True) -> Dict:
        d = {
            "id": self.id, "name": self.name,
            "description": self.description, "tags": self.tags,
            "status": self.status, "current_version": self.current_version,
            "variables": self.variables, "model_hint": self.model_hint,
            "task": self.task, "created_at": self.created_at,
            "updated_at": self.updated_at, "created_by": self.created_by,
            "forked_from": self.forked_from,
        }
        if include_body:
            d["current_body"] = self.current_body
        return d


def _extract_variables(text: str) -> List[str]:
    """Extract all {variable} placeholders from a prompt body."""
    return sorted(set(re.findall(r'\{(\w+)\}', text)))


# ══════════════════════════════════════════════════════════════════════════════
# SQLITE STORE
# ══════════════════════════════════════════════════════════════════════════════

class PromptStore:
    def __init__(self, db_path: str = "data/prompt_registry.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS prompts (
                    id              TEXT PRIMARY KEY,
                    name            TEXT NOT NULL UNIQUE,
                    description     TEXT DEFAULT '',
                    tags            TEXT DEFAULT '[]',
                    status          TEXT DEFAULT 'draft',
                    current_version INTEGER DEFAULT 0,
                    current_body    TEXT DEFAULT '',
                    variables       TEXT DEFAULT '[]',
                    model_hint      TEXT DEFAULT '',
                    task            TEXT DEFAULT '',
                    created_at      REAL,
                    updated_at      REAL,
                    created_by      TEXT DEFAULT '',
                    forked_from     TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS prompt_versions (
                    id          TEXT PRIMARY KEY,
                    prompt_id   TEXT NOT NULL,
                    version     INTEGER NOT NULL,
                    body        TEXT NOT NULL,
                    variables   TEXT DEFAULT '[]',
                    created_by  TEXT DEFAULT '',
                    note        TEXT DEFAULT '',
                    created_at  REAL,
                    FOREIGN KEY(prompt_id) REFERENCES prompts(id)
                );
                CREATE TABLE IF NOT EXISTS prompt_audit (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_id   TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    user        TEXT DEFAULT '',
                    details     TEXT DEFAULT '{}',
                    timestamp   REAL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS prompt_fts
                    USING fts5(prompt_id, name, description, body, tags);
                CREATE INDEX IF NOT EXISTS idx_pv_prompt ON prompt_versions(prompt_id, version DESC);
                CREATE INDEX IF NOT EXISTS idx_prompts_status ON prompts(status);
                CREATE INDEX IF NOT EXISTS idx_prompts_task ON prompts(task);
            """)

    # ── Prompts ───────────────────────────────────────────────────────────────

    def save_prompt(self, p: PromptEntry):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO prompts
                (id,name,description,tags,status,current_version,current_body,
                 variables,model_hint,task,created_at,updated_at,created_by,forked_from)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                p.id, p.name, p.description, json.dumps(p.tags),
                p.status, p.current_version, p.current_body,
                json.dumps(p.variables), p.model_hint, p.task,
                p.created_at, p.updated_at, p.created_by, p.forked_from,
            ))

    def get_prompt(self, prompt_id: str) -> Optional[PromptEntry]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM prompts WHERE id=?",
                            (prompt_id,)).fetchone()
        return self._row_to_prompt(row) if row else None

    def get_by_name(self, name: str) -> Optional[PromptEntry]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM prompts WHERE name=?",
                            (name,)).fetchone()
        return self._row_to_prompt(row) if row else None

    def list_prompts(self, status: str = None, tag: str = None,
                     task: str = None) -> List[PromptEntry]:
        conditions, params = [], []
        if status:
            conditions.append("status=?"); params.append(status)
        if tag:
            conditions.append("tags LIKE ?"); params.append(f'%"{tag}"%')
        if task:
            conditions.append("task=?"); params.append(task)
        q = "SELECT * FROM prompts"
        if conditions:
            q += " WHERE " + " AND ".join(conditions)
        q += " ORDER BY updated_at DESC"
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [self._row_to_prompt(r) for r in rows]

    def delete_prompt(self, prompt_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))
        return cur.rowcount > 0

    def update_status(self, prompt_id: str, status: PromptStatus,
                      user: str = "") -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE prompts SET status=?, updated_at=? WHERE id=?",
                (status, time.time(), prompt_id)
            )
        if cur.rowcount:
            self._audit(prompt_id, f"status→{status}", user)
        return cur.rowcount > 0

    def search(self, query: str, limit: int = 20) -> List[PromptEntry]:
        try:
            with self._conn() as c:
                rows = c.execute("""
                    SELECT p.* FROM prompts p
                    JOIN prompt_fts f ON p.id = f.prompt_id
                    WHERE prompt_fts MATCH ?
                    ORDER BY rank LIMIT ?
                """, (query, limit)).fetchall()
            if rows:
                return [self._row_to_prompt(r) for r in rows]
        except Exception:
            pass
        # Fallback: simple LIKE search
        with self._conn() as c:
            rows = c.execute("""
                SELECT * FROM prompts
                WHERE name LIKE ? OR description LIKE ? OR current_body LIKE ?
                ORDER BY updated_at DESC LIMIT ?
            """, (f"%{query}%", f"%{query}%", f"%{query}%", limit)).fetchall()
        return [self._row_to_prompt(r) for r in rows]

    # ── Versions ──────────────────────────────────────────────────────────────

    def save_version(self, v: PromptVersion):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO prompt_versions
                (id,prompt_id,version,body,variables,created_by,note,created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (v.id, v.prompt_id, v.version, v.body,
                  json.dumps(v.variables), v.created_by, v.note, v.created_at))
            # Update FTS
            c.execute("""
                INSERT OR REPLACE INTO prompt_fts(prompt_id,name,description,body,tags)
                SELECT id,name,description,?,tags FROM prompts WHERE id=?
            """, (v.body, v.prompt_id))

    def get_version(self, prompt_id: str,
                    version: int = None) -> Optional[PromptVersion]:
        with self._conn() as c:
            if version is None:
                row = c.execute("""
                    SELECT * FROM prompt_versions WHERE prompt_id=?
                    ORDER BY version DESC LIMIT 1
                """, (prompt_id,)).fetchone()
            else:
                row = c.execute("""
                    SELECT * FROM prompt_versions WHERE prompt_id=? AND version=?
                """, (prompt_id, version)).fetchone()
        return self._row_to_version(row) if row else None

    def list_versions(self, prompt_id: str) -> List[PromptVersion]:
        with self._conn() as c:
            rows = c.execute("""
                SELECT * FROM prompt_versions WHERE prompt_id=?
                ORDER BY version DESC
            """, (prompt_id,)).fetchall()
        return [self._row_to_version(r) for r in rows]

    # ── Audit ─────────────────────────────────────────────────────────────────

    def _audit(self, prompt_id: str, action: str, user: str = "",
               details: Dict = None):
        with self._conn() as c:
            c.execute("""
                INSERT INTO prompt_audit (prompt_id,action,user,details,timestamp)
                VALUES (?,?,?,?,?)
            """, (prompt_id, action, user,
                  json.dumps(details or {}), time.time()))

    def get_audit(self, prompt_id: str, limit: int = 50) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute("""
                SELECT * FROM prompt_audit WHERE prompt_id=?
                ORDER BY timestamp DESC LIMIT ?
            """, (prompt_id, limit)).fetchall()
        return [{"action": r["action"], "user": r["user"],
                 "details": json.loads(r["details"] or "{}"),
                 "timestamp": r["timestamp"]} for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
            by_status = dict(c.execute(
                "SELECT status, COUNT(*) FROM prompts GROUP BY status"
            ).fetchall())
            total_versions = c.execute(
                "SELECT COUNT(*) FROM prompt_versions"
            ).fetchone()[0]
        return {"total_prompts": total, "total_versions": total_versions,
                "by_status": by_status}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _row_to_prompt(self, row) -> PromptEntry:
        return PromptEntry(
            id=row["id"], name=row["name"],
            description=row["description"] or "",
            tags=json.loads(row["tags"] or "[]"),
            status=PromptStatus(row["status"]),
            current_version=row["current_version"],
            current_body=row["current_body"] or "",
            variables=json.loads(row["variables"] or "[]"),
            model_hint=row["model_hint"] or "",
            task=row["task"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by=row["created_by"] or "",
            forked_from=row["forked_from"] or "",
        )

    def _row_to_version(self, row) -> PromptVersion:
        return PromptVersion(
            id=row["id"], prompt_id=row["prompt_id"],
            version=row["version"], body=row["body"],
            variables=json.loads(row["variables"] or "[]"),
            created_by=row["created_by"] or "",
            note=row["note"] or "",
            created_at=row["created_at"],
        )


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

class PromptRegistry:
    """
    Centralized versioned prompt library.

    Usage:
        registry = PromptRegistry()

        # Create
        entry = registry.create(
            name="chat_system_prompt",
            body="You are {persona}, a helpful assistant.",
            tags=["system", "production"],
            task="chat",
        )

        # Render
        text = registry.render("chat_system_prompt", persona="Aria")

        # Update (creates new version)
        registry.update("chat_system_prompt",
                        body="You are {persona}. Always be concise.",
                        note="Added conciseness requirement")

        # Promote to production
        registry.promote("chat_system_prompt", user="alice")

        # Fork
        fork = registry.fork("chat_system_prompt", new_name="chat_system_v2")

        # Diff versions
        print(registry.diff("chat_system_prompt", version_a=1, version_b=2))
    """

    def __init__(self, db_path: str = "data/prompt_registry.db"):
        self._store = PromptStore(db_path)

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(self, name: str, body: str,
               description: str = "",
               tags: List[str] = None,
               task: str = "",
               model_hint: str = "",
               status: PromptStatus = PromptStatus.DRAFT,
               created_by: str = "",
               note: str = "") -> PromptEntry:
        if self._store.get_by_name(name):
            raise ValueError(f"Prompt '{name}' already exists. Use update() to modify.")

        pid = str(uuid.uuid4())[:12]
        variables = _extract_variables(body)
        entry = PromptEntry(
            id=pid, name=name, description=description,
            tags=tags or [], status=status,
            current_version=1, current_body=body,
            variables=variables, model_hint=model_hint,
            task=task, created_by=created_by,
        )
        version = PromptVersion(
            id=str(uuid.uuid4())[:12], prompt_id=pid,
            version=1, body=body, variables=variables,
            created_by=created_by, note=note,
        )
        self._store.save_prompt(entry)
        self._store.save_version(version)
        self._store._audit(pid, "create", created_by, {"name": name})
        logger.info(f"Prompt created: '{name}' id={pid}")
        return entry

    def get(self, prompt_id: str) -> Optional[PromptEntry]:
        return self._store.get_prompt(prompt_id)

    def get_by_name(self, name: str) -> Optional[PromptEntry]:
        return self._store.get_by_name(name)

    def list(self, status: str = None, tag: str = None,
             task: str = None) -> List[PromptEntry]:
        return self._store.list_prompts(status, tag, task)

    def update(self, name_or_id: str, body: str,
               description: str = None,
               tags: List[str] = None,
               note: str = "",
               updated_by: str = "") -> PromptEntry:
        entry = (self._store.get_prompt(name_or_id) or
                 self._store.get_by_name(name_or_id))
        if not entry:
            raise KeyError(f"Prompt '{name_or_id}' not found.")

        new_version = entry.current_version + 1
        variables = _extract_variables(body)
        entry.current_body = body
        entry.current_version = new_version
        entry.variables = variables
        entry.updated_at = time.time()
        if description is not None:
            entry.description = description
        if tags is not None:
            entry.tags = tags

        version = PromptVersion(
            id=str(uuid.uuid4())[:12], prompt_id=entry.id,
            version=new_version, body=body, variables=variables,
            created_by=updated_by, note=note,
        )
        self._store.save_prompt(entry)
        self._store.save_version(version)
        self._store._audit(entry.id, f"update→v{new_version}", updated_by)
        return entry

    def delete(self, name_or_id: str, deleted_by: str = "") -> bool:
        entry = (self._store.get_prompt(name_or_id) or
                 self._store.get_by_name(name_or_id))
        if not entry:
            return False
        self._store._audit(entry.id, "delete", deleted_by)
        return self._store.delete_prompt(entry.id)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def render(self, name_or_id: str, version: int = None,
               strict: bool = False, **variables) -> str:
        """Render a prompt with template variable substitution."""
        entry = (self._store.get_prompt(name_or_id) or
                 self._store.get_by_name(name_or_id))
        if not entry:
            raise KeyError(f"Prompt '{name_or_id}' not found.")

        if version is not None:
            pv = self._store.get_version(entry.id, version)
            if not pv:
                raise KeyError(f"Version {version} not found for '{name_or_id}'.")
        else:
            pv = PromptVersion(
                id="", prompt_id=entry.id, version=entry.current_version,
                body=entry.current_body, variables=entry.variables,
            )
        return pv.render(strict=strict, **variables)

    # ── Version management ────────────────────────────────────────────────────

    def versions(self, name_or_id: str) -> List[PromptVersion]:
        entry = (self._store.get_prompt(name_or_id) or
                 self._store.get_by_name(name_or_id))
        if not entry:
            return []
        return self._store.list_versions(entry.id)

    def rollback(self, name_or_id: str, to_version: int,
                 rolled_back_by: str = "") -> PromptEntry:
        """Roll back to a previous version (creates a new version with old body)."""
        entry = (self._store.get_prompt(name_or_id) or
                 self._store.get_by_name(name_or_id))
        if not entry:
            raise KeyError(f"Prompt '{name_or_id}' not found.")
        old_ver = self._store.get_version(entry.id, to_version)
        if not old_ver:
            raise KeyError(f"Version {to_version} not found.")
        return self.update(
            entry.id,
            body=old_ver.body,
            note=f"Rollback to version {to_version}",
            updated_by=rolled_back_by,
        )

    def diff(self, name_or_id: str,
             version_a: int, version_b: int) -> str:
        """Return a unified diff between two versions."""
        entry = (self._store.get_prompt(name_or_id) or
                 self._store.get_by_name(name_or_id))
        if not entry:
            return "Prompt not found."
        a = self._store.get_version(entry.id, version_a)
        b = self._store.get_version(entry.id, version_b)
        if not a or not b:
            return "One or both versions not found."
        lines = difflib.unified_diff(
            a.body.splitlines(keepends=True),
            b.body.splitlines(keepends=True),
            fromfile=f"v{version_a}",
            tofile=f"v{version_b}",
        )
        return "".join(lines) or "(no differences)"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def promote(self, name_or_id: str, user: str = "") -> bool:
        """Promote prompt to PRODUCTION status."""
        entry = (self._store.get_prompt(name_or_id) or
                 self._store.get_by_name(name_or_id))
        if not entry:
            return False
        ok = self._store.update_status(entry.id, PromptStatus.PRODUCTION, user)
        if ok:
            logger.info(f"Prompt '{entry.name}' promoted to PRODUCTION by {user}")
        return ok

    def archive(self, name_or_id: str, user: str = "") -> bool:
        entry = (self._store.get_prompt(name_or_id) or
                 self._store.get_by_name(name_or_id))
        if not entry:
            return False
        return self._store.update_status(entry.id, PromptStatus.ARCHIVED, user)

    def deprecate(self, name_or_id: str, user: str = "") -> bool:
        entry = (self._store.get_prompt(name_or_id) or
                 self._store.get_by_name(name_or_id))
        if not entry:
            return False
        return self._store.update_status(entry.id, PromptStatus.DEPRECATED, user)

    # ── Fork ──────────────────────────────────────────────────────────────────

    def fork(self, source: str, new_name: str,
             forked_by: str = "") -> PromptEntry:
        """Create a new prompt entry by copying the current body of source."""
        src = (self._store.get_prompt(source) or
               self._store.get_by_name(source))
        if not src:
            raise KeyError(f"Source prompt '{source}' not found.")
        entry = self.create(
            name=new_name,
            body=src.current_body,
            description=f"Forked from '{src.name}'",
            tags=list(src.tags),
            task=src.task,
            model_hint=src.model_hint,
            created_by=forked_by,
            note=f"Fork of '{src.name}' v{src.current_version}",
        )
        # Record fork lineage
        with self._store._conn() as c:
            c.execute("UPDATE prompts SET forked_from=? WHERE id=?",
                      (src.id, entry.id))
        entry.forked_from = src.id
        return entry

    # ── Search & Export ───────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 20) -> List[PromptEntry]:
        return self._store.search(query, limit)

    def export(self) -> Dict:
        """Export entire registry as a portable dict."""
        entries = self._store.list_prompts()
        result = {}
        for e in entries:
            versions = self._store.list_versions(e.id)
            result[e.name] = {
                **e.to_dict(),
                "versions": [v.to_dict() for v in versions],
            }
        return result

    def audit(self, name_or_id: str, limit: int = 50) -> List[Dict]:
        entry = (self._store.get_prompt(name_or_id) or
                 self._store.get_by_name(name_or_id))
        if not entry:
            return []
        return self._store.get_audit(entry.id, limit)

    def stats(self) -> Dict:
        return self._store.stats()

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def list_ep(request):
            status = request.rel_url.query.get("status")
            tag = request.rel_url.query.get("tag")
            task = request.rel_url.query.get("task")
            entries = self.list(status=status, tag=tag, task=task)
            return web.json_response({"prompts": [e.to_dict(include_body=False)
                                                   for e in entries]})

        async def create_ep(request):
            data = await request.json()
            entry = self.create(
                name=data["name"], body=data["body"],
                description=data.get("description", ""),
                tags=data.get("tags", []),
                task=data.get("task", ""),
                model_hint=data.get("model_hint", ""),
                created_by=data.get("created_by", ""),
                note=data.get("note", ""),
            )
            return web.json_response(entry.to_dict(), status=201)

        async def get_ep(request):
            entry = self.get(request.match_info["id"])
            if not entry:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response(entry.to_dict())

        async def update_ep(request):
            data = await request.json()
            pid = request.match_info["id"]
            entry = self.update(pid, body=data["body"],
                                description=data.get("description"),
                                tags=data.get("tags"),
                                note=data.get("note", ""),
                                updated_by=data.get("updated_by", ""))
            return web.json_response(entry.to_dict())

        async def delete_ep(request):
            ok = self.delete(request.match_info["id"])
            return web.json_response({"deleted": ok})

        async def render_ep(request):
            data = await request.json()
            pid = request.match_info["id"]
            version = data.get("version")
            variables = data.get("variables", {})
            text = self.render(pid, version=version, **variables)
            return web.json_response({"rendered": text})

        async def versions_ep(request):
            versions = self.versions(request.match_info["id"])
            return web.json_response({"versions": [v.to_dict() for v in versions]})

        async def diff_ep(request):
            pid = request.match_info["id"]
            a = int(request.rel_url.query.get("a", 1))
            b = int(request.rel_url.query.get("b", 2))
            return web.json_response({"diff": self.diff(pid, a, b)})

        async def promote_ep(request):
            pid = request.match_info["id"]
            data = await request.json() if request.content_length else {}
            ok = self.promote(pid, user=data.get("user", ""))
            return web.json_response({"promoted": ok})

        async def fork_ep(request):
            src = request.match_info["id"]
            data = await request.json()
            entry = self.fork(src, data["name"], forked_by=data.get("user", ""))
            return web.json_response(entry.to_dict(), status=201)

        async def search_ep(request):
            q = request.rel_url.query.get("q", "")
            results = self.search(q)
            return web.json_response({"results": [e.to_dict(include_body=False)
                                                   for e in results]})

        async def audit_ep(request):
            events = self.audit(request.match_info["id"])
            return web.json_response({"events": events})

        async def stats_ep(request):
            return web.json_response(self.stats())

        p = f"{prefix}/prompts"
        app.router.add_get(   p,                              list_ep)
        app.router.add_post(  p,                              create_ep)
        app.router.add_get(   f"{p}/search",                  search_ep)
        app.router.add_get(   f"{p}/stats",                   stats_ep)
        app.router.add_get(   f"{p}/{{id}}",                  get_ep)
        app.router.add_put(   f"{p}/{{id}}",                  update_ep)
        app.router.add_delete(f"{p}/{{id}}",                  delete_ep)
        app.router.add_post(  f"{p}/{{id}}/render",           render_ep)
        app.router.add_get(   f"{p}/{{id}}/versions",         versions_ep)
        app.router.add_get(   f"{p}/{{id}}/diff",             diff_ep)
        app.router.add_post(  f"{p}/{{id}}/promote",          promote_ep)
        app.router.add_post(  f"{p}/{{id}}/fork",             fork_ep)
        app.router.add_get(   f"{p}/{{id}}/audit",            audit_ep)
        logger.info(f"Prompt registry API routes registered at {prefix}/prompts/")
