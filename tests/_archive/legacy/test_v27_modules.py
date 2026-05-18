"""OMNI AGENT v27: LLMRouter, DocumentStore, SessionManager, EventBus"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# LLM ROUTER
# ════════════════════════════════════════════════════════
class TestLLMRouter(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.llm_router import LLMRouter, RouteRequest, RouteResponse
        self.LLMRouter = LLMRouter
        self.RouteRequest = RouteRequest
        self.RouteResponse = RouteResponse
        self.router = LLMRouter(db_path=os.path.join(td,"llm.db"), dry_run=True)

    def _reg(self, name, **kw):
        return self.router.register(name, model=f"{name}-model", **kw)

    def test_register_provider(self):
        spec = self._reg("openai")
        self.assertEqual(spec.name, "openai")

    def test_route_dry_run(self):
        self._reg("prov1")
        req = self.RouteRequest(messages=[{"role":"user","content":"hi"}])
        resp = _run(self.router.route(req))
        self.assertIn("dry-run", resp.content)

    def test_route_dry_run_returns_response(self):
        self._reg("prov2")
        req = self.RouteRequest(messages=[{"role":"user","content":"hello"}])
        resp = _run(self.router.route(req))
        self.assertIsNotNone(resp.provider)

    def test_round_robin_strategy(self):
        self.router.set_strategy("round_robin")
        self._reg("rr1"); self._reg("rr2"); self._reg("rr3")
        req = self.RouteRequest(messages=[{"role":"user","content":"x"}])
        providers = {_run(self.router.route(req)).provider for _ in range(6)}
        # Should visit multiple providers
        self.assertGreater(len(providers), 1)

    def test_least_latency_strategy(self):
        self.router.set_strategy("least_latency")
        self._reg("fast", priority=1); self._reg("slow", priority=2)
        self.router._providers["fast"].latency_p50 = 50.0
        self.router._providers["slow"].latency_p50 = 500.0
        selected = self.router._select_provider(
            self.RouteRequest(messages=[]))
        self.assertEqual(selected.name, "fast")

    def test_cost_optimized_strategy(self):
        self.router.set_strategy("cost_optimized")
        self._reg("cheap", input_cost_per_1m=0.5, output_cost_per_1m=1.0)
        self._reg("expensive", input_cost_per_1m=10.0, output_cost_per_1m=30.0)
        selected = self.router._select_provider(
            self.RouteRequest(messages=[]))
        self.assertEqual(selected.name, "cheap")

    def test_fallback_chain(self):
        self._reg("primary"); self._reg("backup")
        self.router.set_fallback(["primary","backup"])
        self.assertEqual(self.router._fallback_chain, ["primary","backup"])

    def test_provider_health_check(self):
        spec = self._reg("health_prov")
        self.assertTrue(spec.healthy)
        for _ in range(5):
            spec.record_request(0, 0, 100, True)
        self.assertFalse(spec.healthy)

    def test_reset_provider(self):
        spec = self._reg("reset_prov")
        for _ in range(5): spec.record_request(0, 0, 100, True)
        self.router.reset_provider("reset_prov")
        self.assertTrue(spec.healthy)

    def test_rate_limit_check(self):
        spec = self._reg("rl_prov", rpm_limit=2)
        self.assertTrue(spec.within_rate_limits())
        spec._rpm_window = [time.time(), time.time()]
        self.assertFalse(spec.within_rate_limits())

    def test_cache_hit(self):
        td = tempfile.mkdtemp()
        from agent.llm_router import LLMRouter, RouteRequest
        router = LLMRouter(db_path=os.path.join(td,"c.db"), dry_run=True)
        router.register("cp")
        msgs = [{"role":"user","content":"cached?"}]
        req = RouteRequest(messages=msgs, cache_ttl=3600)
        r1 = _run(router.route(req)); r2 = _run(router.route(req))
        self.assertTrue(r2.cached)

    def test_cost_tracking(self):
        spec = self._reg("cost_prov",
                          input_cost_per_1m=1.0, output_cost_per_1m=2.0)
        spec.record_request(1000, 500, 50.0, False)
        expected = (1000*1.0 + 500*2.0) / 1_000_000
        self.assertAlmostEqual(spec.total_cost_usd, expected, places=8)

    def test_session_cost(self):
        self._reg("sess_prov")
        req = self.RouteRequest(messages=[{"role":"user","content":"x"}],
                                 session_id="sess1")
        _run(self.router.route(req))
        # cost may be 0 in dry-run but session tracking exists
        cost = self.router.session_cost("sess1")
        self.assertIsInstance(cost, float)

    def test_ewma_latency(self):
        from agent.llm_router import _ewma
        val = _ewma(200.0, 100.0, alpha=0.5)
        self.assertAlmostEqual(val, 150.0)

    def test_providers_list(self):
        self._reg("lp1"); self._reg("lp2")
        ps = self.router.providers()
        self.assertGreaterEqual(len(ps), 2)

    def test_providers_healthy_only(self):
        spec = self._reg("unhealthy_p")
        spec.healthy = False
        ps = self.router.providers(healthy_only=True)
        self.assertNotIn("unhealthy_p", [p.name for p in ps])

    def test_cost_report(self):
        self._reg("cr_prov")
        report = self.router.cost_report()
        for k in ["total_cost","input_tokens","requests"]: self.assertIn(k, report)

    def test_stats(self):
        s = self.router.stats()
        for k in ["providers","healthy","strategy"]: self.assertIn(k, s)

    def test_provider_to_dict(self):
        spec = self._reg("dict_prov")
        d = spec.to_dict()
        for k in ["id","name","model","healthy","latency_p50","error_rate"]:
            self.assertIn(k, d)

    def test_response_to_dict(self):
        self._reg("resp_prov")
        req = self.RouteRequest(messages=[{"role":"user","content":"x"}])
        resp = _run(self.router.route(req))
        d = resp.to_dict()
        for k in ["provider","content","latency_ms","cost_usd","cached"]:
            self.assertIn(k, d)

    def test_no_providers_raises(self):
        td = tempfile.mkdtemp()
        from agent.llm_router import LLMRouter, RouteRequest
        router = LLMRouter(db_path=os.path.join(td,"empty.db"), dry_run=False)
        req = RouteRequest(messages=[])
        with self.assertRaises(RuntimeError):
            _run(router.route(req))

# ════════════════════════════════════════════════════════
# DOCUMENT STORE
# ════════════════════════════════════════════════════════
class TestDocumentStore(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.document_store import DocumentStore
        self.store = DocumentStore(db_path=os.path.join(td,"docs.db"),
                                    chunk_size=50, chunk_overlap=5)

    def test_ingest_returns_id(self):
        doc_id = self.store.ingest("Test", "Hello world, this is a test document.")
        self.assertIsNotNone(doc_id)

    def test_get_document(self):
        doc_id = self.store.ingest("Title", "Some content here.")
        doc = self.store.get(doc_id)
        self.assertEqual(doc.title, "Title")

    def test_ingest_creates_chunks(self):
        doc_id = self.store.ingest("Chunked", "word " * 200)
        doc = self.store.get(doc_id)
        self.assertGreater(len(doc.chunk_ids), 1)

    def test_search_returns_results(self):
        self.store.ingest("Python Guide", "Python is a high-level programming language.",
                           metadata={"topic": "python"})
        self.store.ingest("Java Guide", "Java is a compiled object-oriented language.",
                           metadata={"topic": "java"})
        results = self.store.search("programming language")
        self.assertGreater(len(results), 0)

    def test_search_top_result_relevant(self):
        self.store.ingest("A", "asyncio enables concurrent code using coroutines")
        self.store.ingest("B", "pandas is a data manipulation library")
        results = self.store.search("async concurrent coroutines", top_k=2)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].doc.title, "A")

    def test_search_metadata_filter(self):
        self.store.ingest("Py", "Python syntax guide", metadata={"lang":"python"})
        self.store.ingest("JS", "JavaScript syntax guide", metadata={"lang":"js"})
        results = self.store.search("syntax guide", filter={"lang":"python"})
        self.assertTrue(all(r.doc.metadata["lang"]=="python" for r in results))

    def test_dedup(self):
        text = "Unique document content for dedup test."
        id1 = self.store.ingest("D1", text)
        id2 = self.store.ingest("D2", text)
        self.assertEqual(id1, id2)

    def test_dedup_skip(self):
        text = "Another dedup test content."
        id1 = self.store.ingest("D1", text, dedup=True)
        id2 = self.store.ingest("D2", text, dedup=False)
        self.assertNotEqual(id1, id2)

    def test_delete(self):
        doc_id = self.store.ingest("Del", "content to delete")
        ok = self.store.delete(doc_id)
        self.assertTrue(ok)
        self.assertIsNone(self.store.get(doc_id))

    def test_delete_removes_from_bm25(self):
        doc_id = self.store.ingest("BM25 Del", "unique xyz12345 token")
        self.store.delete(doc_id)
        results = self.store.search("unique xyz12345 token")
        ids = [r.doc.id for r in results]
        self.assertNotIn(doc_id, ids)

    def test_search_result_has_snippet(self):
        self.store.ingest("S", "The quick brown fox jumps over the lazy dog")
        results = self.store.search("quick brown fox")
        if results:
            self.assertIsInstance(results[0].snippet, str)

    def test_search_result_has_rank(self):
        self.store.ingest("R1", "machine learning neural network deep")
        self.store.ingest("R2", "statistics regression linear models")
        results = self.store.search("machine learning", top_k=2)
        if len(results) >= 2:
            self.assertEqual(results[0].rank, 1)
            self.assertEqual(results[1].rank, 2)

    def test_facets(self):
        self.store.ingest("F1", "content a", metadata={"cat":"A"})
        self.store.ingest("F2", "content b", metadata={"cat":"B"})
        self.store.ingest("F3", "content c", metadata={"cat":"A"})
        facets = self.store.facets("cat")
        self.assertEqual(facets.get("A"), 2)
        self.assertEqual(facets.get("B"), 1)

    def test_sentence_chunking(self):
        td = tempfile.mkdtemp()
        from agent.document_store import DocumentStore
        ds = DocumentStore(db_path=os.path.join(td,"sc.db"),
                            chunk_strategy="sentence")
        text = "First sentence. Second sentence! Third sentence? Fourth one."
        doc_id = ds.ingest("SC", text)
        doc = ds.get(doc_id)
        self.assertGreater(len(doc.chunk_ids), 0)

    def test_bm25_score_positive(self):
        from agent.document_store import _BM25
        bm = _BM25()
        bm.add("c1", "the quick brown fox")
        bm.add("c2", "the lazy dog sleeps")
        s = bm.score("quick fox", "c1")
        self.assertGreater(s, 0)

    def test_bm25_search_returns_sorted(self):
        from agent.document_store import _BM25
        bm = _BM25()
        bm.add("c1", "python asyncio coroutines await")
        bm.add("c2", "java spring hibernate sql")
        bm.add("c3", "python django web framework")
        results = bm.search("python", top_k=3)
        scores = [s for _, s in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_stats(self):
        self.store.ingest("St", "some text for stats")
        s = self.store.stats()
        for k in ["documents","chunks","index_chunks"]: self.assertIn(k, s)

    def test_doc_to_dict(self):
        doc_id = self.store.ingest("D", "text content")
        doc = self.store.get(doc_id)
        d = doc.to_dict()
        for k in ["id","title","chunk_count","version"]: self.assertIn(k, d)

    def test_result_to_dict(self):
        self.store.ingest("SR", "some searchable content here")
        results = self.store.search("searchable content")
        if results:
            d = results[0].to_dict()
            for k in ["doc_id","title","score","snippet"]: self.assertIn(k, d)

    def test_export(self):
        self.store.ingest("E1", "text"); self.store.ingest("E2", "more text")
        exported = self.store.export()
        self.assertGreaterEqual(len(exported), 2)

# ════════════════════════════════════════════════════════
# SESSION MANAGER
# ════════════════════════════════════════════════════════
class TestSessionManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.session_manager import SessionManager
        self.sm = SessionManager(db_path=os.path.join(td,"sess.db"))

    def test_create_session(self):
        sid = _run(self.sm.create(user_id="alice"))
        self.assertIsNotNone(sid)

    def test_get_session_info(self):
        sid = _run(self.sm.create(user_id="bob"))
        info = self.sm.session_info(sid)
        self.assertEqual(info["user_id"], "bob")

    def test_set_and_get(self):
        sid = _run(self.sm.create())
        _run(self.sm.set(sid, "key1", "value1"))
        val = _run(self.sm.get(sid, "key1"))
        self.assertEqual(val, "value1")

    def test_get_full_namespace(self):
        sid = _run(self.sm.create())
        _run(self.sm.set(sid, "a", 1)); _run(self.sm.set(sid, "b", 2))
        ns = _run(self.sm.get(sid))
        self.assertEqual(ns.get("a"), 1); self.assertEqual(ns.get("b"), 2)

    def test_delete_key(self):
        sid = _run(self.sm.create())
        _run(self.sm.set(sid, "k", "v"))
        ok = _run(self.sm.delete_key(sid, "k"))
        self.assertTrue(ok)
        self.assertIsNone(_run(self.sm.get(sid, "k")))

    def test_consume_tokens(self):
        sid = _run(self.sm.create(token_budget=1000))
        result = _run(self.sm.consume_tokens(sid, 300))
        self.assertTrue(result["ok"])
        self.assertEqual(result["remaining"], 700)

    def test_token_budget_exceeded(self):
        sid = _run(self.sm.create(token_budget=100))
        result = _run(self.sm.consume_tokens(sid, 200))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "budget_exceeded")

    def test_soft_warn_threshold(self):
        sid = _run(self.sm.create(token_budget=1000))
        result = _run(self.sm.consume_tokens(sid, 850))
        self.assertTrue(result["warn"])

    def test_tokens_remaining(self):
        sid = _run(self.sm.create(token_budget=500))
        _run(self.sm.consume_tokens(sid, 200))
        rem = _run(self.sm.tokens_remaining(sid))
        self.assertEqual(rem, 300)

    def test_expire_session(self):
        from agent.session_manager import SessionState
        sid = _run(self.sm.create())
        ok = _run(self.sm.expire(sid, "test"))
        self.assertTrue(ok)
        info = self.sm.session_info(sid)
        self.assertEqual(info["state"], SessionState.EXPIRED)

    def test_resume_session(self):
        from agent.session_manager import SessionState
        sid = _run(self.sm.create())
        _run(self.sm.expire(sid))
        ok = _run(self.sm.resume(sid))
        self.assertTrue(ok)
        info = self.sm.session_info(sid)
        self.assertEqual(info["state"], SessionState.ACTIVE)

    def test_expiry_hook(self):
        expired = []
        self.sm.add_expiry_hook(lambda s, r: expired.append(s.id))
        sid = _run(self.sm.create())
        _run(self.sm.expire(sid))
        self.assertIn(sid, expired)

    def test_idle_expiry(self):
        sid = _run(self.sm.create(ttl_idle_s=0.01))
        time.sleep(0.05)
        n = _run(self.sm.expire_idle())
        self.assertGreater(n, 0)

    def test_tag_session(self):
        sid = _run(self.sm.create())
        ok = _run(self.sm.tag(sid, "env", "prod"))
        self.assertTrue(ok)
        info = self.sm.session_info(sid)
        self.assertEqual(info["tags"]["env"], "prod")

    def test_list_active(self):
        sid1 = _run(self.sm.create(user_id="u1"))
        sid2 = _run(self.sm.create(user_id="u1"))
        active = self.sm.list_active(user_id="u1")
        ids = [s.id for s in active]
        self.assertIn(sid1, ids); self.assertIn(sid2, ids)

    def test_delete_session(self):
        sid = _run(self.sm.create())
        ok = _run(self.sm.delete(sid))
        self.assertTrue(ok)
        self.assertIsNone(self.sm.session_info(sid))

    def test_event_log(self):
        sid = _run(self.sm.create())
        _run(self.sm.set(sid, "x", 1))
        log = self.sm.event_log(sid)
        types = [e["event"] for e in log]
        self.assertIn("created", types)

    def test_session_info_with_namespace(self):
        sid = _run(self.sm.create())
        _run(self.sm.set(sid, "foo", "bar"))
        info = self.sm.session_info(sid, include_namespace=True)
        self.assertEqual(info["namespace"]["foo"], "bar")

    def test_persistence(self):
        td = tempfile.mkdtemp()
        from agent.session_manager import SessionManager
        db = os.path.join(td, "p.db")
        sm1 = SessionManager(db_path=db)
        sid = _run(sm1.create(user_id="persist"))
        _run(sm1.set(sid, "key", "val"))
        sm2 = SessionManager(db_path=db)
        info = sm2.session_info(sid)
        self.assertIsNotNone(info)

    def test_stats(self):
        _run(self.sm.create()); _run(self.sm.create())
        s = self.sm.stats()
        for k in ["active","expired","in_memory"]: self.assertIn(k, s)

    def test_set_returns_false_expired(self):
        sid = _run(self.sm.create())
        _run(self.sm.expire(sid))
        ok = _run(self.sm.set(sid, "k", "v"))
        self.assertFalse(ok)

# ════════════════════════════════════════════════════════
# EVENT BUS
# ════════════════════════════════════════════════════════
class TestEventBus(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.event_bus import EventBus
        self.bus = EventBus(db_path=os.path.join(td,"events.db"),
                             await_handlers=True)

    def test_publish_event(self):
        from agent.event_bus import Event
        events = []
        self.bus.subscribe("test", lambda e: events.append(e))
        _run(self.bus.publish("test", {"x":1}))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["x"], 1)

    def test_subscribe_returns_id(self):
        sid = self.bus.subscribe("topic", lambda e: None)
        self.assertIsNotNone(sid)

    def test_unsubscribe(self):
        received = []
        sid = self.bus.subscribe("unsub", lambda e: received.append(e))
        self.bus.unsubscribe(sid)
        _run(self.bus.publish("unsub", "data"))
        self.assertEqual(len(received), 0)

    def test_wildcard_match(self):
        received = []
        self.bus.subscribe("agent.*", lambda e: received.append(e.topic))
        _run(self.bus.publish("agent.run", {}))
        _run(self.bus.publish("agent.fail", {}))
        _run(self.bus.publish("other.event", {}))
        self.assertIn("agent.run", received)
        self.assertIn("agent.fail", received)
        self.assertNotIn("other.event", received)

    def test_wildcard_star_all(self):
        received = []
        self.bus.subscribe("*", lambda e: received.append(e.topic))
        _run(self.bus.publish("anything", {}))
        _run(self.bus.publish("everything", {}))
        self.assertIn("anything", received)
        self.assertIn("everything", received)

    def test_priority_ordering(self):
        order = []
        self.bus.subscribe("ordered", lambda e: order.append("low"),  priority=10)
        self.bus.subscribe("ordered", lambda e: order.append("high"), priority=1)
        _run(self.bus.publish("ordered", {}))
        self.assertEqual(order, ["high", "low"])

    def test_filter_fn(self):
        received = []
        self.bus.subscribe("filtered",
                            lambda e: received.append(e),
                            filter_fn=lambda e: e.payload.get("ok") is True)
        _run(self.bus.publish("filtered", {"ok": True}))
        _run(self.bus.publish("filtered", {"ok": False}))
        self.assertEqual(len(received), 1)

    def test_async_handler(self):
        received = []
        async def async_handler(event):
            await asyncio.sleep(0.01)
            received.append(event)
        self.bus.subscribe("async_topic", async_handler)
        _run(self.bus.publish("async_topic", "payload"))
        self.assertEqual(len(received), 1)

    def test_dlq_on_failure(self):
        def bad_handler(e): raise RuntimeError("deliberate")
        self.bus.subscribe("dlq_topic", bad_handler, max_retries=0)
        _run(self.bus.publish("dlq_topic", "test"))
        dlq = self.bus.dlq("dlq_topic")
        self.assertGreater(len(dlq), 0)

    def test_retry_on_failure(self):
        calls = [0]
        def flaky(e):
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("not yet")
        self.bus.subscribe("retry_topic", flaky, max_retries=3, retry_delay=0.01)
        _run(self.bus.publish("retry_topic", {}))
        self.assertGreaterEqual(calls[0], 2)

    def test_event_history(self):
        _run(self.bus.publish("hist", {"n":1}))
        _run(self.bus.publish("hist", {"n":2}))
        hist = self.bus.history("hist")
        self.assertEqual(len(hist), 2)

    def test_replay(self):
        received = []
        since = time.time()
        _run(self.bus.publish("replay_topic", {"v":1}))
        _run(self.bus.publish("replay_topic", {"v":2}))
        self.bus.subscribe("replay_topic", lambda e: received.append(e))
        n = _run(self.bus.replay("replay_topic", since=since))
        self.assertGreaterEqual(n, 2)

    def test_topics_list(self):
        _run(self.bus.publish("topic_a", {}))
        _run(self.bus.publish("topic_b", {}))
        topics = self.bus.topics()
        self.assertIn("topic_a", topics)
        self.assertIn("topic_b", topics)

    def test_subscribers_list(self):
        self.bus.subscribe("list_topic", lambda e: None)
        subs = self.bus.subscribers("list_topic")
        self.assertGreater(len(subs), 0)

    def test_event_envelope(self):
        received = []
        self.bus.subscribe("env_topic", lambda e: received.append(e))
        _run(self.bus.publish("env_topic", {"key":"val"},
                               source="test_suite",
                               metadata={"trace":"123"}))
        ev = received[0]
        self.assertEqual(ev.source, "test_suite")
        self.assertEqual(ev.metadata["trace"], "123")

    def test_event_to_dict(self):
        from agent.event_bus import Event
        ev = Event(topic="t", payload={"x":1}, source="src")
        d = ev.to_dict()
        for k in ["id","topic","payload","source","timestamp"]:
            self.assertIn(k, d)

    def test_subscriber_to_dict(self):
        sid = self.bus.subscribe("sd_topic", lambda e: None)
        sub = self.bus._subscribers[sid]
        d = sub.to_dict()
        for k in ["id","pattern","priority","received"]:
            self.assertIn(k, d)

    def test_multiple_subscribers_same_topic(self):
        r1 = []; r2 = []
        self.bus.subscribe("multi", lambda e: r1.append(e))
        self.bus.subscribe("multi", lambda e: r2.append(e))
        _run(self.bus.publish("multi", "data"))
        self.assertEqual(len(r1), 1); self.assertEqual(len(r2), 1)

    def test_stats(self):
        _run(self.bus.publish("stat_topic", {}))
        s = self.bus.stats()
        for k in ["total_events","dlq_size","subscribers","active_topics"]:
            self.assertIn(k, s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v27: {total-failed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    sys.exit(0 if not failed else 1)
