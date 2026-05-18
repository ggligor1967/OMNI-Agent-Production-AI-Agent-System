"""
OMNI AGENT - Agent Builder
Declarative agent construction from dict/YAML spec.
Wires all platform modules (session, search, cache, governance, federation,
observability, optimizer, etc.) into a ready-to-use agent instance.

Features:
- Spec-driven: define an agent entirely in Python dict or YAML
- Module auto-wiring: instantiates and connects all specified modules
- Profile system: reuse common configs (e.g. "research_agent", "coding_assistant")
- Hot reload: update agent config at runtime without restart
- Validation: check spec completeness and module compatibility
- Export: dump a running agent's config back to dict/YAML
- Multi-agent factory: build named fleets of agents from a single spec file
- Presets: built-in presets for common agent archetypes
"""
import copy
import time
import uuid
import logging
from typing import Any, Dict, List, Optional, Type
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SPEC SCHEMA
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_SPEC = {
    "id": None,                    # auto-generated if None
    "name": "omni-agent",
    "description": "",
    "version": "1.0.0",
    "model": {
        "default": "claude-3-5-sonnet",
        "fallback": "claude-3-haiku",
        "temperature": 0.7,
        "max_tokens": 4096,
    },
    "persona": {
        "name": "Assistant",
        "system_prompt": "You are a helpful, accurate, and concise assistant.",
        "traits": [],
    },
    "modules": {
        "session":       {"enabled": True,  "db": "data/sessions.db",    "ttl_days": 30},
        "memory":        {"enabled": True,  "max_items": 1000},
        "rag":           {"enabled": False, "index_path": "data/rag"},
        "search":        {"enabled": True,  "db": "data/search.db"},
        "cache":         {"enabled": True,  "db": "data/llm_cache.db",   "threshold": 0.92},
        "governance":    {"enabled": True,  "db": "data/governance.db"},
        "observability": {"enabled": True},
        "jobs":          {"enabled": True,  "db": "data/jobs.db",        "workers": 4},
        "federation":    {"enabled": False},
        "optimizer":     {"enabled": False, "db": "data/optimizer.db"},
    },
    "tools": [],                   # list of tool names to enable
    "rate_limits": {
        "requests_per_minute": 60,
        "tokens_per_minute": 100000,
    },
    "data": {
        "base_dir": "data",
    },
}

# Built-in presets
PRESETS: Dict[str, Dict] = {
    "minimal": {
        "name": "minimal-agent",
        "description": "Lightweight agent with session and memory only.",
        "modules": {
            "session": {"enabled": True, "db": "data/sessions.db"},
            "memory":  {"enabled": True, "max_items": 500},
        },
    },
    "research": {
        "name": "research-agent",
        "description": "Agent optimized for research: RAG, search, and caching.",
        "model": {"default": "claude-3-5-sonnet", "temperature": 0.3},
        "modules": {
            "session":    {"enabled": True},
            "memory":     {"enabled": True, "max_items": 2000},
            "rag":        {"enabled": True, "index_path": "data/rag"},
            "search":     {"enabled": True},
            "cache":      {"enabled": True, "threshold": 0.95},
            "governance": {"enabled": True},
        },
    },
    "coding": {
        "name": "coding-assistant",
        "description": "Agent optimized for code generation and review.",
        "model": {"default": "claude-3-5-sonnet", "temperature": 0.2},
        "persona": {
            "name": "Code Assistant",
            "system_prompt": (
                "You are an expert software engineer. "
                "Write clean, well-documented code with error handling. "
                "Prefer idiomatic patterns and explain your reasoning."
            ),
        },
        "modules": {
            "session": {"enabled": True},
            "memory":  {"enabled": True},
            "cache":   {"enabled": True, "threshold": 0.98},
        },
    },
    "enterprise": {
        "name": "enterprise-agent",
        "description": "Full-featured enterprise agent with all modules enabled.",
        "modules": {k: {"enabled": True} for k in DEFAULT_SPEC["modules"]},
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# BUILT AGENT
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BuiltAgent:
    """
    A fully constructed agent instance with all wired modules.
    Returned by AgentBuilder.build().
    """
    id: str
    name: str
    spec: Dict
    modules: Dict[str, Any] = field(default_factory=dict)
    built_at: float = field(default_factory=time.time)
    build_warnings: List[str] = field(default_factory=list)

    def get(self, module_name: str) -> Optional[Any]:
        """Get a module instance by name."""
        return self.modules.get(module_name)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "built_at": self.built_at,
            "modules": list(self.modules.keys()),
            "build_warnings": self.build_warnings,
            "spec_version": self.spec.get("version", "1.0.0"),
        }

    def to_yaml(self) -> str:
        try:
            import yaml
            return yaml.dump(self.spec, default_flow_style=False, sort_keys=False)
        except ImportError:
            import json
            return json.dumps(self.spec, indent=2)

    # ── Convenience accessors ──────────────────────────────────────────────────

    @property
    def session_manager(self):
        return self.modules.get("session")

    @property
    def search_service(self):
        return self.modules.get("search")

    @property
    def llm_cache(self):
        return self.modules.get("cache")

    @property
    def governance(self):
        return self.modules.get("governance")

    @property
    def observability(self):
        return self.modules.get("observability")

    @property
    def job_queue(self):
        return self.modules.get("jobs")

    @property
    def optimizer(self):
        return self.modules.get("optimizer")

    @property
    def federation(self):
        return self.modules.get("federation")

    @property
    def model_config(self) -> Dict:
        return self.spec.get("model", DEFAULT_SPEC["model"])

    @property
    def persona_config(self) -> Dict:
        return self.spec.get("persona", DEFAULT_SPEC["persona"])

    @property
    def system_prompt(self) -> str:
        return self.persona_config.get("system_prompt", "You are a helpful assistant.")


# ══════════════════════════════════════════════════════════════════════════════
# AGENT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

class AgentBuilder:
    """
    Declarative agent factory.

    Usage:
        # From dict spec
        builder = AgentBuilder()
        agent = builder.from_spec({
            "name": "my-agent",
            "model": {"default": "claude-3-5-sonnet"},
            "modules": {
                "session": {"enabled": True},
                "cache":   {"enabled": True, "threshold": 0.90},
            }
        }).build()

        # From preset
        agent = builder.from_preset("research").build()

        # From YAML file
        agent = builder.from_yaml("agents/research_agent.yaml").build()

        # Access modules
        session = agent.session_manager
        cache = agent.llm_cache
        gov = agent.governance
    """

    def __init__(self):
        self._spec: Dict = copy.deepcopy(DEFAULT_SPEC)
        self._overrides: Dict = {}

    # ── Spec sources ──────────────────────────────────────────────────────────

    def from_spec(self, spec: Dict) -> "AgentBuilder":
        """Load spec from a dictionary."""
        self._spec = self._merge(copy.deepcopy(DEFAULT_SPEC), spec)
        return self

    def from_preset(self, preset_name: str) -> "AgentBuilder":
        """Load a named built-in preset."""
        if preset_name not in PRESETS:
            raise ValueError(f"Unknown preset '{preset_name}'. "
                           f"Available: {list(PRESETS.keys())}")
        preset = copy.deepcopy(PRESETS[preset_name])
        self._spec = self._merge(copy.deepcopy(DEFAULT_SPEC), preset)
        return self

    def from_yaml(self, path: str) -> "AgentBuilder":
        """Load spec from a YAML file."""
        try:
            import yaml
            with open(path) as f:
                spec = yaml.safe_load(f)
        except ImportError:
            import json
            with open(path) as f:
                spec = json.load(f)
        return self.from_spec(spec)

    def from_dict(self, d: Dict) -> "AgentBuilder":
        return self.from_spec(d)

    # ── Fluent configuration ──────────────────────────────────────────────────

    def name(self, name: str) -> "AgentBuilder":
        self._spec["name"] = name
        return self

    def model(self, model_id: str, temperature: float = None,
              max_tokens: int = None) -> "AgentBuilder":
        self._spec.setdefault("model", {})["default"] = model_id
        if temperature is not None:
            self._spec["model"]["temperature"] = temperature
        if max_tokens is not None:
            self._spec["model"]["max_tokens"] = max_tokens
        return self

    def persona(self, name: str = None, system_prompt: str = None) -> "AgentBuilder":
        p = self._spec.setdefault("persona", {})
        if name:
            p["name"] = name
        if system_prompt:
            p["system_prompt"] = system_prompt
        return self

    def enable(self, module: str, **config) -> "AgentBuilder":
        self._spec.setdefault("modules", {})[module] = {"enabled": True, **config}
        return self

    def disable(self, module: str) -> "AgentBuilder":
        self._spec.setdefault("modules", {})[module] = {"enabled": False}
        return self

    def with_tools(self, *tool_names: str) -> "AgentBuilder":
        self._spec.setdefault("tools", []).extend(tool_names)
        return self

    def data_dir(self, base_dir: str) -> "AgentBuilder":
        self._spec.setdefault("data", {})["base_dir"] = base_dir
        return self

    # ── Validation ────────────────────────────────────────────────────────────

    def validate(self) -> List[str]:
        """Return list of validation warnings/errors."""
        issues: List[str] = []
        spec = self._spec

        if not spec.get("name"):
            issues.append("Agent name is required.")
        if not spec.get("model", {}).get("default"):
            issues.append("model.default is required.")

        mods = spec.get("modules", {})
        if mods.get("rag", {}).get("enabled") and not mods.get("rag", {}).get("index_path"):
            issues.append("modules.rag.index_path required when RAG is enabled.")
        if mods.get("federation", {}).get("enabled"):
            issues.append("Federation enabled: remember to register subagents manually.")

        return issues

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self) -> BuiltAgent:
        """Instantiate all enabled modules and return a BuiltAgent."""
        warnings = self.validate()
        spec = self._spec
        agent_id = spec.get("id") or str(uuid.uuid4())[:12]
        base_dir = spec.get("data", {}).get("base_dir", "data")
        mods_spec = spec.get("modules", {})
        modules: Dict[str, Any] = {}

        def db(name: str) -> str:
            return mods_spec.get(name, {}).get("db", f"{base_dir}/{name}.db")

        # ── Session ────────────────────────────────────────────────────────────
        if mods_spec.get("session", {}).get("enabled", True):
            try:
                from agent.session import SessionManager
                ttl_days = mods_spec.get("session", {}).get("ttl_days", 30)
                modules["session"] = SessionManager(
                    db_path=db("sessions"),
                    default_ttl_s=ttl_days * 86400,
                )
                logger.debug("Module built: session")
            except Exception as e:
                warnings.append(f"session module failed: {e}")

        # ── Search ─────────────────────────────────────────────────────────────
        if mods_spec.get("search", {}).get("enabled", True):
            try:
                from agent.search import SearchService
                modules["search"] = SearchService(db_path=db("search"))
                logger.debug("Module built: search")
            except Exception as e:
                warnings.append(f"search module failed: {e}")

        # ── LLM Cache ─────────────────────────────────────────────────────────
        if mods_spec.get("cache", {}).get("enabled", True):
            try:
                from agent.llm_cache import SemanticCache
                threshold = mods_spec.get("cache", {}).get("threshold", 0.92)
                modules["cache"] = SemanticCache(
                    threshold=threshold, db_path=db("llm_cache")
                )
                logger.debug("Module built: cache")
            except Exception as e:
                warnings.append(f"cache module failed: {e}")

        # ── Governance ────────────────────────────────────────────────────────
        if mods_spec.get("governance", {}).get("enabled", True):
            try:
                from agent.governance import GovernanceManager
                modules["governance"] = GovernanceManager(db_path=db("governance"))
                logger.debug("Module built: governance")
            except Exception as e:
                warnings.append(f"governance module failed: {e}")

        # ── Observability ─────────────────────────────────────────────────────
        if mods_spec.get("observability", {}).get("enabled", True):
            try:
                from agent.observability import MetricsRegistry
                modules["observability"] = MetricsRegistry()
                logger.debug("Module built: observability")
            except Exception as e:
                warnings.append(f"observability module failed: {e}")

        # ── Jobs ──────────────────────────────────────────────────────────────
        if mods_spec.get("jobs", {}).get("enabled", True):
            try:
                from agent.jobs import JobQueue
                workers = mods_spec.get("jobs", {}).get("workers", 4)
                modules["jobs"] = JobQueue(
                    db_path=db("jobs"),
                    num_workers=workers,
                )
                logger.debug("Module built: jobs")
            except Exception as e:
                warnings.append(f"jobs module failed: {e}")

        # ── Optimizer ─────────────────────────────────────────────────────────
        if mods_spec.get("optimizer", {}).get("enabled", False):
            try:
                from agent.optimizer import PromptOptimizer
                modules["optimizer"] = PromptOptimizer(db_path=db("optimizer"))
                logger.debug("Module built: optimizer")
            except Exception as e:
                warnings.append(f"optimizer module failed: {e}")

        # ── Federation ────────────────────────────────────────────────────────
        if mods_spec.get("federation", {}).get("enabled", False):
            try:
                from agent.federation import FederationEngine
                modules["federation"] = FederationEngine()
                logger.debug("Module built: federation")
            except Exception as e:
                warnings.append(f"federation module failed: {e}")

        # ── Datastore ─────────────────────────────────────────────────────────
        if mods_spec.get("datastore", {}).get("enabled", False):
            try:
                from agent.datastore import Datastore
                modules["datastore"] = Datastore(db_path=db("datastore"))
                logger.debug("Module built: datastore")
            except Exception as e:
                warnings.append(f"datastore module failed: {e}")

        agent = BuiltAgent(
            id=agent_id,
            name=spec.get("name", "omni-agent"),
            spec=copy.deepcopy(spec),
            modules=modules,
            build_warnings=warnings,
        )

        logger.info(
            f"Agent built: id={agent_id} name='{agent.name}' "
            f"modules={list(modules.keys())} warnings={len(warnings)}"
        )
        return agent

    # ── Multi-agent factory ───────────────────────────────────────────────────

    @classmethod
    def build_fleet(cls, fleet_spec: Dict[str, Dict]) -> Dict[str, BuiltAgent]:
        """
        Build multiple named agents from a fleet spec.

        fleet_spec = {
            "researcher": {"preset": "research", "name": "research-bot"},
            "coder":      {"preset": "coding",   "name": "code-bot"},
            "generic":    {"modules": {"session": {"enabled": True}}},
        }
        """
        fleet: Dict[str, BuiltAgent] = {}
        for name, spec in fleet_spec.items():
            builder = cls()
            preset = spec.pop("preset", None)
            if preset:
                builder.from_preset(preset)
            builder.from_spec(spec)
            fleet[name] = builder.build()
        return fleet

    # ── Spec utilities ────────────────────────────────────────────────────────

    def to_dict(self) -> Dict:
        return copy.deepcopy(self._spec)

    def to_yaml(self) -> str:
        try:
            import yaml
            return yaml.dump(self._spec, default_flow_style=False, sort_keys=False)
        except ImportError:
            import json
            return json.dumps(self._spec, indent=2)

    @staticmethod
    def _merge(base: Dict, override: Dict) -> Dict:
        """Deep merge override into base."""
        result = copy.deepcopy(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = AgentBuilder._merge(result[k], v)
            else:
                result[k] = copy.deepcopy(v)
        return result

    @staticmethod
    def list_presets() -> List[str]:
        return list(PRESETS.keys())

    @staticmethod
    def get_preset(name: str) -> Optional[Dict]:
        return copy.deepcopy(PRESETS.get(name))
