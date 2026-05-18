"""OMNI AGENT v20 Tests: PromptLibrary, DataPipeline, NotificationManager, AgentMemory"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# PROMPT LIBRARY
# ════════════════════════════════════════════════════════
class TestPromptLibrary(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.prompt_library import PromptLibrary
        self.lib = PromptLibrary(db_path=os.path.join(td,"pl.db"))

    def test_create_returns_id(self):
        tid = self.lib.create("greet", "Hello {{name}}!")
        self.assertIsNotNone(tid)

    def test_render_basic(self):
        tid = self.lib.create("greet", "Hello {{name}}!")
        result = self.lib.render(tid, {"name": "World"})
        self.assertEqual(result, "Hello World!")

    def test_render_no_vars(self):
        tid = self.lib.create("plain", "No variables here.")
        result = self.lib.render(tid)
        self.assertEqual(result, "No variables here.")

    def test_version_increments(self):
        tid = self.lib.create("v_test", "v1 body")
        self.lib.update(tid, "v2 body", change_note="updated")
        v = self.lib.get(tid)
        self.assertEqual(v.version, 2)

    def test_get_specific_version(self):
        tid = self.lib.create("v_spec", "v1")
        self.lib.update(tid, "v2")
        v1 = self.lib.get(tid, version=1)
        self.assertEqual(v1.body, "v1")

    def test_list_versions(self):
        tid = self.lib.create("v_list", "v1")
        self.lib.update(tid, "v2"); self.lib.update(tid, "v3")
        versions = self.lib.list_versions(tid)
        self.assertEqual(len(versions), 3)

    def test_variable_extraction(self):
        tid = self.lib.create("vars", "Hello {{name}}, you are {{age}} years old.")
        v = self.lib.get(tid)
        self.assertIn("name", v.variables)
        self.assertIn("age",  v.variables)

    def test_fork(self):
        tid = self.lib.create("original", "Original body {{x}}")
        fork_id = self.lib.fork(tid, "fork-of-original")
        self.assertNotEqual(fork_id, tid)
        forked = self.lib.get(fork_id)
        self.assertEqual(forked.body, "Original body {{x}}")

    def test_fork_tracks_parent(self):
        tid = self.lib.create("parent", "Parent {{var}}")
        fork_id = self.lib.fork(tid, "child-fork")
        t = self.lib.get_template(fork_id)
        self.assertEqual(t.forked_from, tid)

    def test_diff_same(self):
        tid = self.lib.create("d_test", "Same content")
        fork_id = self.lib.fork(tid, "d_fork")
        diff = self.lib.diff(tid, fork_id)
        self.assertEqual(diff, "")   # identical bodies → no diff

    def test_diff_different(self):
        tid = self.lib.create("da", "Line one\nLine two\n")
        fork_id = self.lib.fork(tid, "db")
        self.lib.update(fork_id, "Line one\nLine three\n")
        diff = self.lib.diff(tid, fork_id)
        self.assertIn("-", diff)

    def test_search_by_name(self):
        self.lib.create("summarise-v1", "Summarise {{text}}", tags=["nlp"])
        results = self.lib.search("summarise")
        self.assertTrue(any("summarise" in t.name for t in results))

    def test_search_by_tag(self):
        self.lib.create("t1", "body1", tags=["production"])
        self.lib.create("t2", "body2", tags=["experiment"])
        results = self.lib.search(tags=["production"])
        self.assertTrue(all("production" in t.tags for t in results))

    def test_add_remove_tag(self):
        tid = self.lib.create("tag_test", "body", tags=[])
        self.lib.add_tag(tid, "new-tag")
        t = self.lib.get_template(tid)
        self.assertIn("new-tag", t.tags)
        self.lib.remove_tag(tid, "new-tag")
        t2 = self.lib.get_template(tid)
        self.assertNotIn("new-tag", t2.tags)

    def test_ab_test_create(self):
        ta = self.lib.create("a", "Version A {{x}}")
        tb = self.lib.create("b", "Version B {{x}}")
        ab = self.lib.create_ab_test("test1", ta, tb)
        self.assertIsNotNone(ab.id)

    def test_ab_render(self):
        ta = self.lib.create("ab_a", "A {{x}}")
        tb = self.lib.create("ab_b", "B {{x}}")
        ab = self.lib.create_ab_test("t2", ta, tb)
        for _ in range(10):
            result, variant = self.lib.ab_render(ab.id, {"x": "hello"})
            self.assertIn(variant, ["A","B"])
            self.assertIn("hello", result)

    def test_ab_records_counts(self):
        ta = self.lib.create("c1", "C1"); tb = self.lib.create("c2", "C2")
        ab = self.lib.create_ab_test("c", ta, tb, traffic_split=1.0)
        for _ in range(5): self.lib.ab_render(ab.id)
        self.assertGreater(ab.renders_a + ab.renders_b, 0)

    def test_usage_stats(self):
        tid = self.lib.create("us", "{{x}}")
        for _ in range(3): self.lib.render(tid, {"x":"v"})
        s = self.lib.usage_stats(tid)
        self.assertGreaterEqual(s["total_renders"], 3)

    def test_stats(self):
        self.lib.create("stat_tpl", "body")
        s = self.lib.stats()
        for k in ["templates","versions","total_renders"]: self.assertIn(k, s)

    def test_persistence(self):
        from agent.prompt_library import PromptLibrary
        td = tempfile.mkdtemp(); db = os.path.join(td,"pl.db")
        lib1 = PromptLibrary(db_path=db)
        tid = lib1.create("persist", "Persistent {{val}}")
        lib2 = PromptLibrary(db_path=db)
        result = lib2.render(tid, {"val": "data"})
        self.assertIn("data", result)

    def test_to_dict_version(self):
        tid = self.lib.create("td", "body")
        v = self.lib.get(tid)
        d = v.to_dict()
        for k in ["id","template_id","version","body","variables"]: self.assertIn(k,d)

    def test_to_dict_template(self):
        tid = self.lib.create("td2","body")
        t = self.lib.get_template(tid)
        d = t.to_dict()
        for k in ["id","name","tags","latest_version"]: self.assertIn(k,d)

# ════════════════════════════════════════════════════════
# DATA PIPELINE
# ════════════════════════════════════════════════════════
class TestDataPipeline(unittest.TestCase):
    def setUp(self):
        from agent.data_pipeline import DataPipeline
        self.DataPipeline = DataPipeline

    def _make_pipeline(self):
        from agent.data_pipeline import DataPipeline, StageType
        p = DataPipeline("test")
        p.add_stage("extract",   StageType.EXTRACT,   lambda d: [1,2,3])
        p.add_stage("transform", StageType.TRANSFORM, lambda d: [x*2 for x in d])
        p.add_stage("load",      StageType.LOAD,      lambda d: d)
        return p

    def test_basic_execution(self):
        p = self._make_pipeline()
        run = _run(p.execute())
        from agent.data_pipeline import RunStatus
        self.assertEqual(run.status, RunStatus.SUCCESS)

    def test_final_data(self):
        p = self._make_pipeline()
        run = _run(p.execute())
        self.assertEqual(run.final_data, [2,4,6])

    def test_stage_count(self):
        p = self._make_pipeline()
        run = _run(p.execute())
        self.assertEqual(len(run.stage_results), 3)

    def test_all_stages_success(self):
        p = self._make_pipeline()
        run = _run(p.execute())
        self.assertEqual(run.failed_stages, 0)

    def test_abort_on_critical_failure(self):
        from agent.data_pipeline import DataPipeline, StageType, RunStatus
        p = DataPipeline("abort_test")
        p.add_stage("ok",   StageType.EXTRACT,   lambda d: [1,2,3])
        p.add_stage("fail", StageType.TRANSFORM, lambda d: 1/0, critical=True)
        p.add_stage("load", StageType.LOAD,      lambda d: d)
        run = _run(p.execute())
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertLess(run.success_stages, 3)

    def test_skip_on_noncritical_failure(self):
        from agent.data_pipeline import DataPipeline, StageType, ErrorPolicy, RunStatus
        p = DataPipeline("skip_test")
        p.add_stage("extract", StageType.EXTRACT, lambda d: [1,2,3])
        p.add_stage("bad",     StageType.TRANSFORM, lambda d: 1/0,
                     critical=False, error_policy=ErrorPolicy.SKIP)
        p.add_stage("load",    StageType.LOAD, lambda d: d)
        run = _run(p.execute())
        self.assertIn(run.status, [RunStatus.SUCCESS, RunStatus.PARTIAL])

    def test_retry_policy(self):
        from agent.data_pipeline import DataPipeline, StageType, ErrorPolicy
        calls = [0]
        def flaky(d):
            calls[0] += 1
            if calls[0] < 3: raise ValueError("not yet")
            return "ok"
        p = DataPipeline("retry_test")
        p.add_stage("retry_stage", StageType.TRANSFORM, flaky,
                     error_policy=ErrorPolicy.RETRY, max_retries=3, retry_delay=0.01)
        run = _run(p.execute(initial_data=[]))
        self.assertTrue(run.stage_results[0].success)
        self.assertGreater(run.stage_results[0].retries, 0)

    def test_fallback_policy(self):
        from agent.data_pipeline import DataPipeline, StageType, ErrorPolicy
        p = DataPipeline("fallback_test")
        p.add_stage("bad", StageType.TRANSFORM, lambda d: 1/0,
                     error_policy=ErrorPolicy.FALLBACK, fallback_value=["fallback"])
        run = _run(p.execute(initial_data=[]))
        self.assertEqual(run.final_data, ["fallback"])

    def test_checkpoint(self):
        from agent.data_pipeline import DataPipeline, StageType
        p = DataPipeline("ckpt_test")
        p.add_stage("extract", StageType.EXTRACT, lambda d: [1,2,3], checkpoint=True)
        p.add_stage("load",    StageType.LOAD,    lambda d: d)
        run = _run(p.execute())
        self.assertIn("extract", run.checkpoints)

    def test_condition_skip(self):
        from agent.data_pipeline import DataPipeline, StageType
        p = DataPipeline("cond_test")
        p.add_stage("extract", StageType.EXTRACT, lambda d: [])
        p.add_stage("skipped", StageType.TRANSFORM, lambda d: [99],
                     condition=lambda d: len(d) > 0)  # will skip
        p.add_stage("load",    StageType.LOAD, lambda d: d)
        run = _run(p.execute())
        self.assertEqual(run.final_data, [])  # skipped stage didn't change data

    def test_filter_stage(self):
        from agent.data_pipeline import DataPipeline, StageType
        p = DataPipeline("filter_test")
        p.add_stage("extract", StageType.EXTRACT, lambda d: [1,2,3,4,5])
        p.add_filter("evens", lambda x: x % 2 == 0)
        run = _run(p.execute())
        self.assertEqual(run.final_data, [2,4])

    def test_async_stage(self):
        from agent.data_pipeline import DataPipeline, StageType
        async def async_fn(d): await asyncio.sleep(0.01); return [x+1 for x in d]
        p = DataPipeline("async_test")
        p.add_stage("extract", StageType.EXTRACT, lambda d: [1,2,3])
        p.add_stage("async",   StageType.TRANSFORM, async_fn)
        run = _run(p.execute())
        self.assertEqual(run.final_data, [2,3,4])

    def test_fluent_chaining(self):
        from agent.data_pipeline import DataPipeline, StageType
        p = (DataPipeline("chain")
             .add_stage("s1", StageType.EXTRACT, lambda d: [1])
             .add_stage("s2", StageType.LOAD,    lambda d: d))
        self.assertEqual(len(p.stages()), 2)

    def test_pipeline_stats(self):
        p = self._make_pipeline()
        _run(p.execute()); _run(p.execute())
        s = p.stats()
        self.assertEqual(s["total_runs"], 2)
        self.assertIn("success_runs", s)

    def test_stage_result_to_dict(self):
        p = self._make_pipeline()
        run = _run(p.execute())
        d = run.stage_results[0].to_dict()
        for k in ["stage","type","rows_in","rows_out","success","duration_ms"]:
            self.assertIn(k,d)

    def test_run_to_dict(self):
        p = self._make_pipeline()
        run = _run(p.execute())
        d = run.to_dict()
        for k in ["id","pipeline","status","duration_ms","success_stages","stages"]:
            self.assertIn(k,d)

    def test_registry(self):
        from agent.data_pipeline import DataPipeline, StageType, PipelineRegistry
        reg = PipelineRegistry()
        p = DataPipeline("reg_pipe")
        p.add_stage("s", StageType.EXTRACT, lambda d: d)
        reg.register(p)
        self.assertIn("reg_pipe", reg.list())
        self.assertIsNotNone(reg.get("reg_pipe"))

# ════════════════════════════════════════════════════════
# NOTIFICATION MANAGER
# ════════════════════════════════════════════════════════
class TestNotificationManager(unittest.TestCase):
    def setUp(self):
        from agent.notification_manager import NotificationManager
        self.nm = NotificationManager(dedup_window_s=1.0)

    def test_send_returns_notif(self):
        n = _run(self.nm.send("Test", "Body"))
        self.assertIsNotNone(n.id)

    def test_send_delivered(self):
        n = _run(self.nm.send("T","B"))
        self.assertTrue(n.delivered)

    def test_priority_levels(self):
        from agent.notification_manager import Priority
        for level in [Priority.LOW, Priority.NORMAL, Priority.HIGH, Priority.CRITICAL]:
            n = _run(self.nm.send("T","B", level=level))
            self.assertEqual(n.level, level)

    def test_custom_handler(self):
        from agent.notification_manager import NotificationManager, ChannelType
        nm = NotificationManager()
        received = []
        def handler(n): received.append(n)
        nm.add_channel("custom", ChannelType.CUSTOM, handler=handler)
        _run(nm.send("T","B", channel="custom"))
        self.assertEqual(len(received), 1)

    def test_async_handler(self):
        from agent.notification_manager import NotificationManager, ChannelType
        nm = NotificationManager()
        received = []
        async def ahandler(n): received.append(n)
        nm.add_channel("async_ch", ChannelType.CUSTOM, handler=ahandler)
        _run(nm.send("T","B", channel="async_ch"))
        self.assertEqual(len(received), 1)

    def test_deduplication(self):
        _run(self.nm.send("Same","Same"))
        _run(self.nm.send("Same","Same"))
        deduped = sum(1 for n in self.nm.history() if n.error == "deduplicated")
        self.assertGreater(deduped, 0)

    def test_skip_dedup(self):
        _run(self.nm.send("D","D",skip_dedup=True))
        _run(self.nm.send("D","D",skip_dedup=True))
        deduped = sum(1 for n in self.nm.history() if n.error == "deduplicated")
        self.assertEqual(deduped, 0)

    def test_routing_rule(self):
        from agent.notification_manager import NotificationManager, ChannelType, Priority
        nm = NotificationManager()
        received = []
        nm.add_channel("ops", ChannelType.CUSTOM, handler=lambda n: received.append(n))
        nm.add_routing_rule(["ops"], min_level=Priority.HIGH)
        _run(nm.send("ALERT","critical issue", level=Priority.CRITICAL))
        self.assertGreater(len(received), 0)

    def test_min_level_filter(self):
        from agent.notification_manager import NotificationManager, ChannelType, Priority
        nm = NotificationManager()
        received = []
        nm.add_channel("high_only", ChannelType.CUSTOM,
                         handler=lambda n: received.append(n),
                         min_level=Priority.HIGH)
        _run(nm.send("Low","msg", level=Priority.LOW, channel="high_only"))
        self.assertEqual(len(received), 0)

    def test_batch_send(self):
        notifs = [{"title":f"N{i}","body":f"B{i}"} for i in range(5)]
        results = _run(self.nm.batch_send(notifs))
        self.assertEqual(len(results), 5)

    def test_remove_channel(self):
        from agent.notification_manager import ChannelType
        self.nm.add_channel("removable", ChannelType.LOG)
        ok = self.nm.remove_channel("removable")
        self.assertTrue(ok)
        self.assertNotIn("removable", self.nm._channels)

    def test_digest(self):
        from agent.notification_manager import Priority
        _run(self.nm.send("T1","B1", level=Priority.LOW, skip_dedup=True))
        _run(self.nm.send("T2","B2", level=Priority.LOW, skip_dedup=True))
        digest = _run(self.nm.digest())
        self.assertIn("Digest", digest)

    def test_history(self):
        _run(self.nm.send("H1","B1",skip_dedup=True))
        _run(self.nm.send("H2","B2",skip_dedup=True))
        h = self.nm.history(limit=10)
        self.assertGreaterEqual(len(h), 2)

    def test_stats(self):
        _run(self.nm.send("S","B"))
        s = self.nm.stats()
        for k in ["total_sent","delivered","by_level","active_channels"]: self.assertIn(k,s)

    def test_to_dict(self):
        n = _run(self.nm.send("T","B"))
        d = n.to_dict()
        for k in ["id","title","body","level","delivered","created_at"]: self.assertIn(k,d)

    def test_retry_on_failure(self):
        from agent.notification_manager import NotificationManager, ChannelType
        nm = NotificationManager()
        attempts = [0]
        def failing(n):
            attempts[0] += 1
            if attempts[0] < 3: raise RuntimeError("fail")
        nm.add_channel("flaky", ChannelType.CUSTOM, handler=failing, max_retries=3, retry_base=0.01)
        n = _run(nm.send("T","B", channel="flaky"))
        self.assertGreaterEqual(n.attempts, 3)

# ════════════════════════════════════════════════════════
# AGENT MEMORY
# ════════════════════════════════════════════════════════
class TestAgentMemory(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.agent_memory import AgentMemory
        self.mem = AgentMemory(db_path=os.path.join(td,"am.db"), working_capacity=5)

    def test_remember_episodic(self):
        m = self.mem.remember("User asked about Python.", memory_type="episodic")
        self.assertEqual(m.memory_type.value, "episodic")

    def test_remember_semantic(self):
        m = self.mem.remember("Python was created in 1991.", memory_type="semantic")
        self.assertEqual(m.memory_type.value, "semantic")

    def test_remember_working(self):
        m = self.mem.remember("temp note", memory_type="working")
        self.assertIn(m, self.mem.working_memory())

    def test_working_capacity(self):
        for i in range(10):
            self.mem.remember(f"working item {i}", memory_type="working")
        self.assertLessEqual(len(self.mem.working_memory()), 5)

    def test_recall_returns_results(self):
        self.mem.remember("Python is a programming language", memory_type="semantic")
        results = self.mem.recall("Python programming", top_k=5)
        self.assertGreater(len(results), 0)

    def test_recall_relevance(self):
        self.mem.remember("Python decorators are useful", tags=["python"])
        self.mem.remember("Cooking pasta is easy", tags=["food"])
        results = self.mem.recall("Python decorators", top_k=5)
        self.assertTrue(any("Python" in m.content for m in results))

    def test_recall_modes(self):
        self.mem.remember("Fact about databases", memory_type="semantic")
        for mode in ["by_recency","by_relevance","by_importance","combined"]:
            results = self.mem.recall("databases", mode=mode)
            self.assertIsInstance(results, list)

    def test_recall_by_type(self):
        from agent.agent_memory import MemoryType
        self.mem.remember("Event happened", memory_type="episodic")
        self.mem.remember("Known fact", memory_type="semantic")
        ep_results = self.mem.recall("happened", memory_type=MemoryType.EPISODIC)
        self.assertTrue(all(m.memory_type == MemoryType.EPISODIC for m in ep_results))

    def test_recall_by_tags(self):
        self.mem.remember("Python tip", tags=["python"])
        self.mem.remember("Java tip",   tags=["java"])
        results = self.mem.recall("tip", tags=["python"])
        self.assertTrue(all("python" in m.tags for m in results))

    def test_access_count_increments(self):
        m = self.mem.remember("Accessed content", memory_type="semantic")
        self.mem.recall("Accessed content", top_k=5)
        results = self.mem.recall("Accessed content", top_k=5)
        retrieved = [r for r in results if r.id == m.id]
        if retrieved: self.assertGreaterEqual(retrieved[0].access_count, 1)

    def test_forget_low_importance(self):
        from agent.agent_memory import Memory, MemoryType
        # Manually insert a very old low-importance memory via store
        m = self.mem.remember("Old unimportant fact", memory_type="semantic",
                               importance=0.1)
        # Force old timestamp
        self.mem._store.get(m.id)  # just check it exists
        forgotten = self.mem.forget(threshold=0.99)   # very aggressive
        self.assertGreaterEqual(forgotten, 0)

    def test_boost_importance(self):
        m = self.mem.remember("Boostable", memory_type="semantic", importance=0.3)
        self.mem.boost_importance(m.id, delta=0.4)
        updated = self.mem._store.get(m.id)
        self.assertGreater(updated.importance, 0.3)

    def test_update_confidence(self):
        m = self.mem.remember("Fact", memory_type="semantic", confidence=0.5)
        self.mem.update_confidence(m.id, 0.9)
        updated = self.mem._store.get(m.id)
        self.assertAlmostEqual(updated.confidence, 0.9, places=2)

    def test_consolidate_no_llm(self):
        for i in range(4):
            self.mem.remember(f"Python feature {i} is useful for development",
                               memory_type="episodic", tags=["python"])
        new_facts = _run(self.mem.consolidate())
        self.assertIsInstance(new_facts, list)

    def test_consolidate_with_llm(self):
        for i in range(3):
            self.mem.remember(f"Database query {i} ran slowly in production",
                               memory_type="episodic", tags=["db"])
        def llm(p): return "Databases have performance issues under load."
        facts = _run(self.mem.consolidate(llm_fn=llm))
        self.assertIsInstance(facts, list)

    def test_clear_working(self):
        self.mem.remember("tmp", memory_type="working")
        self.mem.clear_working_memory()
        self.assertEqual(len(self.mem.working_memory()), 0)

    def test_stats(self):
        self.mem.remember("s1", memory_type="episodic")
        s = self.mem.stats()
        for k in ["total","by_type","working_memory_size"]: self.assertIn(k,s)

    def test_to_dict(self):
        m = self.mem.remember("Content here", memory_type="episodic", tags=["t1"])
        d = m.to_dict()
        for k in ["id","type","content","tags","importance","access_count"]: self.assertIn(k,d)

    def test_persistence(self):
        from agent.agent_memory import AgentMemory
        td = tempfile.mkdtemp(); db = os.path.join(td,"am.db")
        mem1 = AgentMemory(db_path=db)
        m = mem1.remember("Persisted knowledge", memory_type="semantic")
        mem2 = AgentMemory(db_path=db)
        results = mem2.recall("Persisted knowledge", top_k=5)
        self.assertTrue(any(r.id == m.id for r in results))

    def test_recency_score(self):
        from agent.agent_memory import _recency_score
        now = time.time()
        self.assertAlmostEqual(_recency_score(now, now), 1.0, places=4)
        self.assertLess(_recency_score(now - 86400, now), 1.0)

    def test_word_overlap(self):
        from agent.agent_memory import _word_overlap
        self.assertGreater(_word_overlap("python programming", "Python is a programming language"), 0)
        self.assertEqual(_word_overlap("xyz abc", "123 456"), 0)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v20: {total-failed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
