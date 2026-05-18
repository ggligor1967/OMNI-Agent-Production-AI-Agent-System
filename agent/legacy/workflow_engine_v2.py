"""OMNI Agent — Workflow Engine V2: stateful workflows with conditions, loops and retries."""
from __future__ import annotations
import json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class StepType(str, Enum):
    ACTION    = "action"      # execute fn
    CONDITION = "condition"   # branch on predicate
    LOOP      = "loop"        # iterate over list
    PARALLEL  = "parallel"    # run steps concurrently
    WAIT      = "wait"        # sleep N seconds
    SUBFLOW   = "subflow"     # run another workflow
    SET_VAR   = "set_var"     # set context variable
    EMIT      = "emit"        # emit event
    HUMAN     = "human"       # await human input
    END       = "end"


class WorkflowStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    PAUSED    = "paused"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"
    WAITING   = "waiting"    # waiting for human input


class StepStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    DONE      = "done"
    SKIPPED   = "skipped"
    FAILED    = "failed"
    RETRYING  = "retrying"


@dataclass
class StepDef:
    step_id: str
    name: str
    step_type: StepType
    fn: Optional[Callable] = None
    condition: Optional[Callable] = None  # predicate → bool
    on_true:  Optional[str] = None        # next step_id if true
    on_false: Optional[str] = None        # next step_id if false
    next_step: Optional[str] = None       # default successor
    var_name:  Optional[str] = None       # for SET_VAR
    var_value: Any = None                 # for SET_VAR (or callable)
    loop_over: Optional[str] = None       # context key with list
    loop_body: Optional[str] = None       # step_id to call per item
    subflow_id: Optional[str] = None
    wait_s: float = 0.0
    max_retries: int = 0
    retry_delay_s: float = 1.0
    timeout_s: Optional[float] = None
    on_error: str = "fail"               # "fail" | "continue" | "skip"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "type": self.step_type.value,
            "next": self.next_step,
            "on_error": self.on_error,
            "max_retries": self.max_retries,
        }


@dataclass
class StepRun:
    step_id: str
    status: StepStatus = StepStatus.PENDING
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Any = None
    error: Optional[str] = None
    attempt: int = 0

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
            "attempt": self.attempt,
        }


@dataclass
class WorkflowRun:
    run_id: str
    workflow_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    context: Dict[str, Any] = field(default_factory=dict)
    step_runs: List[StepRun] = field(default_factory=list)
    current_step: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    events: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "steps_run": len(self.step_runs),
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
        }


@dataclass
class WorkflowDef:
    workflow_id: str
    name: str
    steps: Dict[str, StepDef] = field(default_factory=dict)
    start_step: Optional[str] = None
    description: str = ""
    version: str = "1.0.0"
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "steps": len(self.steps),
            "start": self.start_step,
            "version": self.version,
        }


class WorkflowEngineV2:
    """
    Stateful workflow engine with:
    - ACTION / CONDITION / LOOP / WAIT / SET_VAR / SUBFLOW / EMIT / HUMAN steps
    - Per-step retry with delay
    - on_error: fail | continue | skip
    - Context variable passing between steps
    - Event emission & hooks
    - Run history + step-level audit
    - SQLite persistence
    - Pause / resume / cancel
    """

    def __init__(self, db_path: str = ":memory:"):
        self._workflows: Dict[str, WorkflowDef] = {}
        self._runs: Dict[str, WorkflowRun] = {}
        self._event_hooks: Dict[str, List[Callable]] = {}
        self._pending_human: Dict[str, Any] = {}      # run_id → awaited value
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS we_runs (
                run_id TEXT PRIMARY KEY, workflow_id TEXT, status TEXT,
                steps_run INTEGER, duration_ms REAL, error TEXT, ts REAL
            );
            CREATE TABLE IF NOT EXISTS we_step_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT, step_id TEXT, status TEXT,
                duration_ms REAL, error TEXT, attempt INTEGER
            );
        """)
        self._db.commit()

    # ── WORKFLOW DEFINITION ───────────────────────────────────────────

    def define(self, name: str, description: str = "",
               version: str = "1.0.0",
               tags: Optional[List[str]] = None,
               workflow_id: Optional[str] = None) -> WorkflowDef:
        wid = workflow_id or str(uuid.uuid4())[:8]
        wf  = WorkflowDef(workflow_id=wid, name=name,
                          description=description, version=version,
                          tags=list(tags or []))
        self._workflows[wid] = wf
        return wf

    def add_step(self, workflow_id: str, **kwargs) -> StepDef:
        wf  = self._get_wf(workflow_id)
        sid = kwargs.pop("step_id", str(uuid.uuid4())[:8])
        name = kwargs.pop("name", sid)
        stype = StepType(kwargs.pop("step_type", StepType.ACTION))
        step = StepDef(step_id=sid, name=name, step_type=stype, **kwargs)
        wf.steps[sid] = step
        if wf.start_step is None:
            wf.start_step = sid
        return step

    def set_start(self, workflow_id: str, step_id: str):
        self._get_wf(workflow_id).start_step = step_id

    def _get_wf(self, workflow_id: str) -> WorkflowDef:
        wf = self._workflows.get(workflow_id)
        if not wf:
            raise KeyError(f"Workflow '{workflow_id}' not found")
        return wf

    # ── EXECUTION ─────────────────────────────────────────────────────

    def run(self, workflow_id: str,
            context: Optional[Dict] = None,
            run_id: Optional[str] = None) -> WorkflowRun:
        wf  = self._get_wf(workflow_id)
        rid = run_id or str(uuid.uuid4())[:8]
        wfr = WorkflowRun(run_id=rid, workflow_id=workflow_id,
                          context=dict(context or {}),
                          status=WorkflowStatus.RUNNING)
        self._runs[rid] = wfr

        if not wf.start_step:
            wfr.status = WorkflowStatus.FAILED
            wfr.error  = "No start step defined"
            return wfr

        cur_step_id = wf.start_step
        max_steps   = len(wf.steps) * 10 + 100    # cycle guard

        for _ in range(max_steps):
            if cur_step_id is None:
                break
            if wfr.status in (WorkflowStatus.CANCELLED,
                               WorkflowStatus.PAUSED):
                break
            step = wf.steps.get(cur_step_id)
            if not step:
                break
            if step.step_type == StepType.END:
                wfr.current_step = cur_step_id
                break

            next_id = self._execute_step(step, wfr)
            if wfr.status == WorkflowStatus.FAILED:
                break
            cur_step_id = next_id

        if wfr.status == WorkflowStatus.RUNNING:
            wfr.status = WorkflowStatus.COMPLETED
        wfr.finished_at = time.time()

        self._persist_run(wfr)
        return wfr

    def _execute_step(self, step: StepDef,
                       wfr: WorkflowRun) -> Optional[str]:
        sr = StepRun(step_id=step.step_id,
                     status=StepStatus.RUNNING,
                     started_at=time.time())
        wfr.current_step = step.step_id
        wfr.step_runs.append(sr)

        try:
            result, next_id = self._dispatch_step(step, wfr, sr)
            sr.result      = result
            sr.status      = StepStatus.DONE
            sr.finished_at = time.time()
            self._persist_step_run(wfr.run_id, sr)
            return next_id
        except Exception as exc:
            sr.error      = str(exc)
            sr.status     = StepStatus.FAILED
            sr.finished_at = time.time()
            self._persist_step_run(wfr.run_id, sr)
            if step.on_error == "continue":
                return step.next_step
            if step.on_error == "skip":
                return step.next_step
            wfr.status = WorkflowStatus.FAILED
            wfr.error  = f"Step '{step.name}': {exc}"
            return None

    def _dispatch_step(self, step: StepDef, wfr: WorkflowRun,
                        sr: StepRun) -> Tuple[Any, Optional[str]]:
        ctx = wfr.context

        if step.step_type == StepType.ACTION:
            result = None
            for attempt in range(max(1, step.max_retries + 1)):
                sr.attempt = attempt + 1
                try:
                    result = step.fn(ctx) if step.fn else None
                    break
                except Exception as exc:
                    if attempt < step.max_retries:
                        time.sleep(step.retry_delay_s)
                    else:
                        raise
            return result, step.next_step

        if step.step_type == StepType.CONDITION:
            pred = step.condition(ctx) if step.condition else False
            nxt  = step.on_true if pred else step.on_false
            return pred, nxt

        if step.step_type == StepType.SET_VAR:
            value = step.var_value(ctx) if callable(step.var_value) \
                    else step.var_value
            if step.var_name:
                ctx[step.var_name] = value
            return value, step.next_step

        if step.step_type == StepType.WAIT:
            time.sleep(step.wait_s)
            return None, step.next_step

        if step.step_type == StepType.LOOP:
            items = ctx.get(step.loop_over, []) if step.loop_over else []
            results = []
            for item in items:
                ctx["_loop_item"] = item
                loop_step = self._get_wf(wfr.workflow_id).steps.get(
                    step.loop_body) if step.loop_body else None
                if loop_step:
                    sub_sr = StepRun(step_id=loop_step.step_id,
                                     started_at=time.time())
                    r, _ = self._dispatch_step(loop_step, wfr, sub_sr)
                    results.append(r)
            ctx["_loop_results"] = results
            return results, step.next_step

        if step.step_type == StepType.EMIT:
            event_name = str(step.var_name or "event")
            wfr.events.append(event_name)
            for fn in self._event_hooks.get(event_name, []):
                try: fn(ctx, wfr)
                except Exception: pass
            return event_name, step.next_step

        if step.step_type == StepType.SUBFLOW:
            if step.subflow_id:
                sub_run = self.run(step.subflow_id,
                                   context=dict(ctx))
                ctx["_subflow_result"] = sub_run.context
                return sub_run.status.value, step.next_step
            return None, step.next_step

        if step.step_type == StepType.HUMAN:
            wfr.status = WorkflowStatus.WAITING
            return None, step.next_step

        return None, step.next_step

    # ── CONTROL ───────────────────────────────────────────────────────

    def cancel(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if run and run.status == WorkflowStatus.RUNNING:
            run.status = WorkflowStatus.CANCELLED
            return True
        return False

    def resume(self, run_id: str,
               human_input: Any = None) -> Optional[WorkflowRun]:
        run = self._runs.get(run_id)
        if not run or run.status not in (WorkflowStatus.PAUSED,
                                          WorkflowStatus.WAITING):
            return None
        if human_input is not None:
            run.context["_human_input"] = human_input
        run.status = WorkflowStatus.RUNNING
        # Continue from current step's next
        wf = self._workflows.get(run.workflow_id)
        if wf and run.current_step:
            step = wf.steps.get(run.current_step)
            if step and step.next_step:
                run.current_step = step.next_step
                # Re-run from current step
                cur = run.current_step
                for _ in range(len(wf.steps) * 10 + 100):
                    if cur is None: break
                    s = wf.steps.get(cur)
                    if not s or s.step_type == StepType.END: break
                    nxt = self._execute_step(s, run)
                    if run.status == WorkflowStatus.FAILED: break
                    cur = nxt
                if run.status == WorkflowStatus.RUNNING:
                    run.status = WorkflowStatus.COMPLETED
                run.finished_at = time.time()
        return run

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_event(self, event_name: str, fn: Callable):
        self._event_hooks.setdefault(event_name, []).append(fn)

    # ── PERSISTENCE ───────────────────────────────────────────────────

    def _persist_run(self, wfr: WorkflowRun):
        self._db.execute(
            "INSERT OR REPLACE INTO we_runs VALUES (?,?,?,?,?,?,?)",
            (wfr.run_id, wfr.workflow_id, wfr.status.value,
             len(wfr.step_runs), wfr.duration_ms,
             wfr.error, wfr.started_at))
        self._db.commit()

    def _persist_step_run(self, run_id: str, sr: StepRun):
        self._db.execute(
            "INSERT INTO we_step_runs (run_id,step_id,status,duration_ms,error,attempt) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, sr.step_id, sr.status.value,
             sr.duration_ms, sr.error, sr.attempt))
        self._db.commit()

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_run(self, run_id: str) -> Optional[WorkflowRun]:
        return self._runs.get(run_id)

    def run_history(self, workflow_id: Optional[str] = None,
                    limit: int = 20) -> List[Dict]:
        q = ("SELECT run_id,workflow_id,status,steps_run,duration_ms,ts "
             "FROM we_runs")
        params: List[Any] = []
        if workflow_id:
            q += " WHERE workflow_id=?"; params.append(workflow_id)
        q += " ORDER BY ts DESC LIMIT ?"; params.append(limit)
        rows = self._db.execute(q, params).fetchall()
        return [{"run_id": r[0], "workflow": r[1], "status": r[2],
                 "steps": r[3], "ms": r[4]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        completed = sum(1 for r in self._runs.values()
                        if r.status == WorkflowStatus.COMPLETED)
        failed    = sum(1 for r in self._runs.values()
                        if r.status == WorkflowStatus.FAILED)
        return {
            "workflows": len(self._workflows),
            "runs": len(self._runs),
            "completed": completed,
            "failed": failed,
        }
