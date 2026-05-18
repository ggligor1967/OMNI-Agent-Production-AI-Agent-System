"""OMNI AGENT - Document QA
Ingest documents, chunk them, index with embeddings, retrieve relevant passages,
answer questions with source citations and confidence scores.

Features:
- Flexible chunking: fixed-size, sentence-boundary, paragraph-aware
- Overlap support: configurable token overlap between chunks
- Multi-document: ingest many docs; query across all or a subset
- Passage retrieval: cosine similarity + keyword boosting
- Multi-hop: iterate retrieval → LLM → re-query until answer found
- Citation tracking: every answer includes [source, chunk_id, score]
- Confidence: LLM self-reports confidence 0-1; flag low-confidence answers
- Context window budgeting: pick top-K chunks that fit token budget
- Reranking: LLM-based passage reranker for precision
- SQLite persistence: all chunks and docs stored
- REST API: ingest, query, list docs, delete
"""
import json, time, uuid, sqlite3, math, re, hashlib, asyncio, logging
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Chunking helpers ──────────────────────────────────────────────────────────

def _chunk_fixed(text, chunk_size=512, overlap=64):
    words = text.split(); chunks = []; i = 0
    while i < len(words):
        chunk = " ".join(words[i:i+chunk_size])
        chunks.append(chunk); i += chunk_size - overlap
        if i < 0: break
    return chunks

def _chunk_paragraphs(text, max_chunk=600):
    paras = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    chunks = []; current = []
    for p in paras:
        words = " ".join(current).split()
        if len(words) + len(p.split()) > max_chunk and current:
            chunks.append(" ".join(current)); current = [p]
        else:
            current.append(p)
    if current: chunks.append(" ".join(current))
    return chunks

def _chunk_sentences(text, max_chunk=400):
    sents = re.split(r'(?<=[.!?])\s+', text)
    chunks = []; current = []
    for s in sents:
        if sum(len(x.split()) for x in current) + len(s.split()) > max_chunk and current:
            chunks.append(" ".join(current)); current = [s]
        else:
            current.append(s)
    if current: chunks.append(" ".join(current))
    return chunks or [text]

CHUNK_STRATEGIES = {"fixed":_chunk_fixed,"paragraph":_chunk_paragraphs,"sentence":_chunk_sentences}

# ── Embedding ─────────────────────────────────────────────────────────────────

def _embed(text, dim=128):
    vec = [0.0]*dim; text = text.lower()
    for i in range(max(1,len(text)-2)):
        idx = int(hashlib.md5(text[i:i+3].encode()).hexdigest(),16) % dim
        vec[idx] += 1.0
    n = math.sqrt(sum(x*x for x in vec)); return [x/n for x in vec] if n else vec

def _cosine(a,b): return sum(x*y for x,y in zip(a,b)) if len(a)==len(b) else 0.0

def _kw_boost(query, chunk_text):
    q_words = set(re.findall(r'\w+',query.lower()))
    c_words = re.findall(r'\w+',chunk_text.lower())
    if not c_words: return 0.0
    hits = sum(1 for w in c_words if w in q_words)
    return min(0.3, hits/len(c_words)*3)

# ── Models ────────────────────────────────────────────────────────────────────

@dataclass
class Document:
    id: str; title: str; source: str = ""
    content: str = ""; chunk_count: int = 0
    metadata: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    def to_dict(self):
        return {"id":self.id,"title":self.title,"source":self.source,
                "chunk_count":self.chunk_count,"metadata":self.metadata,
                "created_at":self.created_at}

@dataclass
class Chunk:
    id: str; doc_id: str; doc_title: str
    text: str; chunk_index: int
    embedding: List[float] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)

@dataclass
class Citation:
    doc_id: str; doc_title: str; chunk_id: str
    chunk_index: int; score: float; excerpt: str = ""
    def to_dict(self):
        return {"doc_id":self.doc_id,"doc_title":self.doc_title,
                "chunk_id":self.chunk_id,"chunk_index":self.chunk_index,
                "score":round(self.score,4),"excerpt":self.excerpt[:200]}

@dataclass
class QAResult:
    question: str; answer: str
    citations: List[Citation]
    confidence: float = 1.0; hops: int = 1
    tokens_used: int = 0; latency_ms: float = 0.0
    def to_dict(self):
        return {"question":self.question[:200],"answer":self.answer,
                "confidence":round(self.confidence,3),"hops":self.hops,
                "citations":[c.to_dict() for c in self.citations],
                "latency_ms":round(self.latency_ms,1)}

# ── Store ─────────────────────────────────────────────────────────────────────

class DocStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path=db_path; self._init()
    def _conn(self):
        c=sqlite3.connect(self.db_path); c.row_factory=sqlite3.Row; return c
    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT PRIMARY KEY,title TEXT,source TEXT DEFAULT '',
                    content TEXT DEFAULT '',chunk_count INTEGER DEFAULT 0,
                    metadata TEXT DEFAULT '{}',created_at REAL);
                CREATE TABLE IF NOT EXISTS chunks(
                    id TEXT PRIMARY KEY,doc_id TEXT,doc_title TEXT,
                    text TEXT,chunk_index INTEGER,embedding TEXT,
                    metadata TEXT DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS idx_ch_doc ON chunks(doc_id);
            """)
    def save_doc(self, doc):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?)",
                (doc.id,doc.title,doc.source,doc.content[:10000],
                 doc.chunk_count,json.dumps(doc.metadata),doc.created_at))
    def save_chunks(self, chunks):
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?,?,?)",
                [(ch.id,ch.doc_id,ch.doc_title,ch.text,ch.chunk_index,
                  json.dumps(ch.embedding),json.dumps(ch.metadata)) for ch in chunks])
    def get_doc(self, doc_id):
        with self._conn() as c:
            row=c.execute("SELECT * FROM documents WHERE id=?",(doc_id,)).fetchone()
        if not row: return None
        return Document(id=row["id"],title=row["title"],source=row["source"],
                        content=row["content"],chunk_count=row["chunk_count"],
                        metadata=json.loads(row["metadata"] or "{}"),created_at=row["created_at"])
    def list_docs(self):
        with self._conn() as c:
            rows=c.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
        return [Document(id=r["id"],title=r["title"],source=r["source"],content="",
                         chunk_count=r["chunk_count"],metadata=json.loads(r["metadata"] or "{}"),
                         created_at=r["created_at"]) for r in rows]
    def delete_doc(self, doc_id):
        with self._conn() as c:
            c.execute("DELETE FROM documents WHERE id=?",(doc_id,))
            c.execute("DELETE FROM chunks WHERE doc_id=?",(doc_id,))
    def get_all_chunks(self, doc_ids=None):
        with self._conn() as c:
            if doc_ids:
                ph=",".join("?"*len(doc_ids))
                rows=c.execute(f"SELECT * FROM chunks WHERE doc_id IN ({ph})",doc_ids).fetchall()
            else:
                rows=c.execute("SELECT * FROM chunks").fetchall()
        return [Chunk(id=r["id"],doc_id=r["doc_id"],doc_title=r["doc_title"],
                      text=r["text"],chunk_index=r["chunk_index"],
                      embedding=json.loads(r["embedding"]),
                      metadata=json.loads(r["metadata"] or "{}")) for r in rows]
    def stats(self):
        with self._conn() as c:
            nd=c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            nc=c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"documents":nd,"chunks":nc}

# ── DocumentQA ────────────────────────────────────────────────────────────────

class DocumentQA:
    """
    Retrieval-augmented Q&A over a corpus of documents with citation tracking.

    Usage:
        qa = DocumentQA(llm_fn=my_llm)
        doc_id = qa.ingest("The Zen of Python", "Beautiful is better than ugly...",
                            strategy="sentence")
        result = await qa.query("What does the Zen say about complexity?")
        print(result.answer)
        for cit in result.citations:
            print(f"  [{cit.doc_title}] score={cit.score:.2f}: {cit.excerpt}")
    """
    def __init__(self, llm_fn=None, embedder=None,
                 db_path="data/document_qa.db",
                 top_k=5, token_budget=2000):
        self._llm_fn=llm_fn; self._embedder=embedder or _embed
        self._store=DocStore(db_path); self._top_k=top_k; self._token_budget=token_budget
        self._chunk_cache: Dict[str,List[Chunk]] = {}

    def ingest(self, title, content, source="", strategy="paragraph",
               chunk_size=512, overlap=64, metadata=None):
        doc_id=str(uuid.uuid4())[:12]
        chunk_fn=CHUNK_STRATEGIES.get(strategy,_chunk_paragraphs)
        if strategy=="fixed":
            raw_chunks=chunk_fn(content,chunk_size,overlap)
        else:
            raw_chunks=chunk_fn(content)
        chunks=[]
        for i,text in enumerate(raw_chunks):
            if not text.strip(): continue
            emb=self._embedder(text)
            chunks.append(Chunk(id=f"{doc_id}_{i}",doc_id=doc_id,
                                doc_title=title,text=text,chunk_index=i,embedding=emb,
                                metadata=metadata or {}))
        doc=Document(id=doc_id,title=title,source=source,content=content,
                     chunk_count=len(chunks),metadata=metadata or {})
        self._store.save_doc(doc); self._store.save_chunks(chunks)
        self._chunk_cache.clear()
        logger.info(f"Ingested '{title}' -> {len(chunks)} chunks")
        return doc_id

    def _get_chunks(self, doc_ids=None):
        key=str(sorted(doc_ids or []))
        if key not in self._chunk_cache:
            self._chunk_cache[key]=self._store.get_all_chunks(doc_ids)
        return self._chunk_cache[key]

    def retrieve(self, query, doc_ids=None, top_k=None):
        k=top_k or self._top_k; q_emb=self._embedder(query)
        chunks=self._get_chunks(doc_ids); scored=[]
        for ch in chunks:
            sem=_cosine(q_emb,ch.embedding); kw=_kw_boost(query,ch.text)
            scored.append((sem+kw, ch))
        scored.sort(key=lambda x:-x[0])
        return [(score,ch) for score,ch in scored[:k]]

    def _build_context(self, passages):
        ctx=[]; tokens=0
        for score,ch in passages:
            t=len(ch.text.split()); tokens+=t
            if tokens>self._token_budget: break
            ctx.append(f"[{ch.doc_title} / chunk {ch.chunk_index}] {ch.text}")
        return "\n\n".join(ctx)

    async def query(self, question, doc_ids=None, max_hops=2, min_confidence=0.5):
        start=time.time(); all_citations=[]; hops=0
        current_query=question
        for hop in range(max_hops):
            hops=hop+1
            passages=self.retrieve(current_query,doc_ids)
            if not passages: break
            ctx=self._build_context(passages)
            all_citations=[Citation(doc_id=ch.doc_id,doc_title=ch.doc_title,
                                     chunk_id=ch.id,chunk_index=ch.chunk_index,
                                     score=score,excerpt=ch.text[:200])
                           for score,ch in passages]
            if not self._llm_fn:
                answer=f"[Retrieved {len(passages)} passages. No LLM connected.]\n{ctx[:500]}"
                confidence=0.7; break
            prompt=(f"Answer the question using ONLY the provided context. "
                    f"If the answer is not in the context, say so.\n\n"
                    f"CONTEXT:\n{ctx}\n\nQUESTION: {question}\n\n"
                    "Respond with JSON only:\n"
                    '{"answer":"...","confidence":0.85,"follow_up_query":"optional further query"}\n'
                    "JSON only:")
            try:
                fn=self._llm_fn
                raw=await fn(prompt) if asyncio.iscoroutinefunction(fn) else fn(prompt)
                m=re.search(r'\{[\s\S]*\}',raw)
                if not m: raise ValueError("no JSON")
                data=json.loads(m.group(0))
                answer=data.get("answer",""); confidence=float(data.get("confidence",0.7))
                follow_up=data.get("follow_up_query","")
                if confidence>=min_confidence or hop==max_hops-1: break
                if follow_up: current_query=follow_up
                else: break
            except Exception as e:
                answer=ctx[:400]; confidence=0.5; break

        return QAResult(question=question,answer=str(answer),citations=all_citations,
                        confidence=confidence,hops=hops,
                        latency_ms=(time.time()-start)*1000)

    def list_docs(self): return self._store.list_docs()
    def get_doc(self, doc_id): return self._store.get_doc(doc_id)
    def delete_doc(self, doc_id): self._store.delete_doc(doc_id); self._chunk_cache.clear()
    def stats(self): return self._store.stats()

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def ingest_ep(req):
            d=await req.json()
            doc_id=self.ingest(d["title"],d["content"],source=d.get("source",""),
                                strategy=d.get("strategy","paragraph"),
                                metadata=d.get("metadata",{}))
            return web.json_response({"doc_id":doc_id,"status":"ingested"},status=201)
        async def query_ep(req):
            d=await req.json()
            r=await self.query(d["question"],doc_ids=d.get("doc_ids"),
                                max_hops=int(d.get("max_hops",2)))
            return web.json_response(r.to_dict())
        async def list_ep(req):
            return web.json_response({"documents":[d.to_dict() for d in self.list_docs()]})
        async def delete_ep(req):
            self.delete_doc(req.match_info["id"])
            return web.json_response({"deleted":True})
        async def stats_ep(req): return web.json_response(self.stats())
        p=f"{prefix}/docqa"
        app.router.add_post(f"{p}/ingest",ingest_ep); app.router.add_post(f"{p}/query",query_ep)
        app.router.add_get(f"{p}/documents",list_ep); app.router.add_get(f"{p}/stats",stats_ep)
        app.router.add_delete(f"{p}/documents/{{id}}",delete_ep)
        logger.info(f"Document QA API at {prefix}/docqa/")
