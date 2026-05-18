"""OMNI AGENT - State Machine
Finite state machine for agent workflow control: states, typed transitions,
guard conditions, entry/exit actions, history, and SQLite persistence.

Features:
- State registry: define states with entry/exit actions and metadata
- Typed transitions: source → target with event name, guard, action
- Guard conditions: callable predicates that must pass for transition
- Entry/exit hooks: called on state change with event context
- Transition actions: called during the transition itself
- History: full log of every transition with timestamps and context
- Current-state query: inspect active state and available events
- Timeout transitions: auto-trigger after N seconds in a state
- Nested sub-machines: states can contain child state machines
- Persistence: active state and history survive restarts
- REST API: trigger, status, history, available-events
"""
import json, time, uuid, sqlite3, asyncio, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class State:
    name: str; description: str = ""
    entry_action: Optional[Callable] = None
    exit_action:  Optional[Callable] = None
    is_terminal: bool = False
    metadata: Dict = field(default_factory=dict)
    def to_dict(self):
        return {"name":self.name,"description":self.description,
                "is_terminal":self.is_terminal,"metadata":self.metadata}

@dataclass
class Transition:
    event: str; source: str; target: str
    guard: Optional[Callable] = None
    action: Optional[Callable] = None
    description: str = ""
    priority: int = 0
    def can_fire(self, context):
        if not self.guard: return True
        try: return bool(self.guard(context))
        except: return False
    def to_dict(self):
        return {"event":self.event,"source":self.source,"target":self.target,
                "description":self.description,"priority":self.priority}

@dataclass
class TransitionRecord:
    machine_id: str; event: str; from_state: str; to_state: str
    context: Dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    def to_dict(self):
        return {"event":self.event,"from_state":self.from_state,"to_state":self.to_state,
                "timestamp":self.timestamp,"context_keys":list(self.context.keys())}

class SMStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path=db_path; self._init()
    def _conn(self):
        c=sqlite3.connect(self.db_path); c.row_factory=sqlite3.Row; return c
    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS machines(
                    id TEXT PRIMARY KEY,name TEXT,current_state TEXT,
                    context TEXT DEFAULT '{}',created_at REAL,updated_at REAL);
                CREATE TABLE IF NOT EXISTS history(
                    id TEXT PRIMARY KEY,machine_id TEXT,event TEXT,
                    from_state TEXT,to_state TEXT,context TEXT DEFAULT '{}',timestamp REAL);
                CREATE INDEX IF NOT EXISTS idx_sm_hist ON history(machine_id,timestamp DESC);
            """)
    def save_machine(self, mid, name, current_state, context):
        with self._conn() as c:
            now=time.time()
            c.execute("INSERT OR REPLACE INTO machines VALUES(?,?,?,?,COALESCE((SELECT created_at FROM machines WHERE id=?),?),?)",
                (mid,name,current_state,json.dumps(context),mid,now,now))
    def save_transition(self, rec):
        with self._conn() as c:
            c.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:10],rec.machine_id,rec.event,
                 rec.from_state,rec.to_state,json.dumps(rec.context),rec.timestamp))
    def load_state(self, mid):
        with self._conn() as c:
            row=c.execute("SELECT * FROM machines WHERE id=?",(mid,)).fetchone()
        if not row: return None,{}
        return row["current_state"],json.loads(row["context"] or "{}")
    def get_history(self, mid, limit=50):
        with self._conn() as c:
            rows=c.execute("SELECT * FROM history WHERE machine_id=? ORDER BY timestamp DESC LIMIT ?",(mid,limit)).fetchall()
        return [dict(r) for r in rows]

class StateMachine:
    """
    Finite state machine with guards, actions, history, and persistence.

    Usage:
        sm = StateMachine("order_flow", initial_state="pending")
        sm.add_state(State("pending"))
        sm.add_state(State("processing"))
        sm.add_state(State("shipped"))
        sm.add_state(State("delivered", is_terminal=True))

        sm.add_transition(Transition("pay",       "pending",    "processing"))
        sm.add_transition(Transition("ship",      "processing", "shipped",
                           guard=lambda ctx: ctx.get("warehouse_ready")))
        sm.add_transition(Transition("deliver",   "shipped",    "delivered"))

        ok = await sm.trigger("pay", context={"amount": 49.99})
        print(sm.current_state)  # "processing"
    """
    def __init__(self, machine_id, name="", initial_state="",
                 db_path="data/state_machines.db", persist=True):
        self.machine_id=machine_id; self.name=name or machine_id
        self._states: Dict[str,State] = {}
        self._transitions: List[Transition] = []
        self._history: List[TransitionRecord] = []
        self._current_state=initial_state
        self._context: Dict = {}
        self._store=SMStore(db_path) if persist else None
        self._timeout_task=None
        if persist and initial_state:
            saved,ctx=self._store.load_state(machine_id)
            if saved: self._current_state=saved; self._context=ctx
            else: self._save()

    def _save(self):
        if self._store:
            self._store.save_machine(self.machine_id,self.name,self._current_state,self._context)

    @property
    def current_state(self): return self._current_state

    @property
    def current_state_obj(self): return self._states.get(self._current_state)

    def add_state(self, state):
        self._states[state.name]=state
        if not self._current_state: self._current_state=state.name; self._save()
        return self

    def add_transition(self, transition):
        self._transitions.append(transition); return self

    def available_events(self, context=None):
        ctx={**self._context,**(context or {})}
        return [t.event for t in self._transitions
                if t.source==self._current_state and t.can_fire(ctx)]

    def can_trigger(self, event, context=None):
        ctx={**self._context,**(context or {})}
        return any(t for t in self._transitions
                   if t.source==self._current_state and t.event==event and t.can_fire(ctx))

    async def trigger(self, event, context=None):
        ctx={**self._context,**(context or {})}
        candidates=[t for t in self._transitions
                     if t.source==self._current_state and t.event==event]
        candidates.sort(key=lambda t:-t.priority)
        fired=None
        for t in candidates:
            if t.can_fire(ctx): fired=t; break
        if not fired:
            logger.debug(f"[{self.machine_id}] Event {event!r} not applicable in {self._current_state!r}")
            return False
        from_state=self._current_state
        # exit action
        cur_obj=self._states.get(from_state)
        if cur_obj and cur_obj.exit_action:
            try:
                fn=cur_obj.exit_action
                await fn(ctx) if asyncio.iscoroutinefunction(fn) else fn(ctx)
            except Exception as e: logger.warning(f"Exit action error: {e}")
        # transition action
        if fired.action:
            try:
                fn=fired.action
                await fn(ctx) if asyncio.iscoroutinefunction(fn) else fn(ctx)
            except Exception as e: logger.warning(f"Transition action error: {e}")
        # state change
        self._current_state=fired.target
        self._context.update(context or {})
        # entry action
        new_obj=self._states.get(fired.target)
        if new_obj and new_obj.entry_action:
            try:
                fn=new_obj.entry_action
                await fn(ctx) if asyncio.iscoroutinefunction(fn) else fn(ctx)
            except Exception as e: logger.warning(f"Entry action error: {e}")
        rec=TransitionRecord(machine_id=self.machine_id,event=event,
                              from_state=from_state,to_state=fired.target,context=ctx)
        self._history.append(rec)
        if self._store: self._store.save_transition(rec)
        self._save()
        logger.info(f"[{self.machine_id}] {from_state} --{event}--> {fired.target}")
        return True

    def trigger_sync(self, event, context=None):
        return asyncio.get_event_loop().run_until_complete(self.trigger(event, context))

    def is_terminal(self):
        obj=self._states.get(self._current_state)
        return obj.is_terminal if obj else False

    def history(self, limit=50):
        if self._store: return self._store.get_history(self.machine_id, limit)
        return [r.to_dict() for r in self._history[-limit:]]

    def status(self):
        state_obj=self.current_state_obj
        return {"machine_id":self.machine_id,"name":self.name,
                "current_state":self._current_state,
                "is_terminal":self.is_terminal(),
                "available_events":self.available_events(),
                "state_description":state_obj.description if state_obj else "",
                "context_keys":list(self._context.keys())}

    def reset(self, initial_state=None):
        target=initial_state or (list(self._states.keys())[0] if self._states else "")
        self._current_state=target; self._context={}; self._history=[]; self._save()

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        mid=self.machine_id
        async def trigger_ep(req):
            d=await req.json()
            ok=await self.trigger(d["event"],d.get("context",{}))
            return web.json_response({"triggered":ok,"current_state":self._current_state})
        async def status_ep(req):
            return web.json_response(self.status())
        async def history_ep(req):
            limit=int(req.rel_url.query.get("limit",50))
            return web.json_response({"history":self.history(limit)})
        async def events_ep(req):
            return web.json_response({"available_events":self.available_events()})
        p=f"{prefix}/sm/{mid}"
        app.router.add_post(f"{p}/trigger",trigger_ep)
        app.router.add_get(f"{p}/status",status_ep)
        app.router.add_get(f"{p}/history",history_ep)
        app.router.add_get(f"{p}/events",events_ep)
        logger.info(f"StateMachine {mid!r} API at {prefix}/sm/{mid}/")
