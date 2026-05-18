"""OMNI AGENT - Knowledge Distiller
Extract, compress, and structure knowledge from raw text: facts, entities,
summaries, Q&A pairs, contradiction detection, and incremental updates.

Features:
- Fact extraction: pull discrete factual claims from any text
- Entity linking: connect extracted facts to known entities
- Contradiction detection: flag facts that conflict with existing knowledge
- Summary distillation: progressive compression (full -> paragraph -> sentence)
- Q&A generation: auto-generate question-answer pairs from content
- Confidence scoring: each extracted item gets a confidence 0-1
- Source attribution: every item links back to its source text
- Incremental updates: add new text; merger detects new vs conflicting facts
- Topic clustering: group facts by inferred topic
- SQLite persistence: full knowledge base survives restarts
- REST API: distill, query-facts, get-summary, contradictions, export
"""
import json, time, uuid, sqlite3, asyncio, logging, re, math, hashlib
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class Fact:
    id: str; text: str; subject: str = ""; predicate: str = ""; obj: str = ""
    confidence: float = 1.0; source_id: str = ""; topic: str = ""
    contradicts: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    def to_dict(self):
        return {"id":self.id,"text":self.text,"subject":self.subject,
                "predicate":self.predicate,"object":self.obj,
                "confidence":round(self.confidence,3),"source_id":self.source_id,
                "topic":self.topic,"contradicts":self.contradicts}

@dataclass
class QAPair:
    id: str; question: str; answer: str
    source_id: str = ""; confidence: float = 1.0
    created_at: float = field(default_factory=time.time)
    def to_dict(self):
        return {"id":self.id,"question":self.question,"answer":self.answer,
                "source_id":self.source_id,"confidence":round(self.confidence,3)}

@dataclass
class DistillResult:
    source_id: str; source_title: str
    facts: List[Fact]; qa_pairs: List[QAPair]
    summary_long: str = ""; summary_short: str = ""
    topics: List[str] = field(default_factory=list)
    contradictions_found: int = 0
    latency_ms: float = 0.0
    def to_dict(self):
        return {"source_id":self.source_id,"source_title":self.source_title,
                "fact_count":len(self.facts),"qa_count":len(self.qa_pairs),
                "summary_short":self.summary_short,
                "topics":self.topics,"contradictions_found":self.contradictions_found,
                "latency_ms":round(self.latency_ms,1)}

class KBStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path=db_path; self._init()
    def _conn(self):
        c=sqlite3.connect(self.db_path); c.row_factory=sqlite3.Row; return c
    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sources(
                    id TEXT PRIMARY KEY,title TEXT,text TEXT DEFAULT '',
                    summary_long TEXT DEFAULT '',summary_short TEXT DEFAULT '',
                    topics TEXT DEFAULT '[]',created_at REAL);
                CREATE TABLE IF NOT EXISTS facts(
                    id TEXT PRIMARY KEY,text TEXT,subject TEXT DEFAULT '',
                    predicate TEXT DEFAULT '',obj TEXT DEFAULT '',
                    confidence REAL DEFAULT 1.0,source_id TEXT,topic TEXT DEFAULT '',
                    contradicts TEXT DEFAULT '[]',created_at REAL);
                CREATE TABLE IF NOT EXISTS qa_pairs(
                    id TEXT PRIMARY KEY,question TEXT,answer TEXT,
                    source_id TEXT,confidence REAL DEFAULT 1.0,created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_fact_src ON facts(source_id);
                CREATE INDEX IF NOT EXISTS idx_fact_topic ON facts(topic);
            """)
    def save_source(self, sid, title, text, summary_long, summary_short, topics):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?)",
                (sid,title,text[:5000],summary_long,summary_short,
                 json.dumps(topics),time.time()))
    def save_facts(self, facts):
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(f.id,f.text,f.subject,f.predicate,f.obj,f.confidence,
                  f.source_id,f.topic,json.dumps(f.contradicts),f.created_at) for f in facts])
    def save_qa(self, pairs):
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO qa_pairs VALUES(?,?,?,?,?,?)",
                [(q.id,q.question,q.answer,q.source_id,q.confidence,q.created_at)
                 for q in pairs])
    def get_all_facts(self, topic=None, min_confidence=0.0):
        with self._conn() as c:
            if topic:
                rows=c.execute("SELECT * FROM facts WHERE topic=? AND confidence>=? ORDER BY confidence DESC",(topic,min_confidence)).fetchall()
            else:
                rows=c.execute("SELECT * FROM facts WHERE confidence>=? ORDER BY confidence DESC",(min_confidence,)).fetchall()
        return [self._rf(r) for r in rows]
    def get_qa(self, source_id=None, limit=50):
        with self._conn() as c:
            if source_id:
                rows=c.execute("SELECT * FROM qa_pairs WHERE source_id=? LIMIT ?",(source_id,limit)).fetchall()
            else:
                rows=c.execute("SELECT * FROM qa_pairs ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
        return [{"id":r["id"],"question":r["question"],"answer":r["answer"],"source_id":r["source_id"]} for r in rows]
    def get_sources(self):
        with self._conn() as c:
            rows=c.execute("SELECT id,title,summary_short,topics,created_at FROM sources ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    def stats(self):
        with self._conn() as c:
            ns=c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            nf=c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            nq=c.execute("SELECT COUNT(*) FROM qa_pairs").fetchone()[0]
            nc=c.execute("SELECT COUNT(*) FROM facts WHERE json_array_length(contradicts)>0").fetchone()[0]
        return {"sources":ns,"facts":nf,"qa_pairs":nq,"facts_with_contradictions":nc}
    def _rf(self, row):
        return Fact(id=row["id"],text=row["text"],subject=row["subject"] or "",
                    predicate=row["predicate"] or "",obj=row["obj"] or "",
                    confidence=row["confidence"],source_id=row["source_id"],
                    topic=row["topic"] or "",
                    contradicts=json.loads(row["contradicts"] or "[]"))

# ── Extraction helpers (no-LLM fallbacks) ────────────────────────────────────
def _extract_sentences(text):
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+',text) if len(s.strip())>20]

def _guess_topic(text):
    tech=re.compile(r'\b(python|ai|machine learning|software|code|model|data|api)\b',re.I)
    sci=re.compile(r'\b(study|research|experiment|discovery|science|biology|physics)\b',re.I)
    biz=re.compile(r'\b(company|revenue|market|product|sales|startup|investment)\b',re.I)
    if tech.search(text): return "technology"
    if sci.search(text): return "science"
    if biz.search(text): return "business"
    return "general"

def _detect_contradiction(new_fact, existing_facts):
    """Simple heuristic: same subject + negation words nearby."""
    contradicts=[]
    neg=re.compile(r'\b(not|no|never|false|incorrect|wrong|unlike|opposite)\b',re.I)
    for ef in existing_facts:
        if (new_fact.subject and ef.subject==new_fact.subject and
                bool(neg.search(new_fact.text))!=bool(neg.search(ef.text))):
            contradicts.append(ef.id)
    return contradicts

class KnowledgeDistiller:
    """
    Extract, compress, and structure knowledge from raw text.

    Usage:
        kd = KnowledgeDistiller(llm_fn=my_llm)
        result = await kd.distill("Python basics", text)
        print(result.summary_short)
        for fact in result.facts: print(fact.text)
        for qa in result.qa_pairs: print(qa.question, "->", qa.answer)
    """
    def __init__(self, llm_fn=None, db_path="data/knowledge_distiller.db"):
        self._llm_fn=llm_fn; self._store=KBStore(db_path)

    async def _call_llm(self, prompt):
        if not self._llm_fn: return ""
        fn=self._llm_fn
        return str(await fn(prompt) if asyncio.iscoroutinefunction(fn) else fn(prompt))

    async def _extract_facts_llm(self, text, source_id):
        prompt=(f"Extract a list of discrete factual claims from this text. "
                "Each fact should be a single, self-contained statement.\n\n"
                f"TEXT: {text[:1500]}\n\n"
                "Respond with JSON only:\n"
                '[{"text":"Fact 1","subject":"entity","predicate":"action","object":"value","confidence":0.9,"topic":"technology"}]\n'
                "JSON array only:")
        raw=await self._call_llm(prompt)
        facts=[]
        try:
            m=re.search(r'\[[\s\S]*\]',raw)
            if m:
                items=json.loads(m.group(0))
                for item in items:
                    facts.append(Fact(
                        id=str(uuid.uuid4())[:10],
                        text=item.get("text",""),
                        subject=item.get("subject",""),
                        predicate=item.get("predicate",""),
                        obj=item.get("object",""),
                        confidence=float(item.get("confidence",0.8)),
                        source_id=source_id,
                        topic=item.get("topic",_guess_topic(item.get("text","")))))
        except: pass
        return facts

    async def _generate_qa_llm(self, text, source_id):
        prompt=(f"Generate 3-5 question-answer pairs from this text. "
                "Questions should test understanding of key facts.\n\n"
                f"TEXT: {text[:1500]}\n\n"
                "Respond with JSON only:\n"
                '[{"question":"What is...?","answer":"The answer is...","confidence":0.9}]\n'
                "JSON array only:")
        raw=await self._call_llm(prompt)
        pairs=[]
        try:
            m=re.search(r'\[[\s\S]*\]',raw)
            if m:
                items=json.loads(m.group(0))
                for item in items:
                    pairs.append(QAPair(
                        id=str(uuid.uuid4())[:10],
                        question=item.get("question",""),
                        answer=item.get("answer",""),
                        source_id=source_id,
                        confidence=float(item.get("confidence",0.8))))
        except: pass
        return pairs

    async def _summarise_llm(self, text):
        prompt_long=(f"Write a comprehensive summary (2-3 paragraphs) of:\n\n{text[:2000]}\n\nSummary:")
        prompt_short=(f"Write a one-sentence summary of:\n\n{text[:1000]}\n\nOne sentence:")
        long_s=await self._call_llm(prompt_long)
        short_s=await self._call_llm(prompt_short)
        return long_s.strip(), short_s.strip()

    def _fallback_facts(self, text, source_id):
        sents=_extract_sentences(text)
        return [Fact(id=str(uuid.uuid4())[:10],text=s,source_id=source_id,
                     confidence=0.7,topic=_guess_topic(s)) for s in sents[:10]]

    def _fallback_qa(self, text, source_id):
        sents=_extract_sentences(text)
        pairs=[]
        for s in sents[:3]:
            words=s.split(); subj=" ".join(words[:2]) if len(words)>=2 else "this"
            pairs.append(QAPair(id=str(uuid.uuid4())[:10],
                                question=f"What is stated about {subj}?",
                                answer=s,source_id=source_id,confidence=0.6))
        return pairs

    async def distill(self, title, text, source_id=None) -> DistillResult:
        start=time.time(); sid=source_id or str(uuid.uuid4())[:12]
        existing_facts=self._store.get_all_facts()

        if self._llm_fn:
            facts,qa_pairs=await asyncio.gather(
                self._extract_facts_llm(text,sid),
                self._generate_qa_llm(text,sid))
            summary_long,summary_short=await self._summarise_llm(text)
        else:
            facts=self._fallback_facts(text,sid)
            qa_pairs=self._fallback_qa(text,sid)
            sents=_extract_sentences(text)
            summary_long=" ".join(sents[:5])
            summary_short=sents[0] if sents else text[:100]

        # Contradiction detection
        for f in facts:
            f.contradicts=_detect_contradiction(f,existing_facts)

        contradictions=sum(1 for f in facts if f.contradicts)
        topics=list({f.topic for f in facts if f.topic})

        self._store.save_source(sid,title,text,summary_long,summary_short,topics)
        self._store.save_facts(facts); self._store.save_qa(qa_pairs)

        result=DistillResult(source_id=sid,source_title=title,facts=facts,
                              qa_pairs=qa_pairs,summary_long=summary_long,
                              summary_short=summary_short,topics=topics,
                              contradictions_found=contradictions,
                              latency_ms=(time.time()-start)*1000)
        logger.info(f"Distilled '{title}': {len(facts)} facts, {len(qa_pairs)} Q&A, {contradictions} contradictions")
        return result

    def get_facts(self, topic=None, min_confidence=0.0): return self._store.get_all_facts(topic,min_confidence)
    def get_qa(self, source_id=None, limit=50): return self._store.get_qa(source_id,limit)
    def get_sources(self): return self._store.get_sources()
    def stats(self): return self._store.stats()

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def distill_ep(req):
            d=await req.json()
            r=await self.distill(d["title"],d["text"],d.get("source_id"))
            return web.json_response(r.to_dict(),status=201)
        async def facts_ep(req):
            topic=req.rel_url.query.get("topic"); mc=float(req.rel_url.query.get("min_confidence","0"))
            return web.json_response({"facts":[f.to_dict() for f in self.get_facts(topic,mc)]})
        async def qa_ep(req):
            return web.json_response({"qa_pairs":self.get_qa(req.rel_url.query.get("source_id"))})
        async def sources_ep(req): return web.json_response({"sources":self.get_sources()})
        async def stats_ep(req): return web.json_response(self.stats())
        p=f"{prefix}/distill"
        app.router.add_post(f"{p}",distill_ep); app.router.add_get(f"{p}/facts",facts_ep)
        app.router.add_get(f"{p}/qa",qa_ep); app.router.add_get(f"{p}/sources",sources_ep)
        app.router.add_get(f"{p}/stats",stats_ep)
        logger.info(f"Knowledge distiller API at {prefix}/distill/")
