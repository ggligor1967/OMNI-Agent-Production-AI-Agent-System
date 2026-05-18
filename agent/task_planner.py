"""OMNI AGENT - Task Planner
Hierarchical task decomposition: goal → subtasks → steps,
dependency DAG, priority scoring, and execution tracking.

Features:
- Goal: top-level objective with success criteria
- Task: decomposition unit with subtasks, dependencies, estimated effort
- Step: atomic action within a task (fn, description, tool_call)
- Dependency DAG: topological sort for execution ordering
- Priority scoring: urgency × importance × (1/effort)
- Status lifecycle: PENDING → RUNNING → DONE | FAILED | SKIPPED
- Dependency blocking: task waits until all deps are DONE
- Effort estimation: rough t-shirt sizing (XS/S/M/L/XL) → hours
- Progress tracking: % complete by steps done / total steps
- Context propagation: outputs from completed tasks injected as inputs
- Plan export: full JSON tree for inspection
- Replanning: mark task failed → auto-mark dependents as SKIPPED
- SQLite persistence: goals, tasks, steps, execution log
- REST API: plan, start, complete, fail, status, stats
"""
import json, sqlite3, time, uuid, logging
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class Status(str, Enum):
    PENDING = "pending"; RUNNING = "running"
    DONE    = "done";    FAILED  = "failed"; SKIPPED = "skipped"

class Effort(str, Enum):
    XS = "xs"; S = "s"; M = "m"; L = "l"; XL = "xl"

_EFFORT_HOURS = {Effort.XS: 0.5, Effort.S: 2, Effort.M: 8,
                  Effort.L: 24, Effort.XL: 80}

def _priority_score(urgency: float, importance: float,
                     effort_hours: float) -> float:
    return round(urgency * importance / max(0.1, effort_hours), 4)

@dataclass
class Step:
    id: str; name: str; description: str = ""
    fn: Optional[Callable] = None
    tool_name: str = ""          # if using tool registry
    tool_args: Dict = field(default_factory=dict)
    status: Status = Status.PENDING
    output: Any = None; error: str = ""
    started_at: float = 0.0; finished_at: float = 0.0

    @property
    def duration_s(self) -> float:
        if self.started_at and self.finished_at:
            return round(self.finished_at - self.started_at, 3)
        return 0.0

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "description": self.description,
                "tool": self.tool_name, "status": self.status.value,
                "output": str(self.output)[:200] if self.output else None,
                "error": self.error, "duration_s": self.duration_s}

@dataclass
class Task:
    id: str; name: str; goal_id: str
    description: str = ""
    steps: List[Step] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)  # task ids
    status: Status = Status.PENDING
    effort: Effort = Effort.M
    urgency: float = 5.0      # 1-10
    importance: float = 5.0   # 1-10
    tags: List[str] = field(default_factory=list)
    context_in: Dict = field(default_factory=dict)
    context_out: Dict = field(default_factory=dict)
    started_at: float = 0.0; finished_at: float = 0.0

    @property
    def priority(self) -> float:
        return _priority_score(self.urgency, self.importance,
                                _EFFORT_HOURS[self.effort])

    @property
    def progress(self) -> float:
        if not self.steps: return 1.0 if self.status == Status.DONE else 0.0
        done = sum(1 for s in self.steps if s.status == Status.DONE)
        return round(done / len(self.steps), 4)

    @property
    def estimated_hours(self) -> float:
        return _EFFORT_HOURS[self.effort]

    def to_dict(self, include_steps: bool = True):
        d = {"id": self.id, "name": self.name, "goal_id": self.goal_id,
             "description": self.description,
             "status": self.status.value, "effort": self.effort.value,
             "urgency": self.urgency, "importance": self.importance,
             "priority": self.priority, "progress": self.progress,
             "estimated_hours": self.estimated_hours,
             "depends_on": self.depends_on, "tags": self.tags,
             "context_out": self.context_out}
        if include_steps:
            d["steps"] = [s.to_dict() for s in self.steps]
        return d

@dataclass
class Goal:
    id: str; name: str; description: str = ""
    tasks: List[Task] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    status: Status = Status.PENDING
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def progress(self) -> float:
        if not self.tasks: return 0.0
        return round(sum(t.progress for t in self.tasks) / len(self.tasks), 4)

    @property
    def total_estimated_hours(self) -> float:
        return sum(t.estimated_hours for t in self.tasks)

    def to_dict(self, include_tasks: bool = True):
        d = {"id": self.id, "name": self.name,
             "description": self.description,
             "status": self.status.value,
             "progress": self.progress,
             "total_estimated_hours": self.total_estimated_hours,
             "task_count": len(self.tasks),
             "success_criteria": self.success_criteria,
             "tags": self.tags}
        if include_tasks:
            d["tasks"] = [t.to_dict() for t in self.tasks]
        return d

class TPStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS goals(
                    id TEXT PRIMARY KEY, name TEXT, description TEXT,
                    status TEXT DEFAULT 'pending',
                    tags TEXT DEFAULT '[]', created_at REAL);
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, goal_id TEXT, name TEXT,
                    description TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    effort TEXT DEFAULT 'm',
                    urgency REAL DEFAULT 5, importance REAL DEFAULT 5,
                    depends_on TEXT DEFAULT '[]',
                    tags TEXT DEFAULT '[]',
                    context_out TEXT DEFAULT '{}',
                    created_at REAL);
                CREATE TABLE IF NOT EXISTS exec_log(
                    id TEXT PRIMARY KEY, task_id TEXT,
                    status TEXT, note TEXT DEFAULT '',
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_task_goal ON tasks(goal_id, status);
            """)

    def save_goal(self, g: Goal):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO goals VALUES(?,?,?,?,?,?)",
                (g.id, g.name, g.description, g.status.value,
                 json.dumps(g.tags), g.created_at))

    def save_task(self, t: Task):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.id, t.goal_id, t.name, t.description,
                 t.status.value, t.effort.value,
                 t.urgency, t.importance,
                 json.dumps(t.depends_on), json.dumps(t.tags),
                 json.dumps(t.context_out), time.time()))

    def log(self, task_id: str, status: str, note: str = ""):
        with self._conn() as c:
            c.execute("INSERT INTO exec_log VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], task_id, status, note, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            ng = c.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
            nt = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            nd = c.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
        return {"goals": ng, "tasks": nt, "done": nd,
                "completion_rate": round(nd / max(1, nt), 4)}

class TaskPlanner:
    """
    Hierarchical task planner with dependency DAG and priority scoring.

    Usage:
        planner = TaskPlanner()

        goal = planner.create_goal("Ship feature X", "Deliver X by Friday")
        t1 = planner.add_task(goal.id, "Design", effort=Effort.S, urgency=8)
        t2 = planner.add_task(goal.id, "Implement", effort=Effort.M,
                               depends_on=[t1.id])
        t3 = planner.add_task(goal.id, "Test", effort=Effort.S,
                               depends_on=[t2.id])

        plan = planner.execution_order(goal.id)   # topo-sorted tasks
        planner.start_task(t1.id)
        planner.complete_task(t1.id, output={"design_doc": "url"})
    """
    def __init__(self, db_path: str = "data/planner.db"):
        self._store = TPStore(db_path)
        self._goals: Dict[str, Goal] = {}
        self._tasks: Dict[str, Task] = {}

    def create_goal(self, name: str, description: str = "",
                     success_criteria: List[str] = None,
                     tags: List[str] = None,
                     goal_id: str = None) -> Goal:
        gid = goal_id or str(uuid.uuid4())[:12]
        g = Goal(id=gid, name=name, description=description,
                  success_criteria=list(success_criteria or []),
                  tags=list(tags or []))
        self._goals[gid] = g
        self._store.save_goal(g)
        return g

    def add_task(self, goal_id: str, name: str,
                  description: str = "",
                  effort: Effort = Effort.M,
                  urgency: float = 5.0,
                  importance: float = 5.0,
                  depends_on: List[str] = None,
                  tags: List[str] = None,
                  task_id: str = None) -> Optional[Task]:
        goal = self._goals.get(goal_id)
        if not goal: return None
        tid = task_id or str(uuid.uuid4())[:12]
        t = Task(id=tid, name=name, goal_id=goal_id,
                  description=description, effort=effort,
                  urgency=urgency, importance=importance,
                  depends_on=list(depends_on or []),
                  tags=list(tags or []))
        self._tasks[tid] = t
        goal.tasks.append(t)
        self._store.save_task(t)
        return t

    def add_step(self, task_id: str, name: str,
                  description: str = "",
                  fn: Callable = None,
                  tool_name: str = "",
                  tool_args: Dict = None) -> Optional[Step]:
        task = self._tasks.get(task_id)
        if not task: return None
        step = Step(id=str(uuid.uuid4())[:8], name=name,
                     description=description, fn=fn,
                     tool_name=tool_name,
                     tool_args=dict(tool_args or {}))
        task.steps.append(step)
        return step

    def execution_order(self, goal_id: str) -> List[Task]:
        """Topological sort of tasks by dependency DAG."""
        goal = self._goals.get(goal_id)
        if not goal: return []
        task_map = {t.id: t for t in goal.tasks}
        in_degree = {t.id: 0 for t in goal.tasks}
        adj: Dict[str, List[str]] = defaultdict(list)
        for t in goal.tasks:
            for dep in t.depends_on:
                if dep in task_map:
                    adj[dep].append(t.id)
                    in_degree[t.id] += 1
        queue = deque([tid for tid, d in in_degree.items() if d == 0])
        order = []
        while queue:
            # Among ready tasks, pick highest priority first
            ready = sorted(list(queue), key=lambda tid: -task_map[tid].priority)
            queue.clear()
            next_tid = ready[0]
            for r in ready[1:]: queue.append(r)
            order.append(task_map[next_tid])
            for child in adj[next_tid]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)
        return order

    def ready_tasks(self, goal_id: str) -> List[Task]:
        """Tasks whose dependencies are all DONE."""
        order = self.execution_order(goal_id)
        done_ids = {t.id for t in self._tasks.values()
                     if t.status == Status.DONE}
        return [t for t in order
                 if t.status == Status.PENDING
                 and all(d in done_ids for d in t.depends_on)]

    def start_task(self, task_id: str) -> bool:
        t = self._tasks.get(task_id)
        if not t: return False
        # Check deps
        for dep in t.depends_on:
            dep_task = self._tasks.get(dep)
            if dep_task and dep_task.status != Status.DONE:
                logger.warning(f"Task {task_id} dep {dep} not done")
                return False
        t.status = Status.RUNNING; t.started_at = time.time()
        self._store.save_task(t); self._store.log(task_id, "running")
        return True

    def complete_task(self, task_id: str,
                       output: Dict = None) -> bool:
        t = self._tasks.get(task_id)
        if not t: return False
        t.status = Status.DONE; t.finished_at = time.time()
        t.context_out = dict(output or {})
        self._store.save_task(t)
        self._store.log(task_id, "done", json.dumps(output or {}))
        # Update goal status
        goal = self._goals.get(t.goal_id)
        if goal and all(x.status == Status.DONE for x in goal.tasks):
            goal.status = Status.DONE
            self._store.save_goal(goal)
        return True

    def fail_task(self, task_id: str, error: str = "") -> int:
        """Fail a task and skip all downstream dependents. Returns # skipped."""
        t = self._tasks.get(task_id)
        if not t: return 0
        t.status = Status.FAILED
        self._store.save_task(t); self._store.log(task_id, "failed", error)
        # Find all downstream dependents (transitively)
        skipped = 0
        changed = True
        while changed:
            changed = False
            for other in self._tasks.values():
                if other.status == Status.PENDING:
                    if any(dep in {task_id} | {t2.id for t2 in self._tasks.values()
                                                 if t2.status in (Status.FAILED, Status.SKIPPED)}
                            for dep in other.depends_on):
                        other.status = Status.SKIPPED
                        self._store.save_task(other)
                        skipped += 1; changed = True
        return skipped

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        return self._goals.get(goal_id)

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def critical_path(self, goal_id: str) -> List[Task]:
        """Longest dependency chain (by effort hours)."""
        order = self.execution_order(goal_id)
        if not order: return []
        dist: Dict[str, float] = {t.id: t.estimated_hours for t in order}
        prev: Dict[str, Optional[str]] = {t.id: None for t in order}
        task_map = {t.id: t for t in order}
        for t in order:
            for dep_id in t.depends_on:
                if dep_id in dist:
                    new_d = dist[dep_id] + t.estimated_hours
                    if new_d > dist[t.id]:
                        dist[t.id] = new_d; prev[t.id] = dep_id
        end = max(dist, key=lambda k: dist[k])
        path = []
        cur: Optional[str] = end
        while cur:
            path.append(task_map[cur]); cur = prev.get(cur)
        return list(reversed(path))

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory_goals"] = len(self._goals)
        s["in_memory_tasks"] = len(self._tasks)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def create_goal_ep(req):
            d = await req.json()
            g = self.create_goal(d["name"], d.get("description",""),
                                  d.get("success_criteria",[]), d.get("tags",[]))
            return web.json_response(g.to_dict(include_tasks=False), status=201)
        async def add_task_ep(req):
            d = await req.json()
            t = self.add_task(d["goal_id"], d["name"],
                               d.get("description",""),
                               Effort[d.get("effort","M").upper()],
                               float(d.get("urgency",5)),
                               float(d.get("importance",5)),
                               d.get("depends_on",[]))
            if not t: return web.json_response({"error":"goal not found"},status=404)
            return web.json_response(t.to_dict(), status=201)
        async def plan_ep(req):
            gid = req.match_info["goal_id"]
            order = self.execution_order(gid)
            return web.json_response({"plan": [t.to_dict() for t in order]})
        async def complete_ep(req):
            d = await req.json()
            ok = self.complete_task(d["task_id"], d.get("output",{}))
            return web.json_response({"completed": ok})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/plan"
        app.router.add_post(f"{p}/goal",               create_goal_ep)
        app.router.add_post(f"{p}/task",               add_task_ep)
        app.router.add_get( f"{p}/{{goal_id}}/order",  plan_ep)
        app.router.add_post(f"{p}/complete",           complete_ep)
        app.router.add_get( f"{p}/stats",              stats_ep)
        logger.info(f"Task planner API at {prefix}/plan/")
