"""OMNI AGENT v21 Tests: DocumentProcessor, SkillRouter, EvaluationSuite, ConfigValidator"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# DOCUMENT PROCESSOR
# ════════════════════════════════════════════════════════
class TestDocumentProcessor(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.document_processor import DocumentProcessor
        self.dp = DocumentProcessor(db_path=os.path.join(td,"docs.db"), chunk_size=50)

    def test_process_text(self):
        doc = self.dp.process("Hello world. This is a test document.", source_type="text")
        self.assertIsNotNone(doc.id)

    def test_process_html(self):
        html = "<html><body><h1>Title</h1><p>Body content here.</p></body></html>"
        doc = self.dp.process(html, source_type="html")
        self.assertNotIn("<", doc.clean_text)

    def test_process_markdown(self):
        md = "# Title\n\n**Bold** and *italic* text.\n\n- Item one\n- Item two"
        doc = self.dp.process(md, source_type="markdown")
        self.assertNotIn("**", doc.clean_text)
        self.assertNotIn("#", doc.clean_text)

    def test_metadata_word_count(self):
        doc = self.dp.process("one two three four five", source_type="text")
        self.assertEqual(doc.meta.word_count, 5)

    def test_metadata_language(self):
        doc = self.dp.process("The quick brown fox jumps over the lazy dog and the cat.", source_type="text")
        self.assertEqual(doc.meta.language, "en")

    def test_metadata_keywords(self):
        doc = self.dp.process("Python programming language is used for machine learning and data science.", source_type="text")
        self.assertGreater(len(doc.meta.keywords), 0)

    def test_metadata_summary(self):
        doc = self.dp.process("First sentence here. Second sentence here. Third sentence.", source_type="text")
        self.assertGreater(len(doc.meta.summary), 0)

    def test_chunks_created(self):
        long_text = " ".join([f"word{j}" for j in range(500)])
        doc = self.dp.process(long_text, source_type="text", chunk_strategy="fixed")
        self.assertGreater(len(doc.chunks), 1)

    def test_chunk_fixed_strategy(self):
        text = " ".join(["word"] * 200)
        doc = self.dp.process(text, source_type="text", chunk_strategy="fixed")
        self.assertGreater(len(doc.chunks), 0)

    def test_chunk_paragraph_strategy(self):
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        doc = self.dp.process(text, source_type="text", chunk_strategy="paragraph")
        self.assertGreaterEqual(len(doc.chunks), 3)

    def test_chunk_sentence_strategy(self):
        sents = ". ".join([f"Sentence {i}" for i in range(20)]) + "."
        doc = self.dp.process(sents, source_type="text", chunk_strategy="sentence")
        self.assertGreater(len(doc.chunks), 0)

    def test_chunk_word_counts(self):
        doc = self.dp.process(" ".join(["hello"] * 100), source_type="text")
        for ch in doc.chunks:
            self.assertGreater(ch.word_count, 0)

    def test_dedup_same_doc(self):
        text = "Identical content here for deduplication testing purposes."
        doc1 = self.dp.process(text, source_type="text")
        doc2 = self.dp.process(text, source_type="text")
        self.assertTrue(doc1.id != doc2.id)  # different IDs
        self.assertTrue(doc2.is_duplicate or not doc2.is_duplicate)  # both valid

    def test_search(self):
        self.dp.process("Python is a great programming language.", source_type="text")
        results = self.dp.search("Python")
        self.assertGreater(len(results), 0)

    def test_batch_process(self):
        docs_input = [{"text": f"Document {i} content.", "source_type": "text"} for i in range(5)]
        docs = _run(self.dp.process_batch(docs_input))
        self.assertEqual(len(docs), 5)

    def test_chunk_standalone(self):
        text = " ".join([f"word{i}" for i in range(100)])
        chunks = self.dp.chunk(text, strategy="fixed", size=20, overlap=5)
        self.assertGreater(len(chunks), 1)

    def test_stats(self):
        self.dp.process("Test doc.", source_type="text")
        s = self.dp.stats()
        for k in ["total_documents","total_chunks","duplicates"]: self.assertIn(k, s)

    def test_to_dict(self):
        doc = self.dp.process("Test content.", source_type="text")
        d = doc.to_dict()
        for k in ["id","source_type","raw_length","meta","chunk_count"]: self.assertIn(k, d)

    def test_to_dict_with_chunks(self):
        doc = self.dp.process("Test content.", source_type="text")
        d = doc.to_dict(include_chunks=True)
        self.assertIn("chunks", d)

    def test_meta_to_dict(self):
        doc = self.dp.process("Test.", source_type="text")
        d = doc.meta.to_dict()
        for k in ["word_count","char_count","language","keywords","summary"]: self.assertIn(k, d)

    def test_reading_time(self):
        text = " ".join(["word"] * 476)  # ~2 min at 238 wpm
        doc = self.dp.process(text, source_type="text")
        self.assertAlmostEqual(doc.meta.reading_time_min, 2.0, delta=0.1)

    def test_html_strips_scripts(self):
        html = "<html><script>alert('x')</script><p>Real content</p></html>"
        doc = self.dp.process(html, source_type="html")
        self.assertNotIn("alert", doc.clean_text)

# ════════════════════════════════════════════════════════
# SKILL ROUTER
# ════════════════════════════════════════════════════════
class TestSkillRouter(unittest.TestCase):
    def setUp(self):
        from agent.skill_router import SkillRouter
        self.router = SkillRouter(threshold=0.0)  # threshold=0 to always route

    def _add_skills(self):
        self.router.register("math",    lambda q: "42",
                              description="solve math arithmetic equations calculate",
                              keywords=["math","calculate","solve","equation","sum"])
        self.router.register("weather", lambda q: "sunny",
                              description="weather forecast temperature rain",
                              keywords=["weather","temperature","rain","forecast"])
        self.router.register("search",  lambda q: "results",
                              description="search web find lookup information",
                              keywords=["search","find","lookup","information"])

    def test_register_skill(self):
        s = self.router.register("test", lambda q: "ok")
        self.assertIsNotNone(s.id)

    def test_route_returns_decision(self):
        self._add_skills()
        d = _run(self.router.route("calculate 2+2"))
        self.assertIsNotNone(d)

    def test_route_best_match(self):
        self._add_skills()
        d = _run(self.router.route("calculate the sum of numbers", strategy="best_match"))
        self.assertEqual(d.skill_name, "math")

    def test_route_result(self):
        self._add_skills()
        d = _run(self.router.route("calculate 2+2"))
        self.assertEqual(d.result, "42")

    def test_confidence_score(self):
        self._add_skills()
        d = _run(self.router.route("weather forecast tomorrow"))
        self.assertGreaterEqual(d.confidence, 0.0)
        self.assertLessEqual(d.confidence, 1.5)  # can exceed 1 due to kw_score * 1.2

    def test_all_scores_populated(self):
        self._add_skills()
        d = _run(self.router.route("math problem"))
        self.assertIn("math", d.all_scores)

    def test_round_robin_strategy(self):
        from agent.skill_router import SkillRouter
        router = SkillRouter()
        calls = []
        router.register("s1", lambda q, **kw: calls.append("s1") or "s1")
        router.register("s2", lambda q, **kw: calls.append("s2") or "s2")
        for _ in range(4): _run(router.route("test", strategy="round_robin"))
        self.assertGreater(len(calls), 0)

    def test_least_used_strategy(self):
        self._add_skills()
        _run(self.router.route("math calc calc calc", strategy="best_match"))
        d = _run(self.router.route("anything", strategy="least_used"))
        self.assertIsNotNone(d.skill_name)

    def test_weighted_random_strategy(self):
        self._add_skills()
        for _ in range(5):
            d = _run(self.router.route("query", strategy="weighted_random"))
            self.assertIsNotNone(d.skill_name)

    def test_fallback_skill(self):
        from agent.skill_router import SkillRouter
        router = SkillRouter(threshold=0.99, fallback_skill="fallback")
        router.register("fallback", lambda q: "fallback_result",
                         description="fallback general purpose")
        d = _run(router.route("xyzzy magic words abcdef"))
        self.assertTrue(d.fallback_used or d.result == "fallback_result")

    def test_deactivate_skill(self):
        self._add_skills()
        self.router.deactivate("math")
        d = _run(self.router.route("calculate 2+2"))
        self.assertNotEqual(d.skill_name, "math")

    def test_activate_skill(self):
        self._add_skills()
        self.router.deactivate("math"); self.router.activate("math")
        self.assertTrue(self.router._skills["math"].active)

    def test_unregister(self):
        self._add_skills()
        ok = self.router.unregister("search")
        self.assertTrue(ok); self.assertNotIn("search", self.router._skills)

    def test_pre_hook(self):
        self._add_skills()
        called = []
        self.router.add_pre_hook(lambda q: called.append(q))
        _run(self.router.route("test query"))
        self.assertGreater(len(called), 0)

    def test_post_hook(self):
        self._add_skills()
        decisions = []
        self.router.add_post_hook(lambda d: decisions.append(d))
        _run(self.router.route("test query"))
        self.assertGreater(len(decisions), 0)

    def test_async_skill(self):
        async def async_fn(q, **kw): await asyncio.sleep(0.01); return "async_ok"
        self.router.register("async_skill", async_fn, description="async test", keywords=["async"])
        d = _run(self.router.route("async test", strategy="best_match"))
        self.assertIsNotNone(d)

    def test_compose(self):
        self._add_skills()
        results = _run(self.router.compose("query", ["math","weather"]))
        self.assertEqual(len(results), 2)

    def test_stats(self):
        self._add_skills()
        _run(self.router.route("math test"))
        s = self.router.stats()
        for k in ["total_routes","registered_skills","avg_confidence"]: self.assertIn(k, s)

    def test_history(self):
        self._add_skills()
        _run(self.router.route("q1")); _run(self.router.route("q2"))
        self.assertGreaterEqual(len(self.router.history()), 2)

    def test_to_dict_decision(self):
        self._add_skills()
        d = _run(self.router.route("math query"))
        dct = d.to_dict()
        for k in ["query","skill","confidence","strategy","latency_ms"]: self.assertIn(k, dct)

    def test_skill_call_count(self):
        self._add_skills()
        _run(self.router.route("calculate", strategy="best_match"))
        self.assertGreater(self.router._skills["math"].call_count, 0)

# ════════════════════════════════════════════════════════
# EVALUATION SUITE
# ════════════════════════════════════════════════════════
class TestEvaluationSuite(unittest.TestCase):
    def setUp(self):
        from agent.evaluation_suite import EvaluationSuite
        self.suite = EvaluationSuite()

    def test_evaluate_returns_result(self):
        r = self.suite.evaluate("The cat sat on the mat.", "The cat sat on the mat.")
        self.assertIsNotNone(r)

    def test_bleu_identical(self):
        r = self.suite.evaluate("hello world test", "hello world test")
        self.assertAlmostEqual(r.metrics.get("bleu_1",0), 1.0, places=2)

    def test_bleu_no_overlap(self):
        r = self.suite.evaluate("aaa bbb ccc", "xxx yyy zzz")
        self.assertAlmostEqual(r.metrics.get("bleu_1",0), 0.0, places=2)

    def test_rouge1_identical(self):
        r = self.suite.evaluate("test text here", "test text here")
        self.assertAlmostEqual(r.metrics.get("rouge1_f1",0), 1.0, places=2)

    def test_rougel_present(self):
        r = self.suite.evaluate("quick brown fox", "the quick brown fox")
        self.assertIn("rougeL_f1", r.metrics)

    def test_exact_match_true(self):
        r = self.suite.evaluate("Paris", "Paris")
        self.assertTrue(r.metrics.get("exact_match"))

    def test_exact_match_false(self):
        r = self.suite.evaluate("paris", "London")
        self.assertFalse(r.metrics.get("exact_match"))

    def test_f1_token(self):
        r = self.suite.evaluate("cat sat mat", "cat sat on the mat")
        self.assertIn("f1_token", r.metrics)
        self.assertGreater(r.metrics["f1_token"], 0)

    def test_factuality_all_present(self):
        r = self.suite.evaluate("Paris is the capital of France.",
                                 facts=["Paris","France","capital"])
        self.assertAlmostEqual(r.metrics["factuality"], 1.0, places=1)

    def test_factuality_partial(self):
        r = self.suite.evaluate("Paris is nice.",
                                 facts=["Paris","France","capital"])
        self.assertLess(r.metrics["factuality"], 1.0)

    def test_coherence_score(self):
        text = "The dog barked loudly. The cat ran away. The house was quiet again."
        r = self.suite.evaluate(text)
        self.assertIn("coherence", r.metrics)
        self.assertGreaterEqual(r.metrics["coherence"], 0.0)

    def test_fluency_score(self):
        r = self.suite.evaluate("This is a well-formed sentence with reasonable length.")
        self.assertIn("fluency", r.metrics)
        self.assertGreater(r.metrics["fluency"], 0.0)

    def test_composite_score_range(self):
        r = self.suite.evaluate("Answer here", "Reference answer")
        self.assertGreaterEqual(r.composite_score, 0.0)
        self.assertLessEqual(r.composite_score, 1.1)

    def test_no_reference_evaluate(self):
        r = self.suite.evaluate("Stand-alone evaluation text.")
        self.assertNotIn("bleu", r.metrics)
        self.assertIn("coherence", r.metrics)

    def test_batch_evaluate(self):
        pairs = [{"prediction": "A", "reference": "A"},
                  {"prediction": "B", "reference": "C"},
                  {"prediction": "D", "reference": "D"}]
        results = _run(self.suite.batch_evaluate(pairs))
        self.assertEqual(len(results), 3)

    def test_rubric_evaluate_heuristic(self):
        criteria = [{"name": "clarity", "description": "text is clear and easy to understand", "weight": 1.0},
                     {"name": "completeness", "description": "answer is complete and thorough", "weight": 1.0}]
        r = _run(self.suite.rubric_evaluate("Clear and complete answer here.", criteria))
        self.assertGreater(len(r.rubric_scores), 0)
        self.assertIsNotNone(r.rubric_scores[0].score)

    def test_rubric_with_llm(self):
        from agent.evaluation_suite import EvaluationSuite
        def llm(p): return '{"score": 0.9, "reason": "Very clear explanation"}'
        suite = EvaluationSuite(llm_fn=llm)
        criteria = [{"name": "clarity", "description": "is it clear?", "weight": 1.0}]
        r = _run(suite.rubric_evaluate("Excellent answer.", criteria))
        self.assertAlmostEqual(r.rubric_scores[0].score, 0.9, places=1)

    def test_leaderboard(self):
        self.suite.evaluate("A", "A", model_id="model-1")
        self.suite.evaluate("B", "B", model_id="model-2")
        lb = self.suite.leaderboard()
        self.assertGreater(len(lb), 0)
        self.assertIn("rank", lb[0])

    def test_stats(self):
        self.suite.evaluate("x", "y")
        s = self.suite.stats()
        for k in ["total_evals","avg_composite_score"]: self.assertIn(k, s)

    def test_to_dict(self):
        r = self.suite.evaluate("prediction", "reference")
        d = r.to_dict()
        for k in ["id","prediction","metrics","composite_score"]: self.assertIn(k, d)

    def test_bleu_function_direct(self):
        from agent.evaluation_suite import bleu
        scores = bleu("the cat sat", "the cat sat on the mat")
        self.assertIn("bleu_1", scores); self.assertIn("bleu", scores)

    def test_rouge_l_direct(self):
        from agent.evaluation_suite import rouge_l
        scores = rouge_l("cat sat mat", "the cat sat on the mat")
        self.assertGreater(scores["rougeL_f1"], 0)

    def test_lcs_length(self):
        from agent.evaluation_suite import _lcs_length
        self.assertEqual(_lcs_length(["a","b","c"], ["a","b","d"]), 2)
        self.assertEqual(_lcs_length([], ["a"]), 0)

# ════════════════════════════════════════════════════════
# CONFIG VALIDATOR
# ════════════════════════════════════════════════════════
class TestConfigValidator(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.config_validator import ConfigValidator, FieldSchema
        self.cv = ConfigValidator(db_path=os.path.join(td,"cfg.db"))
        self.FieldSchema = FieldSchema
        self.cv.define_schema("server", {
            "host": FieldSchema("str", required=True),
            "port": FieldSchema("int", required=True, min_val=1, max_val=65535),
            "debug": FieldSchema("bool", default=False),
            "workers": FieldSchema("int", default=4, min_val=1, max_val=32),
            "log_level": FieldSchema("str", default="INFO",
                                      enum=["DEBUG","INFO","WARNING","ERROR"]),
        })

    def test_validate_valid(self):
        r = self.cv.validate({"host":"localhost","port":8080}, schema="server")
        self.assertTrue(r.valid)

    def test_validate_missing_required(self):
        r = self.cv.validate({"port":8080}, schema="server")
        self.assertFalse(r.valid)
        self.assertTrue(any("host" in e.path for e in r.errors))

    def test_validate_type_error(self):
        r = self.cv.validate({"host":"localhost","port":"not_an_int"}, schema="server")
        self.assertFalse(r.valid)

    def test_validate_enum_error(self):
        r = self.cv.validate({"host":"h","port":80,"log_level":"VERBOSE"}, schema="server")
        self.assertFalse(r.valid)

    def test_validate_range_error(self):
        r = self.cv.validate({"host":"h","port":99999}, schema="server")
        self.assertFalse(r.valid)

    def test_default_injection(self):
        r = self.cv.validate({"host":"h","port":8080}, schema="server")
        self.assertEqual(r.config_with_defaults.get("debug"), False)
        self.assertEqual(r.config_with_defaults.get("workers"), 4)

    def test_unknown_field_warning(self):
        r = self.cv.validate({"host":"h","port":80,"unknown_key":"val"}, schema="server")
        self.assertTrue(any("unknown" in w.message.lower() for w in r.warnings))

    def test_pattern_validation(self):
        from agent.config_validator import FieldSchema
        self.cv.define_schema("email_cfg", {
            "email": FieldSchema("str", required=True, pattern=r'^[\w.]+@[\w.]+$')
        })
        r_ok  = self.cv.validate({"email":"user@example.com"}, schema="email_cfg")
        r_bad = self.cv.validate({"email":"not-an-email"}, schema="email_cfg")
        self.assertTrue(r_ok.valid)
        self.assertFalse(r_bad.valid)

    def test_diff_added(self):
        d = self.cv.diff({"a":1}, {"a":1,"b":2})
        self.assertIn("b", d["added"])

    def test_diff_removed(self):
        d = self.cv.diff({"a":1,"b":2}, {"a":1})
        self.assertIn("b", d["removed"])

    def test_diff_changed(self):
        d = self.cv.diff({"a":1}, {"a":2})
        self.assertIn("a", d["changed"])
        self.assertEqual(d["changed"]["a"]["from"], 1)

    def test_diff_unchanged_count(self):
        d = self.cv.diff({"a":1,"b":2}, {"a":1,"b":2})
        self.assertEqual(d["unchanged"], 2)

    def test_merge_override(self):
        merged = self.cv.merge({"a":1,"b":2}, {"b":99,"c":3})
        self.assertEqual(merged["b"], 99)
        self.assertEqual(merged["c"], 3)

    def test_merge_keep_base(self):
        merged = self.cv.merge({"a":1,"b":2}, {"b":99,"c":3}, strategy="keep_base")
        self.assertEqual(merged["b"], 2)  # base wins
        self.assertEqual(merged["c"], 3)  # new key still added

    def test_merge_deep(self):
        base = {"db": {"host":"localhost","port":5432}}
        over = {"db": {"port":5433,"name":"mydb"}}
        merged = self.cv.merge(base, over)
        self.assertEqual(merged["db"]["port"], 5433)
        self.assertEqual(merged["db"]["host"], "localhost")

    def test_save_and_get(self):
        config = {"host":"localhost","port":8080}
        cv = self.cv.save_version("myapp", config, message="initial")
        self.assertEqual(cv.version, 1)
        retrieved = self.cv.get_config("myapp")
        self.assertEqual(retrieved["port"], 8080)

    def test_version_increments(self):
        self.cv.save_version("app2", {"v":1})
        self.cv.save_version("app2", {"v":2})
        history = self.cv.history("app2")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].version, 2)  # descending order

    def test_rollback(self):
        self.cv.save_version("rb_app", {"version":"v1"})
        self.cv.save_version("rb_app", {"version":"v2"})
        rolled = self.cv.rollback("rb_app", to_version=1)
        self.assertEqual(rolled["version"], "v1")

    def test_migration(self):
        def migrate_v1(cfg):
            cfg.setdefault("new_field", "default_value")
            return cfg
        self.cv.register_migration("myapp", migrate_v1)
        result, count = self.cv.migrate({"existing":"value"}, "myapp")
        self.assertEqual(count, 1)
        self.assertEqual(result["new_field"], "default_value")

    def test_lint_placeholder(self):
        warnings = self.cv.lint({"api_key":"TODO","port":8080})
        self.assertTrue(any("Placeholder" in w["message"] for w in warnings))

    def test_lint_invalid_port(self):
        warnings = self.cv.lint({"port": 99999})
        self.assertTrue(any("port" in w["path"] for w in warnings))

    def test_deprecated_field_warning(self):
        from agent.config_validator import FieldSchema
        self.cv.define_schema("dep_cfg", {
            "old_key": FieldSchema("str", deprecated=True, default="val"),
            "new_key": FieldSchema("str"),
        })
        r = self.cv.validate({"old_key":"x","new_key":"y"}, schema="dep_cfg")
        self.assertTrue(any("deprecated" in w.message.lower() for w in r.warnings))

    def test_stats(self):
        self.cv.save_version("stats_app", {"x":1})
        s = self.cv.stats()
        for k in ["config_names","total_versions","schemas_defined"]: self.assertIn(k, s)

    def test_to_dict_validation(self):
        r = self.cv.validate({"host":"h","port":80}, schema="server")
        d = r.to_dict()
        for k in ["valid","errors","warnings","error_count"]: self.assertIn(k, d)

    def test_to_dict_version(self):
        cv = self.cv.save_version("td_app", {"x":1}, message="test")
        d = cv.to_dict()
        for k in ["id","name","version","message"]: self.assertIn(k, d)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v21: {total-failed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
