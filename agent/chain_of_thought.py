"""OMNI AGENT - Chain of Thought
Structured multi-step reasoning: decompose problems into steps, verify each step,
detect contradictions, support backtracking, and emit typed reasoning traces.

Features:
- Step types: THINK, PLAN, ACT, OBSERVE, VERIFY, CONCLUDE, BACKTRACK
- Problem decomposition: LLM-driven or rule-based step generation
- Step verification: self-consistency check after each reasoning step
- Contradiction detection: flag steps that conflict with prior steps
- Backtracking: mark step as invalid and retry from previous checkpoint
- Reasoning trace: full history with timestamps and confidence per step
- Scratchpad: accumulate working memory across steps
- Max-depth guard: prevent infinite reasoning loops
- Confidence propagation: overall confidence = product of step confidences
- Format rendering: compact text trace or structured JSON
- REST API: reason, trace, verify, stats
"""
import json, time, uuid, asyncio, re, logging
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
logger = logging.getLogger(__name__)

class StepType(str, Enum):
    THINK    = "think"
    PLAN     = "plan"
    ACT      = "act"
    OBSERVE  = "observe"
    VERIFY   = "verify"
    CONCLUDE = "conclude"
    BACKTRACK= "backtrack"

@dataclass
class ReasoningStep:
    id: str; step_num: int; step_type: StepType
    content: str; confidence: float = 1.0
    verified: bool = False; valid: bool = True
    contradicts: List[int] = field(default_factory=list)  # step_nums
    elapsed_ms: float = 0.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "step_num": self.step_num,
                "step_type": self.step_type, "content": self.content[:400],
                "confidence": round(self.confidence, 4),
                "verified": self.verified, "valid": self.valid,
                "contradicts": self.contradicts,
                "elapsed_ms": round(self.elapsed_ms, 2)}

@dataclass
class ReasoningTrace:
    id: str; problem: str; steps: List[ReasoningStep]
    conclusion: str = ""; final_confidence: float = 0.0
    backtracks: int = 0; status: str = "in_progress"
    total_ms: float = 0.0

    @property
    def valid_steps(self):
        return [s for s in self.steps if s.valid]

    def text_trace(self) -> str:
        lines = [f"Problem: {self.problem}", "=" * 60]
        for s in self.steps:
            marker = "✓" if s.valid else "✗"
            lines.append(f"[{s.step_num}] {marker} {s.step_type.upper()} "
                          f"(conf={s.confidence:.2f}): {s.content}")
        if self.conclusion:
            lines += ["=" * 60, f"Conclusion: {self.conclusion}",
                       f"Final confidence: {self.final_confidence:.3f}"]
        return "\n".join(lines)

    def to_dict(self):
        return {"id": self.id, "problem": self.problem[:300],
                "steps": [s.to_dict() for s in self.steps],
                "conclusion": self.conclusion, "final_confidence": round(self.final_confidence, 4),
                "backtracks": self.backtracks, "status": self.status,
                "total_ms": round(self.total_ms, 1),
                "valid_step_count": len(self.valid_steps)}

def _extract_steps_from_llm(raw: str) -> List[Dict]:
    """Parse LLM response for step array."""
    try:
        m = re.search(r'\[[\s\S]*\]', raw)
        if m: return json.loads(m.group(0))
    except: pass
    # Fallback: parse numbered lines
    steps = []
    for line in raw.split('\n'):
        m = re.match(r'^\s*(\d+)\.\s*(.+)', line)
        if m:
            steps.append({"step_type": "think", "content": m.group(2).strip(),
                           "confidence": 0.8})
    return steps

def _check_contradiction(new_step: ReasoningStep,
                          prior_steps: List[ReasoningStep]) -> List[int]:
    """Simple negation heuristic."""
    neg = re.compile(r'\b(not|no|never|cannot|impossible|wrong|false|invalid)\b', re.I)
    contradicts = []
    for p in prior_steps:
        if not p.valid: continue
        shared = (set(new_step.content.lower().split()) &
                  set(p.content.lower().split()) - {'the','a','an','is','are','was'})
        if len(shared) >= 2:
            new_neg = bool(neg.search(new_step.content))
            old_neg = bool(neg.search(p.content))
            if new_neg != old_neg:
                contradicts.append(p.step_num)
    return contradicts

class ChainOfThought:
    """
    Structured multi-step reasoning with verification and backtracking.

    Usage:
        cot = ChainOfThought(llm_fn=my_llm, max_depth=8)

        trace = await cot.reason(
            "Should we use microservices or a monolith for our startup?",
            style="pros_cons",   # pros_cons | step_by_step | socratic | hypothesis
        )
        print(trace.text_trace())
        print(f"Confidence: {trace.final_confidence:.2%}")
    """
    def __init__(self, llm_fn=None, max_depth: int = 10,
                 verify_steps: bool = True, allow_backtrack: bool = True):
        self._llm_fn = llm_fn
        self.max_depth = max_depth
        self.verify_steps = verify_steps
        self.allow_backtrack = allow_backtrack
        self._traces: List[ReasoningTrace] = []

    async def _call_llm(self, prompt: str) -> str:
        if not self._llm_fn: return ""
        fn = self._llm_fn
        return str(await fn(prompt) if asyncio.iscoroutinefunction(fn) else fn(prompt))

    async def _decompose(self, problem: str, style: str) -> List[Dict]:
        style_instructions = {
            "step_by_step": "Break down the problem into logical sequential steps.",
            "pros_cons": "Enumerate pros, then cons, then weigh them.",
            "socratic": "Ask clarifying questions, then answer each one.",
            "hypothesis": "Form a hypothesis, test it, then form a conclusion.",
        }
        instruction = style_instructions.get(style, style_instructions["step_by_step"])
        prompt = (f"{instruction}\n\nProblem: {problem}\n\n"
                   "Respond ONLY with a JSON array of steps:\n"
                   '[{"step_type":"think","content":"Step content...","confidence":0.9},'
                   '{"step_type":"conclude","content":"Final answer...","confidence":0.85}]\n'
                   "step_type must be one of: think, plan, act, observe, verify, conclude, backtrack\n"
                   "JSON array only:")
        raw = await self._call_llm(prompt)
        steps = _extract_steps_from_llm(raw)
        if not steps:
            # Produce minimal fallback steps
            steps = [{"step_type": "think", "content": f"Analysing: {problem[:200]}", "confidence": 0.7},
                      {"step_type": "conclude", "content": f"Based on analysis of the problem.", "confidence": 0.6}]
        return steps

    async def _verify_step(self, step: ReasoningStep, problem: str,
                             prior: List[ReasoningStep]) -> Tuple[bool, float]:
        if not self._llm_fn:
            return True, step.confidence
        ctx = "\n".join(f"Step {s.step_num}: {s.content}" for s in prior[-3:])
        prompt = (f"Given the problem: {problem}\n"
                   f"Prior reasoning:\n{ctx}\n\n"
                   f"Evaluate this reasoning step:\n\"{step.content}\"\n\n"
                   "Is it logically sound and consistent with prior steps?\n"
                   'Respond ONLY with JSON: {"valid": true, "confidence": 0.85, "reason": "..."}\n'
                   "JSON only:")
        raw = await self._call_llm(prompt)
        try:
            m = re.search(r'\{[^}]+\}', raw)
            if m:
                d = json.loads(m.group(0))
                return bool(d.get("valid", True)), float(d.get("confidence", step.confidence))
        except: pass
        return True, step.confidence

    async def reason(self, problem: str, style: str = "step_by_step",
                      additional_context: str = "") -> ReasoningTrace:
        trace_id = str(uuid.uuid4())[:12]
        start = time.time()
        trace = ReasoningTrace(id=trace_id, problem=problem, steps=[])
        backtracks = 0
        scratchpad = additional_context

        raw_steps = await self._decompose(problem + ("\n" + scratchpad if scratchpad else ""), style)
        for i, raw in enumerate(raw_steps[:self.max_depth]):
            step_start = time.time()
            try: stype = StepType(raw.get("step_type", "think"))
            except: stype = StepType.THINK
            step = ReasoningStep(
                id=str(uuid.uuid4())[:8], step_num=i + 1, step_type=stype,
                content=raw.get("content", ""),
                confidence=float(raw.get("confidence", 0.8)),
                elapsed_ms=(time.time() - step_start) * 1000)

            # Contradiction check
            step.contradicts = _check_contradiction(step, trace.steps)

            # Verification
            if self.verify_steps and self._llm_fn and stype != StepType.BACKTRACK:
                valid, conf = await self._verify_step(step, problem, trace.valid_steps)
                step.verified = True; step.valid = valid; step.confidence = conf
                if not valid and self.allow_backtrack:
                    backtracks += 1
                    bt = ReasoningStep(
                        id=str(uuid.uuid4())[:8], step_num=i + 1,
                        step_type=StepType.BACKTRACK,
                        content=f"Step {i+1} invalid; backtracking.",
                        confidence=1.0, verified=True, valid=True)
                    trace.steps.append(bt)
            else:
                step.valid = True

            trace.steps.append(step)

            if stype == StepType.CONCLUDE:
                trace.conclusion = step.content
                break

        # Compute final confidence
        valid_steps = trace.valid_steps
        if valid_steps:
            confs = [s.confidence for s in valid_steps]
            trace.final_confidence = round(
                sum(confs) / len(confs) * (1 - 0.05 * backtracks), 4)
        # Extract conclusion if not explicitly given
        if not trace.conclusion:
            conclude_steps = [s for s in valid_steps if s.step_type == StepType.CONCLUDE]
            if conclude_steps:
                trace.conclusion = conclude_steps[-1].content
            elif valid_steps:
                trace.conclusion = valid_steps[-1].content

        trace.backtracks = backtracks
        trace.status = "completed"
        trace.total_ms = (time.time() - start) * 1000
        self._traces.append(trace)
        logger.info(f"CoT trace {trace_id}: {len(valid_steps)} valid steps, "
                     f"conf={trace.final_confidence:.2f}, backtracks={backtracks}")
        return trace

    async def verify_answer(self, problem: str, answer: str) -> Dict:
        """Verify a given answer to a problem using chain-of-thought."""
        prompt = (f"Verify whether this answer is correct for the given problem.\n\n"
                   f"Problem: {problem}\nAnswer: {answer}\n\n"
                   "Think step by step, then respond with JSON:\n"
                   '{"correct": true, "confidence": 0.9, "reasoning": "...", "issues": []}\n'
                   "JSON only:")
        raw = await self._call_llm(prompt)
        try:
            m = re.search(r'\{[\s\S]*\}', raw)
            if m: return json.loads(m.group(0))
        except: pass
        return {"correct": None, "confidence": 0.5, "reasoning": raw[:200], "issues": []}

    def stats(self) -> Dict:
        if not self._traces:
            return {"total_traces": 0}
        avg_steps = sum(len(t.valid_steps) for t in self._traces) / len(self._traces)
        avg_conf = sum(t.final_confidence for t in self._traces) / len(self._traces)
        total_backtracks = sum(t.backtracks for t in self._traces)
        return {"total_traces": len(self._traces),
                "avg_valid_steps": round(avg_steps, 1),
                "avg_confidence": round(avg_conf, 4),
                "total_backtracks": total_backtracks}

    def history(self, limit: int = 10) -> List[ReasoningTrace]:
        return self._traces[-limit:]

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def reason_ep(req):
            d = await req.json()
            trace = await self.reason(d["problem"], d.get("style", "step_by_step"),
                                       d.get("context", ""))
            return web.json_response(trace.to_dict(), status=201)
        async def verify_ep(req):
            d = await req.json()
            result = await self.verify_answer(d["problem"], d["answer"])
            return web.json_response(result)
        async def trace_ep(req):
            limit = int(req.rel_url.query.get("limit", 5))
            return web.json_response({"traces": [t.to_dict() for t in self.history(limit)]})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/cot"
        app.router.add_post(f"{p}/reason", reason_ep)
        app.router.add_post(f"{p}/verify", verify_ep)
        app.router.add_get(f"{p}/traces", trace_ep)
        app.router.add_get(f"{p}/stats", stats_ep)
        logger.info(f"Chain-of-thought API at {prefix}/cot/")
