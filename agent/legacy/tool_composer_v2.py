"""OMNI Agent — Tool Composer V2: tool chain composition with parallel execution."""
from __future__ import annotations
import json, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class ToolStatus(str, Enum):
    ACTIVE     = "active"
    DEPRECATED = "deprecated"
    DISABLED   = "disabled"


class InvocationStatus(str, Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


@dataclass
class ToolSpec:
    tool_id: str
    name: str
    fn: Callable
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    status: ToolStatus = ToolStatus.ACTIVE
    timeout_s: float = 30.0
    max_retries: int = 0
    tags: List[str] = field(default_factory=list)
    version: str = "1.0.0"
    requires: List[str] = field(default_factory=list)  # tool_ids this depends on
    call_count: int = 0
    error_count: int = 0
    total_ms: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.call_count if self.call_count else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "version": self.version,
            "call_count": self.call_count,
            "avg_ms": round(self.avg_ms, 2),
        }


@dataclass
class ChainStep:
    step_id: str = field(default_factory=lambda: str(uuid.uuid4())[:6])
    tool_id: str = ""
    input_map: Dict[str, Any] = field(default_factory=dict)
    # input_map values: literal value OR "$prev" (result of prev step)
    #                   OR "$step:<step_id>" (result of named step)
    condition: Optional[Callable[[Any, Dict], bool]] = None  # skip if False
    parallel_group: Optional[str] = None  # steps in same group run concurrently
    on_error: str = "raise"   # raise | skip | default
    default_value: Any = None


@dataclass
class ToolChain:
    chain_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    steps: List[ChainStep] = field(default_factory=list)
    description: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    run_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "name": self.name,
            "steps": len(self.steps),
            "run_count": self.run_count,
        }


@dataclass
class InvocationRecord:
    inv_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    tool_id: str = ""
    step_id: str = ""
    chain_id: str = ""
    status: InvocationStatus = InvocationStatus.SUCCESS
    input_data: Any = None
    output_data: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "inv_id": self.inv_id,
            "tool_id": self.tool_id,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
        }


class ToolComposerV2:
    """
    Tool chain composition engine:
    - Tool registry with schema, versioning, status
    - Chain builder (ordered + parallel steps)
    - Input mapping ($prev / $step:<id> / literal)
    - Conditional step execution
    - Parallel step groups (same parallel_group key)
    - Per-tool timeout and retry
    - Step error handling (raise/skip/default)
    - Tool dependency graph resolution
    - Invocation history and per-tool stats
    - Named chain library
    - Pre/post invocation hooks
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._tools:   Dict[str, ToolSpec] = {}
        self._chains:  Dict[str, ToolChain] = {}
        self._history: List[InvocationRecord] = []
        self._pre_hooks:  List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tc_tools (
                tool_id TEXT PRIMARY KEY, name TEXT, description TEXT,
                status TEXT, version TEXT, call_count INTEGER,
                error_count INTEGER, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS tc_invocations (
                inv_id TEXT PRIMARY KEY, tool_id TEXT, chain_id TEXT,
                status TEXT, duration_ms REAL, error TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── TOOL MANAGEMENT ───────────────────────────────────────────────

    def register_tool(self, name: str,
                       fn: Callable,
                       description: str = "",
                       input_schema: Optional[Dict] = None,
                       output_schema: Optional[Dict] = None,
                       timeout_s: float = 30.0,
                       max_retries: int = 0,
                       tags: Optional[List[str]] = None,
                       version: str = "1.0.0",
                       requires: Optional[List[str]] = None,
                       tool_id: Optional[str] = None) -> ToolSpec:
        tid = tool_id or str(uuid.uuid4())[:8]
        t   = ToolSpec(
            tool_id=tid, name=name, fn=fn,
            description=description,
            input_schema=dict(input_schema or {}),
            output_schema=dict(output_schema or {}),
            timeout_s=timeout_s, max_retries=max_retries,
            tags=list(tags or []), version=version,
            requires=list(requires or []))
        self._tools[tid] = t
        self._persist_tool(t)
        return t

    def deprecate_tool(self, tool_id: str):
        t = self._tools.get(tool_id)
        if t: t.status = ToolStatus.DEPRECATED

    def disable_tool(self, tool_id: str):
        t = self._tools.get(tool_id)
        if t: t.status = ToolStatus.DISABLED

    def get_tool(self, tool_id: str) -> Optional[ToolSpec]:
        return self._tools.get(tool_id)

    def find_tool(self, name: str) -> Optional[ToolSpec]:
        return next((t for t in self._tools.values()
                     if t.name == name), None)

    def list_tools(self, tag: Optional[str] = None,
                    status: Optional[ToolStatus] = None) -> List[Dict]:
        tools = list(self._tools.values())
        if tag:    tools = [t for t in tools if tag in t.tags]
        if status: tools = [t for t in tools if t.status == status]
        return [t.to_dict() for t in tools]

    # ── DIRECT INVOCATION ─────────────────────────────────────────────

    def invoke(self, tool_id: str,
               input_data: Any,
               chain_id: str = "",
               step_id: str = "") -> InvocationRecord:
        t = self._tools.get(tool_id)
        if not t or t.status != ToolStatus.ACTIVE:
            rec = InvocationRecord(
                tool_id=tool_id, chain_id=chain_id, step_id=step_id,
                status=InvocationStatus.FAILED,
                error="Tool not found or inactive")
            self._history.append(rec)
            return rec

        for fn in self._pre_hooks:
            try: fn(t, input_data)
            except Exception: pass

        t0  = time.time()
        attempt = 0
        rec = InvocationRecord(tool_id=tool_id,
                                chain_id=chain_id, step_id=step_id,
                                input_data=input_data)
        while True:
            attempt += 1
            result_box: List[Any] = [None]
            exc_box: List[Optional[Exception]] = [None]

            def _run():
                try:
                    result_box[0] = t.fn(input_data)
                except Exception as e:
                    exc_box[0] = e

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            thread.join(timeout=t.timeout_s)

            if thread.is_alive():
                rec.status    = InvocationStatus.TIMEOUT
                rec.error     = f"Timeout after {t.timeout_s}s"
                t.error_count += 1
                break
            elif exc_box[0]:
                if attempt <= t.max_retries:
                    continue
                rec.status    = InvocationStatus.FAILED
                rec.error     = str(exc_box[0])
                t.error_count += 1
                break
            else:
                rec.status      = InvocationStatus.SUCCESS
                rec.output_data = result_box[0]
                break

        rec.duration_ms = (time.time() - t0) * 1000
        t.call_count   += 1
        t.total_ms     += rec.duration_ms
        self._history.append(rec)
        self._persist_invocation(rec)

        for fn in self._post_hooks:
            try: fn(t, rec)
            except Exception: pass

        return rec

    # ── CHAIN MANAGEMENT ─────────────────────────────────────────────

    def create_chain(self, name: str,
                      description: str = "",
                      tags: Optional[List[str]] = None,
                      chain_id: Optional[str] = None) -> ToolChain:
        cid = chain_id or str(uuid.uuid4())[:8]
        c   = ToolChain(chain_id=cid, name=name,
                         description=description,
                         tags=list(tags or []))
        self._chains[cid] = c
        return c

    def add_chain_step(self, chain_id: str,
                        tool_id: str,
                        input_map: Optional[Dict] = None,
                        condition: Optional[Callable] = None,
                        parallel_group: Optional[str] = None,
                        on_error: str = "raise",
                        default_value: Any = None,
                        step_id: Optional[str] = None) -> ChainStep:
        c = self._chains.get(chain_id)
        if not c: raise KeyError(f"Chain {chain_id} not found")
        step = ChainStep(
            step_id=step_id or str(uuid.uuid4())[:6],
            tool_id=tool_id,
            input_map=dict(input_map or {}),
            condition=condition,
            parallel_group=parallel_group,
            on_error=on_error,
            default_value=default_value)
        c.steps.append(step)
        return step

    def run_chain(self, chain_id: str,
                   initial_input: Any,
                   context: Optional[Dict] = None) -> Dict[str, Any]:
        c = self._chains.get(chain_id)
        if not c: raise KeyError(f"Chain {chain_id} not found")
        ctx      = dict(context or {})
        results: Dict[str, Any] = {}
        prev     = initial_input
        c.run_count += 1

        # Group steps by parallel_group
        i = 0
        while i < len(c.steps):
            step = c.steps[i]
            pg   = step.parallel_group

            if pg:
                # Collect all steps in same group
                group = [s for s in c.steps[i:] if s.parallel_group == pg]
                group_results = self._run_parallel_group(
                    group, prev, results, ctx, chain_id)
                results.update(group_results)
                if group_results:
                    prev = list(group_results.values())[-1]
                i += len(group)
                continue

            # Condition check
            if step.condition and not step.condition(prev, ctx):
                results[step.step_id] = None
                i += 1
                continue

            inp = self._resolve_input(step, prev, results)
            rec = self.invoke(step.tool_id, inp, chain_id, step.step_id)

            if rec.status != InvocationStatus.SUCCESS:
                if step.on_error == "raise":
                    raise RuntimeError(
                        f"Step {step.step_id} failed: {rec.error}")
                elif step.on_error == "default":
                    results[step.step_id] = step.default_value
                    prev = step.default_value
                else:  # skip
                    results[step.step_id] = prev
            else:
                results[step.step_id] = rec.output_data
                prev = rec.output_data
            i += 1

        return {"final": prev, "steps": results}

    def _run_parallel_group(self, steps: List[ChainStep],
                             prev: Any, results: Dict,
                             ctx: Dict, chain_id: str) -> Dict[str, Any]:
        group_results: Dict[str, Any] = {}
        lock = threading.Lock()

        def run_step(step):
            inp = self._resolve_input(step, prev, results)
            rec = self.invoke(step.tool_id, inp, chain_id, step.step_id)
            with lock:
                group_results[step.step_id] = (
                    rec.output_data if rec.status == InvocationStatus.SUCCESS
                    else step.default_value)

        threads = [threading.Thread(target=run_step, args=(s,), daemon=True)
                   for s in steps]
        for t in threads: t.start()
        for t in threads: t.join()
        return group_results

    def _resolve_input(self, step: ChainStep,
                        prev: Any, results: Dict) -> Any:
        if not step.input_map:
            return prev
        resolved = {}
        for k, v in step.input_map.items():
            if v == "$prev":
                resolved[k] = prev
            elif isinstance(v, str) and v.startswith("$step:"):
                sid = v[6:]
                resolved[k] = results.get(sid)
            else:
                resolved[k] = v
        return resolved

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_before_invoke(self, fn: Callable): self._pre_hooks.append(fn)
    def on_after_invoke(self, fn: Callable):  self._post_hooks.append(fn)

    # ── STATS ─────────────────────────────────────────────────────────

    def invocation_history(self, tool_id: Optional[str] = None,
                            limit: int = 50) -> List[Dict]:
        hist = self._history
        if tool_id:
            hist = [r for r in hist if r.tool_id == tool_id]
        return [r.to_dict() for r in hist[-limit:]]

    def _persist_tool(self, t: ToolSpec):
        self._db.execute(
            "INSERT OR REPLACE INTO tc_tools VALUES (?,?,?,?,?,?,?,?)",
            (t.tool_id, t.name, t.description, t.status.value,
             t.version, t.call_count, t.error_count, t.created_at))
        self._db.commit()

    def _persist_invocation(self, r: InvocationRecord):
        self._db.execute(
            "INSERT OR REPLACE INTO tc_invocations VALUES (?,?,?,?,?,?,?)",
            (r.inv_id, r.tool_id, r.chain_id, r.status.value,
             r.duration_ms, r.error, r.ts))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "tools": len(self._tools),
            "chains": len(self._chains),
            "invocations": len(self._history),
            "success": sum(1 for r in self._history
                           if r.status == InvocationStatus.SUCCESS),
            "failed": sum(1 for r in self._history
                          if r.status == InvocationStatus.FAILED),
        }
