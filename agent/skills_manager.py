"""
OMNI AGENT - Skills Manager
Dynamic skill loading, execution, and hot-reload from DB and filesystem.
"""
import importlib.util
import inspect
import logging
import json
import textwrap
from typing import Dict, Any, Optional, List, Callable
from pathlib import Path
from agent.memory import MemoryDB
from agent.hooks import hooks, Event, EventType

logger = logging.getLogger(__name__)


class Skill:
    """A callable skill unit with metadata."""

    def __init__(self, name: str, description: str, handler: Callable,
                 triggers: List[str] = None, version: str = "1.0.0"):
        self.name = name
        self.description = description
        self.handler = handler
        self.triggers = triggers or []
        self.version = version
        self.enabled = True
        self.call_count = 0

    async def execute(self, *args, **kwargs) -> Any:
        self.call_count += 1
        await hooks.emit(Event(EventType.SKILL_EXECUTED, {
            "skill": self.name, "args": str(args)[:200]
        }))
        if inspect.iscoroutinefunction(self.handler):
            return await self.handler(*args, **kwargs)
        return self.handler(*args, **kwargs)

    def matches_trigger(self, text: str) -> bool:
        text_lower = text.lower()
        return any(t.lower() in text_lower for t in self.triggers)


class SkillsManager:
    """Registry for agent skills with DB persistence and filesystem loading."""

    def __init__(self, db: MemoryDB, skills_dir: str = "agent/skills"):
        self.db = db
        self.skills_dir = Path(skills_dir)
        self._skills: Dict[str, Skill] = {}

    def register(self, name: str, description: str, triggers: List[str] = None,
                 version: str = "1.0.0"):
        """Decorator to register a function as a skill."""
        def decorator(fn: Callable):
            skill = Skill(name=name, description=description,
                         handler=fn, triggers=triggers or [], version=version)
            self._skills[name] = skill
            logger.info(f"Skill registered: {name} v{version}")
            hooks.emit_sync(Event(EventType.SKILL_LOADED, {"skill": name}))
            return fn
        return decorator

    def load_from_directory(self):
        """Load all .py skill files from the skills directory."""
        if not self.skills_dir.exists():
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            return

        for path in self.skills_dir.glob("*.py"):
            if path.stem.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(path.stem, path)
                mod = importlib.util.module_from_spec(spec)
                # inject manager so skills can self-register
                mod.skills_manager = self
                spec.loader.exec_module(mod)
                logger.info(f"Loaded skill module: {path.name}")
            except Exception as e:
                logger.error(f"Failed to load skill {path.name}: {e}")

    def load_from_db(self):
        """Load skills stored as code strings in the database."""
        with self.db._conn() as conn:
            rows = conn.execute(
                "SELECT name, description, code, triggers FROM skills WHERE enabled=1"
            ).fetchall()
        for row in rows:
            try:
                ns: Dict = {}
                exec(textwrap.dedent(row["code"]), ns)
                handler = ns.get("handler") or ns.get("run") or ns.get("execute")
                if not handler:
                    logger.warning(f"DB skill '{row['name']}' has no handler/run/execute function")
                    continue
                triggers = json.loads(row["triggers"]) if row["triggers"] else []
                skill = Skill(name=row["name"], description=row["description"] or "",
                             handler=handler, triggers=triggers)
                self._skills[row["name"]] = skill
                logger.info(f"Loaded DB skill: {row['name']}")
            except Exception as e:
                logger.error(f"Failed to load DB skill '{row['name']}': {e}")

    def save_skill_to_db(self, name: str, description: str, code: str,
                         triggers: List[str] = None):
        with self.db._conn() as conn:
            conn.execute("""
                INSERT INTO skills (name, description, code, triggers)
                VALUES (?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    description=excluded.description,
                    code=excluded.code,
                    triggers=excluded.triggers
            """, (name, description, code, json.dumps(triggers or [])))

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    async def execute(self, name: str, *args, **kwargs) -> Any:
        skill = self.get(name)
        if not skill:
            raise KeyError(f"Skill '{name}' not found")
        if not skill.enabled:
            raise RuntimeError(f"Skill '{name}' is disabled")
        return await skill.execute(*args, **kwargs)

    def find_by_trigger(self, text: str) -> List[Skill]:
        return [s for s in self._skills.values() if s.enabled and s.matches_trigger(text)]

    def list_skills(self) -> List[Dict]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "triggers": s.triggers,
                "version": s.version,
                "enabled": s.enabled,
                "call_count": s.call_count,
            }
            for s in self._skills.values()
        ]

    def disable(self, name: str):
        if name in self._skills:
            self._skills[name].enabled = False

    def enable(self, name: str):
        if name in self._skills:
            self._skills[name].enabled = True
