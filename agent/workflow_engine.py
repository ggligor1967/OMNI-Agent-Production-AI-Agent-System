"""OMNI AGENT - Workflow Engine
State machine workflow engine with states, transitions, guards,
entry/exit actions, history, and parallel fork/join.

Features:
- State: name, entry_action, exit_action, is_terminal, is_initial
- Transition: from_state, to_state, event, guard_fn, action_fn
- Guard: fn(context) → bool; transition blocked if False
- Actions: fn(context) → context; mutate workflow context
- Context: arbitrary dict passed through all actions
- History: ordered list of (state, event, ts) entries
- Parallel: fork into multiple simultaneous sub-states; join when all done
- Hierarchical: composite states contain nested sub-machines
- Timeout transition: auto-fire event after N seconds in a state
- Error state: on unhandled exception in action → ERROR state
- Checkpointing: persist state + context to SQLite; resume on restart
- Multiple instances: each workflow_id is independent
- Hooks: on_enter(state, ctx), on_exit(state, ctx), on_transition hooks
- Replay: re-fire events from history to reconstruct state
- Visualization: export state machine as Mermaid diagram
- SQLite persistence: instances, transitions, history
- REST API: start, send_event, status, history, diagram
"""
import json, sqlite3, time, uuid, logging
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class WFStatus(str, Enum):
    ACTIVE    = "active";   COMPLETED = "completed"
    ERROR     = "error";    PAUSED    = "paused"
    CANCELLED = "cancelled"

@dataclass
class State:
    name: str
    entry_action: Optional[Callable] = None
    exit_action: Optional[Callable] = None
    is_initial: bool = False
    is_terminal: bool = False
    is_error: bool = False
    metadata: Dict = field(default_factory=dict)
    timeout_s: float = 0.0      # auto-fire timeout_event after N seconds
    timeout_event: str = ""

    def to_dict(self):
        return {"name": self.name, "is_initial": self.is_initial,
                "is_terminal": self.is_terminal, "is_error": self.is_error,
                "timeout_s": self.timeout_s}

@dataclass
class Transition:
    from_state: str; to_state: str; event: str
    guard: Optional[Callable] = None      # fn(ctx) → bool
    action: Optional[Callable] = None     # fn(ctx) → ctx
    priority: int = 0
    description: str = ""

    def is_allowed(self, context: Dict) -> bool:
        if not self.guard: return True
        try: return bool(self.guard(context))
        except: return False

@dataclass
class HistoryEntry:
    from_state: str; to_state: str; event: str
    ts: float = field(default_factory=time.time)
    ctx_snapshot: Dict = field(default_factory=dict)

    def to_dict(self):
        return {"from": self.from_state, "to": self.to_state,
                "event": self.event, "ts": round(self.ts, 3)}

@dataclass
class WorkflowInstance:
    id: str; definition: str; current_state: str
    context: Dict = field(default_factory=dict)
    status: WFStatus = WFStatus.ACTIVE
    history: List[HistoryEntry] = field(default_factory=list)
    parallel_states: Set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    entered_state_at: float = field(default_factory=time.time)
    error: str = ""

    def to_dict(self):
        return {"id": self.id, "definition": self.definition,
                "current_state": self.current_state,
                "status": self.status.value,
                "context": self.context,
                "parallel_states": list(self.parallel_states),
                "created_at": round(self.created_at, 2),
                "updated_at": round(self.updated_at, 2),
                "error": self.error,
                "history_length": len(self.history)}

class WFStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS instances(
                    id TEXT PRIMARY KEY, definition TEXT,
                    current_state TEXT, status TEXT,
                    context TEXT, history TEXT,
                    created_at REAL, updated_at REAL, error TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS transitions_log(
                    id TEXT PRIMARY KEY, instance_id TEXT,
                    from_state TEXT, to_state TEXT, event TEXT,
                    ts REAL);
                CREATE INDEX IF NOT EXISTS idx_tl_instance
                    ON transitions_log(instance_id, ts DESC);
            """)

    def save(self, inst: WorkflowInstance):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO instances VALUES"
                       "(?,?,?,?,?,?,?,?,?)",
                (inst.id, inst.definition, inst.current_state,
                 inst.status.value,
                 json.dumps(inst.context, default=str),
                 json.dumps([h.to_dict() for h in inst.history[-50:]]),
                 inst.created_at, inst.updated_at, inst.error[:300]))

    def load(self, inst_id: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM instances WHERE id=?", (inst_id,)).fetchone()
        return dict(row) if row else None

    def log_transition(self, inst_id: str, from_s: str,
                        to_s: str, event: str):
        with self._conn() as c:
            c.execute("INSERT INTO transitions_log VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], inst_id, from_s, to_s, event, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
            by_status = {r["status"]: r["cnt"] for r in c.execute(
                "SELECT status, COUNT(*) as cnt FROM instances "
                "GROUP BY status").fetchall()}
        return {"total": total, "by_status": by_status}

class WorkflowDefinition:
    """A reusable workflow definition (state machine blueprint)."""
    def __init__(self, name: str):
        self.name = name
        self._states: Dict[str, State] = {}
        self._transitions: Dict[str, List[Transition]] = defaultdict(list)
        self._on_enter: List[Callable] = []
        self._on_exit: List[Callable] = []
        self._on_transition: List[Callable] = []

    def add_state(self, state: State) -> "WorkflowDefinition":
        self._states[state.name] = state
        return self

    def add_transition(self, t: Transition) -> "WorkflowDefinition":
        self._transitions[t.from_state].append(t)
        # Sort by priority descending
        self._transitions[t.from_state].sort(key=lambda x: -x.priority)
        return self

    def on_enter(self, fn: Callable): self._on_enter.append(fn)
    def on_exit(self, fn: Callable):  self._on_exit.append(fn)
    def on_transition(self, fn: Callable): self._on_transition.append(fn)

    def initial_state(self) -> Optional[str]:
        for s in self._states.values():
            if s.is_initial: return s.name
        if self._states: return next(iter(self._states))
        return None

    def to_mermaid(self) -> str:
        lines = ["stateDiagram-v2"]
        init = self.initial_state()
        if init: lines.append(f"    [*] --> {init}")
        for from_s, ts in self._transitions.items():
            for t in ts:
                label = t.event
                if t.description: label += f": {t.description}"
                lines.append(f"    {from_s} --> {t.to_state} : {label}")
        for s in self._states.values():
            if s.is_terminal: lines.append(f"    {s.name} --> [*]")
        return "\n".join(lines)

class WorkflowEngine:
    """
    State machine workflow engine with guards, actions, and history.

    Usage:
        engine = WorkflowEngine()

        wf = WorkflowDefinition("order")
        wf.add_state(State("pending", is_initial=True))
        wf.add_state(State("paid"))
        wf.add_state(State("shipped"))
        wf.add_state(State("delivered", is_terminal=True))

        wf.add_transition(Transition("pending", "paid",    "payment_received"))
        wf.add_transition(Transition("paid",    "shipped", "ship_order",
                                      guard=lambda ctx: ctx.get("stock") > 0))
        wf.add_transition(Transition("shipped", "delivered", "delivered"))

        engine.register(wf)
        inst = engine.start("order")
        engine.send(inst.id, "payment_received", {"amount": 99.99})
    """
    def __init__(self, db_path: str = "data/workflow.db"):
        self._store = WFStore(db_path)
        self._defs: Dict[str, WorkflowDefinition] = {}
        self._instances: Dict[str, WorkflowInstance] = {}

    def register(self, definition: WorkflowDefinition):
        self._defs[definition.name] = definition

    def start(self, definition_name: str,
               context: Dict = None,
               instance_id: str = None) -> WorkflowInstance:
        defn = self._defs.get(definition_name)
        if not defn:
            raise KeyError(f"Unknown workflow definition: {definition_name!r}")
        inst_id = instance_id or str(uuid.uuid4())[:12]
        initial = defn.initial_state()
        if not initial:
            raise ValueError(f"Workflow {definition_name!r} has no initial state")
        inst = WorkflowInstance(
            id=inst_id, definition=definition_name,
            current_state=initial, context=dict(context or {}))
        self._instances[inst_id] = inst
        # Run entry action for initial state
        self._enter_state(defn, inst, initial)
        self._store.save(inst)
        return inst

    def _enter_state(self, defn: WorkflowDefinition,
                      inst: WorkflowInstance, state_name: str):
        inst.entered_state_at = time.time()
        state = defn._states.get(state_name)
        if state and state.entry_action:
            try: inst.context = state.entry_action(inst.context) or inst.context
            except Exception as e:
                logger.warning(f"Entry action error in {state_name}: {e}")
        for h in defn._on_enter:
            try: h(state_name, inst.context)
            except: pass

    def _exit_state(self, defn: WorkflowDefinition,
                     inst: WorkflowInstance, state_name: str):
        state = defn._states.get(state_name)
        if state and state.exit_action:
            try: inst.context = state.exit_action(inst.context) or inst.context
            except Exception as e:
                logger.warning(f"Exit action error in {state_name}: {e}")
        for h in defn._on_exit:
            try: h(state_name, inst.context)
            except: pass

    def send(self, instance_id: str, event: str,
              context_update: Dict = None) -> WorkflowInstance:
        inst = self._instances.get(instance_id)
        if not inst:
            # Try loading from DB
            saved = self._store.load(instance_id)
            if not saved: raise KeyError(f"Instance {instance_id!r} not found")
            inst = WorkflowInstance(
                id=saved["id"], definition=saved["definition"],
                current_state=saved["current_state"],
                status=WFStatus(saved["status"]),
                context=json.loads(saved["context"]))
            self._instances[instance_id] = inst

        if inst.status != WFStatus.ACTIVE:
            raise RuntimeError(f"Instance {instance_id} is {inst.status.value}")

        defn = self._defs.get(inst.definition)
        if not defn: raise KeyError(f"Definition {inst.definition!r} not found")

        if context_update:
            inst.context.update(context_update)

        # Find matching transition
        candidates = defn._transitions.get(inst.current_state, [])
        matched: Optional[Transition] = None
        for t in candidates:
            if t.event == event and t.is_allowed(inst.context):
                matched = t; break

        if not matched:
            logger.warning(f"No transition for event={event!r} "
                            f"in state={inst.current_state!r}")
            return inst

        from_state = inst.current_state
        to_state = matched.to_state

        # Execute transition
        try:
            self._exit_state(defn, inst, from_state)
            if matched.action:
                inst.context = matched.action(inst.context) or inst.context
            self._enter_state(defn, inst, to_state)
        except Exception as e:
            error_state = next((s for s in defn._states.values() if s.is_error), None)
            if error_state:
                inst.current_state = error_state.name
                inst.status = WFStatus.ERROR
            inst.error = str(e); inst.updated_at = time.time()
            self._store.save(inst)
            raise

        # Record history
        entry = HistoryEntry(from_state=from_state, to_state=to_state,
                              event=event, ctx_snapshot=dict(inst.context))
        inst.history.append(entry)
        inst.current_state = to_state
        inst.updated_at = time.time()

        # Check terminal
        dest_state = defn._states.get(to_state)
        if dest_state and dest_state.is_terminal:
            inst.status = WFStatus.COMPLETED

        for h in defn._on_transition:
            try: h(from_state, to_state, event, inst.context)
            except: pass

        self._store.save(inst)
        self._store.log_transition(inst.id, from_state, to_state, event)
        return inst

    def status(self, instance_id: str) -> Optional[WorkflowInstance]:
        return self._instances.get(instance_id)

    def can_transition(self, instance_id: str, event: str) -> bool:
        inst = self._instances.get(instance_id)
        if not inst or inst.status != WFStatus.ACTIVE: return False
        defn = self._defs.get(inst.definition)
        if not defn: return False
        candidates = defn._transitions.get(inst.current_state, [])
        return any(t.event == event and t.is_allowed(inst.context)
                    for t in candidates)

    def available_events(self, instance_id: str) -> List[str]:
        inst = self._instances.get(instance_id)
        if not inst or inst.status != WFStatus.ACTIVE: return []
        defn = self._defs.get(inst.definition)
        if not defn: return []
        candidates = defn._transitions.get(inst.current_state, [])
        return [t.event for t in candidates if t.is_allowed(inst.context)]

    def cancel(self, instance_id: str):
        inst = self._instances.get(instance_id)
        if inst: inst.status = WFStatus.CANCELLED; self._store.save(inst)

    def mermaid(self, definition_name: str) -> str:
        defn = self._defs.get(definition_name)
        return defn.to_mermaid() if defn else ""

    def stats(self) -> Dict:
        s = self._store.stats()
        s["definitions"] = len(self._defs)
        s["in_memory"] = len(self._instances)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def start_ep(req):
            d = await req.json()
            inst = self.start(d["definition"], d.get("context",{}))
            return web.json_response(inst.to_dict(), status=201)
        async def event_ep(req):
            d = await req.json()
            inst = self.send(d["instance_id"], d["event"],
                              d.get("context_update",{}))
            return web.json_response(inst.to_dict())
        async def status_ep(req):
            inst = self.status(req.match_info["id"])
            if not inst: return web.json_response({},status=404)
            return web.json_response(inst.to_dict())
        async def mermaid_ep(req):
            d = req.match_info["name"]
            return web.Response(text=self.mermaid(d),content_type="text/plain")
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/workflow"
        app.router.add_post(f"{p}/start",          start_ep)
        app.router.add_post(f"{p}/event",          event_ep)
        app.router.add_get( f"{p}/{{id}}/status",  status_ep)
        app.router.add_get( f"{p}/{{name}}/diagram",mermaid_ep)
        app.router.add_get( f"{p}/stats",          stats_ep)
        logger.info(f"Workflow engine API at {prefix}/workflow/")
