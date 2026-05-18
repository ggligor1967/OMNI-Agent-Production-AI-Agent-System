"""OMNI AGENT - Multi-Agent Debate: structured deliberation with parallel debaters and consensus synthesis."""
import time, uuid, asyncio, logging
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
logger = logging.getLogger(__name__)

@dataclass
class Debater:
    id: str; name: str; role: str = ""
    system_prompt: str = ""; model: str = ""; temperature: float = 0.7
    def to_dict(self):
        return {"id":self.id,"name":self.name,"role":self.role,"model":self.model,"temperature":self.temperature}

@dataclass
class Turn:
    debater_id: str; debater_name: str; round_num: int; turn_type: str; content: str
    latency_ms: float = 0.0; timestamp: float = field(default_factory=time.time)
    def to_dict(self):
        return {"debater_id":self.debater_id,"debater_name":self.debater_name,
                "round_num":self.round_num,"turn_type":self.turn_type,
                "content":self.content,"latency_ms":round(self.latency_ms,1),"timestamp":self.timestamp}

@dataclass
class DebateResult:
    id: str; topic: str; turns: List[Turn]
    consensus: str = ""; rounds_conducted: int = 0
    duration_ms: float = 0.0; created_at: float = field(default_factory=time.time)
    def transcript(self):
        lines=[f"DEBATE: {self.topic}","="*60]
        for t in self.turns:
            lines.append(f"\n[Round {t.round_num}/{t.turn_type.upper()}] {t.debater_name}:")
            lines.append(t.content)
        if self.consensus: lines+=["","="*60,"CONSENSUS:",self.consensus]
        return "\n".join(lines)
    def to_dict(self):
        return {"id":self.id,"topic":self.topic,"turns":[t.to_dict() for t in self.turns],
                "consensus":self.consensus,"rounds_conducted":self.rounds_conducted,
                "duration_ms":round(self.duration_ms,1),"created_at":self.created_at}

def _expert_panel(topic):
    return [
        Debater(id="optimist",name="Optimist",role="Advocate",
                system_prompt=f"Argue FOR benefits of: {topic}. Be specific."),
        Debater(id="skeptic",name="Skeptic",role="Devil's advocate",
                system_prompt=f"Critically examine: {topic}. Find risks."),
        Debater(id="pragmatist",name="Pragmatist",role="Analyst",
                system_prompt=f"Evaluate practical implementation of: {topic}."),
    ]

def _red_team(topic):
    return [
        Debater(id="attacker",name="Red Teamer",
                system_prompt=f"Find security/safety problems with: {topic}."),
        Debater(id="defender",name="Blue Teamer",
                system_prompt=f"Defend and mitigate issues with: {topic}."),
        Debater(id="auditor",name="Auditor",
                system_prompt=f"Impartially audit both sides on: {topic}."),
    ]

def _socratic(topic):
    return [
        Debater(id="questioner",name="Socrates",
                system_prompt=f"Only ask clarifying questions about: {topic}."),
        Debater(id="answerer",name="Respondent",
                system_prompt=f"Answer questions about {topic} thoughtfully."),
    ]

PRESETS = {"expert_panel":_expert_panel,"red_team":_red_team,"socratic":_socratic}

class MultiAgentDebate:
    """Structured multi-agent deliberation with parallel debaters and consensus synthesis."""
    def __init__(self, llm_fn=None, default_model="claude-sonnet-4-6", max_tokens_per_turn=400):
        self._llm_fn=llm_fn; self._default_model=default_model
        self._max_tokens=max_tokens_per_turn; self._history=[]

    async def _call(self, debater, prompt, history_lines):
        start=time.time()
        ctx="\n".join(history_lines[-6:])
        full=(f"[System: {debater.system_prompt}]\n\n"
              +(f"Recent:\n{ctx}\n\n" if ctx else "")
              +f"Your turn as {debater.name}:\n{prompt}")
        try:
            fn=self._llm_fn
            resp=await fn(full) if (fn and asyncio.iscoroutinefunction(fn)) else (fn(full) if fn else f"[{debater.name}] Response: {prompt[:50]}...")
        except Exception as e:
            resp=f"[{debater.name} error: {e}]"
        return str(resp),(time.time()-start)*1000

    async def _run_round(self, debaters, prompt, rnum, ttype, history_lines):
        results=await asyncio.gather(*[self._call(d,prompt,history_lines) for d in debaters],return_exceptions=True)
        turns=[]
        for d,res in zip(debaters,results):
            content,lat=(f"[Error:{res}]",0.0) if isinstance(res,Exception) else res
            turns.append(Turn(debater_id=d.id,debater_name=d.name,round_num=rnum,turn_type=ttype,content=content,latency_ms=lat))
        return turns

    async def run(self, topic, panel=None, preset="expert_panel", rounds=2, synthesize=True, synthesis_prompt=None):
        start=time.time()
        debaters=panel or PRESETS.get(preset,_expert_panel)(topic)
        all_turns=[]; history_lines=[]
        # Round 1: openings
        op=await self._run_round(debaters,f"Topic: {topic}\n\nGive your opening position in 3-4 sentences.",1,"opening",[])
        all_turns.extend(op)
        for t in op: history_lines.append(f"{t.debater_name}: {t.content}")
        # Rounds 2+: rebuttals
        for r in range(2,rounds+1):
            prev="\n".join(history_lines[-len(debaters)*2:])
            rb=await self._run_round(debaters,
                f"Topic: {topic}\n\nPrevious:\n{prev}\n\nRespond to others, defend or update your position (3-4 sentences).",
                r,"rebuttal",history_lines)
            all_turns.extend(rb)
            for t in rb: history_lines.append(f"{t.debater_name}: {t.content}")
        # Synthesis
        consensus=""
        if synthesize and self._llm_fn:
            tx="\n\n".join(f"{t.debater_name} ({t.turn_type}): {t.content}" for t in all_turns)
            syn=synthesis_prompt or f"Synthesize this debate about '{topic}' into a balanced consensus.\n\nTranscript:\n{tx}\n\nConsensus:"
            try:
                fn=self._llm_fn
                consensus=await fn(syn) if asyncio.iscoroutinefunction(fn) else fn(syn)
            except Exception as e:
                consensus=f"[Synthesis error: {e}]"
        result=DebateResult(id=str(uuid.uuid4())[:12],topic=topic,turns=all_turns,
                            consensus=str(consensus),rounds_conducted=rounds,
                            duration_ms=(time.time()-start)*1000)
        self._history.append(result)
        logger.info(f"Debate '{topic[:50]}': {rounds} rounds, {len(debaters)} debaters ({result.duration_ms:.0f}ms)")
        return result

    def history(self, limit=20): return self._history[-limit:]

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def run_ep(req):
            d=await req.json()
            pd=d.get("panel")
            panel=None
            if pd:
                panel=[Debater(id=x.get("id",str(uuid.uuid4())[:6]),name=x["name"],
                               role=x.get("role",""),system_prompt=x.get("system_prompt",""),
                               model=x.get("model",""),temperature=float(x.get("temperature",0.7))) for x in pd]
            r=await self.run(topic=d["topic"],panel=panel,preset=d.get("preset","expert_panel"),
                              rounds=int(d.get("rounds",2)),synthesize=bool(d.get("synthesize",True)))
            return web.json_response(r.to_dict(),status=201)
        async def presets_ep(req): return web.json_response({"presets":list(PRESETS.keys())})
        async def history_ep(req):
            limit=int(req.rel_url.query.get("limit",10))
            return web.json_response({"debates":[r.to_dict() for r in self.history(limit)]})
        p=f"{prefix}/debate"
        app.router.add_post(f"{p}/run",run_ep)
        app.router.add_get(f"{p}/presets",presets_ep)
        app.router.add_get(f"{p}/history",history_ep)
        logger.info(f"Debate API at {prefix}/debate/")
