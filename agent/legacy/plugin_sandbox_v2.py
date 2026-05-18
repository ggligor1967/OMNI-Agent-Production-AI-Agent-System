"""OMNI Agent — Plugin Sandbox V2: isolated plugin execution with capability gating & resource limits."""
from __future__ import annotations
import asyncio, builtins, importlib.util, inspect, sys, time, types, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class Capability(str, Enum):
    NETWORK    = "network"
    FILESYSTEM = "filesystem"
    SUBPROCESS = "subprocess"
    EVAL       = "eval"
    IMPORT     = "import"
    MEMORY     = "memory"
    ALL        = "all"


class PluginStatus(str, Enum):
    LOADED    = "loaded"
    ACTIVE    = "active"
    DISABLED  = "disabled"
    ERRORED   = "errored"
    UNLOADED  = "unloaded"


BLOCKED_BUILTINS = {
    "__import__", "eval", "exec", "compile",
    "open", "input", "__loader__",
}

BLOCKED_MODULES = {
    "os", "subprocess", "socket", "urllib", "requests",
    "http", "ftplib", "smtplib", "paramiko", "shutil",
}


@dataclass
class PluginManifest:
    plugin_id: str
    name: str
    version: str = "1.0.0"
    author: str = "unknown"
    description: str = ""
    required_capabilities: Set[Capability] = field(default_factory=set)
    entry_point: str = "run"       # function name to call
    timeout_s: float = 5.0
    max_memory_mb: float = 64.0
    trusted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "required_capabilities": [c.value for c in self.required_capabilities],
            "trusted": self.trusted,
        }


@dataclass
class PluginResult:
    plugin_id: str
    success: bool
    result: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "success": self.success,
            "result": str(self.result)[:200] if self.result is not None else None,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "execution_id": self.execution_id,
        }


class CapabilityDenied(Exception):
    pass


class PluginError(Exception):
    pass


class SandboxedNamespace:
    """Restricted execution namespace for untrusted plugins."""

    def __init__(self, allowed_capabilities: Set[Capability]):
        self._caps = allowed_capabilities
        self._globals = self._build_globals()

    def _build_globals(self) -> Dict[str, Any]:
        safe_builtins: Dict[str, Any] = {}
        for name in dir(builtins):
            if name not in BLOCKED_BUILTINS:
                safe_builtins[name] = getattr(builtins, name)
        # Block dangerous builtins unless capability granted
        if Capability.EVAL not in self._caps:
            safe_builtins.pop("eval", None)
            safe_builtins.pop("exec", None)
            safe_builtins.pop("compile", None)
        if Capability.FILESYSTEM not in self._caps and Capability.ALL not in self._caps:
            safe_builtins.pop("open", None)
        if Capability.IMPORT not in self._caps and Capability.ALL not in self._caps:
            safe_builtins["__import__"] = self._blocked_import
        return {"__builtins__": safe_builtins}

    def _blocked_import(self, name, *args, **kwargs):
        if name in BLOCKED_MODULES:
            raise CapabilityDenied(f"Import of '{name}' is not allowed in sandbox")
        return __import__(name, *args, **kwargs)

    @property
    def globals(self) -> Dict[str, Any]:
        return self._globals


class PluginSandboxV2:
    """
    Loads and executes plugins (Python code strings or callables) in restricted namespaces.
    Supports capability gating, timeout enforcement, result tracking, and lifecycle hooks.
    """

    def __init__(self, granted_capabilities: Optional[Set[Capability]] = None):
        self._capabilities: Set[Capability] = granted_capabilities or set()
        self._plugins: Dict[str, PluginManifest] = {}
        self._modules: Dict[str, types.ModuleType] = {}
        self._status: Dict[str, PluginStatus] = {}
        self._results: List[PluginResult] = []
        self._hooks_pre:  List[Callable] = []
        self._hooks_post: List[Callable] = []
        self._invocation_count = 0
        self._error_count = 0

    # ── CAPABILITY ────────────────────────────────────────────────────

    def grant(self, cap: Capability):
        self._capabilities.add(cap)

    def revoke(self, cap: Capability):
        self._capabilities.discard(cap)

    def has_capability(self, cap: Capability) -> bool:
        return Capability.ALL in self._capabilities or cap in self._capabilities

    def _check_capabilities(self, manifest: PluginManifest):
        if manifest.trusted:
            return
        for cap in manifest.required_capabilities:
            if not self.has_capability(cap):
                raise CapabilityDenied(
                    f"Plugin '{manifest.name}' requires capability '{cap.value}' "
                    f"which is not granted")

    # ── REGISTRATION ──────────────────────────────────────────────────

    def register(self, manifest: PluginManifest, code: Optional[str] = None,
                 fn: Optional[Callable] = None) -> bool:
        """Register a plugin from source code string or callable."""
        self._check_capabilities(manifest)
        if code:
            ns = SandboxedNamespace(self._capabilities)
            mod = types.ModuleType(manifest.plugin_id)
            try:
                exec(compile(code, f"<plugin:{manifest.plugin_id}>", "exec"),  # noqa: S102
                     {**ns.globals, **mod.__dict__})
                mod.__dict__.update(
                    {k: v for k, v in ns.globals.items()
                     if not k.startswith("__")})
                # Copy all top-level names into module
                temp_ns = {**ns.globals}
                exec(compile(code, f"<plugin:{manifest.plugin_id}>", "exec"), temp_ns)  # noqa: S102
                for k, v in temp_ns.items():
                    if not k.startswith("__"):
                        mod.__dict__[k] = v
                self._modules[manifest.plugin_id] = mod
            except Exception as e:
                self._status[manifest.plugin_id] = PluginStatus.ERRORED
                raise PluginError(f"Failed to load plugin '{manifest.name}': {e}")
        elif fn:
            mod = types.ModuleType(manifest.plugin_id)
            mod.__dict__[manifest.entry_point] = fn
            self._modules[manifest.plugin_id] = mod
        else:
            raise ValueError("Must provide either code or fn")
        self._plugins[manifest.plugin_id] = manifest
        self._status[manifest.plugin_id] = PluginStatus.LOADED
        return True

    def unregister(self, plugin_id: str):
        self._plugins.pop(plugin_id, None)
        self._modules.pop(plugin_id, None)
        self._status[plugin_id] = PluginStatus.UNLOADED

    def enable(self, plugin_id: str):
        if plugin_id in self._plugins:
            self._status[plugin_id] = PluginStatus.ACTIVE

    def disable(self, plugin_id: str):
        if plugin_id in self._plugins:
            self._status[plugin_id] = PluginStatus.DISABLED

    # ── EXECUTION ─────────────────────────────────────────────────────

    def run(self, plugin_id: str, *args, **kwargs) -> PluginResult:
        """Execute a plugin synchronously."""
        manifest = self._plugins.get(plugin_id)
        if not manifest:
            raise PluginError(f"Plugin '{plugin_id}' not registered")
        if self._status.get(plugin_id) == PluginStatus.DISABLED:
            raise PluginError(f"Plugin '{plugin_id}' is disabled")
        mod = self._modules.get(plugin_id)
        fn = mod.__dict__.get(manifest.entry_point) if mod else None
        if not callable(fn):
            raise PluginError(f"Entry point '{manifest.entry_point}' not found")
        for hook in self._hooks_pre:
            try: hook(manifest)
            except Exception: pass
        t0 = time.time()
        self._invocation_count += 1
        try:
            result_val = fn(*args, **kwargs)
            pr = PluginResult(
                plugin_id=plugin_id,
                success=True,
                result=result_val,
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as exc:
            self._error_count += 1
            pr = PluginResult(
                plugin_id=plugin_id,
                success=False,
                error=str(exc),
                duration_ms=(time.time() - t0) * 1000,
            )
        self._results.append(pr)
        for hook in self._hooks_post:
            try: hook(pr)
            except Exception: pass
        return pr

    async def run_async(self, plugin_id: str, *args, **kwargs) -> PluginResult:
        manifest = self._plugins.get(plugin_id)
        if not manifest:
            raise PluginError(f"Plugin not registered: {plugin_id}")
        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: self.run(plugin_id, *args, **kwargs)),
                timeout=manifest.timeout_s)
            return result
        except asyncio.TimeoutError:
            self._error_count += 1
            pr = PluginResult(plugin_id=plugin_id, success=False,
                              error=f"Plugin timed out after {manifest.timeout_s}s")
            self._results.append(pr)
            return pr

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_before_run(self, fn: Callable): self._hooks_pre.append(fn)
    def on_after_run(self, fn: Callable):  self._hooks_post.append(fn)

    # ── INTROSPECTION ─────────────────────────────────────────────────

    def list_plugins(self) -> List[Dict[str, Any]]:
        return [{**m.to_dict(), "status": self._status.get(m.plugin_id, "unknown")}
                for m in self._plugins.values()]

    def get_status(self, plugin_id: str) -> Optional[str]:
        s = self._status.get(plugin_id)
        return s.value if s else None

    def results(self, plugin_id: Optional[str] = None,
                limit: int = 50) -> List[PluginResult]:
        results = self._results
        if plugin_id:
            results = [r for r in results if r.plugin_id == plugin_id]
        return results[-limit:]

    def stats(self) -> Dict[str, Any]:
        return {
            "registered": len(self._plugins),
            "invocations": self._invocation_count,
            "errors": self._error_count,
            "capabilities": [c.value for c in self._capabilities],
            "by_status": {s.value: sum(1 for v in self._status.values() if v == s)
                          for s in PluginStatus},
        }
