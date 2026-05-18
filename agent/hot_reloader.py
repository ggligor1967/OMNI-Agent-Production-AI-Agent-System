"""OMNI Agent — Hot Reloader: live module reloading without restart."""
from __future__ import annotations
import importlib, importlib.util, os, sys, threading, time, hashlib, types
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ModuleRecord:
    module_name: str
    file_path: str
    loaded_at: float = field(default_factory=time.time)
    reload_count: int = 0
    last_hash: str = ""
    last_error: Optional[str] = None
    module_ref: Optional[types.ModuleType] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_name": self.module_name,
            "file_path": self.file_path,
            "loaded_at": self.loaded_at,
            "reload_count": self.reload_count,
            "last_error": self.last_error,
        }


def _file_hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.md5(  # nosec B324 - file change detection only
                f.read(), usedforsecurity=False
            ).hexdigest()
    except OSError:
        return ""


class HotReloader:
    """
    Monitors Python modules for file changes and reloads them automatically.
    Supports manual reload, hooks, and a background watcher thread.
    """

    def __init__(self, poll_interval: float = 1.0):
        self._records: Dict[str, ModuleRecord] = {}
        self._on_reload_hooks: List[Callable[[ModuleRecord], None]] = []
        self._on_error_hooks:  List[Callable[[ModuleRecord, Exception], None]] = []
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._watcher_thread: Optional[threading.Thread] = None
        self._reload_count = 0
        self._error_count = 0

    # ── REGISTRATION ──────────────────────────────────────────────────

    def register(self, module_name: str,
                 file_path: Optional[str] = None) -> ModuleRecord:
        """Register a module for hot-reloading."""
        if file_path is None:
            mod = sys.modules.get(module_name)
            if mod and hasattr(mod, "__file__") and mod.__file__:
                file_path = mod.__file__
            else:
                raise ValueError(f"Cannot find file for module: {module_name}")

        file_path = os.path.abspath(file_path)
        record = ModuleRecord(
            module_name=module_name,
            file_path=file_path,
            last_hash=_file_hash(file_path),
        )
        with self._lock:
            self._records[module_name] = record
        return record

    def unregister(self, module_name: str):
        with self._lock:
            self._records.pop(module_name, None)

    def is_registered(self, module_name: str) -> bool:
        return module_name in self._records

    # ── RELOAD ────────────────────────────────────────────────────────

    def reload(self, module_name: str) -> bool:
        """Force-reload a registered module. Returns True on success."""
        record = self._records.get(module_name)
        if record is None:
            raise KeyError(f"Module not registered: {module_name}")
        try:
            importlib.invalidate_caches()
            # Read and compile source directly to bypass .pyc cache
            with open(record.file_path, "r", encoding="utf-8") as f:
                source = f.read()
            mod = types.ModuleType(module_name)
            mod.__file__ = record.file_path
            mod.__name__ = module_name
            sys.modules[module_name] = mod
            exec(compile(source, record.file_path, "exec"), mod.__dict__)  # noqa: S102
            record.module_ref = sys.modules.get(module_name)
            record.reload_count += 1
            record.loaded_at = time.time()
            record.last_hash = _file_hash(record.file_path)
            record.last_error = None
            self._reload_count += 1
            for hook in self._on_reload_hooks:
                try:
                    hook(record)
                except Exception:
                    pass
            return True
        except Exception as exc:
            record.last_error = str(exc)
            self._error_count += 1
            for hook in self._on_error_hooks:
                try:
                    hook(record, exc)
                except Exception:
                    pass
            return False

    def reload_all(self) -> Dict[str, bool]:
        return {name: self.reload(name) for name in list(self._records.keys())}

    # ── CHANGE DETECTION ──────────────────────────────────────────────

    def check_changed(self, module_name: str) -> bool:
        record = self._records.get(module_name)
        if record is None:
            return False
        current_hash = _file_hash(record.file_path)
        return current_hash != record.last_hash

    def poll_once(self) -> List[str]:
        """Check all modules for changes, reload changed ones. Return list of reloaded."""
        reloaded = []
        for name, record in list(self._records.items()):
            current_hash = _file_hash(record.file_path)
            if current_hash and current_hash != record.last_hash:
                success = self.reload(name)
                if success:
                    reloaded.append(name)
        return reloaded

    # ── BACKGROUND WATCHER ────────────────────────────────────────────

    def start_watching(self):
        """Start background thread that polls for changes."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._stop_event.clear()
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="HotReloader")
        self._watcher_thread.start()

    def stop_watching(self):
        self._stop_event.set()
        if self._watcher_thread:
            self._watcher_thread.join(timeout=self._poll_interval + 1)
            self._watcher_thread = None

    def is_watching(self) -> bool:
        return bool(self._watcher_thread and self._watcher_thread.is_alive())

    def _watch_loop(self):
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self._poll_interval)

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_reload(self, fn: Callable[[ModuleRecord], None]):
        self._on_reload_hooks.append(fn)

    def on_error(self, fn: Callable[[ModuleRecord, Exception], None]):
        self._on_error_hooks.append(fn)

    def clear_hooks(self):
        self._on_reload_hooks.clear()
        self._on_error_hooks.clear()

    # ── MODULE ACCESS ─────────────────────────────────────────────────

    def get_module(self, module_name: str) -> Optional[types.ModuleType]:
        record = self._records.get(module_name)
        if record is None:
            return None
        return sys.modules.get(module_name)

    def get_attr(self, module_name: str, attr: str) -> Any:
        mod = self.get_module(module_name)
        if mod is None:
            raise KeyError(f"Module not loaded: {module_name}")
        return getattr(mod, attr)

    # ── STATS ─────────────────────────────────────────────────────────

    def list_modules(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self._records.values()]

    def stats(self) -> Dict[str, Any]:
        return {
            "registered": len(self._records),
            "total_reloads": self._reload_count,
            "total_errors": self._error_count,
            "watching": self.is_watching(),
            "poll_interval": self._poll_interval,
        }
