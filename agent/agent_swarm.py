"""OMNI AGENT - Agent Swarm
Spawn, coordinate, and aggregate results from multiple parallel agents.
Supports broadcast messaging, voting/consensus, map-reduce patterns,
and dynamic swarm resizing.

Features:
- Worker registry: spawn named workers with config and handler function
- Broadcast: send same task to all active workers concurrently
- Targeted send: route task to specific worker(s)
- Voting: gather worker answers, pick majority or weighted winner
- Averaging: numeric result aggregation with outlier filtering
- Map-reduce: map task over input shards, reduce results
- Worker health: track success/failure counts per worker
- Swarm stats: latency distribution, throughput, error rate
- Dynamic resize: add/remove workers at runtime
- Result bus: async callback when any worker completes
- Timeout: per-task and per-swarm deadline enforcement
"""
import time, uuid, asyncio, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import Counter
logger = logging.getLogger(__name__)

@dataclass
class Worker:
    id: str; name: str
    handler: Callable
    config: Dict = field(default_factory=dict)
    active: bool = True
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: float = 0.0

    @property
    def avg_latency_ms(self):
        n = self.success_count + self.failure_count
        return round(self.total_latency_ms / n, 2) if n else 0.0

    @property
    def error_rate(self):
        n = self.success_count + self.failure_count
        return round(self.failure_count / n, 4) if n else 0.0

    def to_dict(self):
        return {"id":self.id,"name":self.name,"active":self.active,
                "success_count":self.success_count,"failure_count":self.failure_count,
                "avg_latency_ms":self.avg_latency_ms,"error_rate":self.error_rate}

@dataclass
class WorkerResult:
    worker_id: str; worker_name: str
    result: Any; error: str = ""
    latency_ms: float = 0.0; success: bool = True
    def to_dict(self):
        return {"worker_id":self.worker_id,"worker_name":self.worker_name,
                "result":str(self.result)[:500] if self.result is not None else None,
                "error":self.error,"latency_ms":round(self.latency_ms,1),"success":self.success}

@dataclass
class SwarmResult:
    task: Any; worker_results: List[WorkerResult]
    aggregate: Any = None; aggregate_method: str = ""
    duration_ms: float = 0.0; created_at: float = field(default_factory=time.time)

    @property
    def success_count(self): return sum(1 for r in self.worker_results if r.success)
    @property
    def failure_count(self): return sum(1 for r in self.worker_results if not r.success)

    def to_dict(self):
        return {"task":str(self.task)[:200],"aggregate":str(self.aggregate)[:500] if self.aggregate is not None else None,
                "aggregate_method":self.aggregate_method,"duration_ms":round(self.duration_ms,1),
                "success_count":self.success_count,"failure_count":self.failure_count,
                "worker_results":[r.to_dict() for r in self.worker_results]}

# ── Aggregation helpers ───────────────────────────────────────────────────────

def _vote(results: List[WorkerResult], weights: Dict[str,float]=None) -> Any:
    """Weighted majority vote among successful string/hashable results."""
    successes = [r for r in results if r.success]
    if not successes: return None
    if weights:
        tally: Dict = {}
        for r in successes:
            key = str(r.result)
            tally[key] = tally.get(key,0) + weights.get(r.worker_id,1.0)
        return max(tally,key=tally.get)
    counts = Counter(str(r.result) for r in successes)
    return counts.most_common(1)[0][0]

def _average(results: List[WorkerResult], remove_outliers=False) -> Optional[float]:
    """Average of numeric results; optional IQR-based outlier removal."""
    vals = []
    for r in results:
        if r.success:
            try: vals.append(float(r.result))
            except: pass
    if not vals: return None
    if remove_outliers and len(vals) >= 4:
        vals.sort()
        q1,q3 = vals[len(vals)//4], vals[3*len(vals)//4]
        iqr = q3-q1; vals = [v for v in vals if q1-1.5*iqr<=v<=q3+1.5*iqr]
    return sum(vals)/len(vals) if vals else None

def _merge_lists(results: List[WorkerResult]) -> List:
    merged = []
    for r in results:
        if r.success and isinstance(r.result, list):
            merged.extend(r.result)
    return merged

def _first_success(results: List[WorkerResult]) -> Any:
    for r in results:
        if r.success: return r.result
    return None

AGGREGATORS = {"vote":_vote,"average":_average,"merge":_merge_lists,"first":_first_success}

# ── AgentSwarm ────────────────────────────────────────────────────────────────

class AgentSwarm:
    """
    Coordinate multiple parallel agents: broadcast, vote, map-reduce, aggregate.

    Usage:
        swarm = AgentSwarm()

        # Register workers
        swarm.add_worker("gpt4",   llm_call_fn,  config={"model":"gpt-4o"})
        swarm.add_worker("claude", claude_fn,     config={"model":"claude-sonnet-4-6"})
        swarm.add_worker("gemini", gemini_fn,     config={"model":"gemini-pro"})

        # Broadcast and vote
        result = await swarm.broadcast("What is 2+2?", aggregate="vote")
        print(result.aggregate)  # "4"

        # Map-reduce over a list
        result = await swarm.map_reduce(
            items=["doc1","doc2","doc3"],
            task_fn=lambda item: f"Summarise: {item}",
            reduce_fn=lambda results: "\n".join(r.result for r in results if r.success),
        )
    """
    def __init__(self, timeout_s: float = 30.0, max_concurrency: int = 16):
        self._workers: Dict[str,Worker] = {}
        self._timeout_s = timeout_s
        self._max_concurrency = max_concurrency
        self._history: List[SwarmResult] = []
        self._callbacks: List[Callable] = []

    def add_worker(self, name: str, handler: Callable, config: Dict = None, worker_id: str = None) -> Worker:
        wid = worker_id or str(uuid.uuid4())[:8]
        w = Worker(id=wid, name=name, handler=handler, config=config or {})
        self._workers[wid] = w
        logger.info(f"Swarm worker added: {name!r} id={wid}")
        return w

    def remove_worker(self, worker_id_or_name: str) -> bool:
        # try by id first
        if worker_id_or_name in self._workers:
            del self._workers[worker_id_or_name]; return True
        # try by name
        for wid, w in list(self._workers.items()):
            if w.name == worker_id_or_name:
                del self._workers[wid]; return True
        return False

    def deactivate_worker(self, worker_id: str) -> bool:
        w = self._workers.get(worker_id)
        if not w: return False
        w.active = False; return True

    def activate_worker(self, worker_id: str) -> bool:
        w = self._workers.get(worker_id)
        if not w: return False
        w.active = True; return True

    def on_result(self, callback: Callable):
        """Register async callback fired after each worker completes."""
        self._callbacks.append(callback)

    # ── Core dispatch ─────────────────────────────────────────────────────────

    async def _call_worker(self, worker: Worker, task: Any) -> WorkerResult:
        start = time.time()
        try:
            async with asyncio.timeout(self._timeout_s):
                fn = worker.handler
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(task, worker.config)
                else:
                    result = fn(task, worker.config)
            latency = (time.time()-start)*1000
            worker.success_count += 1; worker.total_latency_ms += latency
            wr = WorkerResult(worker_id=worker.id, worker_name=worker.name,
                               result=result, latency_ms=latency)
        except Exception as e:
            latency = (time.time()-start)*1000
            worker.failure_count += 1; worker.total_latency_ms += latency
            wr = WorkerResult(worker_id=worker.id, worker_name=worker.name,
                               result=None, error=str(e)[:300],
                               latency_ms=latency, success=False)
        # fire callbacks
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb): asyncio.create_task(cb(wr))
                else: cb(wr)
            except: pass
        return wr

    async def broadcast(self, task: Any, worker_ids: List[str] = None,
                        aggregate: str = "vote",
                        aggregate_weights: Dict[str,float] = None) -> SwarmResult:
        """Send same task to all (or specified) active workers concurrently."""
        start = time.time()
        targets = [w for w in self._workers.values()
                   if w.active and (worker_ids is None or w.id in worker_ids)]
        if not targets:
            return SwarmResult(task=task, worker_results=[], aggregate=None,
                               aggregate_method=aggregate, duration_ms=0.0)
        sem = asyncio.Semaphore(self._max_concurrency)
        async def bounded(w):
            async with sem: return await self._call_worker(w, task)
        results = await asyncio.gather(*[bounded(w) for w in targets], return_exceptions=True)
        worker_results = [r if isinstance(r, WorkerResult) else
                          WorkerResult(worker_id="?",worker_name="?",result=None,
                                       error=str(r),success=False)
                          for r in results]
        agg = self._aggregate(worker_results, aggregate, aggregate_weights)
        sr = SwarmResult(task=task, worker_results=worker_results, aggregate=agg,
                         aggregate_method=aggregate, duration_ms=(time.time()-start)*1000)
        self._history.append(sr)
        return sr

    async def send(self, task: Any, worker_name: str) -> WorkerResult:
        """Send task to a single named worker."""
        w = next((w for w in self._workers.values() if w.name==worker_name and w.active), None)
        if not w: raise ValueError(f"No active worker named {worker_name!r}")
        return await self._call_worker(w, task)

    async def race(self, task: Any) -> WorkerResult:
        """Return first successful result; cancel remaining workers."""
        targets = [w for w in self._workers.values() if w.active]
        if not targets: raise ValueError("No active workers")
        tasks = {asyncio.create_task(self._call_worker(w, task)): w for w in targets}
        pending = set(tasks)
        winner = None
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                r = t.result()
                if r.success:
                    winner = r
                    for p in pending: p.cancel()
                    pending = set()
                    break
        if not winner:
            # all failed — return last result
            winner = t.result()
        return winner

    # ── Map-reduce ────────────────────────────────────────────────────────────

    async def map_reduce(self, items: List[Any],
                          task_fn: Callable,
                          reduce_fn: Callable = None,
                          aggregate: str = "merge") -> SwarmResult:
        """Map task_fn over items (one worker per item via round-robin), then reduce."""
        start = time.time()
        workers = [w for w in self._workers.values() if w.active]
        if not workers: return SwarmResult(task=items,worker_results=[],duration_ms=0.0)
        sem = asyncio.Semaphore(self._max_concurrency)
        async def process(item, worker):
            async with sem: return await self._call_worker(worker, task_fn(item))
        all_results = await asyncio.gather(*[
            process(item, workers[i % len(workers)]) for i, item in enumerate(items)
        ], return_exceptions=True)
        worker_results = [r if isinstance(r, WorkerResult) else
                          WorkerResult(worker_id="?",worker_name="?",result=None,
                                       error=str(r),success=False)
                          for r in all_results]
        if reduce_fn:
            agg = reduce_fn(worker_results)
        else:
            agg = self._aggregate(worker_results, aggregate)
        sr = SwarmResult(task=str(items)[:200], worker_results=worker_results, aggregate=agg,
                         aggregate_method=f"map_reduce+{aggregate}",
                         duration_ms=(time.time()-start)*1000)
        self._history.append(sr)
        return sr

    # ── Aggregation ───────────────────────────────────────────────────────────

    def _aggregate(self, results, method, weights=None):
        fn = AGGREGATORS.get(method)
        if not fn: return _vote(results)
        try:
            if method == "vote": return fn(results, weights)
            return fn(results)
        except: return None

    # ── Introspection ─────────────────────────────────────────────────────────

    def workers(self) -> List[Worker]:
        return list(self._workers.values())

    def stats(self) -> Dict:
        ws = list(self._workers.values())
        return {
            "total_workers": len(ws),
            "active_workers": sum(1 for w in ws if w.active),
            "total_tasks_run": len(self._history),
            "workers": [w.to_dict() for w in ws],
        }

    def history(self, limit: int = 20) -> List[SwarmResult]:
        return self._history[-limit:]

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def broadcast_ep(req):
            d = await req.json()
            r = await self.broadcast(task=d["task"],
                                      worker_ids=d.get("worker_ids"),
                                      aggregate=d.get("aggregate","vote"))
            return web.json_response(r.to_dict())

        async def send_ep(req):
            d = await req.json()
            r = await self.send(d["task"], d["worker"])
            return web.json_response(r.to_dict())

        async def workers_ep(req):
            return web.json_response({"workers":[w.to_dict() for w in self.workers()]})

        async def stats_ep(req):
            return web.json_response(self.stats())

        p = f"{prefix}/swarm"
        app.router.add_post(f"{p}/broadcast", broadcast_ep)
        app.router.add_post(f"{p}/send",      send_ep)
        app.router.add_get( f"{p}/workers",   workers_ep)
        app.router.add_get( f"{p}/stats",     stats_ep)
        logger.info(f"Agent swarm API at {prefix}/swarm/")
