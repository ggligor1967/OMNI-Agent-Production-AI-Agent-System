"""OMNI AGENT v54: PipelineOrchestrator, ResponseCacheV2, ConversationState, MetricsAggregator"""
import asyncio, os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# PIPELINE ORCHESTRATOR
# ════════════════════════════════════════════════════════
class TestPipelineOrchestrator(unittest.TestCase):
    def setUp(self):
        from agent.pipeline_orchestrator import PipelineOrchestrator
        self.po = PipelineOrchestrator()

    def _add(self, sid, fn, deps=None, **kw):
        self.po.add_stage(sid, fn, dependencies=deps or [], stage_id=sid, **kw)

    def test_single_stage_runs(self):
        from agent.pipeline_orchestrator import StageStatus
        self._add("s1", lambda ctx, deps: 42)
        run = self.po.run()
        self.assertEqual(run.status, StageStatus.DONE)

    def test_output_in_context(self):
        self._add("s1", lambda ctx, deps: 42)
        run = self.po.run()
        self.assertEqual(run.context.get("__out_s1"), 42)

    def test_dependency_order_respected(self):
        order = []
        self._add("a", lambda ctx, d: order.append("a"))
        self._add("b", lambda ctx, d: order.append("b"), deps=["a"])
        self.po.run()
        self.assertLess(order.index("a"), order.index("b"))

    def test_parallel_stages_run(self):
        results = []
        self._add("p1", lambda ctx, d: results.append("p1"))
        self._add("p2", lambda ctx, d: results.append("p2"))
        self.po.run()
        self.assertIn("p1", results)
        self.assertIn("p2", results)

    def test_failed_stage_stops_pipeline(self):
        from agent.pipeline_orchestrator import StageStatus
        self._add("fail", lambda ctx, d: (_ for _ in ()).throw(RuntimeError("boom")))
        self._add("after", lambda ctx, d: 1, deps=["fail"])
        run = self.po.run()
        self.assertEqual(run.status, StageStatus.FAILED)

    def test_skip_on_fail(self):
        from agent.pipeline_orchestrator import StageStatus
        self._add("soft_fail",
                  lambda ctx, d: (_ for _ in ()).throw(RuntimeError("oops")),
                  skip_on_fail=True)
        run = self.po.run()
        self.assertEqual(run.status, StageStatus.DONE)

    def test_retry_on_failure(self):
        attempts = [0]
        def flaky(ctx, d):
            attempts[0] += 1
            if attempts[0] < 3:
                raise RuntimeError("retry me")
            return "ok"
        self._add("retry_stage", flaky, max_retries=3, retry_delay_s=0.0)
        run = self.po.run()
        self.assertEqual(run.context.get("__out_retry_stage"), "ok")

    def test_initial_context_available(self):
        self._add("use_ctx", lambda ctx, d: ctx.get("x") * 2)
        run = self.po.run(initial_context={"x": 5})
        self.assertEqual(run.context.get("__out_use_ctx"), 10)

    def test_hooks_called(self):
        starts, ends = [], []
        self.po.on_stage_start(lambda spec, sr: starts.append(spec.stage_id))
        self.po.on_stage_end(lambda spec, sr: ends.append(spec.stage_id))
        self._add("hooked", lambda ctx, d: 1)
        self.po.run()
        self.assertIn("hooked", starts)
        self.assertIn("hooked", ends)

    def test_run_done_hook(self):
        fired = []
        self.po.on_run_done(lambda r: fired.append(r.run_id))
        self._add("x", lambda ctx, d: 1)
        run = self.po.run()
        self.assertIn(run.run_id, fired)

    def test_cyclic_dependency_fails(self):
        from agent.pipeline_orchestrator import StageStatus
        self._add("a", lambda ctx, d: 1, deps=["b"])
        self._add("b", lambda ctx, d: 1, deps=["a"])
        run = self.po.run()
        self.assertEqual(run.status, StageStatus.FAILED)

    def test_validate_detects_missing_dep(self):
        self._add("x", lambda ctx, d: 1, deps=["nonexistent"])
        errors = self.po.validate()
        self.assertGreater(len(errors), 0)

    def test_validate_ok(self):
        self._add("a", lambda ctx, d: 1)
        self._add("b", lambda ctx, d: 2, deps=["a"])
        errors = self.po.validate()
        self.assertEqual(errors, [])

    def test_async_run(self):
        from agent.pipeline_orchestrator import StageStatus
        self._add("async_s", lambda ctx, d: "async_ok")
        run = _run(self.po.run_async())
        self.assertEqual(run.status, StageStatus.DONE)

    def test_recent_runs(self):
        self._add("rr", lambda ctx, d: 1)
        self.po.run()
        runs = self.po.recent_runs(1)
        self.assertEqual(len(runs), 1)

    def test_list_stages(self):
        self._add("ls1", lambda ctx, d: 1)
        stages = self.po.list_stages()
        self.assertEqual(len(stages), 1)

    def test_stats(self):
        self._add("st", lambda ctx, d: 1)
        self.po.run()
        s = self.po.stats()
        self.assertEqual(s["total_runs"], 1)
        self.assertEqual(s["done"], 1)

# ════════════════════════════════════════════════════════
# RESPONSE CACHE V2
# ════════════════════════════════════════════════════════
class TestResponseCacheV2(unittest.TestCase):
    def setUp(self):
        from agent.response_cache_v2 import ResponseCacheV2
        self.rc = ResponseCacheV2(capacity=50, db_path=":memory:")

    def test_put_and_exact_get(self):
        self.rc.put("What is AI?", "AI is...", model_id="gpt4")
        e = self.rc.get("What is AI?", model_id="gpt4", semantic=False)
        self.assertIsNotNone(e)
        self.assertEqual(e.response, "AI is...")

    def test_exact_miss(self):
        self.rc.put("Hello", "Hi", model_id="gpt4")
        e = self.rc.get("Goodbye", model_id="gpt4", semantic=False)
        self.assertIsNone(e)

    def test_semantic_hit(self):
        self.rc.put("What is machine learning?", "ML is...", model_id="m1")
        # Same embedding → cosine = 1.0 ≥ threshold
        e = self.rc.get("What is machine learning?", model_id="m1", semantic=True)
        self.assertIsNotNone(e)

    def test_ttl_expiry(self):
        self.rc.put("Expiring", "val", ttl_s=0.01)
        time.sleep(0.05)
        e = self.rc.get("Expiring", semantic=False)
        self.assertIsNone(e)

    def test_invalidate(self):
        e = self.rc.put("Remove me", "val")
        self.assertTrue(self.rc.invalidate(e.key))
        self.assertIsNone(self.rc.get("Remove me", semantic=False))

    def test_invalidate_by_tag(self):
        self.rc.put("T1", "v1", tags=["group_a"])
        self.rc.put("T2", "v2", tags=["group_a"])
        self.rc.put("T3", "v3", tags=["group_b"])
        removed = self.rc.invalidate_by_tag("group_a")
        self.assertEqual(removed, 2)

    def test_invalidate_by_model(self):
        self.rc.put("M1", "v", model_id="old_model")
        self.rc.put("M2", "v", model_id="old_model")
        removed = self.rc.invalidate_by_model("old_model")
        self.assertEqual(removed, 2)

    def test_clear_expired(self):
        self.rc.put("exp1", "v", ttl_s=0.01)
        self.rc.put("exp2", "v", ttl_s=0.01)
        self.rc.put("live", "v")
        time.sleep(0.05)
        removed = self.rc.clear_expired()
        self.assertEqual(removed, 2)

    def test_lru_eviction(self):
        from agent.response_cache_v2 import ResponseCacheV2, EvictionPolicy
        rc = ResponseCacheV2(capacity=3, eviction_policy=EvictionPolicy.LRU,
                             db_path=":memory:")
        rc.put("a", 1); rc.put("b", 2); rc.put("c", 3)
        rc.get("a", semantic=False)  # access 'a' to make it recently used
        rc.put("d", 4)               # should evict LRU (b or c)
        self.assertLessEqual(len(rc._entries), 3)

    def test_fifo_eviction(self):
        from agent.response_cache_v2 import ResponseCacheV2, EvictionPolicy
        rc = ResponseCacheV2(capacity=2, eviction_policy=EvictionPolicy.FIFO,
                             db_path=":memory:")
        rc.put("first", 1); rc.put("second", 2)
        rc.put("third", 3)
        self.assertEqual(len(rc._entries), 2)

    def test_cost_tracked(self):
        from agent.response_cache_v2 import ResponseCacheV2
        rc = ResponseCacheV2(cost_per_1k=0.01, db_path=":memory:")
        rc.put("p", "r", token_count=1000)
        self.assertAlmostEqual(list(rc._entries.values())[0].cost_saved, 0.01)

    def test_hit_rate(self):
        self.rc.put("q", "a")
        self.rc.get("q", semantic=False)
        self.rc.get("q", semantic=False)
        self.rc.get("miss", semantic=False)
        self.assertGreater(self.rc.hit_rate(), 0)

    def test_access_count_increments(self):
        self.rc.put("r", "val")
        self.rc.get("r", semantic=False)
        self.rc.get("r", semantic=False)
        e = self.rc.get("r", semantic=False)
        self.assertGreaterEqual(e.access_count, 2)

    def test_top_entries(self):
        self.rc.put("top", "v")
        for _ in range(3): self.rc.get("top", semantic=False)
        tops = self.rc.top_entries(1)
        self.assertEqual(len(tops), 1)

    def test_event_log(self):
        self.rc.put("logged", "v")
        self.rc.get("logged", semantic=False)
        log = self.rc.event_log()
        self.assertGreater(len(log), 0)

    def test_clear(self):
        self.rc.put("x", "y")
        self.rc.clear()
        self.assertEqual(len(self.rc._entries), 0)

    def test_stats(self):
        self.rc.put("s", "v")
        self.rc.get("s", semantic=False)
        s = self.rc.stats()
        self.assertEqual(s["hits_exact"], 1)
        self.assertIn("hit_rate", s)

# ════════════════════════════════════════════════════════
# CONVERSATION STATE
# ════════════════════════════════════════════════════════
class TestConversationState(unittest.TestCase):
    def setUp(self):
        from agent.conversation_state import ConversationState
        self.cs = ConversationState(system_prompt="You are helpful.", db_path=":memory:")

    def test_add_user_turn(self):
        from agent.conversation_state import Role
        t = self.cs.add_user("Hello!")
        self.assertEqual(t.role, Role.USER)

    def test_add_assistant_turn(self):
        from agent.conversation_state import Role
        t = self.cs.add_assistant("Hi!")
        self.assertEqual(t.role, Role.ASSISTANT)

    def test_turn_index_increments(self):
        t1 = self.cs.add_user("First")
        t2 = self.cs.add_assistant("Second")
        self.assertEqual(t2.turn_index, 1)

    def test_active_turns(self):
        self.cs.add_user("A")
        self.cs.add_assistant("B")
        self.assertEqual(len(self.cs.active_turns()), 2)

    def test_edit_turn(self):
        t = self.cs.add_user("Wrong")
        self.cs.edit_turn(t.turn_id, "Corrected")
        self.assertEqual(self.cs.get_turn(t.turn_id).content, "Corrected")

    def test_delete_turn(self):
        t = self.cs.add_user("Delete me")
        self.cs.delete_turn(t.turn_id)
        active = self.cs.active_turns()
        self.assertTrue(all(tr.turn_id != t.turn_id for tr in active))

    def test_fork_branch(self):
        self.cs.add_user("Shared")
        bid = self.cs.fork(label="alt")
        self.assertIn(bid, self.cs._branches)

    def test_switch_branch(self):
        self.cs.add_user("Main turn")
        bid = self.cs.fork()
        self.cs.switch_branch(bid)
        self.assertEqual(self.cs._active_branch, bid)

    def test_branch_inherits_turns(self):
        self.cs.add_user("Shared turn")
        bid = self.cs.fork()
        self.cs.switch_branch(bid)
        self.assertGreater(len(self.cs.active_turns()), 0)

    def test_add_to_branch(self):
        bid = self.cs.fork(label="b2")
        self.cs.add_user("Branch-specific", branch_id=bid)
        main_turns = self.cs.active_turns("main")
        branch_turns = self.cs.active_turns(bid)
        self.assertGreater(len(branch_turns), len(main_turns))

    def test_to_messages_format(self):
        self.cs.add_user("Hi")
        self.cs.add_assistant("Hello")
        msgs = self.cs.to_messages()
        self.assertTrue(any(m["role"] == "system" for m in msgs))
        self.assertTrue(any(m["role"] == "user" for m in msgs))

    def test_to_text(self):
        self.cs.add_user("Question")
        text = self.cs.to_text()
        self.assertIn("USER:", text)

    def test_last_turn(self):
        from agent.conversation_state import Role
        self.cs.add_user("A"); self.cs.add_assistant("B")
        last = self.cs.last_turn(Role.USER)
        self.assertEqual(last.content, "A")

    def test_search(self):
        self.cs.add_user("Find this keyword")
        results = self.cs.search("keyword")
        self.assertGreater(len(results), 0)

    def test_token_count(self):
        self.cs.add_user("Hello", tokens=5)
        self.cs.add_assistant("Hi", tokens=3)
        self.assertEqual(self.cs.token_count(), 8)

    def test_compress(self):
        for i in range(15):
            self.cs.add_user(f"Message {i}")
        summary = self.cs.compress(keep_last_n=5)
        self.assertGreater(len(summary), 0)

    def test_compress_hook(self):
        fired = []
        self.cs.on_compress(lambda s, t: fired.append(s))
        for i in range(12): self.cs.add_user(f"Msg {i}")
        self.cs.compress(keep_last_n=5)
        self.assertGreater(len(fired), 0)

    def test_list_branches(self):
        self.cs.fork(label="b1")
        self.cs.fork(label="b2")
        branches = self.cs.list_branches()
        self.assertGreaterEqual(len(branches), 3)  # main + 2

    def test_stats(self):
        self.cs.add_user("s")
        s = self.cs.stats()
        self.assertIn("total_turns", s)
        self.assertIn("branches", s)

# ════════════════════════════════════════════════════════
# METRICS AGGREGATOR
# ════════════════════════════════════════════════════════
class TestMetricsAggregator(unittest.TestCase):
    def setUp(self):
        from agent.metrics_aggregator import MetricsAggregator
        self.ma = MetricsAggregator(retention_s=3600, db_path=":memory:")

    def test_record_sample(self):
        s = self.ma.record("latency", 120.0)
        self.assertEqual(s.name, "latency")
        self.assertEqual(s.value, 120.0)

    def test_gauge(self):
        self.ma.gauge("cpu", 0.72)
        self.assertAlmostEqual(self.ma.current_gauge("cpu"), 0.72)

    def test_increment(self):
        self.ma.increment("requests")
        self.ma.increment("requests", 4)
        self.assertEqual(self.ma.current_counter("requests"), 5.0)

    def test_query_avg(self):
        from agent.metrics_aggregator import AggFunc
        for v in [10.0, 20.0, 30.0]:
            self.ma.record("score", v)
        avg = self.ma.query("score", AggFunc.AVG, window_s=3600)
        self.assertAlmostEqual(avg, 20.0)

    def test_query_max(self):
        from agent.metrics_aggregator import AggFunc
        for v in [5.0, 15.0, 10.0]:
            self.ma.record("val", v)
        mx = self.ma.query("val", AggFunc.MAX, window_s=3600)
        self.assertEqual(mx, 15.0)

    def test_query_p95(self):
        from agent.metrics_aggregator import AggFunc
        for i in range(100):
            self.ma.record("lat", float(i))
        p95 = self.ma.query("lat", AggFunc.P95, window_s=3600)
        self.assertGreater(p95, 90.0)

    def test_query_rate(self):
        from agent.metrics_aggregator import AggFunc
        for _ in range(10):
            self.ma.record("events", 1.0)
        rate = self.ma.query("events", AggFunc.RATE, window_s=60)
        self.assertGreater(rate, 0)

    def test_query_with_labels(self):
        from agent.metrics_aggregator import AggFunc
        self.ma.record("req", 1.0, labels={"env": "prod"})
        self.ma.record("req", 2.0, labels={"env": "dev"})
        avg = self.ma.query("req", AggFunc.AVG, window_s=3600,
                            labels={"env": "prod"})
        self.assertAlmostEqual(avg, 1.0)

    def test_query_range(self):
        from agent.metrics_aggregator import AggFunc
        t0 = time.time()
        for v in [1.0, 2.0, 3.0]:
            self.ma.record("rng", v)
        result = self.ma.query_range("rng", t0 - 1, t0 + 1, bucket_s=60)
        self.assertGreater(len(result), 0)

    def test_alert_fires(self):
        from agent.metrics_aggregator import AggFunc
        fired = []
        self.ma.add_alert("cpu", "gt", 0.9, window_s=60,
                          agg_fn=AggFunc.AVG, cooldown_s=0)
        self.ma.on_alert(lambda r, v: fired.append(v))
        self.ma.record("cpu", 0.95)
        self.assertGreater(len(fired), 0)

    def test_alert_no_fire_below_threshold(self):
        from agent.metrics_aggregator import AggFunc
        fired = []
        self.ma.add_alert("mem", "gt", 0.9, cooldown_s=0)
        self.ma.on_alert(lambda r, v: fired.append(v))
        self.ma.record("mem", 0.5)
        self.assertEqual(len(fired), 0)

    def test_alert_cooldown(self):
        from agent.metrics_aggregator import AggFunc
        fired = []
        self.ma.add_alert("disk", "gt", 0.5, cooldown_s=9999)
        self.ma.on_alert(lambda r, v: fired.append(v))
        self.ma.record("disk", 0.9)
        self.ma.record("disk", 0.95)
        self.assertEqual(len(fired), 1)

    def test_remove_alert(self):
        rule = self.ma.add_alert("x", "gt", 1.0, cooldown_s=0)
        fired = []
        self.ma.on_alert(lambda r, v: fired.append(v))
        self.ma.remove_alert(rule.rule_id)
        self.ma.record("x", 99.0)
        self.assertEqual(len(fired), 0)

    def test_timer_context(self):
        with self.ma.timer("op_time"):
            time.sleep(0.01)
        val = self.ma.query("op_time",
                            __import__("agent.metrics_aggregator",
                                       fromlist=["AggFunc"]).AggFunc.LAST,
                            window_s=60)
        self.assertGreater(val, 0)

    def test_metric_names(self):
        self.ma.record("alpha", 1.0)
        self.ma.record("beta", 2.0)
        names = self.ma.metric_names()
        self.assertIn("alpha", names)
        self.assertIn("beta", names)

    def test_latest(self):
        for v in [1.0, 2.0, 3.0]: self.ma.record("lt", v)
        latest = self.ma.latest("lt", 2)
        self.assertEqual(len(latest), 2)

    def test_rollup(self):
        from agent.metrics_aggregator import AggFunc
        for v in [1.0, 2.0, 3.0]: self.ma.record("rl", v)
        result = self.ma.rollup("rl", bucket_s=60, agg_fn=AggFunc.SUM)
        self.assertGreater(len(result), 0)

    def test_stats(self):
        self.ma.record("s", 1.0)
        s = self.ma.stats()
        self.assertGreater(s["total_recorded"], 0)
        self.assertIn("samples_in_window", s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v54: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
