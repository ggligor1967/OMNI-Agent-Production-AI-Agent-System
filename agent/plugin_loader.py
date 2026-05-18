"""
OMNI AGENT - Plugin Loader
Dynamic plugin system: discover, load, and manage Python plugins at runtime.
Plugins can extend the agent with new tools, hooks, and route handlers
without modifying core code.

Features:
- Discovery: scan directories for plugin packages (has plugin.py + metadata)
- Lifecycle: load → validate → activate → deactivate → unload
- Hook registry: plugins attach callbacks to named events
  (before_chat, after_chat, on_error, on_startup, on_shutdown, ...)
- Tool registration: plugins contribute new tools to the tool registry
- Route registration: plugins add HTTP endpoints to the gateway app
- Isolation: each plugin runs in its own import namespace
- Hot reload: reload a plugin's code without restarting the agent
- Dependency checks: plugins declare required agent modules
- Metadata: name, version, author, description, min_agent_version
- REST API: list, load, unload, reload, hook inspection
"""
import os
import sys
import time
import uuid
import json
import importlib
import importlib.util
import logging
import traceback
from typing import Any, Callable, Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# PLUGIN METADATA & STATUS
# ══════════════════════════════════════════════════════════════════════════════

class PluginStatus(str, Enum):
    DISCOVERED  = "discovered"
    LOADED      = "loaded"
    ACTIVE      = "active"
    INACTIVE    = "inactive"
    ERROR       = "error"
    UNLOADED    = "unloaded"


@dataclass
class PluginMeta:
    """Metadata declared by a plugin (read from plugin.py or metadata dict)."""
    name: str
    version: str = "0.1.0"
    author: str = ""
    description: str = ""
    requires: List[str] = field(default_factory=list)   # required agent module names
    hooks: List[str] = field(default_factory=list)       # hook names it handles
    tags: List[str] = field(default_factory=list)
    min_agent_version: str = "0.0.0"

    def to_dict(self) -> Dict:
        return {
            "name": self.name, "version": self.version,
            "author": self.author, "description": self.description,
            "requires": self.requires, "hooks": self.hooks,
            "tags": self.tags,
        }


@dataclass
class Plugin:
    """A loaded plugin instance."""
    id: str
    path: str
    meta: PluginMeta
    module: Any = None    # the loaded Python module
    status: PluginStatus = PluginStatus.DISCOVERED
    loaded_at: Optional[float] = None
    activated_at: Optional[float] = None
    error: str = ""
    reload_count: int = 0

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "path": self.path,
            "status": self.status, "error": self.error,
            "loaded_at": self.loaded_at, "activated_at": self.activated_at,
            "reload_count": self.reload_count,
            **self.meta.to_dict(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# HOOK REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

# Built-in hook names
HOOKS = {
    "on_startup",       "on_shutdown",
    "before_chat",      "after_chat",
    "before_tool_call", "after_tool_call",
    "on_error",         "on_session_start", "on_session_end",
    "before_stream",    "after_stream",
    "on_rate_limit",    "on_budget_alert",
    "before_embed",     "after_embed",
}


class HookRegistry:
    """
    Central registry for plugin event hooks.
    Multiple plugins can register for the same hook.
    """

    def __init__(self):
        self._hooks: Dict[str, List[Callable]] = {}

    def register(self, hook_name: str, fn: Callable):
        if hook_name not in self._hooks:
            self._hooks[hook_name] = []
        self._hooks[hook_name].append(fn)
        logger.debug(f"Hook registered: {hook_name} ← {fn.__name__}")

    def unregister_plugin(self, plugin_id: str):
        """Remove all hooks registered by a plugin (identified by closure attr)."""
        for hook_name in list(self._hooks.keys()):
            self._hooks[hook_name] = [
                fn for fn in self._hooks[hook_name]
                if getattr(fn, "_plugin_id", None) != plugin_id
            ]

    async def fire(self, hook_name: str, **kwargs) -> List[Any]:
        """Fire a hook and collect results from all handlers."""
        import asyncio
        results = []
        for fn in self._hooks.get(hook_name, []):
            try:
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(**kwargs)
                else:
                    result = fn(**kwargs)
                results.append(result)
            except Exception as e:
                logger.warning(f"Hook '{hook_name}' handler error "
                               f"({fn.__name__}): {e}")
        return results

    def fire_sync(self, hook_name: str, **kwargs) -> List[Any]:
        """Synchronous hook dispatch."""
        results = []
        for fn in self._hooks.get(hook_name, []):
            try:
                result = fn(**kwargs)
                results.append(result)
            except Exception as e:
                logger.warning(f"Hook '{hook_name}' handler error: {e}")
        return results

    def list_hooks(self) -> Dict[str, int]:
        return {name: len(fns) for name, fns in self._hooks.items() if fns}

    def list_all_hooks(self) -> List[str]:
        return sorted(HOOKS)


# ══════════════════════════════════════════════════════════════════════════════
# PLUGIN LOADER
# ══════════════════════════════════════════════════════════════════════════════

class PluginLoader:
    """
    Discover, load, and manage plugins.

    Plugin structure (directory-based):
        my_plugin/
            plugin.py          ← required: defines METADATA and setup(context)
            __init__.py        ← optional

    plugin.py must define:
        METADATA = {
            "name": "my_plugin",
            "version": "1.0.0",
            "description": "Does something useful",
            "requires": ["session", "search"],   # optional
            "hooks": ["before_chat", "after_chat"],
        }

        def setup(context):
            # context: dict with agent modules + hook_registry + app
            hooks = context["hook_registry"]
            hooks.register("before_chat", my_before_chat_handler)

        def teardown(context):  # optional
            # cleanup on deactivate
            pass

    Usage:
        loader = PluginLoader(plugin_dirs=["plugins/", "~/.omni/plugins/"])
        loader.discover()
        loader.load_all()
        loader.activate_all(context={"session": session_mgr, ...})

        # Hot reload
        loader.reload("my_plugin")

        # Events
        await loader.hooks.fire("before_chat", message=msg, user_id=uid)
    """

    def __init__(self, plugin_dirs: List[str] = None,
                 hook_registry: HookRegistry = None):
        self._dirs = [Path(d).expanduser() for d in (plugin_dirs or [])]
        self.hooks = hook_registry or HookRegistry()
        self._plugins: Dict[str, Plugin] = {}   # plugin.id → Plugin
        self._name_index: Dict[str, str] = {}   # name → plugin.id

    # ── Discovery ─────────────────────────────────────────────────────────────

    def discover(self) -> List[Plugin]:
        """Scan plugin directories for valid plugin packages."""
        found = []
        for plugin_dir in self._dirs:
            if not plugin_dir.exists():
                logger.debug(f"Plugin dir not found: {plugin_dir}")
                continue
            for entry in plugin_dir.iterdir():
                if entry.is_dir() and (entry / "plugin.py").exists():
                    plugin = self._discover_one(entry)
                    if plugin:
                        found.append(plugin)
        logger.info(f"Discovered {len(found)} plugins in {len(self._dirs)} dirs")
        return found

    def _discover_one(self, path: Path) -> Optional[Plugin]:
        """Try to read metadata from a plugin directory."""
        plugin_py = path / "plugin.py"
        try:
            spec = importlib.util.spec_from_file_location(
                f"_plugin_meta_{path.name}", plugin_py
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            raw_meta = getattr(mod, "METADATA", {})
            meta = PluginMeta(
                name=raw_meta.get("name", path.name),
                version=raw_meta.get("version", "0.1.0"),
                author=raw_meta.get("author", ""),
                description=raw_meta.get("description", ""),
                requires=raw_meta.get("requires", []),
                hooks=raw_meta.get("hooks", []),
                tags=raw_meta.get("tags", []),
            )
            pid = str(uuid.uuid4())[:10]
            plugin = Plugin(id=pid, path=str(path), meta=meta)
            self._plugins[pid] = plugin
            self._name_index[meta.name] = pid
            logger.info(f"Discovered plugin: '{meta.name}' v{meta.version} "
                       f"at {path}")
            return plugin
        except Exception as e:
            logger.warning(f"Failed to discover plugin at {path}: {e}")
            return None

    def register_in_memory(self, name: str, setup_fn: Callable,
                            meta: Dict = None,
                            teardown_fn: Callable = None) -> Plugin:
        """
        Register an in-memory plugin (no files needed).
        Useful for testing or programmatic plugin creation.
        """
        pid = str(uuid.uuid4())[:10]
        m = meta or {}
        plugin_meta = PluginMeta(
            name=name,
            version=m.get("version", "0.1.0"),
            description=m.get("description", ""),
            requires=m.get("requires", []),
            hooks=m.get("hooks", []),
        )

        # Create a synthetic module
        class _SyntheticModule:
            METADATA = m
            setup = staticmethod(setup_fn)
            if teardown_fn:
                teardown = staticmethod(teardown_fn)

        plugin = Plugin(id=pid, path="<in-memory>", meta=plugin_meta,
                        module=_SyntheticModule, status=PluginStatus.LOADED,
                        loaded_at=time.time())
        self._plugins[pid] = plugin
        self._name_index[name] = pid
        return plugin

    # ── Loading ───────────────────────────────────────────────────────────────

    def load(self, name_or_id: str) -> bool:
        """Load a discovered plugin (import its module)."""
        plugin = self._get(name_or_id)
        if not plugin:
            logger.warning(f"Plugin '{name_or_id}' not found")
            return False
        if plugin.status in (PluginStatus.LOADED, PluginStatus.ACTIVE):
            return True
        if plugin.path == "<in-memory>":
            plugin.status = PluginStatus.LOADED
            plugin.loaded_at = time.time()
            return True
        try:
            plugin_py = Path(plugin.path) / "plugin.py"
            spec = importlib.util.spec_from_file_location(
                f"plugin_{plugin.meta.name}", plugin_py
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            plugin.module = mod
            plugin.status = PluginStatus.LOADED
            plugin.loaded_at = time.time()
            logger.info(f"Plugin loaded: '{plugin.meta.name}'")
            return True
        except Exception as e:
            plugin.status = PluginStatus.ERROR
            plugin.error = str(e)
            logger.error(f"Failed to load plugin '{plugin.meta.name}': {e}")
            return False

    def load_all(self) -> Dict[str, bool]:
        return {p.meta.name: self.load(p.id) for p in self._plugins.values()}

    # ── Activation ────────────────────────────────────────────────────────────

    def activate(self, name_or_id: str, context: Dict = None) -> bool:
        """
        Activate a loaded plugin: call its setup(context) function.
        context should contain agent module references.
        """
        plugin = self._get(name_or_id)
        if not plugin or plugin.status not in (PluginStatus.LOADED,
                                                PluginStatus.INACTIVE):
            return False
        ctx = context or {}
        ctx["hook_registry"] = self.hooks
        ctx["plugin_id"] = plugin.id
        try:
            setup_fn = getattr(plugin.module, "setup", None)
            if setup_fn:
                setup_fn(ctx)
            plugin.status = PluginStatus.ACTIVE
            plugin.activated_at = time.time()
            logger.info(f"Plugin activated: '{plugin.meta.name}'")
            return True
        except Exception as e:
            plugin.status = PluginStatus.ERROR
            plugin.error = traceback.format_exc()[:500]
            logger.error(f"Plugin activation failed '{plugin.meta.name}': {e}")
            return False

    def activate_all(self, context: Dict = None) -> Dict[str, bool]:
        results = {}
        for plugin in self._plugins.values():
            if plugin.status == PluginStatus.LOADED:
                results[plugin.meta.name] = self.activate(plugin.id, context)
        return results

    def deactivate(self, name_or_id: str, context: Dict = None) -> bool:
        """Call teardown() on a plugin and mark inactive."""
        plugin = self._get(name_or_id)
        if not plugin or plugin.status != PluginStatus.ACTIVE:
            return False
        try:
            teardown_fn = getattr(plugin.module, "teardown", None)
            if teardown_fn:
                teardown_fn(context or {})
        except Exception as e:
            logger.warning(f"Plugin teardown error '{plugin.meta.name}': {e}")
        self.hooks.unregister_plugin(plugin.id)
        plugin.status = PluginStatus.INACTIVE
        return True

    def reload(self, name_or_id: str, context: Dict = None) -> bool:
        """Deactivate, reload code, and re-activate a plugin."""
        plugin = self._get(name_or_id)
        if not plugin:
            return False
        was_active = plugin.status == PluginStatus.ACTIVE
        if was_active:
            self.deactivate(plugin.id, context)
        loaded = self.load(plugin.id)
        if loaded and was_active:
            activated = self.activate(plugin.id, context)
            plugin.reload_count += 1
            return activated
        return loaded

    def unload(self, name_or_id: str, context: Dict = None) -> bool:
        """Fully remove a plugin."""
        plugin = self._get(name_or_id)
        if not plugin:
            return False
        if plugin.status == PluginStatus.ACTIVE:
            self.deactivate(plugin.id, context)
        plugin.status = PluginStatus.UNLOADED
        plugin.module = None
        return True

    # ── Query ─────────────────────────────────────────────────────────────────

    def _get(self, name_or_id: str) -> Optional[Plugin]:
        if name_or_id in self._plugins:
            return self._plugins[name_or_id]
        pid = self._name_index.get(name_or_id)
        return self._plugins.get(pid) if pid else None

    def list_plugins(self, status: PluginStatus = None) -> List[Plugin]:
        plugins = list(self._plugins.values())
        if status:
            plugins = [p for p in plugins if p.status == status]
        return sorted(plugins, key=lambda p: p.meta.name)

    def get_plugin(self, name: str) -> Optional[Plugin]:
        return self._get(name)

    def stats(self) -> Dict:
        plugins = list(self._plugins.values())
        return {
            "total": len(plugins),
            "active": sum(1 for p in plugins if p.status == PluginStatus.ACTIVE),
            "loaded": sum(1 for p in plugins if p.status == PluginStatus.LOADED),
            "error": sum(1 for p in plugins if p.status == PluginStatus.ERROR),
            "hooks_registered": self.hooks.list_hooks(),
        }

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def list_ep(request):
            plugins = self.list_plugins()
            return web.json_response({"plugins": [p.to_dict() for p in plugins]})

        async def get_ep(request):
            plugin = self._get(request.match_info["name"])
            if not plugin:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response(plugin.to_dict())

        async def control_ep(request):
            name = request.match_info["name"]
            action = request.match_info["action"]
            data = await request.json() if request.content_length else {}
            ctx = data.get("context", {})
            if action == "load":
                ok = self.load(name)
            elif action == "activate":
                ok = self.activate(name, ctx)
            elif action == "deactivate":
                ok = self.deactivate(name, ctx)
            elif action == "reload":
                ok = self.reload(name, ctx)
            elif action == "unload":
                ok = self.unload(name, ctx)
            else:
                return web.json_response({"error": "unknown action"}, status=400)
            plugin = self._get(name)
            return web.json_response({
                "ok": ok,
                "status": plugin.status if plugin else "not_found",
            })

        async def hooks_ep(request):
            return web.json_response({
                "registered": self.hooks.list_hooks(),
                "available": self.hooks.list_all_hooks(),
            })

        async def stats_ep(request):
            return web.json_response(self.stats())

        p = f"{prefix}/plugins"
        app.router.add_get( p,                            list_ep)
        app.router.add_get( f"{p}/hooks",                 hooks_ep)
        app.router.add_get( f"{p}/stats",                 stats_ep)
        app.router.add_get( f"{p}/{{name}}",              get_ep)
        app.router.add_post(f"{p}/{{name}}/{{action}}",   control_ep)
        logger.info(f"Plugin loader API registered at {prefix}/plugins/")
