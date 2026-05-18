"""OMNI Agent — Workflow DSL: declarative dict/YAML workflow runner with branching and loops."""
from __future__ import annotations
import asyncio, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class StepType(str, Enum):
    ACTION    = "action"      # calls a registered handler
    CONDITION = "condition"   # if/elif/else branching
    LOOP      = "loop"        # for_each or while
    PARALLEL  = "parallel"    # run steps concurrently
    WAIT      = "wait"        # sleep
    EMIT      = "emit"        # set a variable in context
    ASSERT    = "assert"      # halt if condition fails
    SUB       = "sub"         # invoke a sub-workflow


class StepStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    FAILED   = "failed"
    SKIPPED  = "skipped"


@dataclass
class StepResult:
    step_id: str
    status: StepStatus
    output: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    iterations: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status.value,
            "output": str(self.output)[:100] if self.output is not None else None,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class WorkflowRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    workflow_name: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    status: StepStatus = StepStatus.PENDING
    steps: List[StepResult] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def duration_ms(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow_name,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 2),
            "steps": [s.to_dict() for s in self.steps],
            "error": self.error,
        }


class WorkflowError(Exception):
    pass


class AssertionFailed(WorkflowError):
    pass


def _resolve(value: Any, ctx: Dict[str, Any]) -> Any:
    """Resolve $var references in strings from context."""
    if isinstance(value, str) and value.startswith("$"):
        key = value[1:]
        return ctx.get(key, value)
    if isinstance(value, dict):
        return {k: _resolve(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, ctx) for v in value]
    return value


def _eval_condition(cond: Any, ctx: Dict[str, Any]) -> bool:
    """Evaluate a simple condition dict or callable."""
    if callable(cond):
        return bool(cond(ctx))
    if isinstance(cond, bool):
        return cond
    if isinstance(cond, dict):
        op    = cond.get("op", "eq")
        left  = _resolve(cond.get("left"), ctx)
        right = _resolve(cond.get("right"), ctx)
        if op == "eq":   return left == right
        if op == "neq":  return left != right
        if op == "gt":   return left > right
        if op == "lt":   return left < right
        if op == "gte":  return left >= right
        if op == "lte":  return left <= right
        if op == "in":   return left in right
        if op == "nin":  return left not in right
        if op == "and":  return all(_eval_condition(c, ctx) for c in cond.get("conds", []))
        if op == "or":   return any(_eval_condition(c, ctx) for c in cond.get("conds", []))
        if op == "not":  return not _eval_condition(cond.get("cond", False), ctx)
    return bool(cond)


class WorkflowDSL:
    """
    Declarative workflow engine. Workflows are plain Python dicts (or YAML-loaded dicts).

    Step schema:
        id:       str (optional)
        type:     action|condition|loop|parallel|wait|emit|assert|sub
        handler:  str  (for action)  → registered handler name
        args:     dict (for action)  → resolved from context with $var
        condition: condition_def     (for condition)
        if_true:  [steps]            (for condition)
        if_false: [steps]            (for condition)
        for_each: "$var"             (for loop - iterates list in context)
        as:       "item_var"         (for loop - name of loop variable)
        steps:    [steps]            (for loop body / parallel / sub)
        while:    condition_def      (for while loop)
        max_iter: int                (for while loop - safety cap)
        seconds:  float              (for wait)
        key:      str                (for emit - context key to set)
        value:    any                (for emit - value or $ref)
        message:  str                (for assert)
        on_error: continue|stop      (default stop)
        output_as: str               (store step output in context key)
    """

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}
        self._sub_workflows: Dict[str, List[Dict]] = {}
        self._runs: List[WorkflowRun] = []
        self._run_count = 0

    # ── REGISTRATION ──────────────────────────────────────────────────

    def register(self, name: str, fn: Callable):
        self._handlers[name] = fn

    def register_workflow(self, name: str, steps: List[Dict]):
        self._sub_workflows[name] = steps

    # ── EXECUTION ─────────────────────────────────────────────────────

    def run(self, steps: List[Dict],
            context: Optional[Dict[str, Any]] = None,
            workflow_name: str = "anonymous") -> WorkflowRun:
        ctx = dict(context or {})
        run = WorkflowRun(workflow_name=workflow_name, context=ctx)
        self._run_count += 1
        run.status = StepStatus.RUNNING
        try:
            self._exec_steps(steps, ctx, run)
            run.status = StepStatus.DONE
        except AssertionFailed as e:
            run.status = StepStatus.FAILED
            run.error  = f"Assertion failed: {e}"
        except WorkflowError as e:
            run.status = StepStatus.FAILED
            run.error  = str(e)
        finally:
            run.finished_at = time.time()
            run.context = dict(ctx)
        self._runs.append(run)
        return run

    async def run_async(self, steps: List[Dict],
                        context: Optional[Dict[str, Any]] = None,
                        workflow_name: str = "anonymous") -> WorkflowRun:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self.run(steps, context, workflow_name))

    def _exec_steps(self, steps: List[Dict], ctx: Dict, run: WorkflowRun):
        for step_def in steps:
            sr = self._exec_step(step_def, ctx, run)
            run.steps.append(sr)
            if sr.status == StepStatus.FAILED:
                on_error = step_def.get("on_error", "stop")
                if on_error != "continue":
                    raise WorkflowError(sr.error or "Step failed")

    def _exec_step(self, step: Dict, ctx: Dict, run: WorkflowRun) -> StepResult:
        sid    = step.get("id", str(uuid.uuid4())[:6])
        stype  = StepType(step.get("type", "action"))
        t0     = time.time()

        try:
            if stype == StepType.ACTION:
                output = self._do_action(step, ctx)
            elif stype == StepType.CONDITION:
                output = self._do_condition(step, ctx, run)
            elif stype == StepType.LOOP:
                output = self._do_loop(step, ctx, run)
            elif stype == StepType.PARALLEL:
                output = self._do_parallel(step, ctx, run)
            elif stype == StepType.WAIT:
                seconds = float(_resolve(step.get("seconds", 0), ctx))
                time.sleep(seconds)
                output = {"waited_s": seconds}
            elif stype == StepType.EMIT:
                key = step["key"]
                val = _resolve(step.get("value"), ctx)
                ctx[key] = val
                output = {key: val}
            elif stype == StepType.ASSERT:
                cond = step.get("condition", True)
                if not _eval_condition(cond, ctx):
                    raise AssertionFailed(step.get("message", f"Assert failed in step {sid}"))
                output = True
            elif stype == StepType.SUB:
                name = step["name"]
                sub_steps = self._sub_workflows.get(name)
                if sub_steps is None:
                    raise WorkflowError(f"Sub-workflow '{name}' not registered")
                sub_run = self.run(sub_steps, context=dict(ctx),
                                   workflow_name=name)
                ctx.update(sub_run.context)
                output = sub_run.to_dict()
            else:
                output = None

            if "output_as" in step:
                ctx[step["output_as"]] = output

            return StepResult(step_id=sid, status=StepStatus.DONE,
                              output=output,
                              duration_ms=(time.time() - t0) * 1000)
        except (AssertionFailed, WorkflowError):
            raise
        except Exception as exc:
            return StepResult(step_id=sid, status=StepStatus.FAILED,
                              error=str(exc),
                              duration_ms=(time.time() - t0) * 1000)

    def _do_action(self, step: Dict, ctx: Dict) -> Any:
        handler_name = step["handler"]
        fn = self._handlers.get(handler_name)
        if fn is None:
            raise WorkflowError(f"Handler '{handler_name}' not registered")
        args   = _resolve(step.get("args", {}), ctx)
        kwargs = _resolve(step.get("kwargs", {}), ctx)
        if isinstance(args, dict):
            return fn(**args, **kwargs)
        if isinstance(args, list):
            return fn(*args, **kwargs)
        return fn(args, **kwargs)

    def _do_condition(self, step: Dict, ctx: Dict, run: WorkflowRun) -> Any:
        cond = step.get("condition", False)
        if _eval_condition(cond, ctx):
            branch = step.get("if_true", [])
        else:
            branch = step.get("if_false", [])
        if branch:
            self._exec_steps(branch, ctx, run)
        return {"branch": "if_true" if _eval_condition(cond, ctx) else "if_false"}

    def _do_loop(self, step: Dict, ctx: Dict, run: WorkflowRun) -> Any:
        body = step.get("steps", [])
        iterations = 0

        if "for_each" in step:
            items_ref = step["for_each"]
            items = _resolve(items_ref, ctx)
            if not isinstance(items, list):
                items = list(items) if hasattr(items, "__iter__") else []
            item_var = step.get("as", "item")
            for item in items:
                ctx[item_var] = item
                self._exec_steps(body, ctx, run)
                iterations += 1

        elif "while" in step:
            max_iter = int(step.get("max_iter", 100))
            while _eval_condition(step["while"], ctx) and iterations < max_iter:
                self._exec_steps(body, ctx, run)
                iterations += 1

        return {"iterations": iterations}

    def _do_parallel(self, step: Dict, ctx: Dict, run: WorkflowRun) -> Any:
        """Run steps sequentially but mark as parallel intent (true async needs async runner)."""
        sub_steps = step.get("steps", [])
        results = []
        for sub in sub_steps:
            sr = self._exec_step(sub, dict(ctx), run)
            results.append(sr.to_dict())
        return {"parallel_results": results}

    # ── INTROSPECTION ─────────────────────────────────────────────────

    def get_run(self, run_id: str) -> Optional[WorkflowRun]:
        for r in self._runs:
            if r.run_id == run_id:
                return r
        return None

    def recent_runs(self, n: int = 10) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self._runs[-n:]]

    def stats(self) -> Dict[str, Any]:
        done   = sum(1 for r in self._runs if r.status == StepStatus.DONE)
        failed = sum(1 for r in self._runs if r.status == StepStatus.FAILED)
        return {
            "run_count": self._run_count,
            "done": done,
            "failed": failed,
            "handlers": len(self._handlers),
            "sub_workflows": len(self._sub_workflows),
        }
