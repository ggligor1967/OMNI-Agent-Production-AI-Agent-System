"""
OMNI AGENT v8 — Test Suite
Tests: Observability, Session, Search, PromptOptimizer
Run: python3 tests/test_v8_modules.py
"""
import asyncio, os, sys, tempfile, time, unittest, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ══════════════════════════════════════════════════════════════════════════════
# OBSERVABILITY — METRICS
# ══════════════════════════════════════════════════════════════════════════════

class TestCounter(unittest.TestCase):
    def setUp(self):
        from agent.observability import Counter
        self.c = Counter("test_counter", "A test counter", labels=["env", "status"])

    def test_increment(self):
        self.c.inc(env="prod", status="ok")
        self.assertEqual(self.c.get(env="prod", status="ok"), 1.0)

    def test_increment_amount(self):
        self.c.inc(5.0, env="prod", status="ok")
        self.assertEqual(self.c.get(env="prod", status="ok"), 5.0)

    def test_cumulative(self):
        self.c.inc(3, env="prod", status="ok")
        self.c.inc(2, env="prod", status="ok")
        self.assertEqual(self.c.get(env="prod", status="ok"), 5.0)

    def test_independent_labels(self):
        self.c.inc(env="prod", status="ok")
        self.c.inc(env="staging", status="error")
        self.assertEqual(self.c.get(env="prod", status="ok"), 1.0)
        self.assertEqual(self.c.get(env="staging", status="error"), 1.0)

    def test_zero_default(self):
        self.assertEqual(self.c.get(env="unknown", status="x"), 0.0)

    def test_samples(self):
        self.c.inc(env="prod", status="ok")
        samples = self.c.samples()
        self.assertGreater(len(samples), 0)

    def test_prometheus_format(self):
        self.c.inc(env="prod", status="ok")
        text = self.c.to_prometheus()
        self.assertIn("# HELP", text)
        self.assertIn("# TYPE", text)
        self.assertIn("test_counter", text)


class TestGauge(unittest.TestCase):
    def setUp(self):
        from agent.observability import Gauge
        self.g = Gauge("test_gauge", "A test gauge", labels=["worker"])

    def test_set(self):
        self.g.set(42.0, worker="w1")
        self.assertEqual(self.g.get(worker="w1"), 42.0)

    def test_inc_dec(self):
        self.g.set(10.0, worker="w2")
        self.g.inc(5.0, worker="w2")
        self.g.dec(3.0, worker="w2")
        self.assertEqual(self.g.get(worker="w2"), 12.0)

    def test_can_go_negative(self):
        self.g.set(0.0, worker="w3")
        self.g.dec(5.0, worker="w3")
        self.assertEqual(self.g.get(worker="w3"), -5.0)

    def test_prometheus_format(self):
        self.g.set(99.0, worker="w4")
        text = self.g.to_prometheus()
        self.assertIn("# TYPE test_gauge gauge", text)
        self.assertIn("99.0", text)


class TestHistogram(unittest.TestCase):
    def setUp(self):
        from agent.observability import Histogram
        self.h = Histogram("test_hist", "A test histogram",
                           buckets=(0.1, 0.5, 1.0, 5.0), labels=["model"])

    def test_observe(self):
        self.h.observe(0.3, model="gpt4")
        self.h.observe(0.7, model="gpt4")
        self.h.observe(2.0, model="gpt4")

    def test_bucket_counts(self):
        self.h.observe(0.05, model="m1")
        self.h.observe(0.3,  model="m1")
        self.h.observe(0.8,  model="m1")
        # 0.1 bucket: 1 (0.05 fits)
        self.assertEqual(self.h._data["model=\"m1\""  ][0.1], 1)
        # 0.5 bucket: 2 (0.05, 0.3 fit)
        self.assertEqual(self.h._data["model=\"m1\""  ][0.5], 2)
        # 1.0 bucket: 3 (all fit)
        self.assertEqual(self.h._data["model=\"m1\""  ][1.0], 3)

    def test_prometheus_format(self):
        self.h.observe(0.25, model="m2")
        text = self.h.to_prometheus()
        self.assertIn("_bucket", text)
        self.assertIn("_sum", text)
        self.assertIn("_count", text)

    def test_percentile(self):
        for v in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
            self.h.observe(v, model="m3")
        p50 = self.h.percentile(0.5, model="m3")
        self.assertGreater(p50, 0.0)


class TestSummary(unittest.TestCase):
    def setUp(self):
        from agent.observability import Summary
        self.s = Summary("test_summary", "A test summary", window=100)

    def test_observe_and_quantile(self):
        for v in range(1, 101):
            self.s.observe(float(v))
        p50 = self.s.quantile(0.5)
        self.assertAlmostEqual(p50, 50.0, delta=2.0)

    def test_empty_quantile(self):
        self.assertEqual(self.s.quantile(0.99), 0.0)

    def test_prometheus_format(self):
        self.s.observe(1.0); self.s.observe(2.0)
        text = self.s.to_prometheus()
        self.assertIn('quantile="0.5"', text)
        self.assertIn("_sum", text)


class TestMetricsRegistry(unittest.TestCase):
    def setUp(self):
        from agent.observability import MetricsRegistry
        self.m = MetricsRegistry()

    def test_prewired_metrics_exist(self):
        self.assertIsNotNone(self.m.requests_total)
        self.assertIsNotNone(self.m.latency_seconds)
        self.assertIsNotNone(self.m.tokens_total)
        self.assertIsNotNone(self.m.active_sessions)
        self.assertIsNotNone(self.m.errors_total)
        self.assertIsNotNone(self.m.cache_hits)

    def test_record_request(self):
        self.m.record_request("gpt4", "success", 0.42, tokens_in=10, tokens_out=50)
        self.assertEqual(self.m.requests_total.get(model="gpt4", status="success"), 1.0)
        self.assertEqual(self.m.tokens_total.get(model="gpt4", direction="in"), 10.0)
        self.assertEqual(self.m.tokens_total.get(model="gpt4", direction="out"), 50.0)

    def test_record_error(self):
        self.m.record_error("timeout", "gpt4")
        self.assertEqual(self.m.errors_total.get(type="timeout", model="gpt4"), 1.0)

    def test_record_cache_hit(self):
        self.m.record_cache(True, backend="redis")
        self.assertEqual(self.m.cache_hits.get(backend="redis"), 1.0)

    def test_record_cache_miss(self):
        self.m.record_cache(False, backend="memory")
        self.assertEqual(self.m.cache_misses.get(backend="memory"), 1.0)

    def test_custom_counter(self):
        c = self.m.counter("my_metric", "My custom metric")
        c.inc(3.0)
        self.assertEqual(c.get(), 3.0)

    def test_to_prometheus(self):
        self.m.requests_total.inc(model="x", status="ok")
        text = self.m.to_prometheus()
        self.assertIn("agent_requests_total", text)
        self.assertIn("# HELP", text)
        self.assertIn("# TYPE", text)

    def test_snapshot(self):
        self.m.active_sessions.set(5.0)
        snap = self.m.snapshot()
        self.assertIsInstance(snap, dict)

    def test_uptime_increases(self):
        self.m.uptime_seconds.set(time.time() - self.m._start_time)
        val = self.m.uptime_seconds.get()
        self.assertGreaterEqual(val, 0.0)


class TestHealthRegistry(unittest.TestCase):
    def setUp(self):
        from agent.observability import HealthRegistry
        self.health = HealthRegistry()

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_healthy_check(self):
        self.health.register("ok_check", lambda: (True, "All good"))
        result = self._run(self.health.run_check("ok_check"))
        from agent.observability import HealthStatus
        self.assertEqual(result.status, HealthStatus.HEALTHY)

    def test_unhealthy_check(self):
        self.health.register("bad_check", lambda: (False, "Down"))
        result = self._run(self.health.run_check("bad_check"))
        from agent.observability import HealthStatus
        self.assertEqual(result.status, HealthStatus.DEGRADED)

    def test_exception_marks_unhealthy(self):
        def boom(): raise RuntimeError("DB down")
        self.health.register("boom", boom, critical=True)
        result = self._run(self.health.run_check("boom"))
        from agent.observability import HealthStatus
        self.assertEqual(result.status, HealthStatus.UNHEALTHY)

    def test_timeout(self):
        async def slow(): await asyncio.sleep(99)
        self.health.register("slow", slow, timeout_s=0.1)
        result = self._run(self.health.run_check("slow"))
        from agent.observability import HealthStatus
        self.assertEqual(result.status, HealthStatus.UNHEALTHY)
        self.assertIn("Timed out", result.message)

    def test_run_all(self):
        self.health.register("a", lambda: True)
        self.health.register("b", lambda: (True, "ok"))
        results = self._run(self.health.run_all())
        self.assertIn("a", results)
        self.assertIn("b", results)

    def test_is_ready_no_critical_failures(self):
        from agent.observability import HealthStatus, HealthResult
        results = {
            "ok": HealthResult("ok", HealthStatus.HEALTHY, critical=False),
            "warn": HealthResult("warn", HealthStatus.DEGRADED, critical=False),
        }
        self.assertTrue(self.health.is_ready(results))

    def test_not_ready_on_critical_failure(self):
        from agent.observability import HealthStatus, HealthResult
        results = {
            "db": HealthResult("db", HealthStatus.UNHEALTHY, critical=True),
        }
        self.assertFalse(self.health.is_ready(results))

    def test_overall_status_healthy(self):
        from agent.observability import HealthStatus, HealthResult
        results = {"a": HealthResult("a", HealthStatus.HEALTHY)}
        self.assertEqual(self.health.overall_status(results), HealthStatus.HEALTHY)

    def test_overall_status_degraded(self):
        from agent.observability import HealthStatus, HealthResult
        results = {
            "a": HealthResult("a", HealthStatus.HEALTHY),
            "b": HealthResult("b", HealthStatus.DEGRADED),
        }
        self.assertEqual(self.health.overall_status(results), HealthStatus.DEGRADED)

    def test_overall_status_unhealthy_critical(self):
        from agent.observability import HealthStatus, HealthResult
        results = {
            "db": HealthResult("db", HealthStatus.UNHEALTHY, critical=True),
        }
        self.assertEqual(self.health.overall_status(results), HealthStatus.UNHEALTHY)


class TestAlertRules(unittest.TestCase):
    def setUp(self):
        from agent.observability import MetricsRegistry, AlertRule, AlertSeverity
        self.m = MetricsRegistry()
        self.AlertRule = AlertRule
        self.AlertSeverity = AlertSeverity

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_alert_fires_when_condition_met(self):
        self.m.errors_total.inc(200, type="timeout", model="gpt4")
        fired = []
        self.m.on_alert(lambda a: fired.append(a))
        self.m.add_alert(self.AlertRule(
            name="high_errors",
            condition=lambda m: m.errors_total.get(type="timeout", model="gpt4") > 100,
            message="Too many errors",
            severity=self.AlertSeverity.CRITICAL,
            cooldown_s=0,
        ))
        self._run(self.m.evaluate_alerts())
        self.assertGreater(len(fired), 0)
        self.assertEqual(fired[0].rule_name, "high_errors")

    def test_alert_resolves(self):
        counter_val = [200]
        self.m.add_alert(self.AlertRule(
            name="test_resolve",
            condition=lambda m: counter_val[0] > 100,
            message="High",
            cooldown_s=0,
        ))
        self._run(self.m.evaluate_alerts())
        active = self.m.active_alerts()
        self.assertEqual(len(active), 1)
        # Now resolve
        counter_val[0] = 0
        self._run(self.m.evaluate_alerts())
        active = self.m.active_alerts()
        self.assertEqual(len(active), 0)

    def test_cooldown_prevents_double_fire(self):
        fired = []
        self.m.on_alert(lambda a: fired.append(a))
        self.m.add_alert(self.AlertRule(
            name="cd_test",
            condition=lambda m: True,
            message="Always firing",
            cooldown_s=9999,
        ))
        self._run(self.m.evaluate_alerts())
        self._run(self.m.evaluate_alerts())
        self.assertEqual(len(fired), 1)

    def test_alert_history(self):
        self.m.add_alert(self.AlertRule(
            name="hist_test",
            condition=lambda m: True,
            message="Test",
            cooldown_s=0,
        ))
        self._run(self.m.evaluate_alerts())
        hist = self.m.alert_history()
        self.assertGreater(len(hist), 0)
        self.assertIn("rule", hist[0])

    def test_start_stop_background(self):
        async def _run():
            await self.m.start(alert_interval_s=0.05)
            await asyncio.sleep(0.1)
            await self.m.stop()
        asyncio.get_event_loop().run_until_complete(_run())


# ══════════════════════════════════════════════════════════════════════════════
# SESSION TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestMessage(unittest.TestCase):
    def test_to_dict(self):
        from agent.session import Message
        m = Message(role="user", content="Hello", model="gpt4", tokens=5)
        d = m.to_dict()
        self.assertEqual(d["role"], "user")
        self.assertEqual(d["content"], "Hello")
        self.assertEqual(d["tokens"], 5)

    def test_from_dict_roundtrip(self):
        from agent.session import Message
        m = Message(role="assistant", content="Hi there", tokens=10)
        m2 = Message.from_dict(m.to_dict())
        self.assertEqual(m2.role, m.role)
        self.assertEqual(m2.content, m.content)
        self.assertEqual(m2.tokens, m.tokens)


class TestSession(unittest.TestCase):
    def setUp(self):
        from agent.session import Session
        self.s = Session(id="sess_1", user_id="user_1", model="gpt4")

    def test_add_message(self):
        msg = self.s.add_message("user", "Hello!")
        self.assertEqual(len(self.s.messages), 1)
        self.assertEqual(msg.role, "user")

    def test_auto_title_from_first_user_message(self):
        self.s.add_message("user", "What is Python?")
        self.assertIn("Python", self.s.title)

    def test_touch_updates_timestamps(self):
        old_ts = self.s.last_active
        time.sleep(0.01)
        self.s.touch()
        self.assertGreater(self.s.last_active, old_ts)

    def test_message_count(self):
        self.s.add_message("user", "Hi")
        self.s.add_message("assistant", "Hello")
        self.assertEqual(self.s.message_count, 2)

    def test_total_tokens(self):
        self.s.add_message("user", "Hi", tokens=5)
        self.s.add_message("assistant", "Hello", tokens=10)
        self.assertEqual(self.s.total_tokens, 15)

    def test_is_expired_false(self):
        self.s.expires_at = time.time() + 9999
        self.assertFalse(self.s.is_expired)

    def test_is_expired_true(self):
        self.s.expires_at = time.time() - 1
        self.assertTrue(self.s.is_expired)

    def test_is_expired_no_expiry(self):
        self.s.expires_at = None
        self.assertFalse(self.s.is_expired)

    def test_to_llm_messages(self):
        self.s.add_message("user", "Hello")
        self.s.add_message("assistant", "Hi there")
        msgs = self.s.to_llm_messages()
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")

    def test_to_llm_messages_with_summary(self):
        self.s.summary = "Previous context summary"
        self.s.add_message("user", "New question")
        msgs = self.s.to_llm_messages()
        # First message should be the summary injection
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("summary", msgs[0]["content"].lower())

    def test_to_llm_messages_max_limit(self):
        for i in range(10):
            self.s.add_message("user" if i % 2 == 0 else "assistant", f"msg {i}")
        msgs = self.s.to_llm_messages(max_messages=4)
        self.assertLessEqual(len(msgs), 4)

    def test_to_dict_has_expected_keys(self):
        d = self.s.to_dict()
        for key in ["id", "user_id", "model", "message_count", "total_tokens"]:
            self.assertIn(key, d)

    def test_to_dict_exclude_messages(self):
        self.s.add_message("user", "Hi")
        d = self.s.to_dict(include_messages=False)
        self.assertNotIn("messages", d)


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.session import SessionStore, Session
        self.store = SessionStore(os.path.join(self.tmpdir, "sess.db"))
        self.Session = Session

    def _make_session(self, uid="user1", **kwargs):
        import uuid
        s = self.Session(id=str(uuid.uuid4())[:8], user_id=uid, **kwargs)
        s.created_at = s.updated_at = s.last_active = time.time()
        return s

    def test_save_and_get(self):
        s = self._make_session()
        self.store.save(s)
        got = self.store.get(s.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.user_id, "user1")

    def test_get_nonexistent(self):
        self.assertIsNone(self.store.get("nope"))

    def test_save_with_messages(self):
        from agent.session import Message
        s = self._make_session()
        s.messages = [Message("user", "Hello"), Message("assistant", "Hi")]
        self.store.save(s)
        got = self.store.get(s.id)
        self.assertEqual(len(got.messages), 2)

    def test_delete(self):
        s = self._make_session()
        self.store.save(s)
        ok = self.store.delete(s.id)
        self.assertTrue(ok)
        self.assertIsNone(self.store.get(s.id))

    def test_archive(self):
        s = self._make_session()
        self.store.save(s)
        self.store.archive(s.id)
        got = self.store.get(s.id)
        self.assertTrue(got.archived)

    def test_list_user_sessions(self):
        for _ in range(3):
            s = self._make_session("user_a")
            self.store.save(s)
        sessions = self.store.list_user_sessions("user_a")
        self.assertEqual(len(sessions), 3)

    def test_list_excludes_other_users(self):
        s = self._make_session("user_b")
        self.store.save(s)
        sessions = self.store.list_user_sessions("user_c")
        self.assertEqual(len(sessions), 0)

    def test_purge_expired(self):
        s = self._make_session()
        s.expires_at = time.time() - 1
        self.store.save(s)
        count = self.store.purge_expired()
        self.assertGreaterEqual(count, 1)
        self.assertIsNone(self.store.get(s.id))

    def test_search_by_tag(self):
        s = self._make_session(tags=["important", "work"])
        self.store.save(s)
        results = self.store.search(tag="important")
        self.assertGreater(len(results), 0)

    def test_stats(self):
        s = self._make_session(model="gpt4")
        self.store.save(s)
        stats = self.store.stats()
        self.assertIn("total_sessions", stats)
        self.assertGreaterEqual(stats["total_sessions"], 1)


class TestSessionManager(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.session import SessionManager
        self.sm = SessionManager(db_path=os.path.join(self.tmpdir, "sm.db"),
                                 default_ttl_s=86400)

    def test_create_session(self):
        s = self.sm.create("user1", model="gpt4", persona="engineer")
        self.assertIsNotNone(s)
        self.assertEqual(s.user_id, "user1")
        self.assertEqual(s.model, "gpt4")

    def test_get_session(self):
        s = self.sm.create("user1")
        got = self.sm.get(s.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.id, s.id)

    def test_get_nonexistent(self):
        self.assertIsNone(self.sm.get("bad_id"))

    def test_hot_cache(self):
        s = self.sm.create("user1")
        self.assertIn(s.id, self.sm._cache)

    def test_save_updates_cache(self):
        s = self.sm.create("user1")
        s.title = "Updated Title"
        self.sm.save(s)
        got = self.sm.get(s.id)
        self.assertEqual(got.title, "Updated Title")

    def test_delete_session(self):
        s = self.sm.create("user1")
        ok = self.sm.delete(s.id)
        self.assertTrue(ok)
        self.assertIsNone(self.sm.get(s.id))

    def test_add_message(self):
        s = self.sm.create("user1")
        msg = self.sm.add_message(s.id, "user", "Hello!")
        self.assertIsNotNone(msg)
        got = self.sm.get(s.id)
        self.assertEqual(len(got.messages), 1)

    def test_add_message_nonexistent_session(self):
        msg = self.sm.add_message("bad_id", "user", "Hello")
        self.assertIsNone(msg)

    def test_clone_session(self):
        s = self.sm.create("user1")
        s.add_message("user", "msg1")
        s.add_message("assistant", "reply1")
        s.add_message("user", "msg2")
        self.sm.save(s)
        fork = self.sm.clone(s.id, at_message=2)
        self.assertIsNotNone(fork)
        self.assertNotEqual(fork.id, s.id)
        self.assertEqual(len(fork.messages), 2)

    def test_clone_preserves_model(self):
        s = self.sm.create("user1", model="deepseek-v3")
        self.sm.save(s)
        fork = self.sm.clone(s.id)
        self.assertEqual(fork.model, "deepseek-v3")

    def test_clone_nonexistent(self):
        self.assertIsNone(self.sm.clone("bad_id"))

    def test_update_summary(self):
        s = self.sm.create("user1")
        self.sm.update_summary(s.id, "Discussed Python async patterns.")
        got = self.sm.get(s.id)
        self.assertIn("async", got.summary)

    def test_list_user_sessions(self):
        for _ in range(3):
            self.sm.create("user2")
        sessions = self.sm.list_user("user2")
        self.assertEqual(len(sessions), 3)

    def test_expire_check(self):
        s = self.sm.create("user1", ttl_s=0.01)
        sid = s.id
        time.sleep(0.02)
        got = self.sm.get(sid, check_expiry=True)
        self.assertIsNone(got)

    def test_stats(self):
        self.sm.create("user3", model="gpt4")
        stats = self.sm.stats("user3")
        self.assertGreaterEqual(stats["total_sessions"], 1)


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestSnippet(unittest.TestCase):
    def test_extract_snippet_with_match(self):
        from agent.search import _extract_snippet
        text = "The quick brown fox jumps over the lazy dog"
        snippet = _extract_snippet(text, ["fox"])
        self.assertIn("fox", snippet.lower())

    def test_highlights_term(self):
        from agent.search import _extract_snippet
        text = "Python is a great programming language for data science"
        snippet = _extract_snippet(text, ["Python"])
        self.assertIn("**Python**", snippet)

    def test_no_match_returns_start(self):
        from agent.search import _extract_snippet
        text = "Hello world this is some text"
        snippet = _extract_snippet(text, ["zzz_no_match"])
        self.assertTrue(len(snippet) > 0)

    def test_parse_query_terms(self):
        from agent.search import _parse_query_terms
        terms = _parse_query_terms("python AND async")
        self.assertIn("python", terms)
        self.assertIn("async", terms)
        self.assertNotIn("AND", terms)

    def test_parse_phrase_query(self):
        from agent.search import _parse_query_terms
        terms = _parse_query_terms('"async await" Python')
        self.assertIn("async await", terms)


class TestSearchDoc(unittest.TestCase):
    def test_to_dict(self):
        from agent.search import SearchDoc
        doc = SearchDoc(id="d1", corpus="memory", title="Test", body="Content here",
                       tags=["tag1"], author="user1")
        d = doc.to_dict()
        self.assertEqual(d["id"], "d1")
        self.assertEqual(d["corpus"], "memory")
        self.assertIn("tag1", d["tags"])

    def test_body_truncated_in_dict(self):
        from agent.search import SearchDoc
        doc = SearchDoc(id="d2", corpus="doc", title="T", body="x" * 1000)
        d = doc.to_dict()
        self.assertLessEqual(len(d["body"]), 500)


class TestSearchIndex(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.search import SearchIndex, SearchDoc
        self.idx = SearchIndex(os.path.join(self.tmpdir, "search.db"))
        self.SearchDoc = SearchDoc

    def _doc(self, doc_id, corpus, title, body, **kwargs):
        return self.SearchDoc(id=doc_id, corpus=corpus, title=title, body=body, **kwargs)

    def test_index_and_search(self):
        self.idx.index(self._doc("d1", "memory", "Python Tips",
                                  "Use list comprehensions for better performance"))
        resp = self.idx.search("comprehensions")
        self.assertGreater(resp.total, 0)
        self.assertEqual(resp.results[0].doc.id, "d1")

    def test_search_not_found(self):
        resp = self.idx.search("xyznonexistenttermzzz")
        self.assertEqual(resp.total, 0)
        self.assertEqual(len(resp.results), 0)

    def test_search_empty_query(self):
        resp = self.idx.search("")
        self.assertEqual(resp.total, 0)

    def test_search_by_corpus(self):
        self.idx.index(self._doc("d2", "memory",   "Mem doc",  "memory content"))
        self.idx.index(self._doc("d3", "document", "Doc file", "document content"))
        resp = self.idx.search("content", corpus="memory")
        ids = [r.doc.id for r in resp.results]
        self.assertIn("d2", ids)
        self.assertNotIn("d3", ids)

    def test_delete(self):
        self.idx.index(self._doc("d4", "memory", "Delete me", "some content to delete"))
        self.idx.index(self._doc("d5", "memory", "Keep me",   "other content to keep"))
        self.idx.delete("d4")
        resp = self.idx.search("delete me")
        ids = [r.doc.id for r in resp.results]
        self.assertNotIn("d4", ids)

    def test_update_doc(self):
        self.idx.index(self._doc("d6", "memory", "Old title", "old body content"))
        self.idx.index(self._doc("d6", "memory", "New title", "new body content updated"))
        resp = self.idx.search("updated")
        self.assertGreater(resp.total, 0)

    def test_snippet_in_result(self):
        self.idx.index(self._doc("d7", "memory", "Snippet test",
                                  "The quick brown fox jumps over the lazy dog"))
        resp = self.idx.search("fox", highlight=True)
        if resp.results:
            self.assertTrue(len(resp.results[0].snippet) > 0)

    def test_stats(self):
        self.idx.index(self._doc("d8", "memory", "Stats", "some content"))
        stats = self.idx.stats()
        self.assertIn("total_indexed", stats)
        self.assertGreaterEqual(stats["total_indexed"], 1)

    def test_suggest(self):
        self.idx.index(self._doc("d9", "doc", "Python tutorial", "learn python"))
        self.idx.index(self._doc("d10", "doc", "Python advanced", "advanced python"))
        suggestions = self.idx.suggest("Python")
        self.assertGreater(len(suggestions), 0)

    def test_batch_index(self):
        docs = [self._doc(f"b{i}", "memory", f"Batch doc {i}", f"content {i}")
                for i in range(5)]
        self.idx.index_batch(docs)
        resp = self.idx.search("Batch doc")
        self.assertGreater(resp.total, 0)

    def test_delete_corpus(self):
        self.idx.index(self._doc("c1", "temp_corpus", "Temp 1", "temp content one"))
        self.idx.index(self._doc("c2", "temp_corpus", "Temp 2", "temp content two"))
        count = self.idx.delete_corpus("temp_corpus")
        self.assertGreaterEqual(count, 2)

    def test_search_response_fields(self):
        self.idx.index(self._doc("s1", "memory", "Search resp", "test response fields"))
        resp = self.idx.search("response")
        self.assertIsInstance(resp.took_ms, float)
        self.assertIsInstance(resp.query, str)
        self.assertIsInstance(resp.facets, dict)


class TestSearchService(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.search import SearchService
        self.svc = SearchService(os.path.join(self.tmpdir, "svc.db"))

    def test_index_memory(self):
        self.svc.index_memory("mem1", "user1", "recursion tip", "Use memoization")
        resp = self.svc.query("memoization", corpus="memory")
        self.assertGreater(resp.total, 0)

    def test_index_session_message(self):
        self.svc.index_session_message("sess1", "user1",
                                        "How do I use async/await?", "user")
        resp = self.svc.query("async await")
        self.assertGreater(resp.total, 0)

    def test_index_document(self):
        self.svc.index_document("doc1", "user1", "API Reference",
                                 "The /chat endpoint accepts POST requests")
        resp = self.svc.query("endpoint POST", corpus="document")
        self.assertGreater(resp.total, 0)

    def test_index_kg_entity(self):
        self.svc.index_kg_entity("ent1", "Elon Musk", "PERSON",
                                  "Entrepreneur and CEO of Tesla and SpaceX")
        resp = self.svc.query("Tesla SpaceX", corpus="knowledge_graph")
        self.assertGreater(resp.total, 0)

    def test_query_with_user_filter(self):
        self.svc.index_document("d1", "user_a", "User A Doc", "content for user a")
        self.svc.index_document("d2", "user_b", "User B Doc", "content for user b")
        resp = self.svc.query("content", user_id="user_a")
        ids = [r.doc.source_id for r in resp.results]
        self.assertIn("d1", ids)
        self.assertNotIn("d2", ids)

    def test_stats(self):
        self.svc.index_memory("m1", "u1", "key", "value")
        stats = self.svc.stats()
        self.assertIn("total_indexed", stats)

    def test_suggest(self):
        self.svc.index_document("doc2", "u1", "Django REST Framework", "web framework")
        suggestions = self.svc.suggest("Django")
        self.assertGreater(len(suggestions), 0)


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT OPTIMIZER TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestPromptVariant(unittest.TestCase):
    def setUp(self):
        from agent.optimizer import PromptVariant
        self.PV = PromptVariant

    def test_render_with_variables(self):
        v = self.PV(id="v1", name="test",
                    text="Hello {user_name}, today is {date}.")
        rendered = v.render(user_name="Alice", date="Monday")
        self.assertEqual(rendered, "Hello Alice, today is Monday.")

    def test_render_missing_variable_unchanged(self):
        v = self.PV(id="v1", name="test", text="Hello {user_name}!")
        rendered = v.render()
        self.assertEqual(rendered, "Hello {user_name}!")

    def test_to_dict(self):
        v = self.PV(id="v1", name="baseline", text="You are helpful.",
                    tags=["prod"], author="alice")
        d = v.to_dict()
        self.assertEqual(d["name"], "baseline")
        self.assertIn("prod", d["tags"])


class TestPromptStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.optimizer import PromptStore, PromptVariant
        self.store = PromptStore(os.path.join(self.tmpdir, "prompts.db"))
        self.PV = PromptVariant

    def _make_variant(self, name="test", text="Hello"):
        return self.PV(id=f"v_{abs(hash(name+text)) % 100000:05d}",
                       name=name, text=text, created_at=time.time())

    def test_save_and_get(self):
        v = self._make_variant("baseline", "You are helpful.")
        self.store.save_variant(v)
        got = self.store.get_variant(v.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "baseline")

    def test_get_nonexistent(self):
        self.assertIsNone(self.store.get_variant("nope"))

    def test_list_variants(self):
        self.store.save_variant(self._make_variant("v1", "text1"))
        self.store.save_variant(self._make_variant("v2", "text2"))
        variants = self.store.list_variants()
        self.assertGreaterEqual(len(variants), 2)

    def test_list_by_tag(self):
        from agent.optimizer import PromptVariant
        v = PromptVariant(id="tagged1", name="tagged",
                          text="text", tags=["prod"], created_at=time.time())
        self.store.save_variant(v)
        results = self.store.list_variants(tag="prod")
        self.assertGreater(len(results), 0)

    def test_delete_variant(self):
        v = self._make_variant("todel", "delete me")
        self.store.save_variant(v)
        ok = self.store.delete_variant(v.id)
        self.assertTrue(ok)
        self.assertIsNone(self.store.get_variant(v.id))

    def test_active_prompt(self):
        v = self._make_variant("active_v", "Active prompt text")
        self.store.save_variant(v)
        self.store.set_active("chat", v.id, "alice")
        active_id = self.store.get_active("chat")
        self.assertEqual(active_id, v.id)

    def test_active_prompt_unknown_namespace(self):
        self.assertIsNone(self.store.get_active("nonexistent_ns"))

    def test_list_active(self):
        v = self._make_variant("la_v", "text")
        self.store.save_variant(v)
        self.store.set_active("ns1", v.id)
        self.store.set_active("ns2", v.id)
        active = self.store.list_active()
        self.assertIn("ns1", active)
        self.assertIn("ns2", active)


class TestPromptOptimizer(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.optimizer import PromptOptimizer
        self.opt = PromptOptimizer(db_path=os.path.join(self.tmpdir, "opt.db"))

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_create_variant(self):
        v = self.opt.create_variant("baseline", "You are a helpful assistant.")
        self.assertIsNotNone(v)
        self.assertEqual(v.name, "baseline")

    def test_get_variant(self):
        v = self.opt.create_variant("test_get", "Some prompt")
        got = self.opt.get_variant(v.id)
        self.assertIsNotNone(got)

    def test_list_variants(self):
        self.opt.create_variant("v1", "Prompt 1")
        self.opt.create_variant("v2", "Prompt 2")
        variants = self.opt.list_variants()
        self.assertGreaterEqual(len(variants), 2)

    def test_delete_variant(self):
        v = self.opt.create_variant("todel", "Delete me")
        ok = self.opt.delete_variant(v.id)
        self.assertTrue(ok)
        self.assertIsNone(self.opt.get_variant(v.id))

    def test_diff_variants(self):
        v1 = self.opt.create_variant("orig", "You are a helpful assistant.")
        v2 = self.opt.create_variant("improved", "You are a precise, helpful assistant.")
        diff = self.opt.diff(v1.id, v2.id)
        self.assertIsInstance(diff, str)
        # Either shows diff or says no differences
        self.assertTrue(len(diff) > 0)

    def test_diff_missing_variant(self):
        v1 = self.opt.create_variant("v1", "text")
        diff = self.opt.diff(v1.id, "nonexistent")
        self.assertIn("not found", diff)

    def test_run_experiment_no_evaluator(self):
        v1 = self.opt.create_variant("a", "Prompt A")
        v2 = self.opt.create_variant("b", "Prompt B")
        exp = self._run(self.opt.run_experiment(
            name="test_exp",
            suite_name="basic_capabilities",
            variant_ids=[v1.id, v2.id],
        ))
        from agent.optimizer import ExperimentStatus
        self.assertEqual(exp.status, ExperimentStatus.COMPLETED)
        self.assertGreater(len(exp.results), 0)
        self.assertIsNotNone(exp.winner_id)

    def test_winner_selected_correctly(self):
        v1 = self.opt.create_variant("low", "Low quality prompt")
        v2 = self.opt.create_variant("high", "High quality prompt")
        exp = self._run(self.opt.run_experiment(
            name="winner_test",
            suite_name="basic_capabilities",
            variant_ids=[v1.id, v2.id],
            winner_metric="avg_score",
        ))
        # Mock results give higher scores to later variants
        best = exp.best_result()
        self.assertIsNotNone(best)
        self.assertEqual(exp.winner_id, best.variant_id)

    def test_promote_winner(self):
        v1 = self.opt.create_variant("promo_a", "Prompt A")
        v2 = self.opt.create_variant("promo_b", "Prompt B")
        exp = self._run(self.opt.run_experiment(
            name="promo_test",
            suite_name="basic_capabilities",
            variant_ids=[v1.id, v2.id],
            auto_promote=False,
        ))
        promoted_id = self.opt.promote(exp.id, namespace="production")
        self.assertIsNotNone(promoted_id)
        self.assertEqual(promoted_id, exp.winner_id)

    def test_get_active_prompt(self):
        v = self.opt.create_variant("active", "Active prompt text!")
        self.opt.store.set_active("chat", v.id)
        prompt = self.opt.get_active_prompt("chat")
        self.assertEqual(prompt, "Active prompt text!")

    def test_get_active_prompt_with_variables(self):
        v = self.opt.create_variant("templ", "Hello {user}!")
        self.opt.store.set_active("greeting", v.id)
        prompt = self.opt.get_active_prompt("greeting", user="Alice")
        self.assertEqual(prompt, "Hello Alice!")

    def test_get_active_prompt_no_active(self):
        self.assertIsNone(self.opt.get_active_prompt("unknown_ns"))

    def test_rollback(self):
        v1 = self.opt.create_variant("old", "Old prompt")
        v2 = self.opt.create_variant("new", "New prompt")
        self.opt.store.set_active("ns", v2.id)
        ok = self.opt.rollback("ns", v1.id)
        self.assertTrue(ok)
        self.assertEqual(self.opt.get_active_prompt("ns"), "Old prompt")

    def test_rollback_nonexistent_variant(self):
        ok = self.opt.rollback("ns", "nonexistent_id")
        self.assertFalse(ok)

    def test_experiment_report(self):
        v1 = self.opt.create_variant("rpt_a", "Report variant A")
        v2 = self.opt.create_variant("rpt_b", "Report variant B")
        exp = self._run(self.opt.run_experiment(
            name="report_test",
            suite_name="basic_capabilities",
            variant_ids=[v1.id, v2.id],
        ))
        report = self.opt.experiment_report(exp.id)
        self.assertIn("# Prompt Experiment", report)
        self.assertIn("Pass Rate", report)
        self.assertIn("🏆", report)

    def test_experiment_report_missing(self):
        report = self.opt.experiment_report("nonexistent")
        self.assertIn("not found", report)

    def test_list_experiments(self):
        v = self.opt.create_variant("le_v", "text")
        self._run(self.opt.run_experiment(
            name="list_exp", suite_name="basic_capabilities", variant_ids=[v.id]
        ))
        exps = self.opt.list_experiments()
        self.assertGreater(len(exps), 0)

    def test_auto_promote(self):
        v1 = self.opt.create_variant("ap1", "Prompt 1")
        v2 = self.opt.create_variant("ap2", "Prompt 2")
        exp = self._run(self.opt.run_experiment(
            name="auto_promo",
            suite_name="basic_capabilities",
            variant_ids=[v1.id, v2.id],
            auto_promote=True,
            namespace="auto_ns",
        ))
        self.assertTrue(exp.promoted)
        prompt = self.opt.get_active_prompt("auto_ns")
        self.assertIsNotNone(prompt)

    def test_generate_variants_no_llm(self):
        variants = self._run(self.opt.generate_variants("Be helpful.", n=2))
        self.assertGreater(len(variants), 0)

    def test_variant_result_to_dict(self):
        from agent.optimizer import VariantResult
        vr = VariantResult(
            variant_id="v1", variant_name="test",
            pass_rate=0.8, avg_score=0.75, avg_latency_ms=350.0,
            cases_total=5, cases_passed=4,
        )
        d = vr.to_dict()
        self.assertAlmostEqual(d["pass_rate"], 0.8)
        self.assertEqual(d["cases_passed"], 4)


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun
    failed = len(result.failures) + len(result.errors)
    passed = total - failed
    print(f"\n{'='*60}")
    print(f"  v8 Test Results: {passed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  ✗ {t}")
            print(f"    {tb.strip().splitlines()[-1]}")
    else:
        print(f"  ✅ ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
