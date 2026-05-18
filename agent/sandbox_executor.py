"""OMNI Agent — Sandbox Executor: safe code execution with resource limits."""
from __future__ import annotations
import ast, io, json, math, queue, sqlite3, sys, threading, time, traceback, uuid
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class SandboxStatus(str, Enum):
    SUCCESS  = "success"
    TIMEOUT  = "timeout"
    ERROR    = "error"
    REJECTED = "rejected"   # blocked by policy


class Language(str, Enum):
    PYTHON = "python"
    EXPR   = "expr"       # single expression evaluation


@dataclass
class SandboxPolicy:
    policy_id: str
    name: str
    allowed_builtins: Set[str] = field(default_factory=set)
    blocked_builtins: Set[str] = field(default_factory=set)
    allowed_modules:  Set[str] = field(default_factory=set)
    blocked_modules:  Set[str] = field(default_factory=set)
    blocked_ast_nodes: Set[str] = field(default_factory=set)
    max_output_bytes: int = 65536    # 64 KB stdout cap
    allow_print: bool = True
    allow_exceptions: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "name": self.name,
            "allowed_modules": list(self.allowed_modules),
            "blocked_modules": list(self.blocked_modules),
            "blocked_ast_nodes": list(self.blocked_ast_nodes),
        }


@dataclass
class ExecutionResult:
    exec_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    code: str = ""
    status: SandboxStatus = SandboxStatus.SUCCESS
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    duration_ms: float = 0.0
    policy_violations: List[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exec_id": self.exec_id,
            "status": self.status.value,
            "result": str(self.result)[:200] if self.result is not None else None,
            "stdout": self.stdout[:500],
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
        }


# Safe builtins set
_SAFE_BUILTINS = {
    "abs", "all", "any", "ascii", "bin", "bool", "breakpoint",
    "bytearray", "bytes", "callable", "chr", "complex", "dict",
    "dir", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "getattr", "hasattr", "hash", "help", "hex",
    "id", "input", "int", "isinstance", "issubclass", "iter",
    "len", "list", "map", "max", "min", "next", "object",
    "oct", "ord", "pow", "print", "property", "range", "repr",
    "reversed", "round", "set", "setattr", "slice", "sorted",
    "staticmethod", "str", "sum", "super", "tuple", "type",
    "vars", "zip",
    # Math
    "True", "False", "None",
}

_DANGEROUS_BUILTINS = {
    "__import__", "eval", "exec", "compile", "open",
    "globals", "locals", "vars", "delattr",
    "memoryview", "classmethod",
}

_DANGEROUS_NODES = {
    "Import", "ImportFrom", "Global", "Nonlocal",
    "AsyncFunctionDef", "AsyncFor", "AsyncWith",
}


def _build_safe_globals(allowed_modules: Set[str],
                         allowed_builtins: Set[str],
                         blocked_builtins: Set[str],
                         allow_print: bool) -> Dict[str, Any]:
    import builtins as _bi
    safe: Dict[str, Any] = {}

    # Builtins
    for name in _SAFE_BUILTINS:
        if name in blocked_builtins:
            continue
        obj = getattr(_bi, name, None)
        if obj is not None:
            safe[name] = obj
    for name in allowed_builtins:
        obj = getattr(_bi, name, None)
        if obj is not None:
            safe[name] = obj
    if not allow_print:
        safe.pop("print", None)

    # Standard safe modules
    _safe_mod_map = {
        "math": math, "json": json, "re": __import__("re"),
        "random": __import__("random"), "time": time,
        "itertools": __import__("itertools"),
        "functools": __import__("functools"),
        "collections": __import__("collections"),
        "string": __import__("string"),
    }
    for mod_name in allowed_modules:
        if mod_name in _safe_mod_map:
            safe[mod_name] = _safe_mod_map[mod_name]

    # Always provide a restricted __import__ so `import math` etc. work
    # (only whitelisted modules are importable)
    _allowed = set(allowed_modules)
    _mod_map  = dict(_safe_mod_map)
    def _restricted_import(name, *args, **kwargs):
        top = name.split(".")[0]
        if top in _allowed and top in _mod_map:
            return _mod_map[top]
        raise ImportError(f"import of '{name}' is not allowed")
    safe["__import__"] = _restricted_import

    safe["__builtins__"] = {"__import__": _restricted_import}
    return safe


class ASTChecker(ast.NodeVisitor):
    def __init__(self, blocked_nodes: Set[str],
                 blocked_modules: Set[str]):
        self.violations: List[str] = []
        self._blocked_nodes   = blocked_nodes
        self._blocked_modules = blocked_modules

    def visit(self, node):
        node_type = type(node).__name__
        if node_type in self._blocked_nodes:
            self.violations.append(f"Blocked AST node: {node_type}")
        # Check imports
        if node_type in ("Import", "ImportFrom"):
            names = []
            if hasattr(node, "names"):
                names = [a.name for a in node.names]
            if hasattr(node, "module") and node.module:
                names.append(node.module)
            for name in names:
                top = name.split(".")[0]
                if top in self._blocked_modules:
                    self.violations.append(f"Blocked import: {name}")
        # Check for dunder attribute access (e.g., __class__)
        if node_type == "Attribute" and node.attr.startswith("__"):
            self.violations.append(f"Blocked dunder access: {node.attr}")
        self.generic_visit(node)


class SandboxExecutor:
    """
    Safe code execution sandbox:
    - Python expression and statement execution
    - AST-level analysis before execution (block dangerous nodes/imports)
    - Configurable allowed/blocked builtins and modules
    - Per-execution timeout (thread-based)
    - Stdout/stderr capture
    - Execution history
    - Named policies (strict/moderate/open)
    - Custom global/local variable injection
    - Result serialization
    - SQLite execution log
    """

    def __init__(self, db_path: str = ":memory:"):
        self._policies:  Dict[str, SandboxPolicy] = {}
        self._history:   List[ExecutionResult] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        # Register default policies
        self._register_defaults()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sb_executions (
                exec_id TEXT PRIMARY KEY, policy_id TEXT,
                status TEXT, duration_ms REAL, error TEXT, ts REAL
            );
        """)
        self._db.commit()

    def _register_defaults(self):
        self.add_policy("strict",
                        allowed_modules={"math", "json", "re"},
                        blocked_ast_nodes=_DANGEROUS_NODES | {"Delete", "Global"},
                        blocked_builtins=_DANGEROUS_BUILTINS)
        self.add_policy("moderate",
                        allowed_modules={"math", "json", "re",
                                         "random", "collections",
                                         "itertools", "functools", "string"},
                        blocked_ast_nodes={"Import", "ImportFrom", "Global"},
                        blocked_builtins=_DANGEROUS_BUILTINS)
        self.add_policy("open",
                        allowed_modules={"math", "json", "re", "random",
                                         "time", "collections", "itertools",
                                         "functools", "string"},
                        blocked_ast_nodes={"Global"},
                        blocked_builtins={"open"})

    # ── POLICY ───────────────────────────────────────────────────────

    def add_policy(self, name: str,
                   allowed_builtins: Optional[Set[str]] = None,
                   blocked_builtins: Optional[Set[str]] = None,
                   allowed_modules:  Optional[Set[str]] = None,
                   blocked_modules:  Optional[Set[str]] = None,
                   blocked_ast_nodes: Optional[Set[str]] = None,
                   max_output_bytes: int = 65536,
                   allow_print: bool = True,
                   policy_id: Optional[str] = None) -> SandboxPolicy:
        pid = policy_id or name
        p   = SandboxPolicy(
            policy_id=pid, name=name,
            allowed_builtins=set(allowed_builtins or []),
            blocked_builtins=set(blocked_builtins or []),
            allowed_modules=set(allowed_modules or []),
            blocked_modules=set(blocked_modules or []),
            blocked_ast_nodes=set(blocked_ast_nodes or []),
            max_output_bytes=max_output_bytes,
            allow_print=allow_print)
        self._policies[pid] = p
        return p

    def get_policy(self, policy_id: str) -> Optional[SandboxPolicy]:
        return self._policies.get(policy_id)

    # ── EXECUTE ──────────────────────────────────────────────────────

    def execute(self, code: str,
                policy_id: str = "strict",
                timeout_s: float = 5.0,
                lang: Language = Language.PYTHON,
                inject_globals: Optional[Dict] = None,
                inject_locals: Optional[Dict] = None,
                exec_id: Optional[str] = None) -> ExecutionResult:

        policy = self._policies.get(policy_id)
        if not policy:
            raise KeyError(f"Policy '{policy_id}' not found")

        res = ExecutionResult(
            exec_id=exec_id or str(uuid.uuid4())[:8],
            code=code)
        t0  = time.time()

        # AST check
        violations = self._check_ast(code, policy)
        if violations:
            res.status = SandboxStatus.REJECTED
            res.policy_violations = violations
            res.error = "Policy violation: " + "; ".join(violations)
            res.duration_ms = (time.time() - t0) * 1000
            self._record(res, policy_id)
            return res

        # Build sandbox environment
        globs = _build_safe_globals(
            policy.allowed_modules,
            policy.allowed_builtins,
            policy.blocked_builtins,
            policy.allow_print)
        if inject_globals:
            globs.update(inject_globals)
        locs: Dict[str, Any] = dict(inject_locals or {})

        # Execute with timeout
        exc_box: List[Optional[Exception]] = [None]
        result_box: List[Any] = [None]
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        def _run():
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    if lang == Language.EXPR:
                        result_box[0] = eval(code, globs, locs)  # noqa: S307
                    else:
                        exec(compile(code, "<sandbox>", "exec"), globs, locs)  # noqa: S102
                        result_box[0] = locs.get("result", None)
            except Exception as e:
                exc_box[0] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout_s)

        res.duration_ms = (time.time() - t0) * 1000

        if t.is_alive():
            res.status = SandboxStatus.TIMEOUT
            res.error  = f"Execution timed out after {timeout_s}s"
        elif exc_box[0] is not None:
            res.status = SandboxStatus.ERROR
            res.error  = str(exc_box[0])
        else:
            res.status = SandboxStatus.SUCCESS
            res.result = result_box[0]

        out = stdout_buf.getvalue()
        err = stderr_buf.getvalue()
        res.stdout = out[:policy.max_output_bytes]
        res.stderr = err[:policy.max_output_bytes]

        self._record(res, policy_id)
        return res

    def evaluate(self, expr: str,
                  policy_id: str = "moderate",
                  timeout_s: float = 2.0,
                  **kwargs) -> ExecutionResult:
        return self.execute(expr, policy_id=policy_id,
                            timeout_s=timeout_s,
                            lang=Language.EXPR, **kwargs)

    def _check_ast(self, code: str, policy: SandboxPolicy) -> List[str]:
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return [f"SyntaxError: {e}"]
        checker = ASTChecker(policy.blocked_ast_nodes, policy.blocked_modules)
        checker.visit(tree)
        return checker.violations

    def _record(self, res: ExecutionResult, policy_id: str):
        self._history.append(res)
        self._db.execute(
            "INSERT OR REPLACE INTO sb_executions VALUES (?,?,?,?,?,?)",
            (res.exec_id, policy_id, res.status.value,
             res.duration_ms, res.error, res.ts))
        self._db.commit()

    # ── QUERY ────────────────────────────────────────────────────────

    def get_result(self, exec_id: str) -> Optional[ExecutionResult]:
        return next((r for r in self._history
                     if r.exec_id == exec_id), None)

    def history(self, status: Optional[SandboxStatus] = None,
                limit: int = 50) -> List[Dict]:
        rows = self._db.execute(
            "SELECT exec_id,policy_id,status,duration_ms,error,ts "
            "FROM sb_executions ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()
        result = [{"id": r[0], "policy": r[1], "status": r[2],
                   "ms": r[3], "error": r[4]} for r in rows]
        if status:
            result = [r for r in result if r["status"] == status.value]
        return result

    def stats(self) -> Dict[str, Any]:
        total   = len(self._history)
        success = sum(1 for r in self._history
                      if r.status == SandboxStatus.SUCCESS)
        return {
            "policies": len(self._policies),
            "total_executions": total,
            "success": success,
            "errors": sum(1 for r in self._history
                          if r.status == SandboxStatus.ERROR),
            "timeouts": sum(1 for r in self._history
                            if r.status == SandboxStatus.TIMEOUT),
            "rejected": sum(1 for r in self._history
                            if r.status == SandboxStatus.REJECTED),
        }
