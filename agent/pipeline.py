"""
OMNI AGENT - Pipeline Executor
Multi-step agentic task chains with tool orchestration, branching,
retries, state management, and result aggregation.
"""
import time
import uuid
import json
import logging
import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

from agent.hooks import hooks, Event, EventType

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════

class StepStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCESS   = "success"
    FAILED    = "failed"
    SKIPPED   = "skipped"
    RETRYING  = "retrying"


@dataclass
class StepResult:
    step_id: str
    name: str
    status: StepStatus
    output: Any = None
    error: str = ""
    duration_ms: float = 0.0
    retries: int = 0
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "status": self.status.value,
            "output": str(self.output)[:500] if self.output else None,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
            "retries": self.retries,
        }


@dataclass
class PipelineRun:
    run_id: str
    pipeline_name: str
    status: StepStatus = StepStatus.PENDING
    steps: List[StepResult] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    error: str = ""

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    def to_dict(self) -> Dict:
        return {
            "run_id": self.run_id,
            "pipeline": self.pipeline_name,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE STEP
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PipelineStep:
    """
    A single step in a pipeline.

    handler: async fn(context: Dict) -> Any
             context is shared across all steps; write outputs to context.
    condition: async fn(context: Dict) -> bool — skip step if False
    on_error: 'fail' | 'skip' | 'retry'
    input_map: rename context keys before passing to handler
                e.g. {"search_result": "web_content"} maps context["search_result"] -> "web_content"
    output_key: store handler return value in context under this key
    """
    name: str
    handler: Callable
    description: str = ""
    condition: Optional[Callable] = None
    on_error: str = "fail"          # 'fail' | 'skip' | 'retry'
    max_retries: int = 2
    retry_delay: float = 1.0
    timeout: float = 60.0
    input_map: Dict[str, str] = field(default_factory=dict)
    output_key: str = ""
    tags: List[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE DEFINITION
# ══════════════════════════════════════════════════════════════════════════════

class Pipeline:
    """
    A named sequence of PipelineSteps with shared execution context.

    Context is a shared dict passed to every step. Steps can read
    outputs from previous steps and write their own results.

    Example:
        pipeline = Pipeline("research")
        pipeline.step("search", handler=search_fn, output_key="results")
        pipeline.step("summarize", handler=summarize_fn,
                      input_map={"results": "content"})
        run = await executor.run(pipeline, {"query": "latest AI news"})
    """

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._steps: List[PipelineStep] = []

    def step(self, name: str, handler: Callable,
             description: str = "",
             condition: Callable = None,
             on_error: str = "fail",
             max_retries: int = 0,
             retry_delay: float = 1.0,
             timeout: float = 60.0,
             output_key: str = "",
             input_map: Dict = None,
             tags: List[str] = None) -> "Pipeline":
        """Add a step. Returns self for chaining."""
        self._steps.append(PipelineStep(
            name=name, handler=handler, description=description,
            condition=condition, on_error=on_error,
            max_retries=max_retries, retry_delay=retry_delay,
            timeout=timeout,
            output_key=output_key or name,
            input_map=input_map or {},
            tags=tags or [],
        ))
        return self

    def __repr__(self) -> str:
        return f"Pipeline({self.name!r}, steps={len(self._steps)})"


# ══════════════════════════════════════════════════════════════════════════════
# EXECUTOR
# ══════════════════════════════════════════════════════════════════════════════

class PipelineExecutor:
    """
    Executes Pipeline instances with:
    - Shared mutable context dict
    - Per-step timeout enforcement
    - Retry with exponential backoff
    - Conditional step skipping
    - Hook events for every step
    - Full execution audit trail
    """

    def __init__(self):
        self._runs: Dict[str, PipelineRun] = {}
        self._pipelines: Dict[str, Pipeline] = {}

    def register(self, pipeline: Pipeline):
        self._pipelines[pipeline.name] = pipeline
        logger.info(f"Pipeline registered: '{pipeline.name}' ({len(pipeline._steps)} steps)")

    def get_pipeline(self, name: str) -> Optional[Pipeline]:
        return self._pipelines.get(name)

    async def run(self, pipeline: Pipeline,
                  initial_context: Dict[str, Any] = None) -> PipelineRun:
        """Execute all steps sequentially. Returns a PipelineRun with full trace."""
        run = PipelineRun(
            run_id=str(uuid.uuid4())[:8],
            pipeline_name=pipeline.name,
            context=dict(initial_context or {}),
        )
        self._runs[run.run_id] = run
        run.status = StepStatus.RUNNING

        logger.info(f"Pipeline '{pipeline.name}' started [run={run.run_id}]")

        try:
            for step in pipeline._steps:
                step_result = await self._execute_step(step, run)
                run.steps.append(step_result)

                if step_result.status == StepStatus.FAILED:
                    run.status = StepStatus.FAILED
                    run.error = step_result.error
                    break
            else:
                run.status = StepStatus.SUCCESS

        except Exception as e:
            run.status = StepStatus.FAILED
            run.error = str(e)
            logger.error(f"Pipeline '{pipeline.name}' crashed: {e}")

        run.finished_at = time.time()
        logger.info(
            f"Pipeline '{pipeline.name}' {run.status.value} "
            f"[{run.duration_ms:.0f}ms, run={run.run_id}]"
        )
        return run

    async def run_by_name(self, name: str,
                          context: Dict = None) -> Optional[PipelineRun]:
        pipeline = self.get_pipeline(name)
        if not pipeline:
            logger.error(f"Pipeline '{name}' not found")
            return None
        return await self.run(pipeline, context)

    async def _execute_step(self, step: PipelineStep,
                             run: PipelineRun) -> StepResult:
        step_id = str(uuid.uuid4())[:6]
        result = StepResult(step_id=step_id, name=step.name,
                           status=StepStatus.PENDING)

        # Evaluate condition
        if step.condition:
            try:
                cond = step.condition
                should_run = (await cond(run.context)
                             if asyncio.iscoroutinefunction(cond)
                             else cond(run.context))
                if not should_run:
                    result.status = StepStatus.SKIPPED
                    logger.debug(f"Step '{step.name}' skipped (condition=False)")
                    return result
            except Exception as e:
                logger.warning(f"Step '{step.name}' condition error: {e}")

        result.status = StepStatus.RUNNING
        await hooks.emit(Event(EventType.TOOL_CALLED, {
            "pipeline": run.pipeline_name,
            "step": step.name,
            "run_id": run.run_id,
        }))

        # Build step-local context with input_map
        step_ctx = dict(run.context)
        for src, dst in step.input_map.items():
            if src in step_ctx:
                step_ctx[dst] = step_ctx.pop(src)

        # Execute with retries
        attempt = 0
        last_error = ""
        start = time.time()

        while attempt <= step.max_retries:
            if attempt > 0:
                result.status = StepStatus.RETRYING
                delay = step.retry_delay * (2 ** (attempt - 1))
                logger.info(f"Step '{step.name}' retry {attempt}/{step.max_retries} "
                           f"(delay={delay:.1f}s)")
                await asyncio.sleep(delay)

            try:
                handler = step.handler
                output = await asyncio.wait_for(
                    handler(step_ctx) if asyncio.iscoroutinefunction(handler)
                    else asyncio.to_thread(handler, step_ctx),
                    timeout=step.timeout,
                )

                result.duration_ms = (time.time() - start) * 1000
                result.status = StepStatus.SUCCESS
                result.output = output
                result.retries = attempt

                # Write output back to shared context
                if output is not None and step.output_key:
                    run.context[step.output_key] = output

                await hooks.emit(Event(EventType.TOOL_RESULT, {
                    "step": step.name, "success": True,
                }))
                logger.debug(f"Step '{step.name}' ✓ ({result.duration_ms:.0f}ms)")
                return result

            except asyncio.TimeoutError:
                last_error = f"Timeout after {step.timeout}s"
                logger.warning(f"Step '{step.name}' timed out")
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"Step '{step.name}' error: {e}")

            attempt += 1

        # All attempts failed
        result.duration_ms = (time.time() - start) * 1000
        result.error = last_error
        result.retries = attempt - 1

        await hooks.emit(Event(EventType.TOOL_ERROR, {
            "step": step.name, "error": last_error
        }))

        if step.on_error == "skip":
            result.status = StepStatus.SKIPPED
            logger.info(f"Step '{step.name}' failed → skipped (on_error=skip)")
        elif step.on_error == "fail":
            result.status = StepStatus.FAILED
            logger.error(f"Step '{step.name}' FAILED: {last_error}")
        else:
            result.status = StepStatus.FAILED

        return result

    # ── Utility ───────────────────────────────────────────────────────────────

    def get_run(self, run_id: str) -> Optional[PipelineRun]:
        return self._runs.get(run_id)

    def list_runs(self, pipeline_name: Optional[str] = None) -> List[Dict]:
        runs = list(self._runs.values())
        if pipeline_name:
            runs = [r for r in runs if r.pipeline_name == pipeline_name]
        return [r.to_dict() for r in sorted(runs, key=lambda r: r.started_at, reverse=True)]

    def list_pipelines(self) -> List[Dict]:
        return [
            {"name": p.name, "description": p.description,
             "steps": len(p._steps)}
            for p in self._pipelines.values()
        ]


# ══════════════════════════════════════════════════════════════════════════════
# BUILT-IN PIPELINES
# ══════════════════════════════════════════════════════════════════════════════

def build_research_pipeline(agent) -> Pipeline:
    """
    Research pipeline: search → scrape top result → summarize → store memory.
    Requires an OmniAgent instance for tool access.
    """
    pipeline = Pipeline("research", "Web research with auto-summarization")

    async def search(ctx: Dict) -> List:
        query = ctx.get("query", "")
        return await agent.scraper.search(query, num_results=3)

    async def scrape_top(ctx: Dict) -> str:
        results = ctx.get("search", [])
        if not results:
            return ""
        url = results[0].get("url", "")
        if not url:
            return results[0].get("snippet", "")
        page = await agent.scraper.fetch(url)
        return page.get("body", "")[:3000]

    async def summarize(ctx: Dict) -> str:
        content = ctx.get("scrape_top", "")
        query = ctx.get("query", "")
        if not content:
            return "No content retrieved."
        messages = [{"role": "user",
                     "content": f"Summarize this content relevant to: {query}\n\n{content}"}]
        resp = await agent.llm.chat(messages, session_id="pipeline:research")
        return resp.get("content", "")

    async def store_result(ctx: Dict) -> str:
        summary = ctx.get("summarize", "")
        query = ctx.get("query", "")
        if summary:
            agent.memory.save_memory(
                f"research:{query[:32]}",
                summary, category="research", importance=6
            )
        return f"Stored: research:{query[:32]}"

    pipeline.step("search", search,
                  description="Search the web for the query",
                  output_key="search", on_error="skip")
    pipeline.step("scrape_top", scrape_top,
                  description="Scrape the top search result",
                  output_key="scrape_top", on_error="skip")
    pipeline.step("summarize", summarize,
                  description="Summarize the retrieved content",
                  condition=lambda ctx: bool(ctx.get("scrape_top")),
                  output_key="summarize", on_error="fail", max_retries=1)
    pipeline.step("store_result", store_result,
                  description="Persist summary to memory",
                  output_key="stored")

    return pipeline


def build_code_pipeline(agent) -> Pipeline:
    """
    Code generation pipeline: plan → generate → review → test.
    """
    pipeline = Pipeline("code_gen", "Generate, review, and test code")

    async def plan(ctx: Dict) -> str:
        task = ctx.get("task", "")
        msgs = [{"role": "user",
                 "content": f"Create a brief implementation plan for: {task}"}]
        resp = await agent.llm.chat(msgs, session_id="pipeline:code")
        return resp.get("content", "")

    async def generate(ctx: Dict) -> str:
        task = ctx.get("task", "")
        plan = ctx.get("plan", "")
        msgs = [{"role": "user",
                 "content": (f"Implement the following. Return ONLY the code.\n\n"
                            f"TASK: {task}\n\nPLAN:\n{plan}")}]
        resp = await agent.llm.chat(msgs, model="qwen3-coder-next:cloud",
                                    session_id="pipeline:code")
        return resp.get("content", "")

    async def extract_code(ctx: Dict) -> str:
        import re
        raw = ctx.get("generate", "")
        match = re.search(r'```(?:python|py)?\n(.*?)```', raw, re.DOTALL)
        return match.group(1).strip() if match else raw

    async def review(ctx: Dict) -> str:
        code = ctx.get("extract_code", "")
        if not code:
            return "No code to review"
        msgs = [{"role": "user",
                 "content": f"Review this code for bugs and improvements:\n\n```python\n{code}\n```"}]
        resp = await agent.llm.chat(msgs, model="devstral-2:123b-cloud",
                                    session_id="pipeline:code")
        return resp.get("content", "")

    async def execute_code(ctx: Dict) -> Dict:
        code = ctx.get("extract_code", "")
        if not code:
            return {"success": False, "error": "No code"}
        return (await agent.sandbox.run_python(code)).to_dict()

    pipeline.step("plan", plan, output_key="plan", on_error="skip")
    pipeline.step("generate", generate, output_key="generate", on_error="fail", max_retries=1)
    pipeline.step("extract_code", extract_code, output_key="extract_code")
    pipeline.step("review", review, output_key="review",
                  condition=lambda ctx: bool(ctx.get("extract_code")),
                  on_error="skip")
    pipeline.step("execute_code", execute_code, output_key="execution",
                  condition=lambda ctx: bool(ctx.get("extract_code")),
                  on_error="skip")

    return pipeline


def build_job_search_pipeline() -> Pipeline:
    """
    Pipeline wrapper around the improved ADR tanker Switzerland job search.

    Context keys:
    - export_format: json | csv | html | all (default: html)
    - verbose: bool (default: False)
    - output_dir: optional custom output directory
    """
    pipeline = Pipeline(
        "job_search_tank_adr_improved",
        "Run the improved ADR tanker Switzerland search and export reports",
    )

    async def run_job_search(ctx: Dict) -> Dict:
        from job_search_tank_adr_improved import run_search_with_summary

        export_format = str(ctx.get("export_format", "html") or "html").strip().lower()
        if export_format not in {"json", "csv", "html", "all"}:
            raise ValueError(
                "export_format must be one of: json, csv, html, all"
            )

        verbose = bool(ctx.get("verbose", False))
        output_dir = ctx.get("output_dir") or None
        return await run_search_with_summary(
            export_format=export_format,
            verbose=verbose,
            output_dir=output_dir,
        )

    pipeline.step(
        "run_job_search",
        run_job_search,
        description="Execute the improved ADR tanker / liquid bulk job search",
        output_key="job_search_result",
        on_error="fail",
        timeout=900.0,
    )

    return pipeline
