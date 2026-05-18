"""OMNI AGENT v62: NotificationManagerV2, RetryManagerV2, SearchEngineV2, TimeSeriesV2"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# NOTIFICATION MANAGER V2
# ════════════════════════════════════════════════════════
class TestNotificationManagerV2(unittest.TestCase):
    def setUp(self):
        from agent.notification_manager_v2 import (
            NotificationManagerV2, NotifChannel)
        self.nm = NotificationManagerV2(db_path=":memory:")
        self.sent = []
        self.nm.register_handler(NotifChannel.IN_APP,
                                  lambda n: self.sent.append(n) or True)
        self.nm.register_handler(NotifChannel.EMAIL,
                                  lambda n: self.sent.append(n) or True)

    def test_notify_in_app(self):
        from agent.notification_manager_v2 import NotifChannel, NotifStatus
        n = self.nm.notify("u1", channel=NotifChannel.IN_APP, body="Hello")
        self.assertEqual(n.status, NotifStatus.SENT)
        self.assertGreater(len(self.sent), 0)

    def test_notify_email(self):
        from agent.notification_manager_v2 import NotifChannel, NotifStatus
        n = self.nm.notify("u2", channel=NotifChannel.EMAIL,
                           subject="Test", body="Body")
        self.assertEqual(n.status, NotifStatus.SENT)

    def test_template_rendering(self):
        from agent.notification_manager_v2 import NotifChannel, NotifStatus
        t = self.nm.add_template("welcome", "Hello {name}!",
                                  subject_tpl="Welcome {name}",
                                  channel=NotifChannel.EMAIL)
        n = self.nm.notify("u3", channel=NotifChannel.EMAIL,
                           template_id=t.template_id,
                           template_vars={"name": "Alice"})
        self.assertEqual(n.status, NotifStatus.SENT)
        self.assertEqual(n.subject, "Welcome Alice")
        self.assertEqual(n.body, "Hello Alice!")

    def test_opt_out_cancels(self):
        from agent.notification_manager_v2 import NotifChannel, NotifStatus
        self.nm.opt_out("u4", NotifChannel.IN_APP)
        n = self.nm.notify("u4", channel=NotifChannel.IN_APP, body="X")
        self.assertEqual(n.status, NotifStatus.CANCELLED)

    def test_opt_in_restores(self):
        from agent.notification_manager_v2 import NotifChannel, NotifStatus
        self.nm.opt_out("u5", NotifChannel.IN_APP)
        self.nm.opt_in("u5", NotifChannel.IN_APP)
        n = self.nm.notify("u5", channel=NotifChannel.IN_APP, body="X")
        self.assertEqual(n.status, NotifStatus.SENT)

    def test_preferred_channel(self):
        from agent.notification_manager_v2 import NotifChannel
        self.nm.set_preference("u6", NotifChannel.EMAIL)
        n = self.nm.notify("u6", body="test")
        self.assertEqual(n.channel, NotifChannel.EMAIL)

    def test_no_handler_fails(self):
        from agent.notification_manager_v2 import NotifChannel, NotifStatus
        n = self.nm.notify("u7", channel=NotifChannel.SMS, body="sms")
        self.assertEqual(n.status, NotifStatus.FAILED)

    def test_retry_on_failure(self):
        from agent.notification_manager_v2 import NotifChannel, NotifStatus
        calls = [0]
        def flaky(n):
            calls[0] += 1
            if calls[0] < 2: raise RuntimeError("fail")
            return True
        self.nm.register_handler(NotifChannel.PUSH, flaky)
        n = self.nm.notify("u8", channel=NotifChannel.PUSH, body="push",
                           max_retries=2, retry_delay_s=0.0)
        self.assertEqual(n.status, NotifStatus.SENT)

    def test_batch_notify(self):
        from agent.notification_manager_v2 import NotifChannel
        notifs = self.nm.notify_batch(
            ["r1", "r2", "r3"], channel=NotifChannel.IN_APP, body="batch")
        self.assertEqual(len(notifs), 3)

    def test_cancel_pending(self):
        from agent.notification_manager_v2 import NotifChannel, NotifStatus
        n = self.nm.notify("u9", channel=NotifChannel.IN_APP,
                           body="cancel me",
                           scheduled_at=time.time() + 3600)
        self.nm.cancel(n.notif_id)
        self.assertEqual(n.status, NotifStatus.CANCELLED)

    def test_flush_scheduled(self):
        from agent.notification_manager_v2 import NotifChannel, NotifStatus
        # Create a notification that is scheduled for past but stored as PENDING
        n = self.nm.notify("u10", channel=NotifChannel.IN_APP,
                           body="scheduled",
                           scheduled_at=time.time() + 3600)
        # Manually set scheduled_at to past so flush picks it up
        n.scheduled_at = time.time() - 1
        n.status = NotifStatus.PENDING
        sent = self.nm.flush_scheduled()
        self.assertGreater(len(sent), 0)

    def test_list_for_recipient(self):
        from agent.notification_manager_v2 import NotifChannel
        self.nm.notify("recip1", channel=NotifChannel.IN_APP, body="a")
        result = self.nm.list_for_recipient("recip1")
        self.assertGreater(len(result), 0)

    def test_list_by_status(self):
        from agent.notification_manager_v2 import NotifChannel, NotifStatus
        self.nm.notify("ls_user", channel=NotifChannel.IN_APP, body="x")
        sent = self.nm.list_by_status(NotifStatus.SENT)
        self.assertGreater(len(sent), 0)

    def test_channel_stats(self):
        from agent.notification_manager_v2 import NotifChannel
        self.nm.notify("cs_user", channel=NotifChannel.IN_APP, body="x")
        cs = self.nm.channel_stats()
        self.assertIn("in_app", cs)

    def test_stats(self):
        from agent.notification_manager_v2 import NotifChannel
        self.nm.notify("st_user", channel=NotifChannel.IN_APP, body="x")
        s = self.nm.stats()
        self.assertGreater(s["sent"], 0)

# ════════════════════════════════════════════════════════
# RETRY MANAGER V2
# ════════════════════════════════════════════════════════
class TestRetryManagerV2(unittest.TestCase):
    def setUp(self):
        from agent.retry_manager_v2 import RetryManagerV2
        self.rm = RetryManagerV2(db_path=":memory:")

    def test_success_first_try(self):
        from agent.retry_manager_v2 import RetryOutcome
        p = self.rm.add_policy("p1", max_attempts=3,
                                base_delay_s=0.0, jitter=False)
        rec = self.rm.execute(lambda: 42, p.policy_id)
        self.assertEqual(rec.outcome, RetryOutcome.SUCCESS)
        self.assertEqual(rec.result, 42)
        self.assertEqual(rec.total_attempts, 1)

    def test_retry_on_failure(self):
        from agent.retry_manager_v2 import RetryOutcome
        calls = [0]
        def flaky():
            calls[0] += 1
            if calls[0] < 2: raise ValueError("err")
            return "ok"
        p = self.rm.add_policy("p2", max_attempts=3, base_delay_s=0.0, jitter=False)
        rec = self.rm.execute(flaky, p.policy_id)
        self.assertEqual(rec.outcome, RetryOutcome.SUCCESS)
        self.assertEqual(rec.total_attempts, 2)

    def test_exhausted(self):
        from agent.retry_manager_v2 import RetryOutcome
        p = self.rm.add_policy("p3", max_attempts=2, base_delay_s=0.0, jitter=False)
        rec = self.rm.execute(lambda: (_ for _ in ()).throw(RuntimeError("x")),
                              p.policy_id)
        self.assertEqual(rec.outcome, RetryOutcome.EXHAUSTED)

    def test_non_retryable_stops(self):
        from agent.retry_manager_v2 import RetryOutcome
        p = self.rm.add_policy("p4", max_attempts=5,
                                base_delay_s=0.0, jitter=False,
                                non_retryable_exceptions=[ValueError])
        rec = self.rm.execute(lambda: (_ for _ in ()).throw(ValueError("nope")),
                              p.policy_id)
        self.assertEqual(rec.outcome, RetryOutcome.FAILED)
        self.assertEqual(rec.total_attempts, 1)

    def test_retryable_only(self):
        from agent.retry_manager_v2 import RetryOutcome
        p = self.rm.add_policy("p5", max_attempts=3,
                                base_delay_s=0.0, jitter=False,
                                retryable_exceptions=[IOError])
        rec = self.rm.execute(lambda: (_ for _ in ()).throw(ValueError("bad")),
                              p.policy_id)
        self.assertEqual(rec.outcome, RetryOutcome.FAILED)

    def test_exponential_delays(self):
        from agent.retry_manager_v2 import BackoffStrategy
        p = self.rm.add_policy("p6", max_attempts=1,
                                strategy=BackoffStrategy.EXPONENTIAL,
                                base_delay_s=1.0, jitter=False)
        d1 = self.rm._compute_delay(p, 1)
        d2 = self.rm._compute_delay(p, 2)
        self.assertGreater(d2, d1)

    def test_linear_delays(self):
        from agent.retry_manager_v2 import BackoffStrategy
        p = self.rm.add_policy("p7", max_attempts=1,
                                strategy=BackoffStrategy.LINEAR,
                                base_delay_s=1.0, jitter=False)
        d1 = self.rm._compute_delay(p, 1)
        d2 = self.rm._compute_delay(p, 2)
        self.assertGreater(d2, d1)

    def test_fixed_delay(self):
        from agent.retry_manager_v2 import BackoffStrategy
        p = self.rm.add_policy("p8", max_attempts=1,
                                strategy=BackoffStrategy.FIXED,
                                base_delay_s=5.0, jitter=False)
        d = self.rm._compute_delay(p, 3)
        self.assertAlmostEqual(d, 5.0)

    def test_fibonacci_delays(self):
        from agent.retry_manager_v2 import BackoffStrategy
        p = self.rm.add_policy("p9", max_attempts=1,
                                strategy=BackoffStrategy.FIBONACCI,
                                base_delay_s=1.0, jitter=False)
        d1 = self.rm._compute_delay(p, 1)
        d3 = self.rm._compute_delay(p, 3)
        self.assertGreater(d3, d1)

    def test_custom_delay_fn(self):
        from agent.retry_manager_v2 import BackoffStrategy
        p = self.rm.add_policy("p10", max_attempts=1,
                                strategy=BackoffStrategy.CUSTOM,
                                jitter=False,
                                custom_delay_fn=lambda n: n * 7.0)
        d = self.rm._compute_delay(p, 3)
        self.assertAlmostEqual(d, 21.0)

    def test_retry_on_predicate(self):
        from agent.retry_manager_v2 import RetryOutcome
        p = self.rm.add_policy("p11", max_attempts=3, base_delay_s=0.0, jitter=False,
                                retry_on=lambda e: "retry" in str(e))
        calls = [0]
        def fn():
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("retry this")
            return "done"
        rec = self.rm.execute(fn, p.policy_id)
        self.assertEqual(rec.outcome, RetryOutcome.SUCCESS)

    def test_hooks_fired(self):
        pre_calls = []; post_calls = []
        self.rm.on_attempt(lambda a, op: pre_calls.append(a))
        self.rm.on_retry(lambda a, exc, d: post_calls.append(a))
        p = self.rm.add_policy("p12", max_attempts=2, base_delay_s=0.0, jitter=False)
        calls = [0]
        def fn():
            calls[0] += 1
            if calls[0] < 2: raise RuntimeError("err")
            return "ok"
        self.rm.execute(fn, p.policy_id)
        self.assertGreater(len(pre_calls), 0)

    def test_global_budget(self):
        from agent.retry_manager_v2 import RetryBudget, RetryOutcome
        budget = RetryBudget(tokens=1, refill_rate=0)
        rm = __import__('agent.retry_manager_v2', fromlist=['RetryManagerV2']).RetryManagerV2(
            db_path=":memory:", global_budget=budget)
        p = rm.add_policy("pb", max_attempts=5, base_delay_s=0.0, jitter=False)
        budget.tokens = 0  # drain budget
        rec = rm.execute(lambda: (_ for _ in ()).throw(RuntimeError("e")), p.policy_id)
        self.assertIn(rec.outcome.value, ("budgeted", "exhausted"))

    def test_history(self):
        p = self.rm.add_policy("ph", max_attempts=1, base_delay_s=0.0, jitter=False)
        self.rm.execute(lambda: 1, p.policy_id)
        h = self.rm.history()
        self.assertGreater(len(h), 0)

    def test_stats(self):
        p = self.rm.add_policy("ps", max_attempts=1, base_delay_s=0.0, jitter=False)
        self.rm.execute(lambda: 1, p.policy_id)
        s = self.rm.stats()
        self.assertGreater(s["success"], 0)

# ════════════════════════════════════════════════════════
# SEARCH ENGINE V2
# ════════════════════════════════════════════════════════
class TestSearchEngineV2(unittest.TestCase):
    def setUp(self):
        from agent.search_engine_v2 import SearchEngineV2
        self.se = SearchEngineV2(db_path=":memory:")
        self.se.index("Python Guide", "Python is a programming language")
        self.se.index("Java Reference", "Java is an object oriented language")
        self.se.index("ML Basics", "Machine learning models and neural networks")

    def test_keyword_search(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        req = SearchRequest(query="Python programming", mode=SearchMode.KEYWORD)
        hits = self.se.search(req)
        self.assertGreater(len(hits), 0)
        self.assertEqual(hits[0].title, "Python Guide")

    def test_vector_search(self):
        from agent.search_engine_v2 import SearchEngineV2, SearchRequest, SearchMode
        dim = 4
        se  = SearchEngineV2(
            embed_fn=lambda t: [hash(w) % 100 / 100 for w in t.split()[:dim]] +
                               [0.0] * max(0, dim - len(t.split())),
            db_path=":memory:")
        se.index("Doc A", "cats and dogs")
        se.index("Doc B", "cars and trucks")
        req = SearchRequest(query="cats dogs", mode=SearchMode.VECTOR)
        hits = se.search(req)
        self.assertGreater(len(hits), 0)

    def test_hybrid_search(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        req = SearchRequest(query="language", mode=SearchMode.HYBRID)
        hits = self.se.search(req)
        self.assertGreater(len(hits), 0)

    def test_no_results(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        req = SearchRequest(query="zzz_nonexistent_term_xyz",
                            mode=SearchMode.KEYWORD)
        hits = self.se.search(req)
        self.assertEqual(len(hits), 0)

    def test_field_filter_exact(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        self.se.index("Filtered", "filter test content",
                      fields={"lang": "fr"})
        req = SearchRequest(query="filter",
                            mode=SearchMode.KEYWORD,
                            filters={"lang": "fr"})
        hits = self.se.search(req)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "Filtered")

    def test_field_filter_range(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        self.se.index("Priced", "buy now", fields={"price": 50})
        self.se.index("Cheap", "deal", fields={"price": 10})
        req = SearchRequest(query="buy deal",
                            mode=SearchMode.KEYWORD,
                            filters={"price": {"gte": 40, "lte": 100}})
        hits = self.se.search(req)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "Priced")

    def test_tag_filter(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        self.se.index("Tagged AI", "ai content here", tags=["ai"])
        req = SearchRequest(query="ai content",
                            mode=SearchMode.KEYWORD,
                            tag_filter=["ai"])
        hits = self.se.search(req)
        self.assertTrue(all("ai" in h.tags for h in hits))

    def test_source_filter(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        self.se.index("Blog Post", "content here", source="blog")
        req = SearchRequest(query="content",
                            mode=SearchMode.KEYWORD,
                            source_filter="blog")
        hits = self.se.search(req)
        self.assertEqual(len(hits), 1)

    def test_boost_affects_score(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        self.se.index("Boosted", "common term shared", boost=5.0)
        self.se.index("Normal", "common term shared", boost=1.0)
        req = SearchRequest(query="common term", mode=SearchMode.KEYWORD)
        hits = self.se.search(req)
        boosted = next(h for h in hits if h.title == "Boosted")
        normal  = next(h for h in hits if h.title == "Normal")
        self.assertGreater(boosted.score, normal.score)

    def test_top_k_respected(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        req = SearchRequest(query="language", mode=SearchMode.KEYWORD, top_k=1)
        hits = self.se.search(req)
        self.assertEqual(len(hits), 1)

    def test_snippet_generated(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        req = SearchRequest(query="programming", mode=SearchMode.KEYWORD)
        hits = self.se.search(req)
        self.assertGreater(len(hits[0].snippet), 0)

    def test_highlights_generated(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        req = SearchRequest(query="programming", mode=SearchMode.KEYWORD)
        hits = self.se.search(req)
        self.assertGreater(len(hits[0].highlights), 0)

    def test_quick_search(self):
        hits = self.se.quick_search("machine learning")
        self.assertGreater(len(hits), 0)

    def test_delete_doc(self):
        from agent.search_engine_v2 import SearchRequest, SearchMode
        doc = self.se.index("Delete Me", "delete test content unique_xyz")
        self.se.delete(doc.doc_id)
        req = SearchRequest(query="unique_xyz", mode=SearchMode.KEYWORD)
        hits = self.se.search(req)
        self.assertEqual(len(hits), 0)

    def test_stats(self):
        s = self.se.stats()
        self.assertGreater(s["docs"], 0)

# ════════════════════════════════════════════════════════
# TIME SERIES V2
# ════════════════════════════════════════════════════════
class TestTimeSeriesV2(unittest.TestCase):
    def setUp(self):
        from agent.time_series_v2 import TimeSeriesV2
        self.ts = TimeSeriesV2(db_path=":memory:")
        self.s  = self.ts.create_series("cpu", unit="%")

    def test_create_series(self):
        self.assertEqual(self.s.name, "cpu")

    def test_append_and_query(self):
        self.ts.append(self.s.series_id, 42.0, ts=1000.0)
        pts = self.ts.query(self.s.series_id)
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0].value, 42.0)

    def test_batch_append(self):
        n = self.ts.append_batch(self.s.series_id,
                                  [(1000 + i, float(i)) for i in range(10)])
        self.assertEqual(n, 10)

    def test_latest(self):
        self.ts.append(self.s.series_id, 1.0, ts=1.0)
        self.ts.append(self.s.series_id, 99.0, ts=2.0)
        pts = self.ts.latest(self.s.series_id, n=1)
        self.assertEqual(pts[0].value, 99.0)

    def test_range_query(self):
        for i in range(10):
            self.ts.append(self.s.series_id, float(i), ts=float(i))
        pts = self.ts.query(self.s.series_id, from_ts=3.0, to_ts=6.0)
        self.assertEqual(len(pts), 4)

    def test_aggregate_avg(self):
        from agent.time_series_v2 import AggFunc
        for i in range(10):
            self.ts.append(self.s.series_id, float(i), ts=float(i))
        windows = self.ts.aggregate(self.s.series_id, window_s=5.0,
                                     func=AggFunc.AVG)
        self.assertGreater(len(windows), 0)

    def test_aggregate_sum(self):
        from agent.time_series_v2 import AggFunc
        self.ts.append(self.s.series_id, 10.0, ts=1.0)
        self.ts.append(self.s.series_id, 20.0, ts=2.0)
        windows = self.ts.aggregate(self.s.series_id, window_s=5.0,
                                     func=AggFunc.SUM)
        self.assertAlmostEqual(windows[0].value, 30.0)

    def test_aggregate_percentile(self):
        from agent.time_series_v2 import AggFunc
        for v in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
            self.ts.append(self.s.series_id, float(v), ts=float(v))
        windows = self.ts.aggregate(self.s.series_id, window_s=200.0,
                                     func=AggFunc.P95)
        self.assertGreater(windows[0].value, 80.0)

    def test_downsample(self):
        for i in range(100):
            self.ts.append(self.s.series_id, float(i), ts=float(i + 1))
        buckets = self.ts.downsample(self.s.series_id, n_buckets=10)
        self.assertGreaterEqual(len(buckets), 5)  # flexible: span/n_buckets

    def test_rolling(self):
        from agent.time_series_v2 import AggFunc
        for i in range(10):
            self.ts.append(self.s.series_id, float(i), ts=float(i))
        result = self.ts.rolling(self.s.series_id, window=3,
                                  func=AggFunc.AVG)
        self.assertEqual(len(result), 8)

    def test_rate_of_change(self):
        for i in range(5):
            self.ts.append(self.s.series_id, float(i * 2), ts=float(i))
        roc = self.ts.rate_of_change(self.s.series_id)
        self.assertEqual(len(roc), 4)
        self.assertAlmostEqual(roc[0][1], 2.0)

    def test_anomaly_zscore(self):
        from agent.time_series_v2 import AnomalyMethod
        for v in [10, 11, 10, 12, 10, 100, 11, 10]:
            self.ts.append(self.s.series_id, float(v))
        anomalies = self.ts.detect_anomalies(
            self.s.series_id, method=AnomalyMethod.ZSCORE, threshold=2.0)
        self.assertGreater(len(anomalies), 0)
        # 100 should be detected
        self.assertTrue(any(a.value == 100.0 for a in anomalies))

    def test_anomaly_iqr(self):
        from agent.time_series_v2 import AnomalyMethod
        for v in [10, 11, 10, 12, 10, 100, 11, 10]:
            self.ts.append(self.s.series_id, float(v))
        anomalies = self.ts.detect_anomalies(
            self.s.series_id, method=AnomalyMethod.IQR, threshold=1.5)
        self.assertGreater(len(anomalies), 0)

    def test_anomaly_mad(self):
        from agent.time_series_v2 import AnomalyMethod
        for v in [10, 11, 10, 12, 10, 200, 11, 10]:
            self.ts.append(self.s.series_id, float(v))
        anomalies = self.ts.detect_anomalies(
            self.s.series_id, method=AnomalyMethod.MAD, threshold=3.0)
        self.assertGreater(len(anomalies), 0)

    def test_forecast_linear(self):
        for i in range(10):
            self.ts.append(self.s.series_id, float(i * 10), ts=float(i + 1))
        forecast = self.ts.forecast_linear(self.s.series_id, steps=3, step_s=1.0)
        self.assertEqual(len(forecast), 3)
        # Linear trend should be increasing (slope ~10)
        self.assertGreater(forecast[2][1], forecast[0][1])

    def test_forecast_ewm(self):
        for i in range(10):
            self.ts.append(self.s.series_id, float(i), ts=float(i))
        forecast = self.ts.forecast_ewm(self.s.series_id, steps=3)
        self.assertEqual(len(forecast), 3)

    def test_retention_evicts(self):
        s = self.ts.create_series("retained", retention_s=0.01)
        self.ts.append(s.series_id, 1.0, ts=time.time() - 1)
        self.ts._apply_retention(s.series_id)
        pts = self.ts.query(s.series_id)
        self.assertEqual(len(pts), 0)

    def test_delete_series(self):
        self.ts.delete_series(self.s.series_id)
        self.assertIsNone(self.ts.get_series(self.s.series_id))

    def test_stats(self):
        self.ts.append(self.s.series_id, 1.0)
        s = self.ts.stats()
        self.assertGreater(s["total_points"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v62: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
