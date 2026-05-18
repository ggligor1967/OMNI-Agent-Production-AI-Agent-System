"""OMNI Agent — Prompt Versioning: versioned prompts with diff, tags, rollback."""
from __future__ import annotations
import difflib, hashlib, json, sqlite3, time, uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PromptVersion:
    version_id: str
    prompt_id: str
    version: int
    content: str
    author: str = "system"
    message: str = ""
    created_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = False

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id": self.version_id,
            "prompt_id": self.prompt_id,
            "version": self.version,
            "content": self.content,
            "author": self.author,
            "message": self.message,
            "created_at": self.created_at,
            "tags": self.tags,
            "content_hash": self.content_hash,
            "is_active": self.is_active,
        }


class PromptNotFound(Exception):
    pass


class VersionNotFound(Exception):
    pass


class PromptVersionStore:
    """
    Git-like versioning for prompts.
    Each named prompt has a linear version history with active/rollback semantics.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        # In-memory index: prompt_id → List[PromptVersion] (sorted by version)
        self._versions: Dict[str, List[PromptVersion]] = {}
        # Active version per prompt_id
        self._active: Dict[str, str] = {}  # prompt_id → version_id

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS pv_prompts (
                prompt_id TEXT, version_id TEXT PRIMARY KEY,
                version INTEGER, content TEXT, author TEXT, message TEXT,
                created_at REAL, tags TEXT, metadata TEXT, is_active INTEGER
            );
            CREATE TABLE IF NOT EXISTS pv_deployments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_id TEXT, version_id TEXT, ts REAL, author TEXT, note TEXT
            );
        """)
        self._db.commit()

    # ── WRITE ─────────────────────────────────────────────────────────

    def commit(
        self,
        prompt_id: str,
        content: str,
        author: str = "system",
        message: str = "",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
        auto_activate: bool = True,
    ) -> PromptVersion:
        """Create a new version of a prompt."""
        versions = self._versions.setdefault(prompt_id, [])
        version_num = (versions[-1].version + 1) if versions else 1
        pv = PromptVersion(
            version_id=str(uuid.uuid4()),
            prompt_id=prompt_id,
            version=version_num,
            content=content,
            author=author,
            message=message,
            tags=tags or [],
            metadata=metadata or {},
        )
        versions.append(pv)
        self._db.execute(
            "INSERT INTO pv_prompts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (prompt_id, pv.version_id, version_num, content,
             author, message, pv.created_at,
             json.dumps(pv.tags), json.dumps(pv.metadata), 0))
        self._db.commit()
        if auto_activate:
            self.activate(prompt_id, pv.version_id)
        return pv

    def activate(self, prompt_id: str, version_id: str):
        """Set a specific version as the active/live version."""
        versions = self._versions.get(prompt_id, [])
        target = next((v for v in versions if v.version_id == version_id), None)
        if target is None:
            raise VersionNotFound(f"{prompt_id}@{version_id}")
        # Deactivate current
        if prompt_id in self._active:
            old_vid = self._active[prompt_id]
            old = next((v for v in versions if v.version_id == old_vid), None)
            if old:
                old.is_active = False
        target.is_active = True
        self._active[prompt_id] = version_id
        self._db.execute("UPDATE pv_prompts SET is_active=0 WHERE prompt_id=?", (prompt_id,))
        self._db.execute("UPDATE pv_prompts SET is_active=1 WHERE version_id=?", (version_id,))
        self._db.execute(
            "INSERT INTO pv_deployments (prompt_id,version_id,ts,author,note) VALUES (?,?,?,?,?)",
            (prompt_id, version_id, time.time(), "system", "activate"))
        self._db.commit()

    def rollback(self, prompt_id: str, steps: int = 1) -> PromptVersion:
        """Roll back N versions from current active."""
        versions = self._versions.get(prompt_id, [])
        if not versions:
            raise PromptNotFound(prompt_id)
        current_vid = self._active.get(prompt_id)
        current_idx = next(
            (i for i, v in enumerate(versions) if v.version_id == current_vid), len(versions) - 1)
        target_idx = max(0, current_idx - steps)
        target = versions[target_idx]
        self.activate(prompt_id, target.version_id)
        return target

    # ── READ ──────────────────────────────────────────────────────────

    def get_active(self, prompt_id: str) -> PromptVersion:
        vid = self._active.get(prompt_id)
        if vid is None:
            raise PromptNotFound(prompt_id)
        versions = self._versions.get(prompt_id, [])
        v = next((x for x in versions if x.version_id == vid), None)
        if v is None:
            raise VersionNotFound(vid)
        return v

    def get_version(self, prompt_id: str, version: int) -> PromptVersion:
        versions = self._versions.get(prompt_id, [])
        v = next((x for x in versions if x.version == version), None)
        if v is None:
            raise VersionNotFound(f"{prompt_id} v{version}")
        return v

    def get_by_id(self, version_id: str) -> PromptVersion:
        for versions in self._versions.values():
            for v in versions:
                if v.version_id == version_id:
                    return v
        raise VersionNotFound(version_id)

    def history(self, prompt_id: str) -> List[PromptVersion]:
        versions = self._versions.get(prompt_id)
        if versions is None:
            raise PromptNotFound(prompt_id)
        return list(versions)

    def list_prompts(self) -> List[str]:
        return list(self._versions.keys())

    def find_by_tag(self, tag: str) -> List[PromptVersion]:
        result = []
        for versions in self._versions.values():
            result.extend(v for v in versions if tag in v.tags)
        return result

    # ── DIFF ──────────────────────────────────────────────────────────

    def diff(self, prompt_id: str,
             version_a: int, version_b: int) -> List[str]:
        """Unified diff between two versions."""
        va = self.get_version(prompt_id, version_a)
        vb = self.get_version(prompt_id, version_b)
        lines_a = va.content.splitlines(keepends=True)
        lines_b = vb.content.splitlines(keepends=True)
        return list(difflib.unified_diff(
            lines_a, lines_b,
            fromfile=f"v{version_a}", tofile=f"v{version_b}"))

    def diff_with_active(self, prompt_id: str, version: int) -> List[str]:
        active = self.get_active(prompt_id)
        return self.diff(prompt_id, version, active.version)

    # ── DELETE ────────────────────────────────────────────────────────

    def delete_prompt(self, prompt_id: str):
        self._versions.pop(prompt_id, None)
        self._active.pop(prompt_id, None)
        self._db.execute("DELETE FROM pv_prompts WHERE prompt_id=?", (prompt_id,))
        self._db.commit()

    def prune(self, prompt_id: str, keep: int = 10) -> int:
        """Remove oldest versions, keeping only the last `keep`."""
        versions = self._versions.get(prompt_id, [])
        if len(versions) <= keep:
            return 0
        to_remove = versions[:-keep]
        kept = versions[-keep:]
        self._versions[prompt_id] = kept
        for v in to_remove:
            self._db.execute("DELETE FROM pv_prompts WHERE version_id=?", (v.version_id,))
        self._db.commit()
        return len(to_remove)

    # ── DEPLOYMENT HISTORY ────────────────────────────────────────────

    def deployment_log(self, prompt_id: str) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT version_id, ts, author, note FROM pv_deployments "
            "WHERE prompt_id=? ORDER BY ts DESC", (prompt_id,)).fetchall()
        return [{"version_id": r[0], "ts": r[1], "author": r[2], "note": r[3]}
                for r in rows]

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        total_versions = sum(len(v) for v in self._versions.values())
        return {
            "prompts": len(self._versions),
            "total_versions": total_versions,
            "active_prompts": len(self._active),
        }
