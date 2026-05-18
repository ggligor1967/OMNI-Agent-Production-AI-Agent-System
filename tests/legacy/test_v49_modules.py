"""OMNI AGENT v49: AdaptiveThrottler, MemoryCompressor, PluginSandboxV2, StreamAggregatorV2"""
import asyncio, os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# ADAPTIVE THROTTLER
# ════════════════════════════════════════════════════════
class TestAdaptiveThrottler(unittest.TestCase):
    def _throttler(self, strategy="token_bucket", **kw):
        from agent.adaptive_throttler import AdaptiveThrottler, ThrottlePolicy, ThrottleStrategy
        p = ThrottlePolicy(
            name="test",
            strategy=ThrottleStrategy(strategy),
            capacity=kw.pop("capacity", 5),
            refill_rate=kw.pop("refill_rate", 100),
            max_requests=kw.pop("max_requests", 5),
            window_s=kw.pop("window_s", 1.0),
            **kw)
        return AdaptiveThrottler(p, db_path=":memory:")

    def test_token_bucket_allows(self):
        t = self._throttler()
        self.assertTrue(t.check("user1"))

    def test_token_bucket_blocks_after_capacity(self):
        t = self._throttler(capacity=3)
        for _ in range(3): t.check("u")
        self.assertFalse(t.check("u"))

    def test_token_bucket_require_raises(self):
        from agent.adaptive_throttler import ThrottleExceeded
        t = self._throttler(capacity=0)
        with self.assertRaises(ThrottleExceeded):
            t.require("u")

    def test_token_bucket_refills(self):
        t = self._throttler(capacity=1, refill_rate=1000)
        t.check("u")  # depletes
        time.sleep(0.01)
        self.assertTrue(t.check("u"))

    def test_sliding_window_allows(self):
        t = self._throttler("sliding_window", max_requests=5, window_s=60)
        self.assertTrue(t.check("u"))

    def test_sliding_window_blocks(self):
        t = self._throttler("sliding_window", max_requests=3, window_s=60)
        for _ in range(3): t.check("u")
        self.assertFalse(t.check("u"))

    def test_fixed_window_allows(self):
        t = self._throttler("fixed_window", max_requests=5, window_s=60)
        self.assertTrue(t.check("u"))

    def test_fixed_window_blocks(self):
        t = self._throttler("fixed_window", max_requests=2, window_s=60)
        t.check("u"); t.check("u")
        self.assertFalse(t.check("u"))

    def test_leaky_bucket_allows(self):
        t = self._throttler("leaky_bucket", capacity=5, refill_rate=100)
        self.assertTrue(t.check("u"))

    def test_leaky_bucket_blocks(self):
        t = self._throttler("leaky_bucket", capacity=2, refill_rate=0.001)
        t.check("u"); t.check("u")
        self.assertFalse(t.check("u"))

    def test_per_key_isolation(self):
        t = self._throttler(capacity=1)
        t.check("userA")   # depletes userA
        self.assertTrue(t.check("userB"))  # userB still fresh

    def test_reset_key(self):
        t = self._throttler(capacity=1)
        t.check("u")
        self.assertFalse(t.check("u"))
        t.reset("u")
        self.assertTrue(t.check("u"))

    def test_reset_all(self):
        t = self._throttler(capacity=1)
        t.check("a"); t.check("b")
        t.reset_all()
        self.assertTrue(t.check("a"))
        self.assertTrue(t.check("b"))

    def test_on_throttle_hook(self):
        blocked = []
        t = self._throttler(capacity=0)
        t.on_throttle(lambda key, state: blocked.append(key))
        t.check("u")
        self.assertIn("u", blocked)

    def test_wait_time_token_bucket(self):
        t = self._throttler(capacity=1, refill_rate=1)
        t.check("u")
        wt = t.wait_time("u")
        self.assertGreater(wt, 0)

    def test_get_state(self):
        t = self._throttler()
        t.check("u")
        s = t.get_state("u")
        self.assertIsNotNone(s)
        self.assertIn("total_allowed", s)

    def test_event_log(self):
        t = self._throttler()
        t.check("u")
        log = t.event_log()
        self.assertGreater(len(log), 0)

    def test_stats(self):
        t = self._throttler(capacity=2)
        t.check("u"); t.check("u"); t.check("u")
        s = t.stats()
        self.assertEqual(s["total_allowed"], 2)
        self.assertEqual(s["total_rejected"], 1)

    def test_async_check(self):
        t = self._throttler()
        result = _run(t.check_async("u"))
        self.assertTrue(result)

    def test_registry(self):
        from agent.adaptive_throttler import ThrottlerRegistry, ThrottlePolicy, ThrottleStrategy
        reg = ThrottlerRegistry()
        p = ThrottlePolicy("api", ThrottleStrategy.TOKEN_BUCKET)
        reg.register("api", p)
        self.assertIn("api", reg.list_throttlers())

    def test_registry_check_unknown_allows(self):
        from agent.adaptive_throttler import ThrottlerRegistry
        reg = ThrottlerRegistry()
        self.assertTrue(reg.check("nonexistent", "user"))

# ════════════════════════════════════════════════════════
# MEMORY COMPRESSOR
# ════════════════════════════════════════════════════════
class TestMemoryCompressor(unittest.TestCase):
    def setUp(self):
        from agent.memory_compressor import MemoryCompressor
        self.mc = MemoryCompressor(hot_limit=4, warm_limit=2, cold_limit=5,
                                   db_path=":memory:")

    def test_add_hot_entry(self):
        from agent.memory_compressor import MemoryTier
        e = self.mc.add("hello", role="user")
        self.assertEqual(e.tier, MemoryTier.HOT)

    def test_get_tier_hot(self):
        from agent.memory_compressor import MemoryTier
        self.mc.add("msg1"); self.mc.add("msg2")
        hot = self.mc.get_tier(MemoryTier.HOT)
        self.assertGreaterEqual(len(hot), 1)

    def test_compression_triggered(self):
        from agent.memory_compressor import MemoryTier
        for i in range(6):
            self.mc.add(f"message {i}")
        self.assertGreater(self.mc._compress_count, 0)

    def test_warm_entries_exist_after_compression(self):
        from agent.memory_compressor import MemoryTier
        for i in range(6):
            self.mc.add(f"message {i}")
        warm = self.mc.get_tier(MemoryTier.WARM)
        self.assertGreater(len(warm), 0)

    def test_hot_bounded_after_compression(self):
        from agent.memory_compressor import MemoryTier
        for i in range(10):
            self.mc.add(f"msg {i}")
        self.assertLessEqual(len(self.mc.get_tier(MemoryTier.HOT)), self.mc.hot_limit)

    def test_add_frozen(self):
        from agent.memory_compressor import MemoryTier
        e = self.mc.add_frozen("User is a Python expert")
        self.assertEqual(e.tier, MemoryTier.FROZEN)

    def test_frozen_not_compressed(self):
        from agent.memory_compressor import MemoryTier
        self.mc.add_frozen("Permanent fact")
        for i in range(10):
            self.mc.add(f"msg {i}")
        frozen = self.mc.get_tier(MemoryTier.FROZEN)
        self.assertEqual(len(frozen), 1)

    def test_get_context_returns_entries(self):
        self.mc.add("user message", role="user", token_count=5)
        ctx = self.mc.get_context(max_tokens=100)
        self.assertGreater(len(ctx), 0)

    def test_get_context_respects_token_limit(self):
        for _ in range(5):
            self.mc.add("word " * 100, token_count=100)
        ctx = self.mc.get_context(max_tokens=150)
        total = sum(e.token_count for e in ctx)
        self.assertLessEqual(total, 150)

    def test_search_finds_content(self):
        self.mc.add("Python is a great language")
        self.mc.add("Java is also popular")
        results = self.mc.search("Python")
        self.assertEqual(len(results), 1)

    def test_delete_entry(self):
        e = self.mc.add("delete me")
        self.assertTrue(self.mc.delete(e.entry_id))
        self.assertIsNone(self.mc.get(e.entry_id))

    def test_clear_tier(self):
        from agent.memory_compressor import MemoryTier
        self.mc.add("msg1"); self.mc.add("msg2")
        self.mc.clear_tier(MemoryTier.HOT)
        self.assertEqual(len(self.mc.get_tier(MemoryTier.HOT)), 0)

    def test_token_count_total(self):
        self.mc.add("five words here now ok", token_count=5)
        self.assertGreaterEqual(self.mc.token_count_total(), 5)

    def test_custom_summarizer(self):
        from agent.memory_compressor import MemoryCompressor
        mc = MemoryCompressor(hot_limit=2, summarize_fn=lambda entries: "CUSTOM_SUMMARY",
                               db_path=":memory:")
        for i in range(4):
            mc.add(f"msg {i}")
        from agent.memory_compressor import MemoryTier
        warm = mc.get_tier(MemoryTier.WARM)
        self.assertTrue(any("CUSTOM_SUMMARY" in e.content for e in warm))

    def test_compress_log(self):
        for i in range(6):
            self.mc.add(f"msg {i}")
        log = self.mc.compress_log()
        self.assertGreater(len(log), 0)

    def test_stats(self):
        self.mc.add("msg1")
        s = self.mc.stats()
        self.assertIn("total_entries", s)
        self.assertIn("by_tier", s)

# ════════════════════════════════════════════════════════
# PLUGIN SANDBOX V2
# ════════════════════════════════════════════════════════
class TestPluginSandboxV2(unittest.TestCase):
    def setUp(self):
        from agent.plugin_sandbox_v2 import PluginSandboxV2, Capability
        self.sb = PluginSandboxV2()
        self.Capability = Capability

    def _manifest(self, name="test_plugin", caps=None):
        from agent.plugin_sandbox_v2 import PluginManifest
        return PluginManifest(
            plugin_id=f"pid_{name}", name=name,
            required_capabilities=set(caps or []))

    def test_register_fn_plugin(self):
        m = self._manifest()
        self.sb.register(m, fn=lambda: 42)
        self.assertIn(m.plugin_id, self.sb._plugins)

    def test_run_fn_plugin(self):
        m = self._manifest()
        self.sb.register(m, fn=lambda: 99)
        pr = self.sb.run(m.plugin_id)
        self.assertTrue(pr.success)
        self.assertEqual(pr.result, 99)

    def test_run_code_plugin(self):
        m = self._manifest("code_plugin")
        code = "def run(): return 'hello from code'"
        self.sb.register(m, code=code)
        pr = self.sb.run(m.plugin_id)
        self.assertTrue(pr.success)
        self.assertEqual(pr.result, "hello from code")

    def test_plugin_with_args(self):
        m = self._manifest("args_plugin")
        self.sb.register(m, fn=lambda x, y: x + y)
        pr = self.sb.run(m.plugin_id, 3, 4)
        self.assertEqual(pr.result, 7)

    def test_plugin_error_captured(self):
        m = self._manifest("bad_plugin")
        self.sb.register(m, fn=lambda: (_ for _ in ()).throw(ValueError("oops")))
        pr = self.sb.run(m.plugin_id)
        self.assertFalse(pr.success)
        self.assertIn("oops", pr.error)

    def test_capability_denied(self):
        from agent.plugin_sandbox_v2 import Capability, CapabilityDenied
        m = self._manifest("net_plugin", caps=[Capability.NETWORK])
        with self.assertRaises(CapabilityDenied):
            self.sb.register(m, fn=lambda: None)

    def test_capability_granted(self):
        from agent.plugin_sandbox_v2 import Capability
        self.sb.grant(Capability.NETWORK)
        m = self._manifest("net_plugin", caps=[Capability.NETWORK])
        self.assertTrue(self.sb.register(m, fn=lambda: "ok"))

    def test_grant_revoke(self):
        from agent.plugin_sandbox_v2 import Capability
        self.sb.grant(Capability.FILESYSTEM)
        self.assertTrue(self.sb.has_capability(Capability.FILESYSTEM))
        self.sb.revoke(Capability.FILESYSTEM)
        self.assertFalse(self.sb.has_capability(Capability.FILESYSTEM))

    def test_disable_plugin(self):
        from agent.plugin_sandbox_v2 import PluginError
        m = self._manifest()
        self.sb.register(m, fn=lambda: 1)
        self.sb.disable(m.plugin_id)
        with self.assertRaises(PluginError):
            self.sb.run(m.plugin_id)

    def test_enable_plugin(self):
        m = self._manifest()
        self.sb.register(m, fn=lambda: 1)
        self.sb.disable(m.plugin_id)
        self.sb.enable(m.plugin_id)
        pr = self.sb.run(m.plugin_id)
        self.assertTrue(pr.success)

    def test_unregister(self):
        from agent.plugin_sandbox_v2 import PluginError
        m = self._manifest()
        self.sb.register(m, fn=lambda: 1)
        self.sb.unregister(m.plugin_id)
        with self.assertRaises(PluginError):
            self.sb.run(m.plugin_id)

    def test_sandbox_blocks_eval(self):
        m = self._manifest("eval_plugin")
        code = "def run(): return eval('1+1')"
        self.sb.register(m, code=code)
        pr = self.sb.run(m.plugin_id)
        self.assertFalse(pr.success)

    def test_on_before_run_hook(self):
        called = []
        self.sb.on_before_run(lambda m: called.append(m.name))
        m = self._manifest()
        self.sb.register(m, fn=lambda: 1)
        self.sb.run(m.plugin_id)
        self.assertIn("test_plugin", called)

    def test_on_after_run_hook(self):
        results = []
        self.sb.on_after_run(lambda pr: results.append(pr.plugin_id))
        m = self._manifest()
        self.sb.register(m, fn=lambda: 1)
        self.sb.run(m.plugin_id)
        self.assertIn(m.plugin_id, results)

    def test_async_run(self):
        m = self._manifest("async_plugin")
        self.sb.register(m, fn=lambda: "async_ok")
        pr = _run(self.sb.run_async(m.plugin_id))
        self.assertTrue(pr.success)
        self.assertEqual(pr.result, "async_ok")

    def test_list_plugins(self):
        m = self._manifest()
        self.sb.register(m, fn=lambda: 1)
        plugins = self.sb.list_plugins()
        self.assertEqual(len(plugins), 1)

    def test_results_history(self):
        m = self._manifest()
        self.sb.register(m, fn=lambda: 1)
        self.sb.run(m.plugin_id)
        self.sb.run(m.plugin_id)
        results = self.sb.results(m.plugin_id)
        self.assertEqual(len(results), 2)

    def test_stats(self):
        m = self._manifest()
        self.sb.register(m, fn=lambda: 1)
        self.sb.run(m.plugin_id)
        s = self.sb.stats()
        self.assertEqual(s["invocations"], 1)
        self.assertEqual(s["registered"], 1)

# ════════════════════════════════════════════════════════
# STREAM AGGREGATOR V2
# ════════════════════════════════════════════════════════
class TestStreamAggregatorV2(unittest.TestCase):
    def setUp(self):
        from agent.stream_aggregator_v2 import StreamAggregatorV2
        self.agg = StreamAggregatorV2()
        self.s1 = self.agg.add_stream("sensor1")
        self.s2 = self.agg.add_stream("sensor2")

    def test_emit_and_latest(self):
        self.s1.emit(42)
        events = self.agg.latest("sensor1", 1)
        self.assertEqual(events[0].value, 42)

    def test_multiple_emit(self):
        for v in [1, 2, 3]: self.s1.emit(v)
        events = self.agg.latest("sensor1", 3)
        self.assertEqual(len(events), 3)

    def test_aggregate_sum(self):
        from agent.stream_aggregator_v2 import AggFunc
        for v in [1, 2, 3]: self.s1.emit(v)
        result = self.agg.aggregate("sensor1", AggFunc.SUM)
        self.assertEqual(result, 6)

    def test_aggregate_avg(self):
        from agent.stream_aggregator_v2 import AggFunc
        for v in [2, 4, 6]: self.s1.emit(v)
        result = self.agg.aggregate("sensor1", AggFunc.AVG)
        self.assertAlmostEqual(result, 4.0)

    def test_aggregate_min_max(self):
        from agent.stream_aggregator_v2 import AggFunc
        for v in [5, 1, 9, 3]: self.s1.emit(v)
        self.assertEqual(self.agg.aggregate("sensor1", AggFunc.MIN), 1)
        self.assertEqual(self.agg.aggregate("sensor1", AggFunc.MAX), 9)

    def test_aggregate_count(self):
        from agent.stream_aggregator_v2 import AggFunc
        for _ in range(5): self.s1.emit(1)
        self.assertEqual(self.agg.aggregate("sensor1", AggFunc.COUNT), 5)

    def test_aggregate_first_last(self):
        from agent.stream_aggregator_v2 import AggFunc
        for v in [10, 20, 30]: self.s1.emit(v)
        self.assertEqual(self.agg.aggregate("sensor1", AggFunc.FIRST), 10)
        self.assertEqual(self.agg.aggregate("sensor1", AggFunc.LAST), 30)

    def test_aggregate_list(self):
        from agent.stream_aggregator_v2 import AggFunc
        for v in [7, 8, 9]: self.s1.emit(v)
        result = self.agg.aggregate("sensor1", AggFunc.LIST)
        self.assertIsInstance(result, list)
        self.assertIn(8, result)

    def test_aggregate_window(self):
        from agent.stream_aggregator_v2 import AggFunc
        for v in [1, 2, 3]: self.s1.emit(v)
        result = self.agg.aggregate("sensor1", AggFunc.SUM, window_s=60)
        self.assertEqual(result, 6)

    def test_tumbling_windows(self):
        from agent.stream_aggregator_v2 import AggFunc, WindowType
        import time
        for v in [1, 2, 3, 4]: self.s1.emit(v)
        wins = self.agg.window("sensor1", size_s=10, agg_fn=AggFunc.SUM,
                               window_type=WindowType.TUMBLING)
        self.assertGreater(len(wins), 0)

    def test_count_windows(self):
        from agent.stream_aggregator_v2 import AggFunc, WindowType
        for v in range(6): self.s1.emit(v)
        wins = self.agg.window("sensor1", size_s=2, agg_fn=AggFunc.SUM,
                               window_type=WindowType.COUNT)
        self.assertGreater(len(wins), 0)
        self.assertEqual(wins[0].event_count, 2)

    def test_cross_aggregate(self):
        from agent.stream_aggregator_v2 import AggFunc
        for v in [1, 2]: self.s1.emit(v)
        for v in [3, 4]: self.s2.emit(v)
        result = self.agg.cross_aggregate(["sensor1", "sensor2"], AggFunc.SUM)
        self.assertEqual(result, 10)

    def test_join_streams(self):
        self.s1.emit(100)
        self.s2.emit(200)
        pairs = self.agg.join("sensor1", "sensor2", window_s=5.0)
        self.assertGreater(len(pairs), 0)

    def test_merged_latest(self):
        self.s1.emit(1); self.s2.emit(2)
        merged = self.agg.merged_latest(2)
        self.assertEqual(len(merged), 2)

    def test_global_subscriber(self):
        received = []
        self.agg.subscribe_all(lambda e: received.append(e.value))
        self.s1.emit(99)
        self.assertIn(99, received)

    def test_stream_filter(self):
        self.s1.filter(lambda e: e.value > 5)
        self.s1.emit(3)    # filtered out
        self.s1.emit(10)   # passes
        events = self.agg.latest("sensor1", 10)
        self.assertTrue(all(e.value > 5 for e in events))

    def test_stream_transform(self):
        from agent.stream_aggregator_v2 import StreamEvent
        self.s1.transform(lambda e: StreamEvent(source=e.source, value=e.value * 2))
        self.s1.emit(5)
        events = self.agg.latest("sensor1", 1)
        self.assertEqual(events[0].value, 10)

    def test_remove_stream(self):
        self.agg.add_stream("temp")
        self.agg.remove_stream("temp")
        self.assertIsNone(self.agg.get_stream("temp"))

    def test_async_emit(self):
        _run(self.agg.emit_async("sensor1", 777))
        events = self.agg.latest("sensor1", 1)
        self.assertEqual(events[0].value, 777)

    def test_stats(self):
        self.s1.emit(1); self.s1.emit(2)
        s = self.agg.stats()
        self.assertEqual(s["streams"], 2)
        self.assertGreaterEqual(s["total_events"], 2)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v49: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
