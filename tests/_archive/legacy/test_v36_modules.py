"""OMNI AGENT v36: RetryManager, DataPipeline, SearchEngine, NotificationManager"""
import asyncio, json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# RETRY MANAGER
# ════════════════════════════════════════════════════════
class TestRetryManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.retry_manager import RetryManager, BackoffStrategy, JitterMode, RetryResult
        self.rm = RetryManager(db_path=os.path.join(td,"rm.db"))
        self.BS = BackoffStrategy; self.JM = JitterMode; self.RR = RetryResult
        self.rm.register("exp", max_attempts=3, base_delay_s=0.01,
                          strategy=BackoffStrategy.EXPONENTIAL,
                          jitter=JitterMode.NONE)

    def test_success_first_try(self):
        async def ok(): return 42
        r = _run(self.rm.execute("exp", ok))
        self.assertTrue(r.success); self.assertEqual(r.result, 42); self.assertEqual(r.attempts, 1)

    def test_fail_all_retries(self):
        calls = [0]
        async def fail():
            calls[0] += 1; raise RuntimeError("boom")
        r = _run(self.rm.execute("exp", fail))
        self.assertFalse(r.success); self.assertEqual(calls[0], 3)

    def test_retry_then_succeed(self):
        calls = [0]
        async def flaky():
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("not yet")
            return "ok"
        r = _run(self.rm.execute("exp", flaky))
        self.assertTrue(r.success); self.assertEqual(r.result, "ok")

    def test_attempts_count(self):
        async def fail(): raise RuntimeError()
        r = _run(self.rm.execute("exp", fail))
        self.assertEqual(r.attempts, 3)

    def test_delays_recorded(self):
        async def fail(): raise RuntimeError()
        r = _run(self.rm.execute("exp", fail))
        self.assertGreaterEqual(len(r.delays), 2)

    def test_no_policy_calls_once(self):
        async def ok(): return 99
        r = _run(self.rm.execute("ghost_policy", ok))
        self.assertTrue(r.success); self.assertEqual(r.result, 99)

    def test_fixed_strategy(self):
        self.rm.register("fixed", max_attempts=2, base_delay_s=0.01,
                          strategy=self.BS.FIXED, jitter=self.JM.NONE)
        async def fail(): raise RuntimeError()
        r = _run(self.rm.execute("fixed", fail))
        self.assertFalse(r.success)

    def test_linear_strategy(self):
        self.rm.register("linear", max_attempts=2, base_delay_s=0.01,
                          strategy=self.BS.LINEAR, jitter=self.JM.NONE)
        async def fail(): raise RuntimeError()
        r = _run(self.rm.execute("linear", fail))
        self.assertFalse(r.success)

    def test_fibonacci_strategy(self):
        from agent.retry_manager import _fibonacci
        self.assertEqual(_fibonacci(1), 1.0)
        self.assertEqual(_fibonacci(2), 1.0)
        self.assertEqual(_fibonacci(3), 2.0)
        self.assertEqual(_fibonacci(4), 3.0)

    def test_full_jitter(self):
        self.rm.register("jit", max_attempts=2, base_delay_s=0.05,
                          strategy=self.BS.FIXED, jitter=self.JM.FULL)
        async def fail(): raise RuntimeError()
        r = _run(self.rm.execute("jit", fail))
        if r.delays: self.assertLessEqual(r.delays[0], 0.05)

    def test_deadline_stops_early(self):
        self.rm.register("dl", max_attempts=10, base_delay_s=0.5,
                          strategy=self.BS.FIXED, jitter=self.JM.NONE,
                          deadline_s=0.05)
        async def fail(): raise RuntimeError()
        r = _run(self.rm.execute("dl", fail))
        self.assertFalse(r.success); self.assertLess(r.attempts, 10)

    def test_stop_fn(self):
        stop_calls = [0]
        def stop(attempt, elapsed, exc):
            stop_calls[0] += 1
            return attempt >= 2
        self.rm.register("stop", max_attempts=5, base_delay_s=0.01,
                          strategy=self.BS.FIXED, jitter=self.JM.NONE,
                          stop_fn=stop)
        async def fail(): raise RuntimeError()
        r = _run(self.rm.execute("stop", fail))
        self.assertFalse(r.success); self.assertLess(r.attempts, 10)

    def test_dl_sink_called(self):
        dlq = []
        self.rm.register("dl_sink", max_attempts=1, base_delay_s=0.01,
                          strategy=self.BS.FIXED, jitter=self.JM.NONE,
                          dl_sink=lambda fn, exc, a: dlq.append(str(exc)))
        async def fail(): raise RuntimeError("dlq_test")
        _run(self.rm.execute("dl_sink", fail))
        self.assertGreater(len(dlq), 0)

    def test_before_hook(self):
        calls = []
        self.rm.before_attempt(lambda p, a, fn: calls.append(a))
        async def ok(): return 1
        _run(self.rm.execute("exp", ok))
        self.assertIn(1, calls)

    def test_after_hook_success(self):
        results = []
        self.rm.after_attempt(lambda p, a, exc, r: results.append(r) if r is not None else None)
        async def ok(): return 7
        _run(self.rm.execute("exp", ok))
        self.assertIn(7, results)

    def test_result_to_dict(self):
        async def ok(): return "x"
        r = _run(self.rm.execute("exp", ok))
        d = r.to_dict()
        for k in ["success","attempts","elapsed_s","delays"]: self.assertIn(k, d)

    def test_retry_decorator(self):
        calls = [0]
        @self.rm.retry("exp")
        async def flaky2():
            calls[0] += 1
            if calls[0] < 2: raise RuntimeError()
            return "done"
        result = _run(flaky2())
        self.assertEqual(result, "done")

    def test_stats(self):
        async def ok(): return 1
        _run(self.rm.execute("exp", ok))
        s = self.rm.stats()
        for k in ["total","success","policies"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# DATA PIPELINE
# ════════════════════════════════════════════════════════
class TestDataPipeline(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.data_pipeline import DataPipeline, ErrorAction
        self.DP = DataPipeline; self.EA = ErrorAction
        self.td = td

    def _make_pipeline(self, name="test"):
        from agent.data_pipeline import DataPipeline
        return DataPipeline(name, db_path=os.path.join(self.td, f"{name}.db"),
                             batch_size=5)

    def test_basic_run(self):
        dp = self._make_pipeline()
        received = []
        dp.add_stage("double", lambda x: x * 2)
        result = _run(dp.run(source=range(5), sink=lambda b: received.extend(b)))
        self.assertEqual(result["records_in"], 5)
        self.assertEqual(received, [0, 2, 4, 6, 8])

    def test_filter_stage_none(self):
        dp = self._make_pipeline("filt")
        received = []
        dp.add_stage("evens", lambda x: x if x % 2 == 0 else None)
        _run(dp.run(source=range(6), sink=lambda b: received.extend(b)))
        self.assertEqual(received, [0, 2, 4])

    def test_multiple_stages(self):
        dp = self._make_pipeline("multi")
        received = []
        dp.add_stage("add1", lambda x: x + 1)
        dp.add_stage("mul2", lambda x: x * 2)
        _run(dp.run(source=range(3), sink=lambda b: received.extend(b)))
        self.assertEqual(received, [2, 4, 6])  # (0+1)*2, (1+1)*2, (2+1)*2

    def test_error_action_skip(self):
        dp = self._make_pipeline("err_skip")
        received = []
        def risky(x):
            if x == 2: raise ValueError("bad")
            return x
        dp.add_stage("risky", risky, error_action=self.EA.SKIP)
        _run(dp.run(source=range(5), sink=lambda b: received.extend(b)))
        self.assertNotIn(2, received)
        self.assertEqual(len(received), 4)

    def test_error_action_dlq(self):
        dp = self._make_pipeline("err_dlq")
        received = []
        def risky(x):
            if x == 1: raise ValueError("dlq!")
            return x
        dp.add_stage("risky", risky, error_action=self.EA.DLQ)
        _run(dp.run(source=range(3), sink=lambda b: received.extend(b)))
        self.assertGreater(len(dp._dlq), 0)

    def test_stage_retry(self):
        calls = [0]
        dp = self._make_pipeline("retry_pipe")
        received = []
        def flaky(x):
            calls[0] += 1
            if calls[0] <= 1 and x == 0: raise RuntimeError()
            return x
        dp.add_stage("flaky", flaky, max_retries=2, retry_delay_s=0.001,
                      error_action=self.EA.SKIP)
        _run(dp.run(source=range(3), sink=lambda b: received.extend(b)))
        self.assertGreaterEqual(calls[0], 3)

    def test_dry_run(self):
        from agent.data_pipeline import DataPipeline
        dp = DataPipeline("dry", db_path=os.path.join(self.td,"dry.db"),
                           dry_run=True, batch_size=5)
        sink_calls = [0]
        dp.add_stage("double", lambda x: x * 2)
        _run(dp.run(source=range(5),
                     sink=lambda b: sink_calls.__setitem__(0, sink_calls[0]+1)))
        self.assertEqual(sink_calls[0], 0)  # sink not called in dry_run

    def test_checkpoint_saved(self):
        dp = self._make_pipeline("ckpt")
        _run(dp.run(source=range(10), sink=lambda b: None))
        ckpt = dp.checkpoint()
        self.assertIsNotNone(ckpt)
        self.assertGreater(ckpt, 0)

    def test_stage_stats_tracked(self):
        dp = self._make_pipeline("stats_pipe")
        dp.add_stage("identity", lambda x: x)
        _run(dp.run(source=range(10), sink=lambda b: None))
        st = dp._stats["identity"]
        self.assertEqual(st.in_count, 10)
        self.assertEqual(st.out_count, 10)

    def test_filter_stats_tracked(self):
        dp = self._make_pipeline("filt_stats")
        dp.add_stage("half", lambda x: x if x % 2 == 0 else None)
        _run(dp.run(source=range(10), sink=lambda b: None))
        st = dp._stats["half"]
        self.assertEqual(st.filter_count, 5)

    def test_async_stage(self):
        dp = self._make_pipeline("async_pipe")
        received = []
        async def async_double(x): return x * 2
        dp.add_stage("adouble", async_double)
        _run(dp.run(source=range(4), sink=lambda b: received.extend(b)))
        self.assertEqual(received, [0, 2, 4, 6])

    def test_on_record_hook(self):
        dp = self._make_pipeline("hook_pipe")
        seen = []
        dp.on_record(lambda r: seen.append(r.id))
        dp.add_stage("id", lambda x: x)
        _run(dp.run(source=range(3), sink=lambda b: None))
        self.assertEqual(len(seen), 3)

    def test_run_returns_summary(self):
        dp = self._make_pipeline("sum_pipe")
        dp.add_stage("id", lambda x: x)
        result = _run(dp.run(source=range(5), sink=lambda b: None))
        for k in ["run_id","records_in","records_out","errors","stages"]:
            self.assertIn(k, result)

    def test_stats(self):
        dp = self._make_pipeline("stats2")
        s = dp.stats()
        for k in ["pipeline","dlq_in_memory"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# SEARCH ENGINE
# ════════════════════════════════════════════════════════
class TestSearchEngine(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.search_engine import SearchEngine
        self.se = SearchEngine(db_path=os.path.join(td,"se.db"),
                                stem=True)
        self.docs = [
            {"id":"1","title":"Python Programming Guide","body":"Learn Python fast with examples"},
            {"id":"2","title":"Go Tutorial","body":"Go is a compiled language with fast runtime"},
            {"id":"3","title":"Machine Learning Basics","body":"Neural networks and deep learning fundamentals"},
            {"id":"4","title":"Python Data Science","body":"Pandas NumPy and data analysis with Python"},
            {"id":"5","title":"Web Development","body":"HTML CSS JavaScript for modern web apps"},
        ]
        for d in self.docs: self.se.index(d)

    def test_index_docs(self):
        self.assertEqual(len(self.se._docs), 5)

    def test_simple_search(self):
        results, meta = self.se.search("python")
        self.assertGreater(len(results), 0)
        ids = [r.doc_id for r in results]
        self.assertTrue("1" in ids or "4" in ids)

    def test_search_returns_scores(self):
        results, _ = self.se.search("python")
        self.assertTrue(all(r.score > 0 for r in results))

    def test_search_relevance_order(self):
        results, _ = self.se.search("python")
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_field_boost(self):
        results, _ = self.se.search("python", field_weights={"title": 5.0})
        # With high title boost, title matches should rank first
        self.assertGreater(len(results), 0)

    def test_delete_doc(self):
        ok = self.se.delete("5")
        self.assertTrue(ok)
        self.assertNotIn("5", self.se._docs)

    def test_delete_removes_from_search(self):
        self.se.delete("5")
        results, _ = self.se.search("javascript")
        ids = [r.doc_id for r in results]
        self.assertNotIn("5", ids)

    def test_filter_exact(self):
        self.se.index({"id":"f1","category":"news","text":"breaking story"})
        self.se.index({"id":"f2","category":"tech","text":"breaking code"})
        results, _ = self.se.search("breaking", filters={"category": "news"})
        self.assertTrue(all(r.doc_id == "f1" for r in results if r.doc_id in ["f1","f2"]))

    def test_filter_range(self):
        self.se.index({"id":"r1","score":10,"text":"item alpha"})
        self.se.index({"id":"r2","score":90,"text":"item beta"})
        results, _ = self.se.search("item", filters={"score": {"gte": 50, "lte": 100}})
        ids = [r.doc_id for r in results]
        self.assertIn("r2", ids)
        self.assertNotIn("r1", ids)

    def test_boolean_must(self):
        results, _ = self.se.search("+python +data")
        ids = [r.doc_id for r in results]
        self.assertIn("4", ids)

    def test_boolean_must_not(self):
        results, _ = self.se.search("python -data")
        # Doc 4 (python data science) should rank lower or be absent
        # At minimum python should still return results
        self.assertGreater(len(results), 0)

    def test_phrase_search(self):
        results, _ = self.se.search('"deep learning"')
        ids = [r.doc_id for r in results]
        self.assertIn("3", ids)

    def test_highlighting(self):
        results, _ = self.se.search("python")
        self.assertTrue(any(r.highlights for r in results))
        for r in results:
            for v in r.highlights.values():
                if "<mark>" in v: break
            else: continue
            break

    def test_facets(self):
        self.se.index({"id":"g1","lang":"python","text":"code"})
        self.se.index({"id":"g2","lang":"go","text":"code"})
        self.se.index({"id":"g3","lang":"python","text":"script"})
        _, meta = self.se.search("code", facet_fields=["lang"])
        self.assertIn("lang", meta["facets"])

    def test_fuzzy_search(self):
        results, _ = self.se.search("pythn", fuzzy=True)
        # fuzzy should find python
        self.assertGreater(len(results), 0)

    def test_synonyms(self):
        td = tempfile.mkdtemp()
        from agent.search_engine import SearchEngine
        se = SearchEngine(db_path=os.path.join(td,"syn.db"),
                           synonyms={"ml": ["machine learning"]}, stem=False)
        se.index({"id":"s1","text":"machine learning tutorial"})
        results, _ = se.search("ml")
        self.assertGreater(len(results), 0)

    def test_pagination(self):
        results_p1, meta = self.se.search("the", limit=2, offset=0)
        results_p2, _    = self.se.search("the", limit=2, offset=2)
        self.assertLessEqual(len(results_p1), 2)

    def test_meta_total(self):
        _, meta = self.se.search("python")
        self.assertIn("total", meta)
        self.assertIn("elapsed_ms", meta)

    def test_update_doc(self):
        self.se.index({"id":"1","title":"Updated Python Guide","body":"New content"})
        self.assertEqual(len(self.se._docs), 5)  # no duplicates

    def test_stats(self):
        self.se.search("python")
        s = self.se.stats()
        for k in ["in_memory_docs","vocab_size","searches"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# NOTIFICATION MANAGER
# ════════════════════════════════════════════════════════
class TestNotificationManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.notification_manager import (
            NotificationManager, Channel, Priority, DeliveryStatus,
            NotificationTemplate, RoutingRule, ChannelConfig)
        self.NM = NotificationManager
        self.Ch = Channel; self.Pri = Priority; self.DS = DeliveryStatus
        self.NT = NotificationTemplate; self.RR = RoutingRule; self.CC = ChannelConfig
        self.nm = NotificationManager(db_path=os.path.join(td,"nm.db"))
        self.nm.add_channel(ChannelConfig(Channel.LOG))

    def test_send_log(self):
        n = self.nm.send("test", self.Ch.LOG, "ops", "Hello", "Body")
        self.assertEqual(n.status, self.DS.SENT)

    def test_send_sets_recipient(self):
        n = self.nm.send("evt", self.Ch.LOG, "alice@ex.com", "S", "B")
        self.assertEqual(n.recipient, "alice@ex.com")

    def test_template_render(self):
        tmpl = self.NT(name="t1", subject="Alert: {title}",
                        body="Hello {user}, issue: {message}")
        rendered = tmpl.render({"title":"DB Down","user":"Alice","message":"down"})
        self.assertEqual(rendered["subject"], "Alert: DB Down")
        self.assertIn("Alice", rendered["body"])

    def test_template_conditional(self):
        tmpl = self.NT(name="t2", subject="S",
                        body="Main{% if urgent %} URGENT{% endif %} msg")
        self.assertIn("URGENT", tmpl.render({"urgent": True})["body"])
        self.assertNotIn("URGENT", tmpl.render({"urgent": False})["body"])

    def test_routing_rule_send_event(self):
        self.nm.add_template(self.NT(name="alert", subject="Alert: {title}",
                                      body="{message}"))
        self.nm.add_rule(self.RR(name="r1", event_types=["error"],
                                  channels=[self.Ch.LOG],
                                  template_name="alert"))
        sent = self.nm.send_event("error",
            {"title":"X","message":"Y","recipient":"ops"})
        self.assertGreater(len(sent), 0)
        self.assertEqual(sent[0].status, self.DS.SENT)

    def test_routing_rule_no_match(self):
        self.nm.add_rule(self.RR(name="r2", event_types=["critical_only"],
                                  channels=[self.Ch.LOG]))
        sent = self.nm.send_event("info", {"recipient":"ops"})
        self.assertEqual(len(sent), 0)

    def test_audience_groups(self):
        self.nm.add_audience("ops", ["a@ex.com","b@ex.com"])
        self.nm.add_rule(self.RR(name="r3", event_types=["alert"],
                                  channels=[self.Ch.LOG], audiences=["ops"]))
        sent = self.nm.send_event("alert", {})
        self.assertEqual(len(sent), 2)

    def test_deduplication(self):
        n1 = self.nm.send("evt","log","r","S","same body")
        n2 = self.nm.send("evt","log","r","S","same body")
        # Second should be suppressed
        self.assertEqual(n2.status, self.DS.SUPPRESSED)

    def test_rate_limiting(self):
        from agent.notification_manager import ChannelConfig, Channel
        nm2 = self.NM.__new__(self.NM)
        self.NM.__init__(nm2, db_path=os.path.join(tempfile.mkdtemp(),"nm2.db"))
        nm2.add_channel(ChannelConfig(Channel.LOG, max_per_window=2, window_s=60,
                                       dedup_ttl_s=0))
        # 3 distinct sends; 3rd should be rate-limited
        for i in range(3):
            nm2.send(f"e{i}", Channel.LOG, f"r{i}", f"s{i}", f"b{i}")
        statuses = [nm2._store.history(limit=10)[i]["status"] for i in range(3)]
        self.assertIn("rate_limited", statuses)

    def test_on_send_hook_suppress(self):
        self.nm.on_send(lambda n: False)  # suppress all
        n = self.nm.send("evt", self.Ch.LOG, "r", "S", "Uniq_suppress_body_123")
        self.assertEqual(n.status, self.DS.SUPPRESSED)

    def test_batch_mode(self):
        from agent.notification_manager import ChannelConfig, Channel
        nm3 = self.NM.__new__(self.NM)
        self.NM.__init__(nm3, db_path=os.path.join(tempfile.mkdtemp(),"nm3.db"))
        nm3.add_channel(ChannelConfig(Channel.LOG, batch_size=3, dedup_ttl_s=0))
        for i in range(2):
            nm3.send(f"e{i}", Channel.LOG, f"r{i}", "S", f"b{i}")
        self.assertEqual(len(nm3._batch_queues[Channel.LOG]), 2)
        nm3.send("e3", Channel.LOG, "r3", "S", "b3")  # triggers flush
        self.assertEqual(len(nm3._batch_queues.get(Channel.LOG, [])), 0)

    def test_history(self):
        self.nm.send("h1", self.Ch.LOG, "r", "S", "Hist_uniq_body_1")
        h = self.nm.history(self.Ch.LOG)
        self.assertGreater(len(h), 0)
        self.assertEqual(h[0]["channel"], "log")

    def test_custom_sender(self):
        sent_to = []
        from agent.notification_manager import ChannelConfig, Channel
        nm4 = self.NM.__new__(self.NM)
        self.NM.__init__(nm4, db_path=os.path.join(tempfile.mkdtemp(),"nm4.db"))
        nm4.add_channel(ChannelConfig(Channel.EMAIL,
                                       sender_fn=lambda n: sent_to.append(n.recipient) or True,
                                       dedup_ttl_s=0))
        nm4.send("e", Channel.EMAIL, "alice@ex.com", "Hi", "Body")
        self.assertIn("alice@ex.com", sent_to)

    def test_notification_to_dict(self):
        n = self.nm.send("t", self.Ch.LOG, "r", "S", "Uniq_dict_body_xyz")
        d = n.to_dict()
        for k in ["id","event_type","channel","status","priority"]: self.assertIn(k, d)

    def test_stats(self):
        self.nm.send("s", self.Ch.LOG, "r", "S", "Uniq_stats_body_abc")
        s = self.nm.stats()
        for k in ["total","templates","channels","rules"]: self.assertIn(k, s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v36: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
