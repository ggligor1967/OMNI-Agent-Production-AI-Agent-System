"""OMNI AGENT - Tool Composer: chain/parallel/conditional tool pipelines with retry and fallback."""
import time, uuid, asyncio, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
logger = logging.getLogger(__name__)

class StepType(str, Enum):
    TOOL="tool"; TRANSFORM="transform"; FILTER="filter"; PARALLEL="parallel"; BRANCH="branch"

class StepStatus(str, Enum):
    PENDING="pending"; RUNNING="running"; DONE="done"; FAILED="failed"; SKIPPED="skipped"

@dataclass
class StepDef:
    id: str; name: str; step_type: StepType = StepType.TOOL
    tool_name: str = ""; tool_args: Dict = field(default_factory=dict)
    transform_fn: Optional[Callable] = None; filter_fn: Optional[Callable] = None
    condition_fn: Optional[Callable] = None; true_step: Optional["StepDef"] = None
    false_step: Optional["StepDef"] = None; parallel_steps: List["StepDef"] = field(default_factory=list)
    max_retries: int = 1; timeout_s: float = 30.0; fallback_value: Any = None; description: str = ""
    def to_dict(self):
        return {"id":self.id,"name":self.name,"step_type":self.step_type,
                "tool_name":self.tool_name,"max_retries":self.max_retries,"timeout_s":self.timeout_s}

@dataclass
class StepTrace:
    step_id: str; step_name: str; status: StepStatus
    input_data: Any = None; output_data: Any = None; error: str = ""
    retries: int = 0; started_at: float = 0.0; finished_at: float = 0.0
    @property
    def duration_ms(self): return round((self.finished_at-self.started_at)*1000,2) if self.finished_at else 0.0
    def to_dict(self):
        return {"step_id":self.step_id,"step_name":self.step_name,"status":self.status,
                "error":self.error,"retries":self.retries,"duration_ms":self.duration_ms,
                "output_data":str(self.output_data)[:200] if self.output_data is not None else None}

@dataclass
class PipelineResult:
    pipeline_id: str; pipeline_name: str; status: StepStatus; final_output: Any
    traces: List[StepTrace]; duration_ms: float = 0.0; created_at: float = field(default_factory=time.time)
    def to_dict(self):
        return {"pipeline_id":self.pipeline_id,"pipeline_name":self.pipeline_name,
                "status":self.status,"final_output":str(self.final_output)[:500] if self.final_output is not None else None,
                "traces":[t.to_dict() for t in self.traces],"duration_ms":round(self.duration_ms,1),"steps_run":len(self.traces)}

@dataclass
class Pipeline:
    id: str; name: str; steps: List[StepDef]; description: str = ""; timeout_s: float = 120.0
    def to_dict(self):
        return {"id":self.id,"name":self.name,"description":self.description,
                "steps":[s.to_dict() for s in self.steps],"timeout_s":self.timeout_s}

class ToolComposer:
    """Dynamic tool pipeline composition with chain, parallel, branch, transform, and filter steps."""
    def __init__(self):
        self._tools: Dict[str,Callable] = {}
        self._pipelines: Dict[str,Pipeline] = {}
        self._history: List[PipelineResult] = []

    def register_tool(self, name, fn): self._tools[name]=fn; logger.debug(f"Tool: {name}")

    def tool(self, name):
        def dec(fn): self.register_tool(name,fn); return fn
        return dec

    def step(self, name, tool_type="", tool="", args=None, max_retries=1, timeout_s=30.0, fallback=None):
        return StepDef(id=str(uuid.uuid4())[:8],name=name,step_type=StepType.TOOL,
                       tool_name=tool or tool_type,tool_args=args or {},
                       max_retries=max_retries,timeout_s=timeout_s,fallback_value=fallback)

    def transform(self, name, fn):
        return StepDef(id=str(uuid.uuid4())[:8],name=name,step_type=StepType.TRANSFORM,transform_fn=fn)

    def filter_step(self, name, fn):
        return StepDef(id=str(uuid.uuid4())[:8],name=name,step_type=StepType.FILTER,filter_fn=fn)

    def parallel_step(self, name, steps):
        return StepDef(id=str(uuid.uuid4())[:8],name=name,step_type=StepType.PARALLEL,parallel_steps=steps)

    def branch(self, name, condition, true_step, false_step):
        return StepDef(id=str(uuid.uuid4())[:8],name=name,step_type=StepType.BRANCH,
                       condition_fn=condition,true_step=true_step,false_step=false_step)

    def build_pipeline(self, name, steps, description="", timeout_s=120.0):
        resolved=[]
        for s in steps:
            if isinstance(s, StepDef):
                resolved.append(s)
            elif isinstance(s, dict):
                resolved.append(StepDef(
                    id=s.get("id",str(uuid.uuid4())[:8]),name=s.get("name","step"),
                    step_type=StepType.TOOL,tool_name=s.get("tool",s.get("tool_name","")),
                    tool_args=s.get("args",s.get("tool_args",{})),
                    max_retries=s.get("max_retries",1),timeout_s=float(s.get("timeout_s",30.0)),
                    fallback_value=s.get("fallback"),description=s.get("description","")))
        pid=str(uuid.uuid4())[:10]
        p=Pipeline(id=pid,name=name,steps=resolved,description=description,timeout_s=timeout_s)
        self._pipelines[pid]=p; return p

    def get_pipeline(self, pid): return self._pipelines.get(pid)
    def list_pipelines(self): return list(self._pipelines.values())

    async def run(self, pipeline_id, initial_input=None, context=None, dry_run=False):
        pipeline=self._pipelines.get(pipeline_id)
        if not pipeline: raise KeyError(f"Pipeline {pipeline_id!r} not found")
        start=time.time(); traces=[]; current=initial_input; ok=StepStatus.DONE; ctx=context or {}
        try:
            async with asyncio.timeout(pipeline.timeout_s):
                for sdef in pipeline.steps:
                    current,trace=await self._exec(sdef,current,ctx,dry_run)
                    traces.append(trace)
                    if trace.status==StepStatus.FAILED: ok=StepStatus.FAILED; break
        except (asyncio.TimeoutError, TimeoutError):
            ok=StepStatus.FAILED
            traces.append(StepTrace(step_id="timeout",step_name="pipeline_timeout",
                status=StepStatus.FAILED,error=f"Pipeline exceeded {pipeline.timeout_s}s",
                started_at=start,finished_at=time.time()))
        r=PipelineResult(pipeline_id=pipeline_id,pipeline_name=pipeline.name,status=ok,
                          final_output=current,traces=traces,duration_ms=(time.time()-start)*1000)
        self._history.append(r); return r

    async def _exec(self, s, inp, ctx, dry_run):
        tr=StepTrace(step_id=s.id,step_name=s.name,status=StepStatus.RUNNING,input_data=inp,started_at=time.time())
        if dry_run:
            tr.status=StepStatus.DONE; tr.output_data=inp; tr.finished_at=time.time(); return inp,tr
        try:
            if s.step_type==StepType.TRANSFORM:
                fn=s.transform_fn; out=await fn(inp) if asyncio.iscoroutinefunction(fn) else fn(inp)
            elif s.step_type==StepType.FILTER:
                fn=s.filter_fn; keep=await fn(inp) if asyncio.iscoroutinefunction(fn) else fn(inp)
                out=inp if keep else None
            elif s.step_type==StepType.BRANCH:
                fn=s.condition_fn; cond=await fn(inp) if asyncio.iscoroutinefunction(fn) else fn(inp)
                ns=s.true_step if cond else s.false_step
                if ns:
                    out,sub=await self._exec(ns,inp,ctx,dry_run)
                    tr.output_data=out; tr.status=sub.status; tr.finished_at=time.time(); return out,tr
                else: out=inp
            elif s.step_type==StepType.PARALLEL:
                results=await asyncio.gather(*[self._exec(sub,inp,ctx,dry_run) for sub in s.parallel_steps],return_exceptions=True)
                out=[r[0] for r in results if not isinstance(r,Exception)]
            else:
                out=await self._call_tool(s,inp,ctx)
            tr.output_data=out; tr.status=StepStatus.DONE
        except Exception as e:
            tr.error=str(e)[:300]
            if s.fallback_value is not None:
                tr.status=StepStatus.DONE; tr.output_data=s.fallback_value; out=s.fallback_value
            else:
                tr.status=StepStatus.FAILED; out=None
        tr.finished_at=time.time(); return out,tr

    async def _call_tool(self, s, inp, ctx):
        fn=self._tools.get(s.tool_name)
        if not fn: raise ValueError(f"Tool {s.tool_name!r} not registered")
        args={**s.tool_args,"input":inp,**ctx}; retries=0
        while True:
            try:
                async with asyncio.timeout(s.timeout_s):
                    return await fn(**args) if asyncio.iscoroutinefunction(fn) else fn(**args)
            except (asyncio.TimeoutError, TimeoutError): raise TimeoutError(f"Tool {s.tool_name!r} timed out")
            except Exception:
                retries+=1
                if retries>s.max_retries: raise
                await asyncio.sleep(0.1*2**retries)

    async def run_chain(self, tool_names, initial_input=None, args_per_tool=None):
        apts=args_per_tool or {}
        p=self.build_pipeline("ad_hoc_chain",[{"name":t,"tool":t,"args":apts.get(t,{})} for t in tool_names])
        return await self.run(p.id, initial_input)

    async def run_parallel(self, tool_names, initial_input=None):
        subs=[self.step(t,tool=t) for t in tool_names]
        ps=self.parallel_step("parallel",subs)
        p=self.build_pipeline("ad_hoc_parallel",[ps])
        return await self.run(p.id, initial_input)

    def history(self, limit=20): return self._history[-limit:]

    def stats(self):
        return {"registered_tools":list(self._tools.keys()),
                "registered_pipelines":len(self._pipelines),
                "executions":len(self._history),"tool_count":len(self._tools)}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def run_ep(req):
            d=await req.json(); pid=d.get("pipeline_id")
            if not pid:
                p=self.build_pipeline("ad_hoc",d.get("steps",[])); pid=p.id
            r=await self.run(pid,initial_input=d.get("input"),context=d.get("context",{}),dry_run=bool(d.get("dry_run",False)))
            return web.json_response(r.to_dict())
        async def list_ep(req): return web.json_response({"pipelines":[p.to_dict() for p in self.list_pipelines()]})
        async def stats_ep(req): return web.json_response(self.stats())
        p=f"{prefix}/compose"
        app.router.add_post(f"{p}/run",run_ep); app.router.add_get(f"{p}/pipelines",list_ep)
        app.router.add_get(f"{p}/stats",stats_ep)
        logger.info(f"Tool composer API at {prefix}/compose/")
