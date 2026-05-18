"""OMNI AGENT - LLM Judge
Use an LLM as an automated evaluator: score outputs on rubrics, run pairwise
comparisons, maintain a leaderboard, and calibrate against human labels.

Features:
- Rubric scoring: define criteria with weights; LLM scores each criterion 0-10
- Pairwise comparison: A vs B with reasons; supports Elo-style ranking
- Batch evaluation: score many outputs concurrently
- Calibration: compare LLM scores against human labels; compute correlation
- Leaderboard: track model/prompt performance over time
- Judge personas: strict, lenient, balanced, domain-specific
- Structured verdicts: JSON with per-criterion scores and overall rationale
- Retry on bad JSON: up to 3 attempts before fallback score
- SQLite persistence: all evaluations stored and queryable
- REST API: judge, compare, leaderboard, stats
"""
import json, time, uuid, sqlite3, asyncio, logging, math
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class Criterion:
    name: str; description: str; weight: float = 1.0
    def to_dict(self): return {"name":self.name,"description":self.description,"weight":self.weight}

@dataclass
class CriterionScore:
    criterion: str; score: float; rationale: str = ""; weight: float = 1.0
    def to_dict(self): return {"criterion":self.criterion,"score":round(self.score,2),
                               "rationale":self.rationale,"weight":self.weight}

@dataclass
class Verdict:
    id: str; input_text: str; output_text: str; rubric_name: str
    scores: List[CriterionScore]
    overall_score: float; rationale: str = ""
    judge_model: str = ""; metadata: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def weighted_score(self):
        total_w = sum(s.weight for s in self.scores)
        if not total_w: return self.overall_score
        return sum(s.score * s.weight for s in self.scores) / total_w

    def to_dict(self):
        return {"id":self.id,"rubric_name":self.rubric_name,
                "overall_score":round(self.overall_score,3),
                "weighted_score":round(self.weighted_score,3),
                "rationale":self.rationale[:500],
                "scores":[s.to_dict() for s in self.scores],
                "judge_model":self.judge_model,"created_at":self.created_at}

@dataclass
class Comparison:
    id: str; rubric_name: str
    output_a: str; output_b: str; input_text: str
    winner: str  # "A" | "B" | "tie"
    score_a: float; score_b: float; rationale: str = ""
    created_at: float = field(default_factory=time.time)
    def to_dict(self):
        return {"id":self.id,"rubric_name":self.rubric_name,"winner":self.winner,
                "score_a":round(self.score_a,3),"score_b":round(self.score_b,3),
                "rationale":self.rationale[:400],"created_at":self.created_at}

class JudgeStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()
    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c
    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS verdicts(
                    id TEXT PRIMARY KEY, input_text TEXT, output_text TEXT,
                    rubric_name TEXT, overall_score REAL, weighted_score REAL,
                    rationale TEXT, scores TEXT, judge_model TEXT,
                    metadata TEXT DEFAULT '{}', created_at REAL);
                CREATE TABLE IF NOT EXISTS comparisons(
                    id TEXT PRIMARY KEY, rubric_name TEXT, input_text TEXT,
                    output_a TEXT, output_b TEXT, winner TEXT,
                    score_a REAL, score_b REAL, rationale TEXT, created_at REAL);
                CREATE TABLE IF NOT EXISTS leaderboard(
                    model_name TEXT NOT NULL, rubric_name TEXT NOT NULL,
                    total_score REAL DEFAULT 0, count INTEGER DEFAULT 0,
                    elo REAL DEFAULT 1200, PRIMARY KEY(model_name, rubric_name));
                CREATE INDEX IF NOT EXISTS idx_vr ON verdicts(rubric_name,created_at DESC);
            """)
    def save_verdict(self, v):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO verdicts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (v.id,v.input_text[:2000],v.output_text[:2000],v.rubric_name,
                 v.overall_score,v.weighted_score,v.rationale,
                 json.dumps([s.to_dict() for s in v.scores]),
                 v.judge_model,json.dumps(v.metadata),v.created_at))
    def save_comparison(self, cmp):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO comparisons VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cmp.id,cmp.rubric_name,cmp.input_text[:2000],
                 cmp.output_a[:2000],cmp.output_b[:2000],
                 cmp.winner,cmp.score_a,cmp.score_b,cmp.rationale,cmp.created_at))
    def update_leaderboard(self, model_name, rubric_name, score):
        with self._conn() as c:
            c.execute("""INSERT INTO leaderboard(model_name,rubric_name,total_score,count,elo)
                         VALUES(?,?,?,1,1200)
                         ON CONFLICT(model_name,rubric_name) DO UPDATE SET
                         total_score=total_score+?, count=count+1""",
                (model_name,rubric_name,score,score))
    def update_elo(self, model_a, model_b, rubric, winner):
        K = 32
        with self._conn() as c:
            def get_elo(m):
                row=c.execute("SELECT elo FROM leaderboard WHERE model_name=? AND rubric_name=?",(m,rubric)).fetchone()
                return row["elo"] if row else 1200.0
            ea,eb = get_elo(model_a),get_elo(model_b)
            pa,pb = 1/(1+10**((eb-ea)/400)), 1/(1+10**((ea-eb)/400))
            sa = 1.0 if winner=="A" else (0.5 if winner=="tie" else 0.0)
            sb = 1.0 - sa
            new_ea,new_eb = ea+K*(sa-pa), eb+K*(sb-pb)
            for m,e in [(model_a,new_ea),(model_b,new_eb)]:
                c.execute("INSERT INTO leaderboard(model_name,rubric_name,elo) VALUES(?,?,?) ON CONFLICT DO UPDATE SET elo=?",(m,rubric,e,e))
    def get_leaderboard(self, rubric_name=None):
        with self._conn() as c:
            if rubric_name:
                rows=c.execute("SELECT *,CASE WHEN count>0 THEN total_score/count ELSE 0 END as avg_score FROM leaderboard WHERE rubric_name=? ORDER BY avg_score DESC",(rubric_name,)).fetchall()
            else:
                rows=c.execute("SELECT *,CASE WHEN count>0 THEN total_score/count ELSE 0 END as avg_score FROM leaderboard ORDER BY elo DESC").fetchall()
        return [dict(r) for r in rows]
    def get_verdicts(self, rubric_name=None, limit=50):
        with self._conn() as c:
            if rubric_name:
                rows=c.execute("SELECT * FROM verdicts WHERE rubric_name=? ORDER BY created_at DESC LIMIT ?",(rubric_name,limit)).fetchall()
            else:
                rows=c.execute("SELECT * FROM verdicts ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
        return [dict(r) for r in rows]
    def stats(self):
        with self._conn() as c:
            nv=c.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
            nc=c.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0]
            avg=c.execute("SELECT AVG(overall_score) FROM verdicts").fetchone()[0]
        return {"total_verdicts":nv,"total_comparisons":nc,"avg_score":round(avg or 0,3)}

JUDGE_PERSONAS = {
    "balanced":   "You are a fair, objective evaluator. Score strictly but reasonably.",
    "strict":     "You are a harsh critic. Only award high scores for genuinely exceptional work.",
    "lenient":    "You are an encouraging evaluator. Give benefit of the doubt for minor issues.",
    "technical":  "You are a senior engineer. Focus on correctness, efficiency, and best practices.",
    "creative":   "You are a creative director. Prioritize originality, style, and impact.",
}

DEFAULT_RUBRIC = [
    Criterion("accuracy","Factual correctness and absence of errors",weight=2.0),
    Criterion("relevance","Addresses the question directly",weight=1.5),
    Criterion("clarity","Clear, well-structured, easy to understand",weight=1.0),
    Criterion("completeness","Covers all important aspects",weight=1.0),
]

class LLMJudge:
    """Automated LLM-as-judge evaluator with rubrics, pairwise comparison, and leaderboard."""

    def __init__(self, llm_fn=None, db_path="data/llm_judge.db",
                 judge_model="claude-sonnet-4-6", persona="balanced"):
        self._llm_fn = llm_fn; self._judge_model = judge_model
        self._persona = JUDGE_PERSONAS.get(persona, JUDGE_PERSONAS["balanced"])
        self._store = JudgeStore(db_path)
        self._rubrics: Dict[str, List[Criterion]] = {"default": DEFAULT_RUBRIC}

    def register_rubric(self, name, criteria):
        self._rubrics[name] = [c if isinstance(c,Criterion) else
                                 Criterion(**c) for c in criteria]

    async def _call_llm(self, prompt):
        if not self._llm_fn: return ""
        fn = self._llm_fn
        return await fn(prompt) if asyncio.iscoroutinefunction(fn) else fn(prompt)

    async def judge(self, input_text, output_text, rubric_name="default",
                    model_name="", metadata=None):
        criteria = self._rubrics.get(rubric_name, DEFAULT_RUBRIC)
        criteria_text = "\n".join(f"- {c.name} (weight {c.weight}): {c.description}" for c in criteria)
        prompt = (f"[System: {self._persona}]\n\n"
                  f"Evaluate the following AI response on these criteria (score 0-10 each):\n"
                  f"{criteria_text}\n\n"
                  f"INPUT: {input_text[:500]}\n\nOUTPUT: {output_text[:800]}\n\n"
                  "Respond ONLY with JSON:\n"
                  '{"scores":{"criterion_name":{"score":8.5,"rationale":"..."},...},'
                  '"overall_score":8.0,"overall_rationale":"..."}\n'
                  "JSON only:")
        scores = []; overall = 5.0; rationale = ""
        for attempt in range(3):
            try:
                raw = await self._call_llm(prompt)
                if not raw:
                    scores = [CriterionScore(c.name,5.0,"no llm",c.weight) for c in criteria]
                    overall = 5.0; break
                import re as _re
                m = _re.search(r'\{[\s\S]*\}', raw)
                if not m: raise ValueError("no JSON")
                data = json.loads(m.group(0))
                raw_scores = data.get("scores", {})
                scores = []
                for c in criteria:
                    sd = raw_scores.get(c.name, {})
                    if isinstance(sd, dict):
                        sc = float(sd.get("score", 5.0))
                        rat = sd.get("rationale", "")
                    else:
                        sc = float(sd) if sd else 5.0; rat = ""
                    scores.append(CriterionScore(c.name, max(0,min(10,sc)), rat, c.weight))
                overall = float(data.get("overall_score", 5.0))
                rationale = data.get("overall_rationale", data.get("rationale",""))
                break
            except Exception as e:
                if attempt == 2:
                    scores = [CriterionScore(c.name,5.0,f"parse error: {e}",c.weight) for c in criteria]
                    overall = 5.0

        verdict = Verdict(id=str(uuid.uuid4())[:12], input_text=input_text,
                          output_text=output_text, rubric_name=rubric_name,
                          scores=scores, overall_score=overall, rationale=str(rationale),
                          judge_model=self._judge_model, metadata=metadata or {})
        self._store.save_verdict(verdict)
        if model_name:
            self._store.update_leaderboard(model_name, rubric_name, verdict.weighted_score)
        return verdict

    async def compare(self, input_text, output_a, output_b,
                      rubric_name="default", model_a="A", model_b="B"):
        prompt = (f"[System: {self._persona}]\n\n"
                  f"Compare two AI responses to the same input.\n\n"
                  f"INPUT: {input_text[:500]}\n\n"
                  f"RESPONSE A: {output_a[:600]}\n\nRESPONSE B: {output_b[:600]}\n\n"
                  "Which is better? Respond ONLY with JSON:\n"
                  '{"winner":"A"|"B"|"tie","score_a":8.0,"score_b":7.5,"rationale":"..."}\n'
                  "JSON only:")
        winner, sa, sb, rat = "tie", 5.0, 5.0, ""
        try:
            raw = await self._call_llm(prompt)
            if raw:
                import re as _re
                m = _re.search(r'\{[\s\S]*\}', raw)
                if m:
                    data = json.loads(m.group(0))
                    winner = data.get("winner","tie")
                    sa = float(data.get("score_a",5.0)); sb = float(data.get("score_b",5.0))
                    rat = data.get("rationale","")
        except Exception as e:
            rat = f"error: {e}"
        cmp = Comparison(id=str(uuid.uuid4())[:12], rubric_name=rubric_name,
                         output_a=output_a, output_b=output_b, input_text=input_text,
                         winner=winner, score_a=sa, score_b=sb, rationale=str(rat))
        self._store.save_comparison(cmp)
        self._store.update_elo(model_a, model_b, rubric_name, winner)
        return cmp

    async def batch_judge(self, items, rubric_name="default", concurrency=4):
        sem = asyncio.Semaphore(concurrency)
        async def _j(item):
            async with sem:
                return await self.judge(item.get("input",""), item.get("output",""),
                                         rubric_name=rubric_name,
                                         model_name=item.get("model",""),
                                         metadata=item.get("metadata",{}))
        return await asyncio.gather(*[_j(i) for i in items], return_exceptions=True)

    def calibrate(self, human_labels):
        """Compute Pearson r between LLM scores and human labels.
        human_labels: list of {"verdict_id":..., "human_score": 7.5}"""
        verdicts = {v["id"]: v for v in self._store.get_verdicts(limit=10000)}
        pairs = [(verdicts[l["verdict_id"]]["overall_score"], l["human_score"])
                 for l in human_labels if l["verdict_id"] in verdicts]
        if len(pairs) < 2: return {"r": None, "n": len(pairs)}
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
        num = sum((x-mx)*(y-my) for x,y in pairs)
        den = math.sqrt(sum((x-mx)**2 for x in xs)*sum((y-my)**2 for y in ys))
        r = num/den if den else 0.0
        return {"r":round(r,4),"n":len(pairs)}

    def leaderboard(self, rubric_name=None): return self._store.get_leaderboard(rubric_name)
    def get_verdicts(self, rubric_name=None, limit=50): return self._store.get_verdicts(rubric_name,limit)
    def stats(self): return self._store.stats()

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def judge_ep(req):
            d=await req.json()
            v=await self.judge(d["input"],d["output"],d.get("rubric","default"),d.get("model",""),d.get("metadata",{}))
            return web.json_response(v.to_dict(),status=201)
        async def compare_ep(req):
            d=await req.json()
            c=await self.compare(d["input"],d["output_a"],d["output_b"],d.get("rubric","default"),d.get("model_a","A"),d.get("model_b","B"))
            return web.json_response(c.to_dict(),status=201)
        async def batch_ep(req):
            d=await req.json()
            results=await self.batch_judge(d.get("items",[]),d.get("rubric","default"))
            return web.json_response({"results":[r.to_dict() if hasattr(r,"to_dict") else str(r) for r in results]})
        async def lb_ep(req):
            return web.json_response({"leaderboard":self.leaderboard(req.rel_url.query.get("rubric"))})
        async def stats_ep(req): return web.json_response(self.stats())
        p=f"{prefix}/judge"
        app.router.add_post(f"{p}/score",judge_ep); app.router.add_post(f"{p}/compare",compare_ep)
        app.router.add_post(f"{p}/batch",batch_ep); app.router.add_get(f"{p}/leaderboard",lb_ep)
        app.router.add_get(f"{p}/stats",stats_ep)
        logger.info(f"LLM judge API at {prefix}/judge/")
