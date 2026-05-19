"""
OMNI AGENT - Secure Code Sandbox
Execute Python and shell code in an isolated subprocess with resource limits,
AST-based safety scanning, stdin/stdout/stderr capture, and execution history.

Safety layers:
  1. AST analysis — blocks dangerous imports, attribute access, and builtins
  2. Subprocess isolation — code never runs in the agent process
  3. Timeout enforcement — hard kill after max_seconds
  4. Output truncation — prevents log flooding
  5. Allowlist mode — only allow explicitly approved modules

Supports:
  - Python 3 execution
  - Bash/shell execution (optional, disabled by default)
  - Persistent session state between calls (via pickled namespace)
  - Execution history with timing, exit codes, and truncated output
"""
import ast
import sys
import os
import time
import uuid
import json
import asyncio
import hashlib
import logging
import subprocess
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from agent.security_audit import AuditCallback, code_fingerprint

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SAFETY SCANNER
# ══════════════════════════════════════════════════════════════════════════════

# Modules that are always blocked regardless of allow/deny lists
BLOCKED_IMPORTS: Set[str] = {
    "os", "sys", "subprocess", "socket", "ctypes", "multiprocessing",
    "threading", "importlib", "builtins", "__builtin__", "eval", "exec",
    "compile", "open", "io", "shutil", "pathlib", "glob", "tempfile",
    "signal", "fcntl", "pty", "tty", "termios", "resource", "gc",
    "pickle", "marshal", "shelve", "copyreg",
    "urllib", "http", "ftplib", "smtplib", "imaplib",
    "xmlrpc", "wsgiref",
}

# Attributes that are always blocked when accessed on any object
BLOCKED_ATTRS: Set[str] = {
    "__import__", "__class__", "__subclasses__", "__bases__",
    "__mro__", "__globals__", "__locals__", "__code__",
    "__builtins__", "__dict__", "__reduce__", "__reduce_ex__",
    "__getattribute__", "__setattr__", "__delattr__",
    "system", "popen", "popen2", "popen3", "popen4",
    "spawn", "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "execv", "execve", "execvp", "execvpe",
    "fork", "forkpty", "kill",
}

BLOCKED_BUILTINS: Set[str] = {
    "__import__", "eval", "exec", "compile", "open",
    "breakpoint", "vars", "dir", "globals", "locals",
    "input", "print",   # print is re-provided safely
}


class SecurityViolation(Exception):
    """Raised when AST scan finds unsafe code."""
    pass


class ASTScanner(ast.NodeVisitor):
    """Walk an AST and raise SecurityViolation on dangerous patterns."""

    def __init__(self, extra_blocked: Set[str] = None,
                 allowed_imports: Set[str] = None):
        self.violations: List[str] = []
        self.extra_blocked = extra_blocked or set()
        self.allowed_imports = allowed_imports  # None = block nothing extra

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            module = alias.name.split(".")[0]
            self._check_import(module, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            module = node.module.split(".")[0]
            self._check_import(module, node.lineno)
        self.generic_visit(node)

    def _check_import(self, module: str, lineno: int):
        if module in BLOCKED_IMPORTS or module in self.extra_blocked:
            self.violations.append(
                f"Line {lineno}: blocked import '{module}'"
            )
        if self.allowed_imports is not None and module not in self.allowed_imports:
            self.violations.append(
                f"Line {lineno}: import '{module}' not in allowlist"
            )

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr in BLOCKED_ATTRS:
            self.violations.append(
                f"Line {node.lineno}: blocked attribute access '.{node.attr}'"
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        # Block calls like eval("..."), exec("...")
        if isinstance(node.func, ast.Name):
            if node.func.id in BLOCKED_BUILTINS and node.func.id != "print":
                self.violations.append(
                    f"Line {node.lineno}: blocked builtin call '{node.func.id}'"
                )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        # Direct reference to blocked names (not just calls)
        if node.id in BLOCKED_BUILTINS and isinstance(node.ctx, ast.Load):
            if node.id not in ("print",):  # print is allowed as name (we'll wrap it)
                self.violations.append(
                    f"Line {node.lineno}: blocked name '{node.id}'"
                )
        self.generic_visit(node)


def scan_code(code: str,
              extra_blocked: Set[str] = None,
              allowed_imports: Set[str] = None) -> List[str]:
    """
    Parse and scan code for security violations.
    Returns list of violation strings (empty = safe).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError: {e}"]
    scanner = ASTScanner(extra_blocked=extra_blocked,
                         allowed_imports=allowed_imports)
    scanner.visit(tree)
    return scanner.violations


# ══════════════════════════════════════════════════════════════════════════════
# EXECUTION RESULT
# ══════════════════════════════════════════════════════════════════════════════

class ExecLanguage(str, Enum):
    PYTHON = "python"
    BASH   = "bash"


class SandboxCapability(str, Enum):
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    NETWORK = "network"
    SUBPROCESS = "subprocess"
    ENVIRONMENT = "environment"
    TIMEOUT = "timeout"
    MAX_OUTPUT_BYTES = "max_output_bytes"


_ESSENTIAL_RUNTIME_ENV_KEYS: Set[str] = {
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
}


def _normalize_policy_paths(paths: Tuple[str, ...]) -> Tuple[str, ...]:
    normalized: List[str] = []
    seen: Set[str] = set()
    for raw_path in paths:
        if not raw_path:
            continue
        resolved = str(Path(raw_path).expanduser().resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        normalized.append(resolved)
    return tuple(normalized)


def _path_in_allowlist(path: str, allowed_roots: Tuple[str, ...]) -> bool:
    if not allowed_roots:
        return False
    candidate = Path(path).expanduser().resolve(strict=False)
    for root in allowed_roots:
        root_path = Path(root).expanduser().resolve(strict=False)
        if candidate == root_path or root_path in candidate.parents:
            return True
    return False


@dataclass
class SandboxPolicy:
    allowed_read_paths: Tuple[str, ...] = field(default_factory=tuple)
    allowed_write_paths: Tuple[str, ...] = field(default_factory=tuple)
    allow_network: bool = False
    allowed_env_keys: Tuple[str, ...] = field(default_factory=tuple)
    timeout_seconds: float = 10.0
    max_output_bytes: int = 8000
    backend: str = "subprocess"

    def __post_init__(self):
        self.allowed_read_paths = _normalize_policy_paths(tuple(self.allowed_read_paths))
        self.allowed_write_paths = _normalize_policy_paths(tuple(self.allowed_write_paths))
        self.allowed_env_keys = tuple(sorted({key for key in self.allowed_env_keys if key}))
        self.timeout_seconds = float(self.timeout_seconds)
        self.max_output_bytes = int(self.max_output_bytes)
        if self.timeout_seconds <= 0:
            raise ValueError("SandboxPolicy.timeout_seconds must be > 0")
        if self.max_output_bytes <= 0:
            raise ValueError("SandboxPolicy.max_output_bytes must be > 0")

    @classmethod
    def default(cls, *, timeout_seconds: float = 10.0,
                max_output_bytes: int = 8000,
                backend: str = "subprocess") -> "SandboxPolicy":
        return cls(
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            backend=backend,
        )

    def allows(self, capability: SandboxCapability) -> bool:
        if capability == SandboxCapability.FILESYSTEM_READ:
            return bool(self.allowed_read_paths)
        if capability == SandboxCapability.FILESYSTEM_WRITE:
            return bool(self.allowed_write_paths)
        if capability == SandboxCapability.NETWORK:
            return self.allow_network
        if capability == SandboxCapability.SUBPROCESS:
            return True
        if capability == SandboxCapability.ENVIRONMENT:
            return bool(self.allowed_env_keys)
        if capability == SandboxCapability.TIMEOUT:
            return self.timeout_seconds > 0
        if capability == SandboxCapability.MAX_OUTPUT_BYTES:
            return self.max_output_bytes > 0
        return False

    def allows_env_key(self, key: str) -> bool:
        return key in self.allowed_env_keys

    def filter_env(self, source_env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        source = dict(source_env or os.environ)
        filtered: Dict[str, str] = {}
        for key in _ESSENTIAL_RUNTIME_ENV_KEYS:
            value = source.get(key)
            if value:
                filtered[key] = value
        for key in self.allowed_env_keys:
            value = source.get(key)
            if value is not None:
                filtered[key] = value
        filtered["PYTHONDONTWRITEBYTECODE"] = "1"
        return filtered

    def can_read_path(self, path: str) -> bool:
        return _path_in_allowlist(path, self.allowed_read_paths)

    def can_write_path(self, path: str) -> bool:
        return _path_in_allowlist(path, self.allowed_write_paths)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "allowed_read_paths": list(self.allowed_read_paths),
            "allowed_write_paths": list(self.allowed_write_paths),
            "allow_network": self.allow_network,
            "allowed_env_keys": list(self.allowed_env_keys),
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
        }


@dataclass
class ExecResult:
    exec_id: str
    language: ExecLanguage
    code: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
    timed_out: bool = False
    security_violations: List[str] = field(default_factory=list)
    error: str = ""
    started_at: float = field(default_factory=time.time)

    @property
    def success(self) -> bool:
        return (self.exit_code == 0 and not self.timed_out
                and not self.security_violations and not self.error)

    @property
    def output(self) -> str:
        """Combined stdout + stderr for display."""
        parts = []
        if self.security_violations:
            parts.append("🚫 Security violations:\n" +
                        "\n".join(f"  - {v}" for v in self.security_violations))
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        if self.timed_out:
            parts.append(f"⏱ Execution timed out")
        if self.error:
            parts.append(f"[error] {self.error}")
        return "\n".join(parts) if parts else "(no output)"

    def to_dict(self) -> Dict:
        return {
            "exec_id": self.exec_id,
            "language": self.language.value,
            "success": self.success,
            "exit_code": self.exit_code,
            "duration_ms": round(self.duration_ms, 1),
            "timed_out": self.timed_out,
            "stdout": self.stdout[:2000],
            "stderr": self.stderr[:1000],
            "security_violations": self.security_violations,
            "error": self.error,
        }


# ══════════════════════════════════════════════════════════════════════════════
# SANDBOX
# ══════════════════════════════════════════════════════════════════════════════

# Safe preamble injected before every Python execution
_PYTHON_PREAMBLE = """\
import sys as _sys
import io as _io
_sys.stdout = _io.StringIO()
_sys.stderr = _io.StringIO()

# Safe math/data imports are pre-imported
import math
import json
import re
import random
import datetime
import itertools
import functools
import collections
import string
import base64
import hashlib
import struct
import time as _time_mod
import copy
import decimal
import fractions
import statistics
import textwrap
import unicodedata

# Convenience aliases
from math import *
from collections import Counter, defaultdict, OrderedDict, deque, namedtuple
from itertools import *
from functools import *
from datetime import datetime as dt, date, timedelta

def print(*args, **kwargs):
    sep = kwargs.get('sep', ' ')
    end = kwargs.get('end', '\\n')
    _sys.stdout.write(sep.join(str(a) for a in args) + end)

"""

_PYTHON_POSTAMBLE = """\

# Capture output
_captured_stdout = _sys.stdout.getvalue()
_captured_stderr = _sys.stderr.getvalue()
"""


class Sandbox:
    """
    Secure code execution sandbox.

    Usage:
        sandbox = Sandbox(max_seconds=10, max_output_chars=8000)

        result = await sandbox.run_python("print(2 + 2)")
        print(result.stdout)   # "4"
        print(result.success)  # True

        result = await sandbox.run_python("import os")
        print(result.security_violations)  # ["Line 1: blocked import 'os'"]
    """

    def __init__(
        self,
        max_seconds: float = 10.0,
        max_output_chars: int = 8000,
        allow_shell: bool = False,
        extra_blocked_imports: Set[str] = None,
        allowed_imports: Set[str] = None,
        history_limit: int = 100,
        audit_callback: Optional[AuditCallback] = None,
        policy: Optional[SandboxPolicy] = None,
    ):
        self.policy = policy or SandboxPolicy.default(
            timeout_seconds=max_seconds,
            max_output_bytes=max_output_chars,
            backend="subprocess",
        )
        self.max_seconds = self.policy.timeout_seconds
        self.max_output_chars = self.policy.max_output_bytes
        self.allow_shell = allow_shell
        self.extra_blocked = extra_blocked_imports or set()
        self.allowed_imports = allowed_imports  # None = no extra restriction
        self._history: List[ExecResult] = []
        self._history_limit = history_limit
        self._audit_callback = audit_callback

    def _audit(self, action: str, actor: str, details: Dict[str, Any]) -> None:
        if not self._audit_callback:
            return
        try:
            self._audit_callback(action, actor or "sandbox", details)
        except Exception as exc:
            logger.warning("Sandbox security audit callback failed for %s: %s", action, exc)

    def _audit_policy_decision(self, exec_id: str,
                               capability: SandboxCapability,
                               allowed: bool,
                               reason: str,
                               **details: Any) -> None:
        payload = {
            "backend": self.policy.backend,
            "capability": capability.value,
            "decision": "allow" if allowed else "deny",
            "reason": reason,
            "timeout_seconds": self.policy.timeout_seconds,
            "max_output_bytes": self.policy.max_output_bytes,
        }
        payload.update(details)
        self._audit("security.sandbox_decision", exec_id, payload)
        if not allowed:
            self._audit("security.sandbox_denied", exec_id, payload)

    def allows_network(self, *, exec_id: str = "sandbox-policy") -> bool:
        allowed = self.policy.allow_network
        self._audit_policy_decision(
            exec_id,
            SandboxCapability.NETWORK,
            allowed,
            "network explicitly allowlisted" if allowed else "network denied by default",
        )
        return allowed

    def allows_env_key(self, key: str, *, exec_id: str = "sandbox-policy") -> bool:
        allowed = self.policy.allows_env_key(key)
        self._audit_policy_decision(
            exec_id,
            SandboxCapability.ENVIRONMENT,
            allowed,
            "environment key allowlisted" if allowed else "environment key not allowlisted",
            requested_env_key=key,
        )
        return allowed

    def can_read_path(self, path: str, *, exec_id: str = "sandbox-policy") -> bool:
        allowed = self.policy.can_read_path(path)
        self._audit_policy_decision(
            exec_id,
            SandboxCapability.FILESYSTEM_READ,
            allowed,
            "path is inside read allowlist" if allowed else "path outside read allowlist",
            requested_path=str(path),
        )
        return allowed

    def can_write_path(self, path: str, *, exec_id: str = "sandbox-policy") -> bool:
        allowed = self.policy.can_write_path(path)
        self._audit_policy_decision(
            exec_id,
            SandboxCapability.FILESYSTEM_WRITE,
            allowed,
            "path is inside write allowlist" if allowed else "path outside write allowlist",
            requested_path=str(path),
        )
        return allowed

    def build_subprocess_env(self,
                             source_env: Optional[Mapping[str, str]] = None,
                             *,
                             exec_id: str = "sandbox-policy") -> Dict[str, str]:
        env = self.policy.filter_env(source_env)
        self._audit_policy_decision(
            exec_id,
            SandboxCapability.ENVIRONMENT,
            True,
            "subprocess environment filtered through policy allowlist",
            allowed_env_keys=list(self.policy.allowed_env_keys),
            preserved_env_keys=sorted(env.keys()),
        )
        return env

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_python(self, code: str) -> ExecResult:
        """Execute Python code in a sandboxed subprocess."""
        exec_id = str(uuid.uuid4())[:8]
        start = time.time()
        self._audit("security.sandbox_trigger", exec_id, {
            "language": ExecLanguage.PYTHON.value,
            **code_fingerprint(code),
        })
        self._audit_policy_decision(
            exec_id,
            SandboxCapability.SUBPROCESS,
            True,
            "python execution dispatched to sandbox backend",
            language=ExecLanguage.PYTHON.value,
        )

        # Safety scan
        violations = scan_code(code,
                               extra_blocked=self.extra_blocked,
                               allowed_imports=self.allowed_imports)
        if violations:
            self._audit_policy_decision(
                exec_id,
                SandboxCapability.SUBPROCESS,
                False,
                "execution blocked by sandbox safety scan",
                language=ExecLanguage.PYTHON.value,
                violations=violations[:10],
            )
            result = ExecResult(
                exec_id=exec_id, language=ExecLanguage.PYTHON,
                code=code[:500], stdout="", stderr="",
                exit_code=1, duration_ms=0.0,
                security_violations=violations,
            )
            self._record(result)
            return result

        # Build script
        script = _PYTHON_PREAMBLE + code + _PYTHON_POSTAMBLE
        # Append print-capture wrapper
        script += """
import json as _json_mod
_sys.__stdout__.write(_json_mod.dumps({
    'stdout': _captured_stdout,
    'stderr': _captured_stderr,
}))
"""
        return await self._run_subprocess(
            cmd=[sys.executable, "-c", script],
            exec_id=exec_id,
            language=ExecLanguage.PYTHON,
            code=code,
            start=start,
        )

    async def run_bash(self, code: str) -> ExecResult:
        """Execute shell code (only if allow_shell=True)."""
        exec_id = str(uuid.uuid4())[:8]
        self._audit("security.sandbox_trigger", exec_id, {
            "language": ExecLanguage.BASH.value,
            **code_fingerprint(code),
        })
        if not self.allow_shell:
            self._audit_policy_decision(
                exec_id,
                SandboxCapability.SUBPROCESS,
                False,
                "shell execution denied unless explicitly enabled",
                language=ExecLanguage.BASH.value,
            )
            result = ExecResult(
                exec_id=exec_id, language=ExecLanguage.BASH,
                code=code[:500], stdout="", stderr="",
                exit_code=1, duration_ms=0.0,
                error="Shell execution is disabled in this sandbox.",
            )
            self._record(result)
            return result
        start = time.time()
        self._audit_policy_decision(
            exec_id,
            SandboxCapability.SUBPROCESS,
            True,
            "shell execution explicitly enabled for sandbox backend",
            language=ExecLanguage.BASH.value,
        )
        return await self._run_subprocess(
            cmd=["bash", "-c", code],
            exec_id=exec_id,
            language=ExecLanguage.BASH,
            code=code,
            start=start,
            raw_output=True,
        )

    async def run(self, code: str,
                  language: ExecLanguage = ExecLanguage.PYTHON) -> ExecResult:
        """Dispatch to run_python or run_bash."""
        if language == ExecLanguage.BASH:
            return await self.run_bash(code)
        return await self.run_python(code)

    # ── Subprocess runner ─────────────────────────────────────────────────────

    async def _run_subprocess(
        self, cmd: List[str], exec_id: str, language: ExecLanguage,
        code: str, start: float, raw_output: bool = False,
    ) -> ExecResult:
        timed_out = False
        stdout_raw = b""
        stderr_raw = b""
        exit_code = -1
        error = ""

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.build_subprocess_env(exec_id=exec_id),
            )
            try:
                stdout_raw, stderr_raw = await asyncio.wait_for(
                    proc.communicate(), timeout=self.max_seconds
                )
                exit_code = proc.returncode
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                    await proc.communicate()
                except Exception as exc:
                    logger.warning("Failed to terminate timed out sandbox process: %s", exc)
                exit_code = -1
        except Exception as e:
            error = str(e)

        duration_ms = (time.time() - start) * 1000

        # Parse output
        if raw_output:
            stdout = self._truncate(stdout_raw.decode("utf-8", errors="replace"))
            stderr = self._truncate(stderr_raw.decode("utf-8", errors="replace"))
        else:
            try:
                raw_text = stdout_raw.decode("utf-8", errors="replace").strip()
                if raw_text:
                    parsed = json.loads(raw_text)
                    stdout = self._truncate(parsed.get("stdout", ""))
                    stderr = self._truncate(parsed.get("stderr", ""))
                else:
                    stdout = ""
                    stderr = self._truncate(stderr_raw.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                stdout = self._truncate(stdout_raw.decode("utf-8", errors="replace"))
                stderr = self._truncate(stderr_raw.decode("utf-8", errors="replace"))

        result = ExecResult(
            exec_id=exec_id, language=language, code=code[:500],
            stdout=stdout, stderr=stderr, exit_code=exit_code,
            duration_ms=duration_ms, timed_out=timed_out, error=error,
        )
        self._record(result)
        return result

    def _truncate(self, text: str) -> str:
        if len(text) > self.max_output_chars:
            return text[:self.max_output_chars] + f"\n... [truncated at {self.max_output_chars} chars]"
        return text

    def _record(self, result: ExecResult):
        self._history.append(result)
        if len(self._history) > self._history_limit:
            self._history = self._history[-(self._history_limit // 2):]
        self._audit("security.sandbox_result", result.exec_id, {
            "language": result.language.value,
            "success": result.success,
            "timed_out": result.timed_out,
            "blocked": bool(result.security_violations),
            "exit_code": result.exit_code,
            "duration_ms": round(result.duration_ms, 1),
            "violations": result.security_violations[:10],
        })

    # ── History ───────────────────────────────────────────────────────────────

    def get_history(self, limit: int = 20,
                    language: ExecLanguage = None) -> List[Dict]:
        hist = self._history
        if language:
            hist = [r for r in hist if r.language == language]
        return [r.to_dict() for r in hist[-limit:]]

    def stats(self) -> Dict:
        if not self._history:
            return {"total": 0, "success_rate": 0.0, "avg_duration_ms": 0.0}
        total = len(self._history)
        successes = sum(1 for r in self._history if r.success)
        avg_dur = sum(r.duration_ms for r in self._history) / total
        blocked = sum(1 for r in self._history if r.security_violations)
        timeouts = sum(1 for r in self._history if r.timed_out)
        return {
            "total": total,
            "success": successes,
            "success_rate": round(successes / total, 3),
            "blocked_by_security": blocked,
            "timeouts": timeouts,
            "avg_duration_ms": round(avg_dur, 1),
        }

    def clear_history(self):
        self._history.clear()
