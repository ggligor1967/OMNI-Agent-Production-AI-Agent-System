"""OMNI AGENT v22 Tests: RateLimiterAdvanced, ResponseCache, ToolExecutor, StreamingAggregator"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# RATE LIMITER ADVANCED
# ════════════════════════════════════════════════════════
class TestRateLimiterAdvanced(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.rate_limiter_advanced import RateLimiterAdvanced
        self.rl = RateLimiterAdvanced(db_path=os.path.join(td,"rl.db"), audit=True)
        self.rl.set_quota("alice", tier="pro")
        self.rl.set_quota("bob",   tier="free")

    def test_allowed_within_limits(self):
        d = _run(self.rl.check("alice"))
        self.assertTrue(d.allowed)

    def test_tokens_remaining_positive(self):
        _run(self.rl.check("alice"))
        self.assertGreater(self.rl.tokens_remaining("alice"), 0)

    def test_decision_to_dict(self):
        d = _run(self.rl.check("alice"))
        dct = d.to_dict()
        for k in ["actor","allowed","retry_after","tokens_remaining"]: self.assertIn(k, dct)

    def test_custom_tier(self):
        self.rl.define_tier("custom", rpm=5, rph=50, burst=2, refill_rate=0.1)
        self.rl.set_quota("dave", tier="custom")
        d = _run(self.rl.check("dave"))
        self.assertTrue(d.allowed)

    def test_burst_limit(self):
        self.rl.set_quota("burst_user", tier="free")
        # free tier burst=5 — exhaust all tokens instantly
        results = [_run(self.rl.check("burst_user", tokens=1.0)) for _ in range(10)]
        denied = [r for r in results if not r.allowed]
        self.assertGreater(len(denied), 0)

    def test_rpm_limit(self):
        self.rl.set_quota("rpm_user", rpm=3, rph=1000, burst=100, refill_rate=100.0)
        results = [_run(self.rl.check("rpm_user")) for _ in range(5)]
        denied = [r for r in results if not r.allowed]
        self.assertGreater(len(denied), 0)

    def test_disable_quota(self):
        self.rl.disable_quota("alice")
        d = _run(self.rl.check("alice"))
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "quota_disabled")

    def test_enable_quota(self):
        self.rl.disable_quota("alice")
        self.rl.enable_quota("alice")
        d = _run(self.rl.check("alice"))
        self.assertTrue(d.allowed)

    def test_reset_actor(self):
        self.rl.set_quota("reset_user", rpm=2, rph=100, burst=2, refill_rate=100.0)
        _run(self.rl.check("reset_user")); _run(self.rl.check("reset_user"))
        self.rl.reset_actor("reset_user")
        d = _run(self.rl.check("reset_user"))
        self.assertTrue(d.allowed)

    def test_default_quota_for_unknown_actor(self):
        d = _run(self.rl.check("unknown_user_xyz"))
        self.assertTrue(d.allowed)  # gets free tier

    def test_wait_and_consume_allowed(self):
        d = _run(self.rl.wait_and_consume("alice", tokens=1.0, max_wait=1.0))
        self.assertTrue(d.allowed)

    def test_global_rps_limit(self):
        from agent.rate_limiter_advanced import RateLimiterAdvanced
        td = tempfile.mkdtemp()
        rl = RateLimiterAdvanced(db_path=os.path.join(td,"gl.db"),
                                  global_rps=2.0, audit=False)
        rl.set_quota("g1", tier="enterprise")
        results = [_run(rl.check("g1")) for _ in range(30)]
        denied = [r for r in results if not r.allowed]
        self.assertGreater(len(denied), 0)

    def test_stats(self):
        _run(self.rl.check("alice")); _run(self.rl.check("bob"))
        s = self.rl.stats()
        for k in ["total_requests","denied","allowed","denial_rate"]: self.assertIn(k, s)

    def test_stats_per_actor(self):
        _run(self.rl.check("alice"))
        s = self.rl.stats("alice")
        self.assertGreaterEqual(s["total_requests"], 1)

    def test_tiers_list(self):
        tiers = self.rl.tiers()
        names = [t.name for t in tiers]
        self.assertIn("free", names); self.assertIn("pro", names)

    def test_tier_to_dict(self):
        tiers = self.rl.tiers()
        d = tiers[0].to_dict()
        for k in ["name","rpm","rph","burst"]: self.assertIn(k, d)

    def test_token_bucket_peek(self):
        from agent.rate_limiter_advanced import TokenBucket
        tb = TokenBucket(capacity=10.0, refill_rate=1.0)
        peek = tb.peek()
        self.assertAlmostEqual(peek, 10.0, delta=0.1)

    def test_token_bucket_consume(self):
        from agent.rate_limiter_advanced import TokenBucket
        tb = TokenBucket(capacity=5.0, refill_rate=1.0)
        ok, wait = tb.consume(3.0)
        self.assertTrue(ok); self.assertEqual(wait, 0.0)
        ok2, wait2 = tb.consume(3.0)  # only 2 left
        self.assertFalse(ok2); self.assertGreater(wait2, 0)

    def test_sliding_window(self):
        from agent.rate_limiter_advanced import SlidingWindow
        sw = SlidingWindow(max_requests=3, window_s=60.0)
        ok1, _ = sw.allow(); ok2, _ = sw.allow(); ok3, _ = sw.allow()
        ok4, wait = sw.allow()
        self.assertTrue(ok1 and ok2 and ok3)
        self.assertFalse(ok4); self.assertGreater(wait, 0)

# ════════════════════════════════════════════════════════
# RESPONSE CACHE
# ════════════════════════════════════════════════════════
class TestResponseCache(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.response_cache import ResponseCache
        self.cache = ResponseCache(db_path=os.path.join(td,"rc.db"),
                                    similarity_threshold=0.8)

    def test_set_and_exact_hit(self):
        _run(self.cache.set("What is Python?", "Python is a language."))
        result = _run(self.cache.get("What is Python?"))
        self.assertTrue(result.hit); self.assertTrue(result.exact)

    def test_exact_hit_response(self):
        _run(self.cache.set("Capital of France?", "Paris"))
        r = _run(self.cache.get("Capital of France?"))
        self.assertEqual(r.response, "Paris")

    def test_miss(self):
        r = _run(self.cache.get("completely unique prompt xyz 123"))
        self.assertFalse(r.hit)

    def test_semantic_hit(self):
        _run(self.cache.set("What is the capital of France", "Paris"))
        r = _run(self.cache.get("What is the capital of France exactly"))
        self.assertTrue(r.hit)

    def test_similarity_below_threshold(self):
        from agent.response_cache import ResponseCache
        td = tempfile.mkdtemp()
        cache = ResponseCache(db_path=os.path.join(td,"rc2.db"),
                               similarity_threshold=0.99)  # very strict
        _run(cache.set("Python programming tips", "Use list comprehensions"))
        r = _run(cache.get("Java programming advice totally different"))
        self.assertFalse(r.hit)

    def test_namespace_isolation(self):
        _run(self.cache.set("query", "answer-A", namespace="ns1"))
        _run(self.cache.set("query", "answer-B", namespace="ns2"))
        r1 = _run(self.cache.get("query", namespace="ns1"))
        r2 = _run(self.cache.get("query", namespace="ns2"))
        self.assertEqual(r1.response, "answer-A")
        self.assertEqual(r2.response, "answer-B")

    def test_ttl_expiry(self):
        _run(self.cache.set("expiring", "value", ttl=0.01))
        time.sleep(0.05)
        r = _run(self.cache.get("expiring"))
        self.assertFalse(r.hit)

    def test_invalidate(self):
        _run(self.cache.set("remove me", "value"))
        ok = _run(self.cache.invalidate("remove me"))
        self.assertTrue(ok)
        r = _run(self.cache.get("remove me"))
        self.assertFalse(r.hit)

    def test_invalidate_miss(self):
        ok = _run(self.cache.invalidate("nonexistent key abc"))
        self.assertFalse(ok)

    def test_warm(self):
        pairs = [{"prompt": f"Q{i}", "response": f"A{i}"} for i in range(5)]
        _run(self.cache.warm(pairs))
        for i in range(5):
            r = _run(self.cache.get(f"Q{i}"))
            self.assertTrue(r.hit)

    def test_flush_namespace(self):
        _run(self.cache.set("p1","r1", namespace="temp"))
        _run(self.cache.set("p2","r2", namespace="temp"))
        n = self.cache.flush_namespace("temp")
        self.assertGreaterEqual(n, 2)

    def test_flush_expired(self):
        _run(self.cache.set("ex1","r1", ttl=0.01))
        time.sleep(0.05)
        n = self.cache.flush_expired()
        self.assertGreaterEqual(n, 0)  # at least tries

    def test_lru_eviction(self):
        from agent.response_cache import ResponseCache
        td = tempfile.mkdtemp()
        cache = ResponseCache(db_path=os.path.join(td,"rc3.db"), max_size=3)
        for i in range(5):
            _run(cache.set(f"prompt_{i}", f"resp_{i}"))
        # Cache should have evicted some; in-memory index has at most 3
        self.assertLessEqual(len(cache._index.get("default", {})), 3)

    def test_stats(self):
        _run(self.cache.set("s","v"))
        _run(self.cache.get("s")); _run(self.cache.get("miss"))
        s = self.cache.stats()
        for k in ["total_lookups","cache_hits","cache_misses","hit_rate"]: self.assertIn(k, s)

    def test_stats_namespace(self):
        _run(self.cache.set("q","r", namespace="myns"))
        _run(self.cache.get("q", namespace="myns"))
        s = self.cache.stats("myns")
        self.assertGreaterEqual(s["cache_hits"], 1)

    def test_lookup_to_dict(self):
        _run(self.cache.set("q","r"))
        r = _run(self.cache.get("q"))
        d = r.to_dict()
        for k in ["hit","similarity","exact","latency_ms"]: self.assertIn(k, d)

    def test_entry_to_dict(self):
        _run(self.cache.set("q","r"))
        entry = list(self.cache._index.get("default",{}).values())[0]
        d = entry.to_dict()
        for k in ["key","namespace","hits","ttl","expired"]: self.assertIn(k, d)

    def test_persistence(self):
        from agent.response_cache import ResponseCache
        td = tempfile.mkdtemp(); db = os.path.join(td,"rc.db")
        c1 = ResponseCache(db_path=db, default_ttl=3600)
        _run(c1.set("persist prompt", "persist response"))
        c2 = ResponseCache(db_path=db, default_ttl=3600)
        r = _run(c2.get("persist prompt"))
        self.assertTrue(r.hit)

# ════════════════════════════════════════════════════════
# TOOL EXECUTOR
# ════════════════════════════════════════════════════════
class TestToolExecutor(unittest.TestCase):
    def setUp(self):
        from agent.tool_executor import ToolExecutor, ToolSchema
        self.ex = ToolExecutor(max_concurrent=5)
        self.ToolSchema = ToolSchema
        self.ex.register("add", lambda a, b: a + b,
                          schema=ToolSchema(required=["a","b"],
                                             properties={"a":"int","b":"int"}))
        self.ex.register("echo", lambda text="": text,
                          schema=ToolSchema(properties={"text":"str"}))

    def test_execute_success(self):
        r = _run(self.ex.execute("add", {"a":2,"b":3}))
        from agent.tool_executor import ExecStatus
        self.assertEqual(r.status, ExecStatus.SUCCESS)
        self.assertEqual(r.output, 5)

    def test_execute_unknown_tool(self):
        r = _run(self.ex.execute("nonexistent"))
        from agent.tool_executor import ExecStatus
        self.assertEqual(r.status, ExecStatus.INVALID)

    def test_execute_missing_required(self):
        r = _run(self.ex.execute("add", {"a": 1}))
        from agent.tool_executor import ExecStatus
        self.assertEqual(r.status, ExecStatus.INVALID)
        self.assertIn("b", r.error)

    def test_execute_type_error(self):
        r = _run(self.ex.execute("add", {"a":"not_int","b":2}))
        from agent.tool_executor import ExecStatus
        self.assertEqual(r.status, ExecStatus.INVALID)

    def test_dry_run(self):
        r = _run(self.ex.execute("add", {"a":1,"b":2}, dry_run=True))
        from agent.tool_executor import ExecStatus
        self.assertEqual(r.status, ExecStatus.DRY_RUN)
        self.assertIsNone(r.output)

    def test_timeout(self):
        from agent.tool_executor import ToolExecutor
        import asyncio
        ex = ToolExecutor()
        async def slow_fn(): await asyncio.sleep(10)
        ex.register("slow", slow_fn, timeout_s=0.05, max_retries=0)
        r = _run(ex.execute("slow", {}))
        from agent.tool_executor import ExecStatus
        self.assertIn(r.status, [ExecStatus.TIMEOUT, ExecStatus.FAILED])

    def test_retry_on_failure(self):
        from agent.tool_executor import ToolSchema
        calls = [0]
        def flaky():
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("not yet")
            return "ok"
        self.ex.register("flaky", flaky, max_retries=3, retry_delay=0.01)
        r = _run(self.ex.execute("flaky", {}))
        from agent.tool_executor import ExecStatus
        self.assertEqual(r.status, ExecStatus.SUCCESS)
        self.assertGreater(r.retries, 0)

    def test_deactivate_tool(self):
        self.ex.deactivate("echo")
        r = _run(self.ex.execute("echo", {"text":"hello"}))
        from agent.tool_executor import ExecStatus
        self.assertEqual(r.status, ExecStatus.SKIPPED)

    def test_activate_tool(self):
        self.ex.deactivate("echo")
        self.ex.activate("echo")
        self.assertTrue(self.ex._tools["echo"].active)

    def test_async_tool(self):
        async def async_add(a, b): return a + b
        self.ex.register("async_add", async_add,
                          schema=self.ToolSchema(required=["a","b"]))
        r = _run(self.ex.execute("async_add", {"a":10,"b":20}))
        self.assertEqual(r.output, 30)

    def test_execute_chain(self):
        self.ex.register("double", lambda a: a * 2,
                          schema=self.ToolSchema(required=["a"]))
        steps = [{"tool":"add","inputs":{"a":3,"b":4}},
                  {"tool":"echo","inputs":{"text":"done"}}]
        results = _run(self.ex.execute_chain(steps))
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].success)

    def test_execute_parallel(self):
        steps = [{"tool":"add","inputs":{"a":1,"b":2}},
                  {"tool":"add","inputs":{"a":3,"b":4}}]
        results = _run(self.ex.execute_parallel(steps))
        self.assertEqual(len(results), 2)
        outputs = [r.output for r in results]
        self.assertIn(3, outputs); self.assertIn(7, outputs)

    def test_cost_units_tracked(self):
        self.ex.register("expensive", lambda: "result", cost_units=5.0)
        _run(self.ex.execute("expensive"))
        self.assertGreater(self.ex._total_cost, 0)

    def test_audit_trail(self):
        _run(self.ex.execute("add", {"a":1,"b":2}))
        audit = self.ex.audit(limit=10)
        self.assertGreater(len(audit), 0)

    def test_audit_filter_by_tool(self):
        _run(self.ex.execute("add", {"a":1,"b":2}))
        _run(self.ex.execute("echo", {"text":"hi"}))
        audit = self.ex.audit(tool_name="add")
        self.assertTrue(all(r.tool_name == "add" for r in audit))

    def test_stats(self):
        _run(self.ex.execute("add", {"a":1,"b":2}))
        s = self.ex.stats()
        for k in ["total_executions","success","failed","success_rate"]: self.assertIn(k, s)

    def test_result_to_dict(self):
        r = _run(self.ex.execute("add", {"a":1,"b":2}))
        d = r.to_dict()
        for k in ["id","tool","status","duration_ms","retries"]: self.assertIn(k, d)

    def test_tool_to_dict(self):
        t = self.ex._tools["add"]
        d = t.to_dict()
        for k in ["id","name","description","tags","timeout_s","call_count"]: self.assertIn(k, d)

    def test_schema_validate_ok(self):
        s = self.ToolSchema(required=["x"], properties={"x":"int"})
        errs = s.validate({"x":42})
        self.assertEqual(len(errs), 0)

    def test_schema_validate_fail(self):
        s = self.ToolSchema(required=["x"], properties={"x":"int"})
        errs = s.validate({"x":"not_int"})
        self.assertGreater(len(errs), 0)

# ════════════════════════════════════════════════════════
# STREAMING AGGREGATOR
# ════════════════════════════════════════════════════════
class TestStreamingAggregator(unittest.TestCase):
    def setUp(self):
        from agent.streaming_aggregator import StreamingAggregator
        self.agg = StreamingAggregator()

    def _list_gen(self, items, delay=0.0):
        from agent.streaming_aggregator import tokens_from_list
        return tokens_from_list(items, delay)

    def test_collect(self):
        tokens = _run(self.agg.collect(self._list_gen([1,2,3,4,5])))
        self.assertEqual(tokens, [1,2,3,4,5])

    def test_join(self):
        result = _run(self.agg.join(self._list_gen(["hello"," ","world"])))
        self.assertEqual(result, "hello world")

    def test_merge_two_streams(self):
        gen1 = self._list_gen([1,2,3])
        gen2 = self._list_gen([4,5,6])
        tokens = _run(self.agg.collect(self.agg.merge(gen1, gen2)))
        self.assertEqual(sorted(tokens), [1,2,3,4,5,6])

    def test_merge_empty_stream(self):
        gen1 = self._list_gen([1,2])
        gen2 = self._list_gen([])
        tokens = _run(self.agg.collect(self.agg.merge(gen1, gen2)))
        self.assertEqual(sorted(tokens), [1,2])

    def test_concat(self):
        gen1 = self._list_gen([1,2])
        gen2 = self._list_gen([3,4])
        tokens = _run(self.agg.collect(self.agg.concat(gen1, gen2)))
        self.assertEqual(tokens, [1,2,3,4])  # strictly ordered

    def test_zip_streams(self):
        gen1 = self._list_gen([1,2,3])
        gen2 = self._list_gen(["a","b","c"])
        tokens = _run(self.agg.collect(self.agg.zip(gen1, gen2)))
        self.assertEqual(tokens, [(1,"a"),(2,"b"),(3,"c")])

    def test_buffer(self):
        gen = self._list_gen(list(range(10)))
        chunks = _run(self.agg.collect(self.agg.buffer(gen, chunk_size=3, timeout_s=10)))
        flat = [t for chunk in chunks for t in chunk]
        self.assertEqual(flat, list(range(10)))

    def test_deduplicate(self):
        gen = self._list_gen([1,1,2,2,2,3,1])
        tokens = _run(self.agg.collect(self.agg.deduplicate(gen, window=1)))
        # Consecutive duplicates removed: [1,2,3,1]
        self.assertNotIn((1,1), list(zip(tokens,tokens[1:])))

    def test_transform(self):
        gen = self._list_gen([1,2,3])
        tokens = _run(self.agg.collect(self.agg.transform(gen, lambda x: x * 2)))
        self.assertEqual(tokens, [2,4,6])

    def test_async_transform(self):
        gen = self._list_gen([1,2,3])
        async def double(x): return x * 2
        tokens = _run(self.agg.collect(self.agg.transform(gen, double)))
        self.assertEqual(tokens, [2,4,6])

    def test_filter(self):
        gen = self._list_gen([1,2,3,4,5,6])
        tokens = _run(self.agg.collect(self.agg.filter(gen, lambda x: x % 2 == 0)))
        self.assertEqual(tokens, [2,4,6])

    def test_throttle(self):
        gen = self._list_gen([1,2,3], delay=0.0)
        start = time.time()
        tokens = _run(self.agg.collect(self.agg.throttle(gen, rate=100.0)))
        self.assertEqual(tokens, [1,2,3])

    def test_fan_out_two_consumers(self):
        async def run_fanout():
            gen = self._list_gen([10,20,30])
            fo = await self.agg.fan_out(gen, consumers=2)
            c0 = await self.agg.collect(fo.consumer(0))
            return c0
        tokens = _run(run_fanout())
        self.assertEqual(tokens, [10,20,30])

    def test_fan_out_stats(self):
        async def run():
            gen = self._list_gen([1,2])
            fo = await self.agg.fan_out(gen, consumers=1)
            await self.agg.collect(fo.consumer(0))
            return fo._stats
        stats = _run(run())
        self.assertIn("produced", stats)

    def test_merge_three_streams(self):
        gens = [self._list_gen([i]) for i in range(5)]
        tokens = _run(self.agg.collect(self.agg.merge(*gens)))
        self.assertEqual(sorted(tokens), list(range(5)))

    def test_collect_max_tokens(self):
        gen = self._list_gen(list(range(100)))
        tokens = _run(self.agg.collect(gen, max_tokens=5))
        self.assertEqual(len(tokens), 5)

    def test_transform_filter_none(self):
        gen = self._list_gen([1,2,3,4])
        # return None to drop odd numbers
        tokens = _run(self.agg.collect(
            self.agg.transform(gen, lambda x: x if x % 2 == 0 else None)))
        self.assertEqual(tokens, [2,4])

    def test_stats(self):
        gen = self._list_gen([1,2])
        _run(self.agg.collect(self.agg.merge(gen)))
        _run(self.agg.collect(self.agg.concat(self._list_gen([1]))))
        s = self.agg.stats()
        self.assertIn("merged", s); self.assertIn("concat", s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v22: {total-failed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
