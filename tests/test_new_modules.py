"""
OMNI AGENT - Integration Tests v2
Tests for RAG pipeline, cache layer, prompt templates, pipeline executor,
and conversation summarizer.
Run: pytest tests/test_new_modules.py -v
"""
import sys
import os
import asyncio
import json
import tempfile
import math
import pytest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ══════════════════════════════════════════════════════════════════════════════
# RAG PIPELINE TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestVectorStore:

    @pytest.fixture
    def store(self, tmp_path):
        from agent.rag import VectorStore
        return VectorStore(db_path=str(tmp_path / "rag.db"))

    def test_schema_created(self, store, tmp_path):
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "rag.db"))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "documents" in tables
        assert "chunks" in tables

    def test_save_and_get_document(self, store):
        from agent.rag import Document
        doc = Document(id="d1", title="Test Doc", source="test.txt",
                      doc_type="txt", total_chunks=3, metadata={"key": "val"})
        store.save_document(doc)
        retrieved = store.get_document("d1")
        assert retrieved.id == "d1"
        assert retrieved.title == "Test Doc"
        assert retrieved.metadata["key"] == "val"

    def test_list_documents(self, store):
        from agent.rag import Document
        for i in range(3):
            store.save_document(Document(id=f"d{i}", title=f"Doc {i}",
                                        source="", doc_type="txt", total_chunks=1))
        docs = store.list_documents()
        assert len(docs) == 3

    def test_delete_document(self, store):
        from agent.rag import Document
        store.save_document(Document(id="del1", title="Delete Me",
                                    source="", doc_type="txt", total_chunks=1))
        assert store.get_document("del1") is not None
        store.delete_document("del1")
        assert store.get_document("del1") is None

    def test_save_and_get_chunks(self, store):
        from agent.rag import Document, Chunk
        doc = Document(id="dc1", title="Chunk Test", source="", doc_type="txt", total_chunks=2)
        store.save_document(doc)
        chunks = [
            Chunk(id="c1", doc_id="dc1", text="Hello world", index=0),
            Chunk(id="c2", doc_id="dc1", text="Python rocks", index=1),
        ]
        store.save_chunks(chunks)
        retrieved = store.get_chunks("dc1")
        assert len(retrieved) == 2
        assert retrieved[0].text == "Hello world"

    def test_cosine_similarity_identical(self, store):
        v = [1.0, 0.0, 0.0]
        assert abs(store._cosine(v, v) - 1.0) < 1e-6

    def test_cosine_similarity_orthogonal(self, store):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(store._cosine(a, b)) < 1e-6

    def test_cosine_empty_vectors(self, store):
        assert store._cosine([], []) == 0.0
        assert store._cosine([1.0], []) == 0.0

    def test_similarity_search(self, store):
        from agent.rag import Document, Chunk
        doc = Document(id="sem1", title="Semantic", source="", doc_type="txt", total_chunks=3)
        store.save_document(doc)
        chunks = [
            Chunk(id="s1", doc_id="sem1", text="A", index=0,
                  embedding=[1.0, 0.0, 0.0]),
            Chunk(id="s2", doc_id="sem1", text="B", index=1,
                  embedding=[0.0, 1.0, 0.0]),
            Chunk(id="s3", doc_id="sem1", text="C", index=2,
                  embedding=[0.0, 0.0, 1.0]),
        ]
        store.save_chunks(chunks)
        results = store.similarity_search([1.0, 0.0, 0.0], top_k=1)
        assert len(results) == 1
        assert results[0].chunk.id == "s1"
        assert abs(results[0].score - 1.0) < 1e-6
        assert results[0].rank == 1

    def test_keyword_search(self, store):
        from agent.rag import Document, Chunk
        doc = Document(id="kw1", title="KW Test", source="", doc_type="txt", total_chunks=2)
        store.save_document(doc)
        store.save_chunks([
            Chunk(id="k1", doc_id="kw1", text="Python is a programming language", index=0),
            Chunk(id="k2", doc_id="kw1", text="JavaScript runs in browsers", index=1),
        ])
        results = store.keyword_search("Python programming", top_k=5)
        assert any(r.chunk.id == "k1" for r in results)

    def test_store_stats(self, store):
        from agent.rag import Document, Chunk
        store.save_document(Document(id="st1", title="Stats", source="", doc_type="txt", total_chunks=1))
        store.save_chunks([Chunk(id="sc1", doc_id="st1", text="hi", index=0, embedding=[1.0])])
        stats = store.stats()
        assert stats["documents"] == 1
        assert stats["chunks"] == 1
        assert stats["embedded_chunks"] == 1


class TestTextChunker:

    def test_basic_chunking(self):
        from agent.rag import TextChunker
        chunker = TextChunker(chunk_size=5, chunk_overlap=1)
        words = "a b c d e f g h i j k".split()
        text = " ".join(words)
        chunks = chunker.chunk_text(text, "doc1")
        assert len(chunks) > 1
        assert all(c.doc_id == "doc1" for c in chunks)
        assert chunks[0].index == 0

    def test_overlap_between_chunks(self):
        from agent.rag import TextChunker
        chunker = TextChunker(chunk_size=4, chunk_overlap=2)
        text = "one two three four five six seven eight"
        chunks = chunker.chunk_text(text, "overlap_doc")
        if len(chunks) > 1:
            words_c0 = set(chunks[0].text.split())
            words_c1 = set(chunks[1].text.split())
            assert len(words_c0 & words_c1) > 0

    def test_paragraph_chunking(self):
        from agent.rag import TextChunker
        chunker = TextChunker(chunk_size=20, chunk_overlap=2)
        text = "First paragraph content here.\n\nSecond paragraph with more text.\n\nThird one."
        chunks = chunker.chunk_by_paragraph(text, "para_doc")
        assert len(chunks) >= 1
        assert all(c.doc_id == "para_doc" for c in chunks)

    def test_single_word_doc(self):
        from agent.rag import TextChunker
        chunker = TextChunker(chunk_size=10, chunk_overlap=2)
        chunks = chunker.chunk_text("hello", "tiny")
        assert len(chunks) == 1
        assert chunks[0].text == "hello"

    def test_chunk_ids_unique(self):
        from agent.rag import TextChunker
        chunker = TextChunker(chunk_size=3, chunk_overlap=1)
        chunks = chunker.chunk_text("a b c d e f g h i", "unique_doc")
        ids = [c.id for c in chunks]
        assert len(ids) == len(set(ids))


class TestDocumentParser:

    def test_parse_txt(self, tmp_path):
        from agent.rag import DocumentParser
        f = tmp_path / "test.txt"
        f.write_text("Hello world")
        parser = DocumentParser()
        text, dtype, meta = parser.parse_file(str(f))
        assert text == "Hello world"
        assert dtype == "txt"
        assert meta["filename"] == "test.txt"

    def test_parse_json(self, tmp_path):
        from agent.rag import DocumentParser
        f = tmp_path / "data.json"
        f.write_text('{"key": "value", "num": 42}')
        parser = DocumentParser()
        text, dtype, _ = parser.parse_file(str(f))
        assert dtype == "json"
        assert "key" in text

    def test_parse_csv(self, tmp_path):
        from agent.rag import DocumentParser
        f = tmp_path / "data.csv"
        f.write_text("name,age\nAlice,30\nBob,25\n")
        parser = DocumentParser()
        text, dtype, meta = parser.parse_file(str(f))
        assert dtype == "csv"
        assert "Alice" in text
        assert meta["rows"] == 2

    def test_parse_raw(self):
        from agent.rag import DocumentParser
        parser = DocumentParser()
        text, dtype, meta = parser.parse_raw("raw text", "My Title")
        assert text == "raw text"
        assert dtype == "raw"
        assert meta["title"] == "My Title"


class TestRAGPipeline:

    @pytest.fixture
    def pipeline(self, tmp_path):
        from agent.rag import RAGPipeline, VectorStore
        vs = VectorStore(db_path=str(tmp_path / "rag.db"))
        return RAGPipeline(vector_store=vs, embed_fn=None)

    @pytest.mark.asyncio
    async def test_ingest_text(self, pipeline):
        doc = await pipeline.ingest_text("Python is a great language", title="Python Doc")
        assert doc.id
        assert doc.title == "Python Doc"
        assert doc.total_chunks >= 1

    @pytest.mark.asyncio
    async def test_ingest_file(self, pipeline, tmp_path):
        f = tmp_path / "sample.md"
        f.write_text("# Title\n\nThis is a test document about machine learning.")
        doc = await pipeline.ingest_file(str(f))
        assert doc.title == "sample.md"
        assert doc.total_chunks >= 1

    @pytest.mark.asyncio
    async def test_retrieve_keyword_fallback(self, pipeline):
        await pipeline.ingest_text("Python is awesome for data science", title="d1")
        await pipeline.ingest_text("JavaScript is used for web development", title="d2")
        results = await pipeline.retrieve("Python data", top_k=3)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_augment_prompt(self, pipeline):
        await pipeline.ingest_text("The capital of France is Paris.", title="facts")
        augmented, results = await pipeline.augment_prompt("What is the capital of France?")
        assert "France" in augmented or len(results) == 0  # keyword may or may not match

    def test_generate_context_empty(self, pipeline):
        ctx = pipeline.generate_context([])
        assert ctx == ""

    def test_list_documents(self, pipeline):
        docs = pipeline.list_documents()
        assert isinstance(docs, list)

    def test_stats(self, pipeline):
        stats = pipeline.stats()
        assert "documents" in stats
        assert "chunks" in stats

    @pytest.mark.asyncio
    async def test_delete_document(self, pipeline):
        doc = await pipeline.ingest_text("Delete me", title="temp")
        assert len(pipeline.list_documents()) >= 1
        pipeline.delete_document(doc.id)
        remaining = [d for d in pipeline.list_documents() if d["id"] == doc.id]
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_ingest_directory(self, pipeline, tmp_path):
        (tmp_path / "a.txt").write_text("Document A content")
        (tmp_path / "b.md").write_text("Document B content")
        (tmp_path / "c.ignore").write_text("Should be skipped")
        docs = await pipeline.ingest_directory(str(tmp_path), extensions=["txt", "md"])
        assert len(docs) == 2


# ══════════════════════════════════════════════════════════════════════════════
# CACHE LAYER TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestCacheClient:

    @pytest.fixture
    def cache(self):
        from agent.cache import CacheClient
        c = CacheClient()
        c._backend = "memory"  # force memory mode
        return c

    @pytest.mark.asyncio
    async def test_set_and_get_string(self, cache):
        await cache.set("k1", "hello")
        val = await cache.get("k1")
        assert val == "hello"

    @pytest.mark.asyncio
    async def test_set_and_get_dict(self, cache):
        await cache.set("k2", {"x": 1, "y": [1, 2, 3]})
        val = await cache.get("k2")
        assert val == {"x": 1, "y": [1, 2, 3]}

    @pytest.mark.asyncio
    async def test_missing_key_returns_none(self, cache):
        val = await cache.get("nonexistent_key_xyz")
        assert val is None

    @pytest.mark.asyncio
    async def test_delete(self, cache):
        await cache.set("del_key", "value")
        assert await cache.exists("del_key")
        await cache.delete("del_key")
        assert not await cache.exists("del_key")

    @pytest.mark.asyncio
    async def test_ttl_expiry(self, cache):
        import time
        cache._mem.set("ttl_key", "val", ttl=1)
        assert cache._mem.get("ttl_key") == "val"
        # Simulate expiry
        cache._mem._expiry["ttl_key"] = time.time() - 1
        assert cache._mem.get("ttl_key") is None

    @pytest.mark.asyncio
    async def test_rate_check_allows_under_limit(self, cache):
        result = await cache.rate_check("user1", limit=10)
        assert result["allowed"]
        assert result["count"] == 1
        assert result["remaining"] == 9

    @pytest.mark.asyncio
    async def test_rate_check_blocks_over_limit(self, cache):
        for _ in range(5):
            await cache.rate_check("user2", limit=5)
        result = await cache.rate_check("user2", limit=5)
        assert not result["allowed"]
        assert result["retry_after"] > 0

    @pytest.mark.asyncio
    async def test_rate_check_separate_users(self, cache):
        r1 = await cache.rate_check("ua", limit=5)
        r2 = await cache.rate_check("ub", limit=5)
        assert r1["allowed"] and r2["allowed"]

    @pytest.mark.asyncio
    async def test_response_key_deterministic(self, cache):
        msgs = [{"role": "user", "content": "hello"}]
        k1 = cache._response_key("model1", msgs)
        k2 = cache._response_key("model1", msgs)
        k3 = cache._response_key("model2", msgs)
        assert k1 == k2
        assert k1 != k3

    @pytest.mark.asyncio
    async def test_session_set_and_get(self, cache):
        await cache.set_session("sess1", {"user": "alice", "turn": 3})
        data = await cache.get_session("sess1")
        assert data["user"] == "alice"

    @pytest.mark.asyncio
    async def test_session_update(self, cache):
        await cache.set_session("sess2", {"a": 1})
        await cache.update_session("sess2", {"b": 2})
        data = await cache.get_session("sess2")
        assert data["a"] == 1 and data["b"] == 2

    @pytest.mark.asyncio
    async def test_flush(self, cache):
        await cache.set("flush_k1", "v1")
        await cache.set("flush_k2", "v2")
        await cache.flush()
        assert await cache.get("flush_k1") is None

    @pytest.mark.asyncio
    async def test_memory_stats(self, cache):
        await cache.set("stat_key", "val")
        stats = await cache.stats()
        assert stats["backend"] == "memory"
        assert stats["keys"] >= 1


class TestMemoryStore:

    def test_set_get(self):
        from agent.cache import _MemoryStore
        m = _MemoryStore()
        m.set("k", "v")
        assert m.get("k") == "v"

    def test_missing(self):
        from agent.cache import _MemoryStore
        assert _MemoryStore().get("no") is None

    def test_incr(self):
        from agent.cache import _MemoryStore
        m = _MemoryStore()
        assert m.incr("counter") == 1
        assert m.incr("counter") == 2
        assert m.incr("counter") == 3

    def test_keys_pattern(self):
        from agent.cache import _MemoryStore
        m = _MemoryStore()
        m.set("rate:user1", "1")
        m.set("rate:user2", "2")
        m.set("session:s1", "x")
        rate_keys = m.keys("rate:*")
        assert len(rate_keys) == 2
        assert all(k.startswith("rate:") for k in rate_keys)

    def test_flush(self):
        from agent.cache import _MemoryStore
        m = _MemoryStore()
        m.set("a", "1"); m.set("b", "2")
        m.flush()
        assert m.size() == 0


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT TEMPLATE TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestPromptTemplate:

    def test_get_variables_no_default(self):
        from agent.prompt_templates import PromptTemplate
        t = PromptTemplate("t", "Hello {{name}}! You are {{age}} years old.")
        v = t.get_variables()
        assert "name" in v and v["name"] is None
        assert "age" in v and v["age"] is None

    def test_get_variables_with_defaults(self):
        from agent.prompt_templates import PromptTemplate
        t = PromptTemplate("t", "Style: {{style:formal}}. Length: {{length:short}}.")
        v = t.get_variables()
        assert v["style"] == "formal"
        assert v["length"] == "short"

    def test_render_fills_variables(self):
        from agent.prompt_templates import PromptTemplate
        t = PromptTemplate("t", "Hello {{name}}! You are {{age}} years old.")
        result = t.render({"name": "Alice", "age": "30"})
        assert result == "Hello Alice! You are 30 years old."

    def test_render_uses_defaults(self):
        from agent.prompt_templates import PromptTemplate
        t = PromptTemplate("t", "Style: {{style:formal}}.")
        result = t.render({})
        assert result == "Style: formal."

    def test_render_missing_required_raises(self):
        from agent.prompt_templates import PromptTemplate
        t = PromptTemplate("t", "Hello {{name}}!")
        with pytest.raises(ValueError, match="missing required variables"):
            t.render({})

    def test_validate_ok(self):
        from agent.prompt_templates import PromptTemplate
        t = PromptTemplate("t", "Hi {{name}}!")
        valid, missing = t.validate({"name": "Bob"})
        assert valid and not missing

    def test_validate_missing(self):
        from agent.prompt_templates import PromptTemplate
        t = PromptTemplate("t", "{{a}} and {{b}}!")
        valid, missing = t.validate({"a": "x"})
        assert not valid
        assert "b" in missing

    def test_build_messages_structure(self):
        from agent.prompt_templates import PromptTemplate
        t = PromptTemplate("t", "Translate: {{text}}",
                          system_prompt="You are a translator.",
                          few_shot_examples=[
                              {"user": "Translate: Hello", "assistant": "Bonjour"}
                          ])
        msgs = t.build_messages({"text": "world"})
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"   # few-shot
        assert msgs[2]["role"] == "assistant"
        assert msgs[3]["role"] == "user"
        assert "world" in msgs[3]["content"]

    def test_to_dict(self):
        from agent.prompt_templates import PromptTemplate
        t = PromptTemplate("mytemplate", "{{x}} and {{y:default}}")
        d = t.to_dict()
        assert d["name"] == "mytemplate"
        assert "x" in d["variables"]
        assert "y" in d["variables"]


class TestPromptTemplateRegistry:

    @pytest.fixture
    def registry(self):
        from agent.prompt_templates import PromptTemplateRegistry
        return PromptTemplateRegistry(memory=None)

    def test_builtin_templates_loaded(self, registry):
        templates = registry.list_templates()
        names = [t["name"] for t in templates]
        assert "summarize" in names
        assert "code_review" in names
        assert "translate" in names
        assert "rag_answer" in names
        assert "chain_of_thought" in names
        assert len(templates) >= 9

    def test_get_existing(self, registry):
        t = registry.get("summarize")
        assert t is not None
        assert t.name == "summarize"

    def test_get_nonexistent(self, registry):
        assert registry.get("no_such_template") is None

    def test_register_custom(self, registry):
        from agent.prompt_templates import PromptTemplate
        t = PromptTemplate("custom", "Custom: {{msg}}")
        registry.register(t)
        assert registry.get("custom") is not None

    def test_delete(self, registry):
        from agent.prompt_templates import PromptTemplate
        registry.register(PromptTemplate("to_delete", "{{x}}"))
        assert registry.delete("to_delete")
        assert registry.get("to_delete") is None

    def test_delete_nonexistent(self, registry):
        assert not registry.delete("ghost_template")

    def test_render_summarize(self, registry):
        msgs = registry.render("summarize", {"text": "Some text here"})
        assert any(m["role"] == "user" for m in msgs)
        user_msgs = [m for m in msgs if m["role"] == "user"]
        assert "Some text here" in user_msgs[-1]["content"]

    def test_render_missing_required_raises(self, registry):
        with pytest.raises(ValueError):
            # 'text' is required in translate
            registry.render("translate", {"target_lang": "Spanish"})

    def test_quick_render(self, registry):
        msgs = registry.quick_render("summarize", text="Hello world content")
        assert len(msgs) > 0

    def test_search(self, registry):
        results = registry.search("code")
        names = [t.name for t in results]
        assert "code_review" in names or "write_tests" in names

    def test_list_by_tag(self, registry):
        code_templates = registry.list_templates(tag="code")
        assert len(code_templates) >= 2
        assert all("code" in t["name"] or "code" in str(t)
                  for t in code_templates)

    def test_export_import_json(self, registry, tmp_path):
        export_path = str(tmp_path / "templates.json")
        registry.export_json(export_path)
        # Re-import into fresh registry
        from agent.prompt_templates import PromptTemplateRegistry
        fresh = PromptTemplateRegistry(memory=None)
        count = fresh.import_json(export_path)
        assert count >= 9


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE EXECUTOR TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestPipelineExecutor:

    @pytest.fixture
    def executor(self):
        from agent.pipeline import PipelineExecutor
        return PipelineExecutor()

    @pytest.fixture
    def simple_pipeline(self):
        from agent.pipeline import Pipeline
        p = Pipeline("simple", "A simple test pipeline")

        async def step_a(ctx):
            return ctx.get("input", "default") + "_a"

        async def step_b(ctx):
            return ctx["step_a"] + "_b"

        async def step_c(ctx):
            return ctx["step_b"].upper()

        p.step("step_a", step_a, output_key="step_a")
        p.step("step_b", step_b, output_key="step_b")
        p.step("step_c", step_c, output_key="step_c")
        return p

    @pytest.mark.asyncio
    async def test_pipeline_runs_all_steps(self, executor, simple_pipeline):
        from agent.pipeline import StepStatus
        run = await executor.run(simple_pipeline, {"input": "hello"})
        assert run.status == StepStatus.SUCCESS
        assert len(run.steps) == 3
        assert all(s.status == StepStatus.SUCCESS for s in run.steps)

    @pytest.mark.asyncio
    async def test_context_flows_between_steps(self, executor, simple_pipeline):
        run = await executor.run(simple_pipeline, {"input": "test"})
        assert run.context["step_a"] == "test_a"
        assert run.context["step_b"] == "test_a_b"
        assert run.context["step_c"] == "TEST_A_B"

    @pytest.mark.asyncio
    async def test_failed_step_halts_pipeline(self, executor):
        from agent.pipeline import Pipeline, StepStatus

        async def explode(ctx):
            raise RuntimeError("Intentional error")

        async def never_runs(ctx):
            return "should not reach here"

        p = Pipeline("failing")
        p.step("explode", explode, output_key="x", on_error="fail")
        p.step("never", never_runs, output_key="y")

        run = await executor.run(p, {})
        assert run.status == StepStatus.FAILED
        assert len(run.steps) == 1  # stopped after first fail

    @pytest.mark.asyncio
    async def test_on_error_skip_continues(self, executor):
        from agent.pipeline import Pipeline, StepStatus

        async def bad_step(ctx):
            raise ValueError("Skippable error")

        async def good_step(ctx):
            return "success"

        p = Pipeline("skip_test")
        p.step("bad", bad_step, output_key="bad", on_error="skip")
        p.step("good", good_step, output_key="good")

        run = await executor.run(p, {})
        assert run.status == StepStatus.SUCCESS
        assert run.steps[0].status == StepStatus.SKIPPED
        assert run.steps[1].status == StepStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_conditional_step_skipped(self, executor):
        from agent.pipeline import Pipeline, StepStatus

        async def conditional_step(ctx):
            return "ran"

        p = Pipeline("conditional")
        p.step("cond", conditional_step, output_key="result",
               condition=lambda ctx: ctx.get("run_it", False))

        run = await executor.run(p, {"run_it": False})
        assert run.steps[0].status == StepStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_conditional_step_runs_when_true(self, executor):
        from agent.pipeline import Pipeline, StepStatus

        async def conditional_step(ctx):
            return "ran"

        p = Pipeline("cond_true")
        p.step("cond", conditional_step, output_key="result",
               condition=lambda ctx: ctx.get("run_it", False))

        run = await executor.run(p, {"run_it": True})
        assert run.steps[0].status == StepStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_sync_handler_works(self, executor):
        from agent.pipeline import Pipeline, StepStatus

        def sync_step(ctx):
            return "sync_result"

        p = Pipeline("sync_test")
        p.step("sync", sync_step, output_key="result")
        run = await executor.run(p, {})
        assert run.status == StepStatus.SUCCESS
        assert run.context["result"] == "sync_result"

    @pytest.mark.asyncio
    async def test_retry_on_failure(self, executor):
        from agent.pipeline import Pipeline, StepStatus
        attempt = [0]

        async def flaky(ctx):
            attempt[0] += 1
            if attempt[0] < 3:
                raise RuntimeError(f"Attempt {attempt[0]} failed")
            return "finally succeeded"

        p = Pipeline("retry_test")
        p.step("flaky", flaky, output_key="r", on_error="retry",
               max_retries=3, retry_delay=0.01)

        run = await executor.run(p, {})
        assert run.status == StepStatus.SUCCESS
        assert run.context["r"] == "finally succeeded"
        assert run.steps[0].retries == 2

    @pytest.mark.asyncio
    async def test_timeout_enforced(self, executor):
        from agent.pipeline import Pipeline, StepStatus

        async def slow_step(ctx):
            await asyncio.sleep(10)
            return "done"

        p = Pipeline("timeout_test")
        p.step("slow", slow_step, output_key="r", timeout=0.05, on_error="skip")

        run = await executor.run(p, {})
        assert run.steps[0].status == StepStatus.SKIPPED

    def test_register_and_list(self, executor):
        from agent.pipeline import Pipeline
        p = Pipeline("listed_pipeline", "A listed one")
        executor.register(p)
        listings = executor.list_pipelines()
        assert any(pl["name"] == "listed_pipeline" for pl in listings)

    @pytest.mark.asyncio
    async def test_run_by_name(self, executor):
        from agent.pipeline import Pipeline

        async def handler(ctx):
            return 42

        p = Pipeline("named_pipe")
        p.step("s1", handler, output_key="answer")
        executor.register(p)

        run = await executor.run_by_name("named_pipe", {"x": 1})
        assert run is not None
        assert run.context["answer"] == 42

    @pytest.mark.asyncio
    async def test_run_by_name_not_found(self, executor):
        result = await executor.run_by_name("ghost_pipeline")
        assert result is None

    @pytest.mark.asyncio
    async def test_build_job_search_pipeline_returns_summary(self, executor, monkeypatch):
        from agent.pipeline import build_job_search_pipeline, StepStatus
        import job_search_tank_adr_improved as job_search_module

        expected = {
            "search_date": "2026-05-18",
            "total_results": 7,
            "report_files": {"json": "C:/tmp/report.json", "html": "C:/tmp/report.html"},
        }

        async def fake_run_search_with_summary(export_format="html", verbose=False, output_dir=None):
            assert export_format == "json"
            assert verbose is False
            assert output_dir is None
            return expected

        monkeypatch.setattr(job_search_module, "run_search_with_summary", fake_run_search_with_summary)

        pipeline = build_job_search_pipeline()
        run = await executor.run(pipeline, {"export_format": "json", "verbose": False})

        assert run.status == StepStatus.SUCCESS
        assert run.context["job_search_result"] == expected

    @pytest.mark.asyncio
    async def test_list_runs(self, executor, simple_pipeline):
        run = await executor.run(simple_pipeline, {"input": "test"})
        runs = executor.list_runs(pipeline_name="simple")
        assert any(r["run_id"] == run.run_id for r in runs)

    @pytest.mark.asyncio
    async def test_run_duration_tracked(self, executor, simple_pipeline):
        run = await executor.run(simple_pipeline, {"input": "perf"})
        assert run.duration_ms > 0
        assert run.finished_at is not None


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATION SUMMARIZER TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestConversationSummarizer:

    @pytest.fixture
    def summarizer(self):
        from agent.summarizer import ConversationSummarizer
        return ConversationSummarizer(llm=None, threshold=6, keep_recent=2)

    def _make_messages(self, n: int):
        msgs = [{"role": "system", "content": "You are helpful."}]
        for i in range(n):
            msgs.append({"role": "user", "content": f"User message {i}"})
            msgs.append({"role": "assistant", "content": f"Assistant response {i}"})
        return msgs

    @pytest.mark.asyncio
    async def test_no_compression_under_threshold(self, summarizer):
        msgs = self._make_messages(2)  # 5 messages total
        compressed, meta = await summarizer.maybe_compress(msgs)
        assert meta is None
        assert len(compressed) == len(msgs)

    @pytest.mark.asyncio
    async def test_sliding_window_compresses(self, summarizer):
        msgs = self._make_messages(8)  # 17 messages
        compressed, meta = await summarizer.maybe_compress(msgs, strategy="sliding_window")
        assert meta is not None
        assert meta.strategy == "sliding_window"
        assert len(compressed) < len(msgs)

    @pytest.mark.asyncio
    async def test_sliding_window_keeps_system(self, summarizer):
        msgs = self._make_messages(8)
        compressed, _ = await summarizer.maybe_compress(msgs, strategy="sliding_window")
        assert compressed[0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_extractive_compresses(self, summarizer):
        msgs = self._make_messages(8)
        compressed, meta = await summarizer.maybe_compress(msgs, strategy="extractive")
        assert meta is not None
        assert meta.strategy == "extractive"
        assert len(compressed) < len(msgs)

    @pytest.mark.asyncio
    async def test_extractive_injects_summary_block(self, summarizer):
        msgs = self._make_messages(8)
        compressed, _ = await summarizer.maybe_compress(msgs, strategy="extractive")
        system_msgs = [m for m in compressed if m["role"] == "system"]
        assert any("PRIOR CONTEXT" in m["content"] for m in system_msgs)

    def test_estimate_tokens(self, summarizer):
        msgs = [{"role": "user", "content": "a" * 400}]  # 400 chars ~ 100 tokens
        est = summarizer.estimate_tokens(msgs)
        assert 90 <= est <= 110

    def test_needs_compression_small(self, summarizer):
        msgs = [{"role": "user", "content": "hi"}]
        assert not summarizer.needs_compression(msgs, model_context=131072)

    def test_needs_compression_large(self, summarizer):
        msgs = [{"role": "user", "content": "x" * 500000}]
        assert summarizer.needs_compression(msgs, model_context=131072)

    def test_compression_stats(self, summarizer):
        msgs = self._make_messages(3)
        stats = summarizer.compression_stats(msgs)
        assert "message_count" in stats
        assert "estimated_tokens" in stats
        assert "needs_compression_128k" in stats

    @pytest.mark.asyncio
    async def test_auto_strategy_no_llm_uses_extractive(self, summarizer):
        msgs = self._make_messages(8)
        compressed, meta = await summarizer.maybe_compress(msgs, strategy="auto")
        assert meta.strategy == "extractive"  # no LLM, falls back to extractive
