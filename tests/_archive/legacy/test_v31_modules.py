"""OMNI AGENT v31: EmbeddingStore, DocumentParser, FeedbackCollector, PromptOptimizer"""
import os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# EMBEDDING STORE
# ════════════════════════════════════════════════════════
class TestEmbeddingStore(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.embedding_store import EmbeddingStore, hash_embed
        self.ES = EmbeddingStore
        self.store = EmbeddingStore(db_path=os.path.join(td,"es.db"),
                                     dim=64, use_hnsw=True)
        self.embed = lambda t: hash_embed(t, 64)

    def test_upsert_and_get(self):
        self.store.upsert("e1", self.embed("hello world"), content="hello world")
        e = self.store.get("e1")
        self.assertIsNotNone(e)
        self.assertEqual(e.content, "hello world")

    def test_search_returns_results(self):
        for i in range(5):
            self.store.upsert(f"d{i}", self.embed(f"document {i}"), content=f"doc {i}")
        q = self.embed("document 2")
        results = self.store.search(q, k=3)
        self.assertGreater(len(results), 0)

    def test_search_top_result_is_most_similar(self):
        self.store.upsert("fox", self.embed("the quick brown fox"), content="fox")
        self.store.upsert("dog", self.embed("the lazy dog"),        content="dog")
        self.store.upsert("cat", self.embed("a sleepy cat"),        content="cat")
        results = self.store.search(self.embed("quick fox"), k=3)
        self.assertGreater(results[0].score, 0)
        self.assertEqual(results[0].entry.content, "fox")

    def test_scores_sorted_descending(self):
        for i in range(4):
            self.store.upsert(f"x{i}", self.embed(f"item {i}"), content=f"item {i}")
        results = self.store.search(self.embed("item 1"), k=4)
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_namespace_isolation(self):
        self.store.upsert("n1", self.embed("text a"), namespace="ns_a")
        self.store.upsert("n2", self.embed("text b"), namespace="ns_b")
        results = self.store.search(self.embed("text a"), k=5, namespace="ns_a")
        self.assertTrue(all(r.entry.namespace == "ns_a" for r in results))

    def test_min_score_filter(self):
        self.store.upsert("f1", self.embed("completely different xyz abc"))
        results = self.store.search(self.embed("hello world"), k=5, min_score=0.99)
        # Very high threshold — likely 0 results
        self.assertIsInstance(results, list)

    def test_tag_filter(self):
        self.store.upsert("t1", self.embed("tagged doc"), tags=["important"])
        self.store.upsert("t2", self.embed("other doc"),  tags=["normal"])
        results = self.store.search(self.embed("doc"), k=5, tags=["important"])
        ids = [r.entry.id for r in results]
        self.assertIn("t1", ids)

    def test_filter_fn(self):
        self.store.upsert("m1", self.embed("meta doc"),
                           metadata={"category": "A"})
        self.store.upsert("m2", self.embed("meta doc"),
                           metadata={"category": "B"})
        results = self.store.search(
            self.embed("meta doc"), k=5,
            filter_fn=lambda e: e.metadata.get("category") == "A")
        self.assertTrue(all(r.entry.metadata.get("category") == "A"
                             for r in results))

    def test_delete(self):
        self.store.upsert("del1", self.embed("to delete"), content="delete me")
        ok = self.store.delete("del1")
        self.assertTrue(ok)
        self.assertIsNone(self.store.get("del1"))

    def test_delete_nonexistent(self):
        self.assertFalse(self.store.delete("no_such_id"))

    def test_upsert_overwrites(self):
        self.store.upsert("ov1", self.embed("old"), content="old")
        self.store.upsert("ov1", self.embed("new"), content="new")
        e = self.store.get("ov1")
        self.assertEqual(e.content, "new")

    def test_batch_upsert(self):
        entries = [{"eid": f"b{i}", "vector": self.embed(f"batch {i}"),
                     "content": f"batch {i}"} for i in range(5)]
        results = self.store.upsert_batch(entries)
        self.assertEqual(len(results), 5)

    def test_count(self):
        before = self.store.count()
        self.store.upsert("c1", self.embed("count test"))
        self.assertEqual(self.store.count(), before + 1)

    def test_namespaces_list(self):
        self.store.upsert("ns1", self.embed("a"), namespace="alpha")
        self.store.upsert("ns2", self.embed("b"), namespace="beta")
        ns = self.store.namespaces()
        self.assertIn("alpha", ns)
        self.assertIn("beta", ns)

    def test_dim_validation(self):
        with self.assertRaises(ValueError):
            self.store.upsert("bad", [0.1] * 32)   # wrong dim

    def test_persistence_reload(self):
        td = tempfile.mkdtemp()
        db = os.path.join(td, "persist.db")
        from agent.embedding_store import EmbeddingStore
        s1 = EmbeddingStore(db_path=db, dim=64)
        s1.upsert("p1", self.embed("persisted"), content="persisted")
        s2 = EmbeddingStore(db_path=db, dim=64)
        e = s2.get("p1")
        self.assertIsNotNone(e)
        self.assertEqual(e.content, "persisted")

    def test_search_many(self):
        self.store.upsert("sm1", self.embed("search many a"))
        queries = [self.embed("search many a"), self.embed("search many b")]
        all_results = self.store.search_many(queries, k=3)
        self.assertEqual(len(all_results), 2)

    def test_stats(self):
        self.store.upsert("st1", self.embed("stats"))
        self.store.search(self.embed("stats"), k=1)
        s = self.store.stats()
        for k in ["total_entries","dim","use_hnsw","avg_search_ms"]:
            self.assertIn(k, s)

    def test_hnsw_index_search(self):
        # Add > 20 entries to trigger HNSW path
        for i in range(25):
            self.store.upsert(f"h{i}", self.embed(f"hnsw item {i}"))
        results = self.store.search(self.embed("hnsw item 5"), k=5)
        self.assertGreater(len(results), 0)

    def test_result_to_dict(self):
        self.store.upsert("rd1", self.embed("to dict"), content="content")
        results = self.store.search(self.embed("to dict"), k=1)
        if results:
            d = results[0].to_dict()
            for k in ["id","score","namespace"]: self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# DOCUMENT PARSER
# ════════════════════════════════════════════════════════
class TestDocumentParser(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.document_parser import DocumentParser, SourceType, ChunkStrategy
        self.parser = DocumentParser(db_path=os.path.join(td,"dp.db"),
                                      chunk_size=200, overlap=40)
        self.ST = SourceType; self.CS = ChunkStrategy

    def test_parse_text(self):
        doc = self.parser.parse("Hello world. This is a test document.", source="test.txt")
        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.chunks), 0)

    def test_chunks_have_content(self):
        doc = self.parser.parse("A" * 500, source="big.txt")
        for ch in doc.chunks:
            self.assertGreater(len(ch.content), 0)

    def test_markdown_strips_syntax(self):
        md = "# Title\n\n**bold** and *italic* text here."
        doc = self.parser.parse(md, source_type=self.ST.MARKDOWN)
        content = " ".join(ch.content for ch in doc.chunks)
        self.assertNotIn("**", content)
        self.assertNotIn("# ", content)

    def test_markdown_extracts_title(self):
        md = "# My Article\n\nSome content here."
        doc = self.parser.parse(md, source_type=self.ST.MARKDOWN)
        self.assertIn("My Article", doc.title)

    def test_html_strips_tags(self):
        html = "<html><body><h1>Title</h1><p>Content here</p></body></html>"
        doc = self.parser.parse(html, source_type=self.ST.HTML)
        content = " ".join(ch.content for ch in doc.chunks)
        self.assertNotIn("<h1>", content)

    def test_json_flattens(self):
        data = json.dumps({"name": "Alice", "age": 30, "city": "NY"})
        doc = self.parser.parse(data, source_type=self.ST.JSON)
        content = " ".join(ch.content for ch in doc.chunks)
        self.assertIn("Alice", content)

    def test_csv_parses(self):
        csv_data = "name,age\nAlice,30\nBob,25"
        doc = self.parser.parse(csv_data, source_type=self.ST.CSV)
        content = " ".join(ch.content for ch in doc.chunks)
        self.assertIn("Alice", content)

    def test_chunk_indices(self):
        doc = self.parser.parse("A" * 1000)
        for i, ch in enumerate(doc.chunks):
            self.assertEqual(ch.chunk_index, i)
            self.assertEqual(ch.total_chunks, len(doc.chunks))

    def test_token_count_estimated(self):
        doc = self.parser.parse("word " * 100)
        for ch in doc.chunks:
            self.assertGreater(ch.token_count, 0)

    def test_word_count(self):
        doc = self.parser.parse("one two three four five")
        self.assertGreater(doc.word_count, 0)

    def test_metadata_propagated(self):
        doc = self.parser.parse("content", metadata={"author": "Bob"})
        for ch in doc.chunks:
            self.assertEqual(ch.metadata.get("author"), "Bob")

    def test_paragraph_strategy(self):
        from agent.document_parser import DocumentParser, ChunkStrategy
        td = tempfile.mkdtemp()
        p = DocumentParser(db_path=os.path.join(td,"p.db"),
                            chunk_size=200, strategy=ChunkStrategy.PARAGRAPH)
        text = "Para 1.\n\nPara 2.\n\nPara 3."
        doc = p.parse(text)
        self.assertGreater(len(doc.chunks), 0)

    def test_sentence_strategy(self):
        from agent.document_parser import DocumentParser, ChunkStrategy
        td = tempfile.mkdtemp()
        p = DocumentParser(db_path=os.path.join(td,"s.db"),
                            chunk_size=50, strategy=ChunkStrategy.SENTENCE)
        text = "First sentence. Second sentence. Third sentence here."
        doc = p.parse(text)
        self.assertGreater(len(doc.chunks), 0)

    def test_fixed_strategy(self):
        from agent.document_parser import DocumentParser, ChunkStrategy
        td = tempfile.mkdtemp()
        p = DocumentParser(db_path=os.path.join(td,"f.db"),
                            chunk_size=100, overlap=10,
                            strategy=ChunkStrategy.FIXED)
        text = "X" * 500
        doc = p.parse(text)
        for ch in doc.chunks:
            self.assertLessEqual(len(ch.content), 110)

    def test_deduplication(self):
        text = "Same sentence. Same sentence. Same sentence."
        doc = self.parser.parse(text)
        # Should deduplicate near-identical chunks
        self.assertLessEqual(len(doc.chunks), 3)

    def test_batch_parse(self):
        docs_input = [{"content": f"doc {i}", "source": f"d{i}.txt"}
                       for i in range(3)]
        docs = self.parser.parse_batch(docs_input)
        self.assertEqual(len(docs), 3)

    def test_chunks_iter(self):
        text = "Sentence one. Sentence two. Sentence three."
        chunks = list(self.parser.chunks_iter(text))
        self.assertGreater(len(chunks), 0)

    def test_language_hint(self):
        doc = self.parser.parse("Hello world in English")
        self.assertIn(doc.language, ["en", "zh/ja", "ru"])

    def test_parsed_doc_to_dict(self):
        doc = self.parser.parse("test content")
        d = doc.to_dict()
        for k in ["id","source_type","word_count","chunk_count"]:
            self.assertIn(k, d)

    def test_stats(self):
        self.parser.parse("stats test content here")
        s = self.parser.stats()
        for k in ["documents","chunks","chunk_size"]:
            self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# FEEDBACK COLLECTOR
# ════════════════════════════════════════════════════════
class TestFeedbackCollector(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.feedback_collector import FeedbackCollector, FeedbackType
        self.fc = FeedbackCollector(db_path=os.path.join(td,"fb.db"),
                                     allow_multiple=True)
        self.FT = FeedbackType

    def test_submit_rating(self):
        fb = self.fc.submit("item1", self.FT.RATING, rating=4.0)
        self.assertIsNotNone(fb)
        self.assertEqual(fb.rating, 4.0)

    def test_submit_thumbs_up(self):
        fb = self.fc.submit("item1", self.FT.THUMBS, thumbs=True)
        self.assertTrue(fb.thumbs)

    def test_submit_text(self):
        fb = self.fc.submit("item1", self.FT.TEXT, text="Great response!")
        self.assertEqual(fb.text, "Great response!")

    def test_rating_clamped(self):
        fb = self.fc.submit("item1", self.FT.RATING, rating=10.0)
        self.assertLessEqual(fb.rating, 5.0)
        fb2 = self.fc.submit("item1", self.FT.RATING, rating=-1.0)
        self.assertGreaterEqual(fb2.rating, 1.0)

    def test_aggregate_avg_rating(self):
        self.fc.submit("agg1", self.FT.RATING, rating=4.0)
        self.fc.submit("agg1", self.FT.RATING, rating=2.0)
        agg = self.fc.aggregate("agg1")
        self.assertAlmostEqual(agg.avg_rating, 3.0)

    def test_aggregate_thumbs_ratio(self):
        self.fc.submit("th1", self.FT.THUMBS, thumbs=True)
        self.fc.submit("th1", self.FT.THUMBS, thumbs=True)
        self.fc.submit("th1", self.FT.THUMBS, thumbs=False)
        agg = self.fc.aggregate("th1")
        self.assertAlmostEqual(agg.thumbs_ratio, 2/3, places=2)

    def test_aggregate_empty(self):
        agg = self.fc.aggregate("no_feedback_item")
        self.assertEqual(agg.total, 0)
        self.assertIsNone(agg.avg_rating)

    def test_sentiment_positive(self):
        fb = self.fc.submit("s1", self.FT.TEXT, text="great excellent helpful")
        self.assertGreater(fb.sentiment, 0)

    def test_sentiment_negative(self):
        fb = self.fc.submit("s2", self.FT.TEXT, text="bad wrong useless")
        self.assertLess(fb.sentiment, 0)

    def test_dedup_prevents_double_submit(self):
        fc2_td = tempfile.mkdtemp()
        from agent.feedback_collector import FeedbackCollector
        fc2 = FeedbackCollector(db_path=os.path.join(fc2_td,"fb2.db"),
                                 allow_multiple=False)
        r1 = fc2.submit("dedup1", self.FT.RATING, rating=4.0, user_id="u1")
        r2 = fc2.submit("dedup1", self.FT.RATING, rating=3.0, user_id="u1")
        self.assertIsNotNone(r1)
        self.assertIsNone(r2)

    def test_allow_multiple(self):
        r1 = self.fc.submit("multi1", self.FT.RATING, rating=4.0, user_id="u1")
        r2 = self.fc.submit("multi1", self.FT.RATING, rating=3.0, user_id="u1")
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)

    def test_low_rating_hook(self):
        alerts = []
        self.fc.on_low_rating(3.0, lambda item_id, avg: alerts.append((item_id, avg)))
        self.fc.submit("lr1", self.FT.RATING, rating=1.0)
        self.fc.submit("lr1", self.FT.RATING, rating=1.5)
        self.assertGreater(len(alerts), 0)

    def test_any_hook(self):
        received = []
        self.fc.on_feedback(lambda fb: received.append(fb.id))
        self.fc.submit("hook1", self.FT.RATING, rating=5.0)
        self.assertGreater(len(received), 0)

    def test_recent_returns_list(self):
        self.fc.submit("rc1", self.FT.RATING, rating=4.0)
        recent = self.fc.recent(hours=1)
        self.assertIsInstance(recent, list)
        self.assertGreater(len(recent), 0)

    def test_trends_returns_list(self):
        self.fc.submit("tr1", self.FT.RATING, rating=4.0)
        trends = self.fc.trends(bucket="day")
        self.assertIsInstance(trends, list)

    def test_aggregate_to_dict(self):
        self.fc.submit("td1", self.FT.RATING, rating=4.0)
        agg = self.fc.aggregate("td1")
        d = agg.to_dict()
        for k in ["item_id","total","thumbs_ratio","avg_sentiment"]:
            self.assertIn(k, d)

    def test_feedback_to_dict(self):
        fb = self.fc.submit("fb_dict", self.FT.RATING, rating=3.0)
        d = fb.to_dict()
        for k in ["id","item_id","type","rating","created_at"]:
            self.assertIn(k, d)

    def test_export(self):
        self.fc.submit("exp1", self.FT.RATING, rating=4.0)
        data = self.fc.export()
        self.assertIsInstance(data, list)

    def test_stats(self):
        self.fc.submit("st1", self.FT.RATING, rating=4.0)
        self.fc.submit("st2", self.FT.THUMBS, thumbs=True)
        s = self.fc.stats()
        for k in ["total","ratings","thumbs"]:
            self.assertIn(k, s)

    def test_tag_filter(self):
        self.fc.submit("tg1", self.FT.RATING, rating=5.0, tags=["premium"])
        recent = self.fc.recent(hours=1, tag="premium")
        self.assertGreater(len(recent), 0)

# ════════════════════════════════════════════════════════
# PROMPT OPTIMIZER
# ════════════════════════════════════════════════════════
class TestPromptOptimizer(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.prompt_optimizer import PromptOptimizer
        self.opt = PromptOptimizer(db_path=os.path.join(td,"po.db"),
                                    auto_promote_threshold=4.8,
                                    min_score_samples=2)

    def test_register_and_render(self):
        self.opt.register("greet", "Hello {{name}}!")
        r = self.opt.render("greet", {"name": "World"})
        self.assertIsNotNone(r)
        self.assertEqual(r.text, "Hello World!")

    def test_slots_filled_missing(self):
        self.opt.register("tmpl", "Say {{greeting}} to {{name}}!")
        r = self.opt.render("tmpl", {"greeting": "hi"})
        self.assertIn("greeting", r.slots_filled)
        self.assertIn("name", r.slots_missing)

    def test_add_and_render_variant(self):
        self.opt.register("qa", "Answer: {{question}}")
        self.opt.add_variant("qa", "formal",
                              "Please provide a detailed answer: {{question}}")
        r = self.opt.render("qa", {"question": "What is AI?"}, variant_label="formal")
        self.assertIn("detailed answer", r.text)

    def test_list_variants(self):
        self.opt.register("lv", "Template {{x}}")
        self.opt.add_variant("lv", "alt", "Alt {{x}}")
        variants = self.opt.list_variants("lv")
        self.assertEqual(len(variants), 2)

    def test_score_variant(self):
        self.opt.register("sc", "Score {{x}}")
        ok = self.opt.score("sc", "default", 4.0)
        self.assertTrue(ok)
        v = self.opt.get_variant("sc", "default")
        self.assertAlmostEqual(v.avg_score, 4.0)

    def test_best_variant_returns_highest_scored(self):
        self.opt.register("bv", "Template A {{x}}")
        self.opt.add_variant("bv", "better", "Template B {{x}}")
        self.opt.score("bv", "default", 2.0)
        self.opt.score("bv", "better", 4.5)
        best = self.opt.best_variant("bv")
        self.assertEqual(best.label, "better")

    def test_best_variant_unscored_returns_default(self):
        self.opt.register("usc", "Template {{x}}")
        best = self.opt.best_variant("usc")
        self.assertIsNotNone(best)

    def test_auto_promote(self):
        self.opt.register("ap", "Template {{x}}")
        self.opt.add_variant("ap", "promo", "Promo {{x}}")
        self.opt.score("ap", "promo", 4.9)
        self.opt.score("ap", "promo", 4.9)
        v = self.opt.get_variant("ap", "promo")
        self.assertTrue(v.is_default)

    def test_token_budget_trims(self):
        long_text = "Word " * 500
        self.opt.register("tb", long_text)
        r = self.opt.render("tb", {}, max_tokens=50)
        self.assertIsNotNone(r)
        self.assertLessEqual(r.token_count, 60)

    def test_few_shot_added_and_selected(self):
        self.opt.add_few_shot("input A", "output A", quality_score=1.0)
        self.opt.add_few_shot("input B", "output B", quality_score=1.0)
        self.opt.register("fs", "Context: {{query}}")
        r = self.opt.render("fs", {"query": "input A"}, few_shot_query="input A", few_shot_k=1)
        self.assertIn("output A", r.text)

    def test_persona_applied(self):
        self.opt.add_persona("assistant", "a helpful AI assistant")
        self.opt.set_persona("assistant")
        self.opt.register("p_tmpl", "You are {{persona}}. Answer: {{q}}")
        r = self.opt.render("p_tmpl", {"q": "hello?"})
        self.assertIn("assistant", r.text)

    def test_clear_persona(self):
        self.opt.add_persona("expert", "an expert")
        self.opt.set_persona("expert")
        self.opt.clear_persona()
        self.assertIsNone(self.opt._active_persona)

    def test_system_prompt_builder(self):
        sp = self.opt.build_system_prompt(
            role="an AI assistant",
            constraints=["Be concise", "Be accurate"],
            output_format="JSON",
            cot=True)
        self.assertIn("AI assistant", sp)
        self.assertIn("Be concise", sp)
        self.assertIn("JSON", sp)
        self.assertIn("step by step", sp)

    def test_diff(self):
        self.opt.register("df", "Template A: {{x}}")
        self.opt.add_variant("df", "v2", "Template B: {{x}} {{y}}")
        d = self.opt.diff("df", "default", "v2")
        self.assertIn("added_slots", d)
        self.assertIn("y", d["added_slots"])

    def test_token_budget_check(self):
        self.opt.register("tbc", "Hello {{name}}! " + "word " * 100)
        result = self.opt.token_budget("tbc", {"name": "Alice"}, max_tokens=20)
        self.assertIn("within_budget", result)

    def test_usage_count_increments(self):
        self.opt.register("uc", "Usage {{x}}")
        v_before = self.opt.get_variant("uc").usage_count
        self.opt.render("uc", {"x": "1"})
        v_after = self.opt.get_variant("uc").usage_count
        self.assertGreater(v_after, v_before)

    def test_render_nonexistent(self):
        r = self.opt.render("no_such_template", {})
        self.assertIsNone(r)

    def test_rendered_to_dict(self):
        self.opt.register("rtd", "Hello {{name}}!")
        r = self.opt.render("rtd", {"name": "World"})
        d = r.to_dict()
        for k in ["template_id","variant_id","text","token_count"]:
            self.assertIn(k, d)

    def test_variant_to_dict(self):
        self.opt.register("vtd", "Template")
        v = self.opt.get_variant("vtd")
        d = v.to_dict()
        for k in ["id","label","is_default","usage_count"]:
            self.assertIn(k, d)

    def test_stats(self):
        self.opt.register("st", "Stats {{x}}")
        self.opt.add_few_shot("in", "out")
        s = self.opt.stats()
        for k in ["templates","variants","few_shot_examples"]:
            self.assertIn(k, s)

import json  # needed for test_json_flattens

if __name__ == "__main__":
    import sys as _sys
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v31: {total-failed}/{total} passed")
    _sys.exit(0 if not failed else 1)
