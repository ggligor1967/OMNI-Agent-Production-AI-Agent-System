"""OMNI AGENT v47: SemanticCache, PromptVersioning, AgentCoordinator, ChaosEngine"""
import asyncio, math, os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# SEMANTIC CACHE
# ════════════════════════════════════════════════════════
def _emb(val: float, dim: int = 4) -> list:
    """Unit vector scaled by val."""
    base = [1.0] + [0.0] * (dim - 1)
    norm = math.sqrt(sum(x*x for x in base))
    return [x / norm * (0.5 + val * 0.5) for x in base]

class TestSemanticCache(unittest.TestCase):
    def setUp(self):
        from agent.semantic_cache import SemanticCache
        self.sc = SemanticCache(threshold=0.90, db_path=":memory:")

    def test_put_and_get_exact(self):
        emb = _emb(1.0)
        self.sc.put("hello", "world", emb)
        result = self.sc.get_exact("hello")
        self.assertIsNotNone(result)
        self.assertEqual(result.response, "world")

    def test_exact_miss_returns_none(self):
        self.assertIsNone(self.sc.get_exact("missing"))

    def test_semantic_hit_above_threshold(self):
        emb = [1.0, 0.0, 0.0, 0.0]
        self.sc.put("capital of France", "Paris", emb)
        # Same vector → similarity = 1.0
        results = self.sc.get_semantic([1.0, 0.0, 0.0, 0.0])
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0][0].response, "Paris")

    def test_semantic_miss_below_threshold(self):
        emb_a = [1.0, 0.0, 0.0, 0.0]
        emb_b = [0.0, 1.0, 0.0, 0.0]  # orthogonal → similarity = 0
        self.sc.put("question A", "answer A", emb_a)
        results = self.sc.get_semantic(emb_b)
        self.assertEqual(len(results), 0)

    def test_get_combined_exact_first(self):
        emb = [1.0, 0.0, 0.0, 0.0]
        self.sc.put("exact query", "exact answer", emb)
        result = self.sc.get("exact query", emb)
        self.assertEqual(result, "exact answer")

    def test_get_combined_semantic_fallback(self):
        emb = [1.0, 0.0, 0.0, 0.0]
        self.sc.put("stored query", "stored answer", emb)
        # Different query text, same embedding → semantic hit
        result = self.sc.get("different text", [1.0, 0.0, 0.0, 0.0])
        self.assertEqual(result, "stored answer")

    def test_get_combined_miss(self):
        emb = [1.0, 0.0, 0.0, 0.0]
        self.sc.put("q", "a", emb)
        result = self.sc.get("miss", [0.0, 1.0, 0.0, 0.0])
        self.assertIsNone(result)

    def test_hit_count_increments(self):
        emb = [1.0, 0.0, 0.0, 0.0]
        self.sc.put("q", "a", emb)
        self.sc.get_exact("q")
        self.sc.get_exact("q")
        entry = self.sc.get_exact("q")
        self.assertGreaterEqual(entry.hit_count, 2)

    def test_invalidate_by_id(self):
        emb = _emb(1.0)
        e = self.sc.put("del_me", "response", emb)
        self.assertTrue(self.sc.invalidate(e.entry_id))
        self.assertIsNone(self.sc.get_exact("del_me"))

    def test_invalidate_by_query(self):
        emb = _emb(1.0)
        self.sc.put("remove_me", "r", emb)
        self.assertTrue(self.sc.invalidate_by_query("remove_me"))
        self.assertIsNone(self.sc.get_exact("remove_me"))

    def test_invalidate_unknown_returns_false(self):
        self.assertFalse(self.sc.invalidate("nonexistent_id"))

    def test_flush_clears_all(self):
        self.sc.put("q1", "a1", _emb(0.9))
        self.sc.put("q2", "a2", _emb(0.8))
        self.sc.flush()
        self.assertEqual(len(self.sc._entries), 0)

    def test_flush_expired(self):
        emb = _emb(1.0)
        self.sc.put("exp", "val", emb, ttl=0.01)
        time.sleep(0.02)
        removed = self.sc.flush_expired()
        self.assertEqual(removed, 1)

    def test_expired_not_returned(self):
        emb = [1.0, 0.0, 0.0, 0.0]
        self.sc.put("expire_me", "val", emb, ttl=0.01)
        time.sleep(0.02)
        self.assertIsNone(self.sc.get_exact("expire_me"))

    def test_ttl_forever(self):
        emb = _emb(1.0)
        e = self.sc.put("forever", "val", emb, ttl=-1)
        self.assertFalse(e.is_expired())

    def test_lru_eviction(self):
        from agent.semantic_cache import SemanticCache
        sc = SemanticCache(max_size=2, threshold=0.90, db_path=":memory:")
        sc.put("q1", "a1", [1.0, 0.0])
        sc.put("q2", "a2", [0.0, 1.0])
        sc.put("q3", "a3", [0.5, 0.5])  # triggers eviction
        self.assertLessEqual(len(sc._entries), 2)

    def test_resize_reduces(self):
        from agent.semantic_cache import SemanticCache
        sc = SemanticCache(max_size=10, threshold=0.90, db_path=":memory:")
        for i in range(5):
            sc.put(f"q{i}", f"a{i}", [float(i % 2), float((i+1) % 2)])
        sc.resize(3)
        self.assertLessEqual(len(sc._entries), 3)

    def test_top_k_semantic(self):
        self.sc.put("q1", "a1", [1.0, 0.0, 0.0, 0.0])
        self.sc.put("q2", "a2", [0.99, 0.1, 0.0, 0.0])
        results = self.sc.get_semantic([1.0, 0.0, 0.0, 0.0], top_k=2)
        self.assertEqual(len(results), 2)

    def test_dimension_mismatch_skipped(self):
        self.sc.put("q", "a", [1.0, 0.0, 0.0, 0.0])
        results = self.sc.get_semantic([1.0, 0.0])  # wrong dim
        self.assertEqual(len(results), 0)

    def test_stats(self):
        emb = [1.0, 0.0, 0.0, 0.0]
        self.sc.put("q", "a", emb)
        self.sc.get_exact("q")
        s = self.sc.stats()
        self.assertIn("hits", s)
        self.assertIn("hit_rate", s)
        self.assertGreater(s["hits"], 0)

    def test_invalid_threshold_raises(self):
        from agent.semantic_cache import SemanticCache
        with self.assertRaises(ValueError):
            SemanticCache(threshold=0.0)

    def test_list_entries(self):
        self.sc.put("q1", "a1", _emb(1.0))
        entries = self.sc.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertIn("entry_id", entries[0])

# ════════════════════════════════════════════════════════
# PROMPT VERSIONING
# ════════════════════════════════════════════════════════
class TestPromptVersioning(unittest.TestCase):
    def setUp(self):
        from agent.prompt_versioning import PromptVersionStore
        self.store = PromptVersionStore(db_path=":memory:")

    def test_commit_creates_version(self):
        v = self.store.commit("greet", "Hello {name}!")
        self.assertEqual(v.version, 1)

    def test_second_commit_increments(self):
        self.store.commit("greet", "Hello!")
        v2 = self.store.commit("greet", "Hi there!")
        self.assertEqual(v2.version, 2)

    def test_get_active(self):
        self.store.commit("greet", "Hello!")
        active = self.store.get_active("greet")
        self.assertEqual(active.content, "Hello!")

    def test_auto_activate_latest(self):
        self.store.commit("p", "v1")
        self.store.commit("p", "v2")
        active = self.store.get_active("p")
        self.assertEqual(active.version, 2)

    def test_activate_specific_version(self):
        v1 = self.store.commit("p", "v1 content", auto_activate=False)
        self.store.commit("p", "v2 content")
        self.store.activate("p", v1.version_id)
        self.assertEqual(self.store.get_active("p").version, 1)

    def test_rollback_one_step(self):
        self.store.commit("p", "v1")
        self.store.commit("p", "v2")
        rolled = self.store.rollback("p", steps=1)
        self.assertEqual(rolled.version, 1)

    def test_rollback_clamps_to_zero(self):
        self.store.commit("p", "v1")
        rolled = self.store.rollback("p", steps=99)
        self.assertEqual(rolled.version, 1)

    def test_get_version_by_number(self):
        self.store.commit("p", "v1")
        self.store.commit("p", "v2")
        v = self.store.get_version("p", 1)
        self.assertEqual(v.content, "v1")

    def test_history_ordered(self):
        for i in range(1, 4):
            self.store.commit("p", f"v{i}")
        hist = self.store.history("p")
        versions = [v.version for v in hist]
        self.assertEqual(versions, [1, 2, 3])

    def test_list_prompts(self):
        self.store.commit("p1", "a")
        self.store.commit("p2", "b")
        self.assertIn("p1", self.store.list_prompts())
        self.assertIn("p2", self.store.list_prompts())

    def test_find_by_tag(self):
        self.store.commit("p", "v1", tags=["production"])
        self.store.commit("p", "v2", tags=["staging"])
        found = self.store.find_by_tag("production")
        self.assertEqual(len(found), 1)

    def test_diff_returns_lines(self):
        self.store.commit("p", "line one\nline two\n")
        self.store.commit("p", "line one\nline THREE\n")
        diff = self.store.diff("p", 1, 2)
        self.assertIsInstance(diff, list)
        self.assertTrue(any("-line two" in l or "+line THREE" in l for l in diff))

    def test_diff_identical_empty(self):
        self.store.commit("p", "same content")
        self.store.commit("p", "same content")
        diff = self.store.diff("p", 1, 2)
        self.assertEqual(len(diff), 0)

    def test_content_hash(self):
        v = self.store.commit("p", "content")
        self.assertIsNotNone(v.content_hash)
        self.assertEqual(len(v.content_hash), 12)

    def test_delete_prompt(self):
        from agent.prompt_versioning import PromptNotFound
        self.store.commit("p", "v")
        self.store.delete_prompt("p")
        with self.assertRaises(PromptNotFound):
            self.store.get_active("p")

    def test_prune_keeps_last_n(self):
        for i in range(10):
            self.store.commit("p", f"v{i}")
        removed = self.store.prune("p", keep=3)
        self.assertEqual(removed, 7)
        self.assertEqual(len(self.store.history("p")), 3)

    def test_prompt_not_found_raises(self):
        from agent.prompt_versioning import PromptNotFound
        with self.assertRaises(PromptNotFound):
            self.store.get_active("no_such_prompt")

    def test_version_not_found_raises(self):
        from agent.prompt_versioning import VersionNotFound
        self.store.commit("p", "v")
        with self.assertRaises(VersionNotFound):
            self.store.get_version("p", 999)

    def test_deployment_log(self):
        v = self.store.commit("p", "v1")
        log = self.store.deployment_log("p")
        self.assertGreater(len(log), 0)
        self.assertEqual(log[0]["version_id"], v.version_id)

    def test_stats(self):
        self.store.commit("p1", "v1")
        self.store.commit("p1", "v2")
        self.store.commit("p2", "v1")
        s = self.store.stats()
        self.assertEqual(s["prompts"], 2)
        self.assertEqual(s["total_versions"], 3)

    def test_to_dict(self):
        v = self.store.commit("p", "content")
        d = v.to_dict()
        for k in ["version_id", "prompt_id", "version", "content", "is_active"]:
            self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# AGENT COORDINATOR
# ════════════════════════════════════════════════════════
class TestAgentCoordinator(unittest.TestCase):
    def setUp(self):
        from agent.agent_coordinator import AgentCoordinator
        self.coord = AgentCoordinator()

    def _atask(self, val):
        async def fn(ctx): return val
        return fn

    def test_register_agent(self):
        a = self.coord.register_agent("a1", "Agent 1", capabilities=["llm"])
        self.assertEqual(a.agent_id, "a1")

    def test_unregister_agent(self):
        self.coord.register_agent("a1", "Agent 1")
        self.coord.unregister_agent("a1")
        self.assertEqual(len(self.coord._agents), 0)

    def test_add_task(self):
        t = self.coord.add_task("fetch", self._atask("data"))
        self.assertIsNotNone(t.task_id)

    def test_run_single_task(self):
        self.coord.add_task("t1", self._atask(42))
        results = _run(self.coord.run())
        self.assertIn(42, results.values())

    def test_run_with_dependency(self):
        t1 = self.coord.add_task("t1", self._atask("hello"))
        t2 = self.coord.add_task("t2", self._atask("world"), depends_on=[t1.task_id])
        results = _run(self.coord.run())
        self.assertEqual(results[t1.task_id], "hello")
        self.assertEqual(results[t2.task_id], "world")

    def test_run_parallel_tasks(self):
        order = []
        async def timed(ctx):
            order.append(time.time())
            return True
        t1 = self.coord.add_task("t1", timed)
        t2 = self.coord.add_task("t2", timed)
        _run(self.coord.run())
        self.assertEqual(len(order), 2)

    def test_dependency_chain(self):
        results_list = []
        async def fn(ctx):
            results_list.append(1)
        t1 = self.coord.add_task("t1", fn)
        t2 = self.coord.add_task("t2", fn, depends_on=[t1.task_id])
        t3 = self.coord.add_task("t3", fn, depends_on=[t2.task_id])
        _run(self.coord.run())
        self.assertEqual(len(results_list), 3)

    def test_task_status_done(self):
        from agent.agent_coordinator import TaskStatus
        t = self.coord.add_task("t", self._atask("ok"))
        _run(self.coord.run())
        self.assertEqual(self.coord.task_status(t.task_id), TaskStatus.DONE)

    def test_failed_task_on_error_fail(self):
        from agent.agent_coordinator import TaskStatus
        async def bad(ctx): raise ValueError("oops")
        t = self.coord.add_task("bad", bad, on_error="fail")
        _run(self.coord.run())
        self.assertEqual(self.coord.task_status(t.task_id), TaskStatus.FAILED)

    def test_failed_task_on_error_skip(self):
        from agent.agent_coordinator import TaskStatus
        async def bad(ctx): raise ValueError("oops")
        t = self.coord.add_task("bad", bad, on_error="skip")
        _run(self.coord.run())
        self.assertEqual(self.coord.task_status(t.task_id), TaskStatus.SKIPPED)

    def test_dependent_skipped_when_dep_fails(self):
        from agent.agent_coordinator import TaskStatus
        async def bad(ctx): raise ValueError("oops")
        t1 = self.coord.add_task("t1", bad, on_error="fail")
        t2 = self.coord.add_task("t2", self._atask("ok"), depends_on=[t1.task_id])
        _run(self.coord.run())
        self.assertEqual(self.coord.task_status(t2.task_id), TaskStatus.SKIPPED)

    def test_retry_on_failure(self):
        attempts = [0]
        async def flaky(ctx):
            attempts[0] += 1
            if attempts[0] < 3:
                raise RuntimeError("retry me")
            return "ok"
        t = self.coord.add_task("flaky", flaky, max_retries=2)
        _run(self.coord.run())
        self.assertEqual(attempts[0], 3)

    def test_cycle_detection(self):
        t1 = self.coord.add_task("t1", self._atask(1), task_id="t1")
        t2 = self.coord.add_task("t2", self._atask(2), depends_on=["t1"], task_id="t2")
        self.coord._tasks["t1"].depends_on = ["t2"]  # create cycle
        self.assertTrue(self.coord.has_cycle())

    def test_no_cycle_dag(self):
        t1 = self.coord.add_task("t1", self._atask(1))
        t2 = self.coord.add_task("t2", self._atask(2), depends_on=[t1.task_id])
        self.assertFalse(self.coord.has_cycle())

    def test_on_task_done_hook(self):
        done = []
        self.coord.on_task_done(lambda t: done.append(t.name))
        self.coord.add_task("hook_task", self._atask("ok"))
        _run(self.coord.run())
        self.assertIn("hook_task", done)

    def test_on_task_fail_hook(self):
        failed = []
        async def bad(ctx): raise RuntimeError("fail")
        self.coord.on_task_fail(lambda t, e: failed.append(t.name))
        self.coord.add_task("bad_task", bad)
        _run(self.coord.run())
        self.assertIn("bad_task", failed)

    def test_agent_assigned(self):
        self.coord.register_agent("a1", "Worker", max_concurrent=5)
        t = self.coord.add_task("t", self._atask("ok"))
        _run(self.coord.run())
        self.assertEqual(t.assigned_agent, "a1")

    def test_capability_matching(self):
        self.coord.register_agent("fast", "Fast", capabilities=["coding"])
        self.coord.register_agent("slow", "Slow", capabilities=["analysis"])
        agents = self.coord.available_agents(capability="coding")
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].agent_id, "fast")

    def test_stats(self):
        self.coord.add_task("t1", self._atask(1))
        self.coord.add_task("t2", self._atask(2))
        _run(self.coord.run())
        s = self.coord.stats()
        self.assertEqual(s["completed"], 2)

    def test_clear_tasks(self):
        self.coord.add_task("t", self._atask(1))
        self.coord.clear_tasks()
        self.assertEqual(len(self.coord._tasks), 0)

    def test_list_tasks(self):
        self.coord.add_task("t1", self._atask(1))
        tasks = self.coord.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertIn("task_id", tasks[0])

# ════════════════════════════════════════════════════════
# CHAOS ENGINE
# ════════════════════════════════════════════════════════
class TestChaosEngine(unittest.TestCase):
    def setUp(self):
        from agent.chaos_engine import ChaosEngine, FaultType
        self.FaultType = FaultType
        self.ce = ChaosEngine(seed=42, db_path=":memory:")

    def test_add_rule(self):
        r = self.ce.add_rule("slow", self.FaultType.LATENCY, probability=1.0, latency_ms=1)
        self.assertIsNotNone(r.rule_id)

    def test_remove_rule(self):
        r = self.ce.add_rule("r", self.FaultType.NONE)
        self.ce.remove_rule(r.rule_id)
        self.assertEqual(len(self.ce._rules), 0)

    def test_enable_disable_rule(self):
        r = self.ce.add_rule("r", self.FaultType.NONE)
        self.ce.disable_rule(r.rule_id)
        self.assertFalse(r.enabled)
        self.ce.enable_rule(r.rule_id)
        self.assertTrue(r.enabled)

    def test_engine_disable_skips_faults(self):
        self.ce.add_rule("exc", self.FaultType.EXCEPTION, probability=1.0)
        self.ce.disable()
        self.ce.maybe_inject("*")  # should NOT raise

    def test_inject_exception_sync(self):
        self.ce.add_rule("exc", self.FaultType.EXCEPTION, probability=1.0,
                         exception_class=ValueError, exception_msg="test_fault")
        with self.assertRaises(ValueError):
            self.ce.maybe_inject("*")

    def test_inject_latency_sync(self):
        self.ce.add_rule("slow", self.FaultType.LATENCY, probability=1.0, latency_ms=10)
        t0 = time.time()
        self.ce.maybe_inject("*")
        elapsed = (time.time() - t0) * 1000
        self.assertGreater(elapsed, 5)

    def test_inject_exception_async(self):
        self.ce.add_rule("exc", self.FaultType.EXCEPTION, probability=1.0,
                         exception_class=RuntimeError)
        async def go():
            await self.ce.maybe_inject_async("*")
        with self.assertRaises(RuntimeError):
            _run(go())

    def test_inject_timeout_async(self):
        self.ce.add_rule("timeout", self.FaultType.TIMEOUT, probability=1.0)
        async def go():
            await self.ce.maybe_inject_async("*")
        with self.assertRaises(asyncio.TimeoutError):
            _run(go())

    def test_rate_limit_sync(self):
        from agent.chaos_engine import RateLimitError
        self.ce.add_rule("rl", self.FaultType.RATE_LIMIT, probability=1.0)
        with self.assertRaises(RateLimitError):
            self.ce.maybe_inject("*")

    def test_corrupt_string(self):
        self.ce.add_rule("corrupt", self.FaultType.CORRUPT, probability=1.0)

        @self.ce.inject()
        def get_text():
            return "hello"

        result = get_text()
        self.assertNotEqual(result, "hello")

    def test_partial_result(self):
        self.ce.add_rule("partial", self.FaultType.PARTIAL, probability=1.0,
                         partial_slice=0.5)

        @self.ce.inject()
        def get_list():
            return [1, 2, 3, 4]

        result = get_list()
        self.assertLess(len(result), 4)

    def test_probability_zero_no_fault(self):
        self.ce.add_rule("exc", self.FaultType.EXCEPTION, probability=0.0)
        for _ in range(10):
            self.ce.maybe_inject("*")  # should never raise

    def test_target_filter(self):
        self.ce.add_rule("exc", self.FaultType.EXCEPTION, probability=1.0, target="bad_fn")
        # Should NOT trigger for "good_fn"
        self.ce.maybe_inject("good_fn")

    def test_target_wildcard_hits_all(self):
        self.ce.add_rule("exc", self.FaultType.EXCEPTION, probability=1.0, target="*")
        with self.assertRaises(Exception):
            self.ce.maybe_inject("any_fn")

    def test_inject_decorator_sync(self):
        self.ce.add_rule("exc", self.FaultType.EXCEPTION, probability=1.0,
                         target="decorated_fn")

        @self.ce.inject(target="decorated_fn")
        def decorated_fn():
            return "ok"

        with self.assertRaises(Exception):
            decorated_fn()

    def test_inject_decorator_async(self):
        self.ce.add_rule("exc", self.FaultType.EXCEPTION, probability=1.0,
                         target="async_fn")

        @self.ce.inject_async(target="async_fn")
        async def async_fn():
            return "ok"

        with self.assertRaises(Exception):
            _run(async_fn())

    def test_hit_count_tracked(self):
        r = self.ce.add_rule("exc", self.FaultType.NONE, probability=1.0)
        self.ce.maybe_inject("*")
        self.assertEqual(r.hit_count, 1)

    def test_total_injections_tracked(self):
        self.ce.add_rule("exc", self.FaultType.NONE, probability=1.0)
        for _ in range(3):
            self.ce.maybe_inject("*")
        self.assertEqual(self.ce._total_injections, 3)

    def test_event_log(self):
        self.ce.add_rule("exc", self.FaultType.NONE, probability=1.0)
        self.ce.maybe_inject("my_target")
        log = self.ce.event_log()
        self.assertGreater(len(log), 0)
        self.assertEqual(log[0]["target"], "my_target")

    def test_clear_rules(self):
        self.ce.add_rule("r1", self.FaultType.NONE)
        self.ce.add_rule("r2", self.FaultType.NONE)
        self.ce.clear_rules()
        self.assertEqual(len(self.ce._rules), 0)

    def test_stats(self):
        self.ce.add_rule("r", self.FaultType.NONE, probability=1.0)
        self.ce.maybe_inject("*")
        s = self.ce.stats()
        self.assertTrue(s["enabled"])
        self.assertEqual(s["rules"], 1)
        self.assertGreaterEqual(s["total_injections"], 1)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v47: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
