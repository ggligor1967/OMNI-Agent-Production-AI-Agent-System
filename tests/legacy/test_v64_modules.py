"""OMNI AGENT v64: EventBusV2, SandboxExecutor, BatchProcessorV2, KnowledgeBaseV2"""
import os, sys, time, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# EVENT BUS V2
# ════════════════════════════════════════════════════════
class TestEventBusV2(unittest.TestCase):
    def setUp(self):
        from agent.event_bus_v2 import EventBusV2
        self.bus = EventBusV2(db_path=":memory:")

    def test_publish_subscribe(self):
        received = []
        self.bus.subscribe("user.created", lambda e: received.append(e))
        self.bus.publish("user.created", {"id": 1})
        self.assertEqual(len(received), 1)

    def test_wildcard_single(self):
        received = []
        self.bus.subscribe("user.*", lambda e: received.append(e))
        self.bus.publish("user.created", {})
        self.bus.publish("user.deleted", {})
        self.assertEqual(len(received), 2)

    def test_wildcard_multi(self):
        received = []
        self.bus.subscribe("app.**", lambda e: received.append(e))
        self.bus.publish("app.user.created", {})
        self.bus.publish("app.order.placed", {})
        self.assertEqual(len(received), 2)

    def test_no_match(self):
        received = []
        self.bus.subscribe("order.*", lambda e: received.append(e))
        self.bus.publish("user.created", {})
        self.assertEqual(len(received), 0)

    def test_filter_fn(self):
        received = []
        self.bus.subscribe("order.placed",
                            lambda e: received.append(e),
                            filter_fn=lambda e: e.payload.get("total", 0) > 100)
        self.bus.publish("order.placed", {"total": 50})
        self.bus.publish("order.placed", {"total": 200})
        self.assertEqual(len(received), 1)

    def test_unsubscribe(self):
        received = []
        sub = self.bus.subscribe("ev", lambda e: received.append(e))
        self.bus.publish("ev", {})
        self.bus.unsubscribe(sub.sub_id)
        self.bus.publish("ev", {})
        self.assertEqual(len(received), 1)

    def test_pause_resume_subscription(self):
        received = []
        sub = self.bus.subscribe("ev", lambda e: received.append(e))
        self.bus.pause_subscription(sub.sub_id)
        self.bus.publish("ev", {})
        self.assertEqual(len(received), 0)
        self.bus.resume_subscription(sub.sub_id)
        self.bus.publish("ev", {})
        self.assertEqual(len(received), 1)

    def test_pause_resume_topic(self):
        received = []
        self.bus.subscribe("tpc", lambda e: received.append(e))
        self.bus.pause_topic("tpc")
        self.bus.publish("tpc", {})
        self.assertIsNone(self.bus.publish("tpc", {}))
        self.bus.resume_topic("tpc")
        self.bus.publish("tpc", {})
        self.assertEqual(len(received), 1)

    def test_dead_letter(self):
        self.bus.subscribe("bad_ev",
                            lambda e: (_ for _ in ()).throw(RuntimeError("fail")),
                            max_retries=0)
        self.bus.publish("bad_ev", {})
        dl = self.bus.dead_letter()
        self.assertGreater(len(dl), 0)

    def test_middleware_transform(self):
        received = []
        def mw(e):
            e.metadata["mw"] = True
            return e
        self.bus.use(mw)
        self.bus.subscribe("ev", lambda e: received.append(e))
        self.bus.publish("ev", {})
        self.assertTrue(received[0].metadata.get("mw"))

    def test_middleware_drop(self):
        received = []
        self.bus.use(lambda e: None)  # drop all
        self.bus.subscribe("ev", lambda e: received.append(e))
        self.bus.publish("ev", {})
        self.assertEqual(len(received), 0)

    def test_correlation(self):
        self.bus.subscribe("ev", lambda e: None)
        self.bus.publish("ev.start", {}, correlation_id="chain1")
        self.bus.publish("ev.done",  {}, correlation_id="chain1")
        chain = self.bus.correlated("chain1")
        self.assertEqual(len(chain), 2)

    def test_history(self):
        self.bus.subscribe("hist", lambda e: None)
        self.bus.publish("hist", {"x": 1})
        h = self.bus.history("hist")
        self.assertGreater(len(h), 0)

    def test_replay(self):
        received = []
        sub = self.bus.subscribe("rp", lambda e: received.append(e))
        self.bus.publish("rp", {})
        before = len(received)
        self.bus.replay(topic_pattern="rp", sub_id=sub.sub_id)
        self.assertGreater(len(received), before)

    def test_stats(self):
        self.bus.subscribe("sv", lambda e: None)
        self.bus.publish("sv", {})
        s = self.bus.stats()
        self.assertGreater(s["published"], 0)
        self.assertGreater(s["delivered"], 0)

# ════════════════════════════════════════════════════════
# SANDBOX EXECUTOR
# ════════════════════════════════════════════════════════
class TestSandboxExecutor(unittest.TestCase):
    def setUp(self):
        from agent.sandbox_executor import SandboxExecutor
        self.sb = SandboxExecutor(db_path=":memory:")

    def test_simple_expression(self):
        from agent.sandbox_executor import Language, SandboxStatus
        res = self.sb.execute("result = 2 + 2",
                               policy_id="strict",
                               lang=Language.PYTHON)
        self.assertEqual(res.status, SandboxStatus.SUCCESS)
        self.assertEqual(res.result, 4)

    def test_eval_expression(self):
        from agent.sandbox_executor import SandboxStatus
        res = self.sb.evaluate("3 * 7")
        self.assertEqual(res.status, SandboxStatus.SUCCESS)
        self.assertEqual(res.result, 21)

    def test_print_captured(self):
        from agent.sandbox_executor import SandboxStatus
        res = self.sb.execute("print('hello sandbox')", policy_id="strict")
        self.assertEqual(res.status, SandboxStatus.SUCCESS)
        self.assertIn("hello sandbox", res.stdout)

    def test_math_module(self):
        from agent.sandbox_executor import Language, SandboxStatus
        res = self.sb.execute("import math\nresult = math.sqrt(16)",
                               policy_id="open")
        self.assertEqual(res.status, SandboxStatus.SUCCESS)
        self.assertAlmostEqual(res.result, 4.0)

    def test_blocked_import(self):
        from agent.sandbox_executor import SandboxStatus
        res = self.sb.execute("import os", policy_id="strict")
        self.assertIn(res.status, (SandboxStatus.REJECTED, SandboxStatus.ERROR))

    def test_blocked_builtin(self):
        from agent.sandbox_executor import SandboxStatus
        res = self.sb.execute("eval('1+1')", policy_id="strict")
        self.assertIn(res.status, (SandboxStatus.REJECTED, SandboxStatus.ERROR))

    def test_syntax_error_rejected(self):
        from agent.sandbox_executor import SandboxStatus
        res = self.sb.execute("def bad(:", policy_id="strict")
        self.assertEqual(res.status, SandboxStatus.REJECTED)
        self.assertGreater(len(res.policy_violations), 0)

    def test_runtime_error(self):
        from agent.sandbox_executor import SandboxStatus
        res = self.sb.execute("result = 1/0", policy_id="strict")
        self.assertEqual(res.status, SandboxStatus.ERROR)
        self.assertIsNotNone(res.error)

    def test_timeout(self):
        from agent.sandbox_executor import SandboxStatus
        res = self.sb.execute(
            "x = 0\nwhile True:\n    x += 1",
            policy_id="open", timeout_s=0.1)
        self.assertEqual(res.status, SandboxStatus.TIMEOUT)

    def test_inject_globals(self):
        from agent.sandbox_executor import Language, SandboxStatus
        res = self.sb.execute("result = x + y", policy_id="strict",
                               inject_globals={"x": 5, "y": 3})
        self.assertEqual(res.status, SandboxStatus.SUCCESS)
        self.assertEqual(res.result, 8)

    def test_custom_policy(self):
        from agent.sandbox_executor import SandboxStatus
        self.sb.add_policy("custom", allowed_modules={"math"},
                            blocked_ast_nodes={"Import"})
        res = self.sb.execute("result = 1 + 1", policy_id="custom")
        self.assertEqual(res.status, SandboxStatus.SUCCESS)

    def test_dunder_blocked(self):
        from agent.sandbox_executor import SandboxStatus
        res = self.sb.execute(
            "result = ().__class__", policy_id="strict")
        self.assertIn(res.status, (SandboxStatus.REJECTED, SandboxStatus.ERROR))

    def test_moderate_policy(self):
        from agent.sandbox_executor import SandboxStatus
        res = self.sb.execute("result = [x**2 for x in range(5)]",
                               policy_id="moderate")
        self.assertEqual(res.status, SandboxStatus.SUCCESS)
        self.assertEqual(res.result, [0, 1, 4, 9, 16])

    def test_history(self):
        self.sb.execute("result = 1", policy_id="strict")
        h = self.sb.history()
        self.assertGreater(len(h), 0)

    def test_stats(self):
        self.sb.execute("result = 1", policy_id="strict")
        s = self.sb.stats()
        self.assertGreater(s["total_executions"], 0)
        self.assertGreater(s["success"], 0)

# ════════════════════════════════════════════════════════
# BATCH PROCESSOR V2
# ════════════════════════════════════════════════════════
class TestBatchProcessorV2(unittest.TestCase):
    def setUp(self):
        from agent.batch_processor_v2 import BatchProcessorV2
        self.bp = BatchProcessorV2(db_path=":memory:")

    def test_simple_process(self):
        from agent.batch_processor_v2 import BatchStatus
        run = self.bp.process(range(10), lambda x: x * 2)
        self.assertEqual(run.status, BatchStatus.DONE)
        self.assertEqual(run.succeeded, 10)

    def test_results_correct(self):
        run = self.bp.process([1, 2, 3], lambda x: x + 10)
        self.assertEqual(sorted(run.results), [11, 12, 13])

    def test_batch_size_chunking(self):
        from agent.batch_processor_v2 import BatchConfig
        batches_seen = [0]
        orig = self.bp._chunk

        cfg = BatchConfig(batch_size=3)
        run = self.bp.process(range(9), lambda x: x, config=cfg)
        self.assertEqual(run.total_items, 9)
        self.assertEqual(run.succeeded, 9)

    def test_error_skip_policy(self):
        from agent.batch_processor_v2 import BatchConfig, ErrorPolicy, BatchStatus
        def risky(x):
            if x == 5: raise ValueError("bad")
            return x
        cfg = BatchConfig(error_policy=ErrorPolicy.SKIP)
        run = self.bp.process(range(10), risky, config=cfg)
        self.assertEqual(run.status, BatchStatus.DONE)
        self.assertEqual(run.failed, 1)
        self.assertEqual(run.succeeded, 9)

    def test_error_stop_policy(self):
        from agent.batch_processor_v2 import BatchConfig, ErrorPolicy, BatchStatus
        def risky(x):
            if x == 2: raise RuntimeError("stop!")
            return x
        cfg = BatchConfig(error_policy=ErrorPolicy.STOP, batch_size=10)
        run = self.bp.process(range(5), risky, config=cfg)
        self.assertEqual(run.status, BatchStatus.FAILED)

    def test_error_collect_policy(self):
        from agent.batch_processor_v2 import BatchConfig, ErrorPolicy
        def risky(x):
            if x % 3 == 0: raise ValueError("every 3rd")
            return x
        cfg = BatchConfig(error_policy=ErrorPolicy.COLLECT)
        run = self.bp.process(range(9), risky, config=cfg)
        self.assertGreater(run.failed, 0)
        self.assertGreater(len(run.errors), 0)

    def test_retry_policy(self):
        from agent.batch_processor_v2 import BatchConfig, ErrorPolicy
        calls = {}
        def flaky(x):
            calls[x] = calls.get(x, 0) + 1
            if calls[x] < 2: raise RuntimeError("retry")
            return x
        cfg = BatchConfig(error_policy=ErrorPolicy.RETRY, max_retries=2)
        run = self.bp.process([1], flaky, config=cfg)
        self.assertEqual(run.succeeded, 1)

    def test_parallel_processing(self):
        from agent.batch_processor_v2 import BatchConfig, BatchStatus
        cfg = BatchConfig(max_workers=3)
        run = self.bp.process(range(30), lambda x: x * 2, config=cfg)
        self.assertEqual(run.status, BatchStatus.DONE)
        self.assertEqual(run.succeeded, 30)

    def test_create_and_run_job(self):
        from agent.batch_processor_v2 import BatchStatus
        job = self.bp.create_job("doubler", lambda x: x * 2)
        run = self.bp.run_job(job.job_id, range(5))
        self.assertEqual(run.status, BatchStatus.DONE)
        self.assertEqual(job.run_count, 1)

    def test_resume_from_checkpoint(self):
        run = self.bp.process(range(10), lambda x: x, resume_from=5)
        self.assertEqual(run.total_items, 10)
        self.assertEqual(run.succeeded, 5)

    def test_pre_post_hooks(self):
        from agent.batch_processor_v2 import BatchConfig
        pre_calls = []; post_calls = []
        cfg = BatchConfig(
            pre_batch_hook=lambda bi, b: pre_calls.append(bi),
            post_batch_hook=lambda bi, b, r: post_calls.append(bi))
        self.bp.process(range(5), lambda x: x, config=cfg)
        self.assertGreater(len(pre_calls), 0)

    def test_on_item_error_hook(self):
        from agent.batch_processor_v2 import BatchConfig, ErrorPolicy
        errors = []
        cfg = BatchConfig(error_policy=ErrorPolicy.SKIP,
                          on_item_error=lambda idx, item, exc: errors.append(item))
        self.bp.process([1, "bad", 3],
                         lambda x: int(x) * 2, config=cfg)
        self.assertGreater(len(errors), 0)

    def test_progress(self):
        run = self.bp.process(range(10), lambda x: x)
        self.assertAlmostEqual(run.progress, 1.0)

    def test_throughput(self):
        run = self.bp.process(range(10), lambda x: x)
        self.assertGreater(run.throughput, 0)

    def test_stats(self):
        self.bp.process(range(5), lambda x: x)
        s = self.bp.stats()
        self.assertGreater(s["total_processed"], 0)

# ════════════════════════════════════════════════════════
# KNOWLEDGE BASE V2
# ════════════════════════════════════════════════════════
class TestKnowledgeBaseV2(unittest.TestCase):
    def setUp(self):
        from agent.knowledge_base_v2 import KnowledgeBaseV2
        self.kb = KnowledgeBaseV2(db_path=":memory:")

    def test_add_entry(self):
        e = self.kb.add("Python", "Python is a programming language")
        self.assertIsNotNone(e.entry_id)

    def test_get_entry(self):
        e = self.kb.add("Go", "Go is compiled")
        fetched = self.kb.get(e.entry_id)
        self.assertEqual(fetched.title, "Go")

    def test_update_entry(self):
        e = self.kb.add("Rust", "Rust is safe")
        self.kb.update(e.entry_id, content="Rust is a systems language")
        self.assertEqual(e.content, "Rust is a systems language")
        self.assertEqual(e.version, 2)

    def test_version_history(self):
        e = self.kb.add("Versioned", "v1 content")
        self.kb.update(e.entry_id, content="v2 content")
        h = self.kb.get_history(e.entry_id)
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["version"], 1)

    def test_delete_entry(self):
        e = self.kb.add("Temp", "delete me")
        ok = self.kb.delete(e.entry_id)
        self.assertTrue(ok)
        self.assertIsNone(self.kb.get(e.entry_id))

    def test_keyword_search(self):
        self.kb.add("Python Guide", "Python programming language tutorial")
        self.kb.add("Java Guide", "Java object oriented language")
        results = self.kb.search("Python programming", mode="keyword")
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].entry.title, "Python Guide")

    def test_semantic_search(self):
        from agent.knowledge_base_v2 import KnowledgeBaseV2
        dim = 3
        def embed(t): return [hash(w) % 10 / 10 for w in (t.split() + [""])[:dim]]
        kb = KnowledgeBaseV2(embed_fn=embed, db_path=":memory:")
        kb.add("Cat", "cats and kittens")
        kb.add("Car", "cars and vehicles")
        results = kb.search("cats kittens", mode="semantic")
        self.assertGreater(len(results), 0)

    def test_add_relation(self):
        from agent.knowledge_base_v2 import RelationType
        e1 = self.kb.add("Dog", "a pet animal")
        e2 = self.kb.add("Animal", "living creature")
        rel = self.kb.add_relation(e1.entry_id, e2.entry_id, RelationType.IS_A)
        self.assertIsNotNone(rel.relation_id)

    def test_get_relations(self):
        from agent.knowledge_base_v2 import RelationType
        e1 = self.kb.add("A", "a")
        e2 = self.kb.add("B", "b")
        self.kb.add_relation(e1.entry_id, e2.entry_id, RelationType.RELATED_TO)
        rels = self.kb.get_relations(e1.entry_id)
        self.assertEqual(len(rels), 1)

    def test_remove_relation(self):
        from agent.knowledge_base_v2 import RelationType
        e1 = self.kb.add("X", "x")
        e2 = self.kb.add("Y", "y")
        rel = self.kb.add_relation(e1.entry_id, e2.entry_id, RelationType.DEPENDS_ON)
        ok = self.kb.remove_relation(rel.relation_id)
        self.assertTrue(ok)
        self.assertEqual(len(self.kb.get_relations(e1.entry_id)), 0)

    def test_traverse_bfs(self):
        from agent.knowledge_base_v2 import RelationType
        e1 = self.kb.add("E1", "content")
        e2 = self.kb.add("E2", "content")
        e3 = self.kb.add("E3", "content")
        self.kb.add_relation(e1.entry_id, e2.entry_id, RelationType.RELATED_TO)
        self.kb.add_relation(e2.entry_id, e3.entry_id, RelationType.RELATED_TO)
        visited = self.kb.traverse(e1.entry_id, max_depth=2)
        self.assertIn(e2.entry_id, visited)

    def test_find_duplicates(self):
        self.kb.add("D1", "duplicate content here")
        self.kb.add("D2", "duplicate content here")
        dupes = self.kb.find_duplicates()
        self.assertGreater(len(dupes), 0)

    def test_find_conflicts(self):
        from agent.knowledge_base_v2 import RelationType
        e1 = self.kb.add("C1", "claim A")
        e2 = self.kb.add("C2", "claim B")
        self.kb.add_relation(e1.entry_id, e2.entry_id, RelationType.CONTRADICTS)
        conflicts = self.kb.find_conflicts()
        self.assertEqual(len(conflicts), 1)

    def test_filter_by_type(self):
        from agent.knowledge_base_v2 import EntryType
        self.kb.add("Rule 1", "do this", entry_type=EntryType.RULE)
        self.kb.add("Fact 1", "is true", entry_type=EntryType.FACT)
        rules = self.kb.list_entries(entry_type=EntryType.RULE)
        self.assertEqual(len(rules), 1)

    def test_filter_by_tag(self):
        self.kb.add("Tagged", "content", tags=["ml"])
        self.kb.add("Other", "content")
        tagged = self.kb.list_entries(tag="ml")
        self.assertEqual(len(tagged), 1)

    def test_confidence_filter(self):
        self.kb.add("High conf", "content", confidence=0.9)
        self.kb.add("Low conf",  "content", confidence=0.3)
        high = self.kb.list_entries(min_confidence=0.8)
        self.assertEqual(len(high), 1)

    def test_stats(self):
        self.kb.add("S1", "content")
        self.kb.add("S2", "content")
        s = self.kb.stats()
        self.assertGreaterEqual(s["entries"], 2)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v64: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
