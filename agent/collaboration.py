"""
OMNI AGENT - Collaboration Module
Shared workspaces, multi-user sessions, notes, tasks, and team context.
"""
import time
import json
import uuid
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from agent.memory import MemoryDB
from agent.hooks import hooks, Event, EventType

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    status: str = "todo"           # todo | in_progress | done | blocked
    assignee: Optional[str] = None
    priority: int = 3              # 1=critical 5=low
    workspace_id: str = ""
    created_by: str = "system"
    created_at: float = field(default_factory=time.time)
    due_at: Optional[float] = None
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return self.__dict__.copy()

    @staticmethod
    def from_dict(d: Dict) -> "Task":
        return Task(**d)


@dataclass
class Note:
    id: str
    title: str
    content: str
    workspace_id: str
    author: str = "system"
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return self.__dict__.copy()


@dataclass
class Workspace:
    id: str
    name: str
    description: str = ""
    members: List[str] = field(default_factory=list)
    created_by: str = "system"
    created_at: float = field(default_factory=time.time)
    settings: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return self.__dict__.copy()


# ══════════════════════════════════════════════════════════════════════════════
# COLLABORATION MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class CollaborationManager:
    """
    Manages workspaces, tasks, notes, and shared agent context.
    Persists all data to MemoryDB under structured keys.
    """

    def __init__(self, memory: MemoryDB):
        self.memory = memory

    def _key(self, *parts) -> str:
        return ":".join(str(p) for p in parts)

    def _save(self, key: str, value: Any, category: str = "collab", importance: int = 5):
        self.memory.save_memory(key, json.dumps(value), category=category, importance=importance)

    def _load(self, key: str) -> Optional[Any]:
        raw = self.memory.get_memory(key)
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return raw
        return raw

    # ── Workspaces ────────────────────────────────────────────────────────────

    def create_workspace(self, name: str, created_by: str,
                         description: str = "", members: List[str] = None) -> Workspace:
        ws = Workspace(
            id=str(uuid.uuid4())[:8],
            name=name,
            description=description,
            members=members or [created_by],
            created_by=created_by,
        )
        self._save(self._key("ws", ws.id), ws.to_dict(), importance=7)
        # Add to workspace index
        index = self._load("ws:index") or []
        index.append(ws.id)
        self._save("ws:index", index)
        logger.info(f"Workspace created: {ws.name} [{ws.id}]")
        return ws

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        data = self._load(self._key("ws", workspace_id))
        return Workspace(**data) if data else None

    def list_workspaces(self) -> List[Workspace]:
        index = self._load("ws:index") or []
        workspaces = []
        for wid in index:
            ws = self.get_workspace(wid)
            if ws:
                workspaces.append(ws)
        return workspaces

    def add_member(self, workspace_id: str, user_id: str) -> bool:
        ws = self.get_workspace(workspace_id)
        if not ws:
            return False
        if user_id not in ws.members:
            ws.members.append(user_id)
            self._save(self._key("ws", workspace_id), ws.to_dict())
        return True

    # ── Tasks ─────────────────────────────────────────────────────────────────

    def create_task(self, workspace_id: str, title: str, created_by: str,
                    description: str = "", assignee: str = None,
                    priority: int = 3, tags: List[str] = None,
                    due_at: float = None) -> Task:
        task = Task(
            id=str(uuid.uuid4())[:8],
            title=title,
            description=description,
            workspace_id=workspace_id,
            created_by=created_by,
            assignee=assignee,
            priority=priority,
            tags=tags or [],
            due_at=due_at,
        )
        self._save(self._key("task", task.id), task.to_dict())

        # Add to workspace task index
        idx_key = self._key("ws_tasks", workspace_id)
        index = self._load(idx_key) or []
        index.append(task.id)
        self._save(idx_key, index)

        self.memory.audit("task.created", actor=created_by,
                         details={"task_id": task.id, "title": title})
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        data = self._load(self._key("task", task_id))
        return Task(**data) if data else None

    def update_task_status(self, task_id: str, status: str,
                           actor: str = "system") -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        valid = {"todo", "in_progress", "done", "blocked"}
        if status not in valid:
            raise ValueError(f"Invalid status. Must be one of: {valid}")
        task.status = status
        self._save(self._key("task", task_id), task.to_dict())
        self.memory.audit("task.status_changed", actor=actor,
                         details={"task_id": task_id, "status": status})
        return True

    def assign_task(self, task_id: str, assignee: str, actor: str = "system") -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        task.assignee = assignee
        self._save(self._key("task", task_id), task.to_dict())
        return True

    def list_tasks(self, workspace_id: str, status: str = None) -> List[Task]:
        idx_key = self._key("ws_tasks", workspace_id)
        task_ids = self._load(idx_key) or []
        tasks = []
        for tid in task_ids:
            t = self.get_task(tid)
            if t and (status is None or t.status == status):
                tasks.append(t)
        return sorted(tasks, key=lambda t: t.priority)

    def get_tasks_for_user(self, user_id: str, workspace_id: str = None) -> List[Task]:
        wss = [self.get_workspace(workspace_id)] if workspace_id else self.list_workspaces()
        tasks = []
        for ws in wss:
            if ws:
                tasks.extend(self.list_tasks(ws.id))
        return [t for t in tasks if t.assignee == user_id]

    # ── Notes ─────────────────────────────────────────────────────────────────

    def create_note(self, workspace_id: str, title: str, content: str,
                    author: str = "system", tags: List[str] = None) -> Note:
        note = Note(
            id=str(uuid.uuid4())[:8],
            title=title,
            content=content,
            workspace_id=workspace_id,
            author=author,
            tags=tags or [],
        )
        self._save(self._key("note", note.id), note.to_dict())

        idx_key = self._key("ws_notes", workspace_id)
        index = self._load(idx_key) or []
        index.append(note.id)
        self._save(idx_key, index)
        return note

    def get_note(self, note_id: str) -> Optional[Note]:
        data = self._load(self._key("note", note_id))
        return Note(**data) if data else None

    def update_note(self, note_id: str, content: str, author: str = "system") -> bool:
        note = self.get_note(note_id)
        if not note:
            return False
        note.content = content
        note.updated_at = time.time()
        self._save(self._key("note", note_id), note.to_dict())
        return True

    def list_notes(self, workspace_id: str) -> List[Note]:
        idx_key = self._key("ws_notes", workspace_id)
        note_ids = self._load(idx_key) or []
        notes = []
        for nid in note_ids:
            n = self.get_note(nid)
            if n:
                notes.append(n)
        return sorted(notes, key=lambda n: n.updated_at, reverse=True)

    def search_notes(self, workspace_id: str, query: str) -> List[Note]:
        query_lower = query.lower()
        return [
            n for n in self.list_notes(workspace_id)
            if query_lower in n.title.lower() or query_lower in n.content.lower()
        ]

    # ── Shared Agent Context ──────────────────────────────────────────────────

    def share_context(self, workspace_id: str, key: str, value: Any,
                      author: str = "system"):
        """Store shared context accessible to all workspace members."""
        ctx_key = self._key("ws_ctx", workspace_id, key)
        self._save(ctx_key, {"value": value, "author": author, "ts": time.time()},
                  category="workspace", importance=6)

    def get_shared_context(self, workspace_id: str, key: str) -> Optional[Any]:
        ctx_key = self._key("ws_ctx", workspace_id, key)
        data = self._load(ctx_key)
        return data.get("value") if data else None

    # ── Summary ───────────────────────────────────────────────────────────────

    def workspace_summary(self, workspace_id: str) -> Dict:
        ws = self.get_workspace(workspace_id)
        if not ws:
            return {"error": "Workspace not found"}
        tasks = self.list_tasks(workspace_id)
        notes = self.list_notes(workspace_id)
        return {
            "workspace": ws.to_dict(),
            "tasks": {
                "total": len(tasks),
                "todo": sum(1 for t in tasks if t.status == "todo"),
                "in_progress": sum(1 for t in tasks if t.status == "in_progress"),
                "done": sum(1 for t in tasks if t.status == "done"),
                "blocked": sum(1 for t in tasks if t.status == "blocked"),
            },
            "notes_count": len(notes),
        }
