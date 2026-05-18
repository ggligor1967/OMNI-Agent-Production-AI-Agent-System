"""OMNI AGENT v44: EventStore, SecretManager, BloomFilter, APIGateway"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# EVENT STORE
# ════════════════════════════════════════════════════════
class TestEventStore(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.event_store import EventStore
        self.es = EventStore(db_path=os.path.join(td, "es.db"))

    def test_append_returns_event(self):
        ev = self.es.append("stream-1", "UserCreated", {"name": "Alice"})
        self.assertIsNotNone(ev.id)
        self.assertEqual(ev.type, "UserCreated")

    def test_append_increments_version(self):
        e1 = self.es.append("s1", "A", {})
        e2 = self.es.append("s1", "B", {})
        self.assertEqual(e1.version, 1)
        self.assertEqual(e2.version, 2)

    def test_global_pos_monotone(self):
        e1 = self.es.append("s1", "A", {})
        e2 = self.es.append("s2", "B", {})
        self.assertGreater(e2.global_pos, e1.global_pos)

    def test_expected_version_ok(self):
        self.es.append("sv", "A", {})
        e2 = self.es.append("sv", "B", {}, expected_version=1)
        self.assertEqual(e2.version, 2)

    def test_expected_version_conflict(self):
        self.es.append("cv", "A", {})
        with self.assertRaises(ValueError):
            self.es.append("cv", "B", {}, expected_version=99)

    def test_read_stream(self):
        for i in range(5):
            self.es.append("r1", "Evt", {"i": i})
        events = self.es.read_stream("r1")
        self.assertEqual(len(events), 5)

    def test_read_stream_from_version(self):
        for i in range(5):
            self.es.append("rf", "Evt", {"i": i})
        events = self.es.read_stream("rf", from_version=3)
        self.assertEqual(len(events), 3)

    def test_read_all(self):
        self.es.append("a1", "X", {})
        self.es.append("a2", "Y", {})
        events = self.es.read_all()
        self.assertGreaterEqual(len(events), 2)

    def test_read_all_type_filter(self):
        self.es.append("tf", "TypeA", {})
        self.es.append("tf", "TypeB", {})
        events = self.es.read_all(types=["TypeA"])
        self.assertTrue(all(e.type == "TypeA" for e in events))

    def test_projection(self):
        def reducer(state, event):
            if event.type == "Deposited":
                state["bal"] = state.get("bal", 0) + event.data["amount"]
            elif event.type == "Withdrawn":
                state["bal"] = state.get("bal", 0) - event.data["amount"]
            return state
        self.es.register_projection("balance", reducer)
        self.es.append("acct", "Deposited", {"amount": 100})
        self.es.append("acct", "Withdrawn", {"amount": 30})
        state = self.es.project("acct", "balance")
        self.assertEqual(state["bal"], 70)

    def test_projection_cached(self):
        def reducer(state, ev): return state
        self.es.register_projection("noop", reducer)
        self.es.append("pc", "X", {})
        self.es.project("pc", "noop")
        cache_before = len(self.es._proj_cache)
        self.es.project("pc", "noop")
        self.assertEqual(len(self.es._proj_cache), cache_before)

    def test_snapshot_speeds_replay(self):
        def reducer(state, ev):
            state["count"] = state.get("count", 0) + 1
            return state
        self.es.register_projection("counter", reducer)
        for _ in range(5):
            self.es.append("snp", "Evt", {})
        snap = self.es.take_snapshot("snp", "counter")
        self.assertEqual(snap.version, 5)
        self.assertEqual(snap.state["count"], 5)

    def test_snapshot_used_on_project(self):
        calls = [0]
        def reducer(state, ev):
            calls[0] += 1
            state["n"] = state.get("n", 0) + 1
            return state
        self.es.register_projection("cnt2", reducer)
        for _ in range(5):
            self.es.append("u2", "E", {})
        self.es.take_snapshot("u2", "cnt2")
        calls[0] = 0
        self.es.append("u2", "E", {})
        self.es.project("u2", "cnt2")
        self.assertEqual(calls[0], 1)  # only 1 event since snapshot

    def test_replay(self):
        replayed = []
        for i in range(3):
            self.es.append("rp", "E", {"i": i})
        n = self.es.replay("rp", lambda ev: replayed.append(ev.data["i"]))
        self.assertEqual(n, 3)
        self.assertEqual(replayed, [0, 1, 2])

    def test_subscription_stream(self):
        received = []
        self.es.subscribe(lambda ev: received.append(ev.type), "sub-s")
        self.es.append("sub-s", "SubEvt", {})
        self.assertIn("SubEvt", received)

    def test_subscription_global(self):
        received = []
        self.es.subscribe(lambda ev: received.append(ev.stream_id))
        self.es.append("glob-s", "X", {})
        self.assertIn("glob-s", received)

    def test_upcaster(self):
        def upcast(ev):
            ev.data["migrated"] = True
            return ev
        self.es.register_upcaster("OldEvent", upcast)
        self.es.append("up-s", "OldEvent", {"v": 1})
        events = self.es.read_stream("up-s")
        self.assertTrue(events[0].data.get("migrated"))

    def test_truncate(self):
        for i in range(5):
            self.es.append("trunc", "E", {"i": i})
        n = self.es.truncate("trunc", before_version=3)
        self.assertGreaterEqual(n, 2)
        events = self.es.read_stream("trunc")
        self.assertTrue(all(e.version >= 3 for e in events))

    def test_stream_version(self):
        self.es.append("ver-s", "A", {})
        self.es.append("ver-s", "B", {})
        self.assertEqual(self.es.stream_version("ver-s"), 2)

    def test_append_batch(self):
        evs = self.es.append_batch("batch-s", [("A",{}),("B",{}),("C",{})])
        self.assertEqual(len(evs), 3)
        self.assertEqual(self.es.stream_version("batch-s"), 3)

    def test_stats(self):
        self.es.append("stat-s", "X", {})
        s = self.es.stats()
        for k in ["total_events","global_position"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# SECRET MANAGER
# ════════════════════════════════════════════════════════
class TestSecretManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.secret_manager import SecretManager
        self.sm = SecretManager(
            master_key=b"test-master-key-32-bytes-exactly",
            db_path=os.path.join(td, "sm.db"))

    def test_put_and_get(self):
        self.sm.put("prod", "db_pw", "s3cr3t!")
        val = self.sm.get("prod", "db_pw")
        self.assertEqual(val, "s3cr3t!")

    def test_encryption_opaque(self):
        self.sm.put("prod", "enc_test", "plaintext")
        secret = self.sm._cache.get("prod/enc_test")
        enc = secret.current_version.encrypted
        self.assertNotIn("plaintext", enc)

    def test_versioning(self):
        v1 = self.sm.put("prod", "versioned", "v1")
        v2 = self.sm.put("prod", "versioned", "v2")
        self.assertGreater(v2, v1)
        val = self.sm.get("prod", "versioned")
        self.assertEqual(val, "v2")

    def test_get_specific_version(self):
        self.sm.put("prod", "mv", "version-one")
        self.sm.put("prod", "mv", "version-two")
        val = self.sm.get("prod", "mv", version=1)
        self.assertEqual(val, "version-one")

    def test_rotate(self):
        self.sm.put("prod", "rotated", "old-value")
        ver = self.sm.rotate("prod", "rotated", new_value="new-value")
        self.assertGreater(ver, 1)
        val = self.sm.get("prod", "rotated")
        self.assertEqual(val, "new-value")

    def test_rotate_with_fn(self):
        self.sm.put("prod", "fn_rotated", "old")
        self.sm.rotate("prod", "fn_rotated",
                         rotation_fn=lambda old: old.upper())
        val = self.sm.get("prod", "fn_rotated")
        self.assertEqual(val, "OLD")

    def test_delete(self):
        self.sm.put("prod", "del_me", "bye")
        ok = self.sm.delete("prod", "del_me")
        self.assertTrue(ok)
        val = self.sm.get("prod", "del_me")
        self.assertIsNone(val)

    def test_expiry(self):
        self.sm.put("prod", "expired", "gone",
                     expiry_ts=time.time() - 1)
        val = self.sm.get("prod", "expired")
        self.assertIsNone(val)

    def test_not_expired(self):
        self.sm.put("prod", "fresh", "here",
                     expiry_ts=time.time() + 3600)
        val = self.sm.get("prod", "fresh")
        self.assertEqual(val, "here")

    def test_access_control_allowed(self):
        self.sm.put("prod", "guarded", "secret",
                     principals=["svc-a"])
        val = self.sm.get("prod", "guarded", caller="svc-a")
        self.assertEqual(val, "secret")

    def test_access_control_denied(self):
        self.sm.put("prod", "protected", "secret",
                     principals=["svc-a"])
        with self.assertRaises(PermissionError):
            self.sm.get("prod", "protected", caller="svc-b")

    def test_system_caller_bypasses(self):
        self.sm.put("prod", "sys_sec", "value",
                     principals=["specific-svc"])
        val = self.sm.get("prod", "sys_sec", caller="system")
        self.assertEqual(val, "value")

    def test_get_missing_returns_none(self):
        val = self.sm.get("prod", "nonexistent")
        self.assertIsNone(val)

    def test_list_secrets(self):
        self.sm.put("ns1", "a", "v")
        self.sm.put("ns1", "b", "v")
        names = self.sm.list_secrets("ns1")
        self.assertIn("a", names); self.assertIn("b", names)

    def test_interpolate(self):
        self.sm.put("prod", "token", "abc123")
        result = self.sm.interpolate(
            "Bearer ${secret:token}", "prod")
        self.assertEqual(result, "Bearer abc123")

    def test_on_rotate_hook(self):
        rotated = []
        self.sm.on_rotate(lambda s: rotated.append(s.name))
        self.sm.put("prod", "h_rot", "v1")
        self.sm.rotate("prod", "h_rot", new_value="v2")
        self.assertIn("h_rot", rotated)

    def test_on_access_hook(self):
        accessed = []
        self.sm.on_access(lambda s, c: accessed.append(s.name))
        self.sm.put("prod", "h_acc", "v")
        self.sm.get("prod", "h_acc")
        self.assertIn("h_acc", accessed)

    def test_on_expire_hook(self):
        expired = []
        self.sm.on_expire(lambda s: expired.append(s.name))
        self.sm.put("prod", "h_exp", "v", expiry_ts=time.time()-1)
        self.sm.get("prod", "h_exp")
        self.assertIn("h_exp", expired)

    def test_audit_log(self):
        self.sm.put("prod", "aud", "v")
        self.sm.get("prod", "aud")
        log = self.sm.audit_log()
        self.assertGreater(len(log), 0)

    def test_stats(self):
        self.sm.put("prod", "s", "v")
        s = self.sm.stats()
        for k in ["secrets","cached"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# BLOOM FILTER / PROBABILISTIC
# ════════════════════════════════════════════════════════
class TestBloomFilter(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.bloom_filter import (ProbabilisticStore, BloomFilter,
                                         CountMinSketch, HyperLogLog, TopK)
        self.PS = ProbabilisticStore; self.BF = BloomFilter
        self.CMS = CountMinSketch; self.HLL = HyperLogLog; self.TK = TopK
        self.ps = ProbabilisticStore(db_path=os.path.join(td,"bf.db"))

    def test_bloom_add_contains(self):
        bf = self.BF(capacity=1000, fpr=0.01)
        bf.add("hello")
        self.assertIn("hello", bf)

    def test_bloom_not_contains(self):
        bf = self.BF(capacity=1000, fpr=0.01)
        # Very unlikely to false-positive with low fill
        self.assertNotIn("zzz_not_added_xyz", bf)

    def test_bloom_no_false_negatives(self):
        bf = self.BF(capacity=1000, fpr=0.01)
        items = [f"item-{i}" for i in range(100)]
        for it in items: bf.add(it)
        for it in items: self.assertIn(it, bf)

    def test_bloom_fill_ratio(self):
        bf = self.BF(capacity=1000, fpr=0.01)
        self.assertEqual(bf.fill_ratio, 0.0)
        bf.add("x")
        self.assertGreater(bf.fill_ratio, 0.0)

    def test_bloom_union(self):
        bf1 = self.BF(capacity=1000, fpr=0.01)
        bf2 = self.BF(capacity=1000, fpr=0.01)
        bf1.add("a"); bf2.add("b")
        union = bf1.union(bf2)
        self.assertIn("a", union); self.assertIn("b", union)

    def test_bloom_serialization(self):
        bf = self.BF(capacity=500, fpr=0.05)
        bf.add("persist")
        d = bf.to_dict()
        bf2 = self.BF.from_dict(d)
        self.assertIn("persist", bf2)

    def test_cms_update_query(self):
        cms = self.CMS(epsilon=0.01, delta=0.001)
        cms.update("word", 5)
        self.assertGreaterEqual(cms.query("word"), 5)

    def test_cms_zero_for_unseen(self):
        cms = self.CMS()
        self.assertEqual(cms.query("never_seen"), 0)

    def test_cms_accumulates(self):
        cms = self.CMS()
        cms.update("x", 3); cms.update("x", 4)
        self.assertGreaterEqual(cms.query("x"), 7)

    def test_cms_serialization(self):
        cms = self.CMS()
        cms.update("key", 10)
        d = cms.to_dict()
        cms2 = self.CMS.from_dict(d)
        self.assertGreaterEqual(cms2.query("key"), 10)

    def test_hll_cardinality_empty(self):
        hll = self.HLL(b=10)
        self.assertEqual(hll.count(), 0)

    def test_hll_cardinality_small(self):
        hll = self.HLL(b=14)
        for i in range(100):
            hll.add(f"user:{i}")
        est = hll.count()
        self.assertGreater(est, 50)
        self.assertLess(est, 200)

    def test_hll_cardinality_accurate(self):
        hll = self.HLL(b=14)
        n = 1000
        for i in range(n): hll.add(f"item:{i}")
        est = hll.count()
        error = abs(est - n) / n
        self.assertLess(error, 0.1)

    def test_hll_merge(self):
        h1 = self.HLL(b=14); h2 = self.HLL(b=14)
        for i in range(500): h1.add(f"a:{i}")
        for i in range(500): h2.add(f"b:{i}")
        merged = h1.merge(h2)
        self.assertGreater(merged.count(), 500)

    def test_hll_serialization(self):
        hll = self.HLL(b=10)
        hll.add("x"); hll.add("y")
        d = hll.to_dict()
        hll2 = self.HLL.from_dict(d)
        self.assertGreater(hll2.count(), 0)

    def test_topk_basic(self):
        tk = self.TK(k=3)
        for _ in range(10): tk.update("a")
        for _ in range(5):  tk.update("b")
        for _ in range(1):  tk.update("c")
        top = tk.top_k()
        self.assertEqual(top[0][0], "a")

    def test_ps_create_bloom(self):
        self.ps.create_bloom("urls", capacity=10000)
        self.ps.add("urls", "http://example.com")
        self.assertTrue(self.ps.contains("urls", "http://example.com"))

    def test_ps_create_cms(self):
        self.ps.create_cms("words")
        self.ps.update("words", "hello", 7)
        self.assertGreaterEqual(self.ps.query_freq("words","hello"), 7)

    def test_ps_create_hll(self):
        self.ps.create_hll("visitors")
        for i in range(50): self.ps.add("visitors", f"u{i}")
        c = self.ps.cardinality("visitors")
        self.assertGreater(c, 20)

    def test_ps_stats(self):
        self.ps.create_bloom("s_bf")
        s = self.ps.stats("s_bf")
        self.assertIn("type", s)

# ════════════════════════════════════════════════════════
# API GATEWAY
# ════════════════════════════════════════════════════════
class TestAPIGateway(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.api_gateway import (APIGateway, GatewayRequest,
                                        GatewayResponse, AuthStrategy)
        self.GW = APIGateway; self.GReq = GatewayRequest
        self.GResp = GatewayResponse; self.AS = AuthStrategy
        self.gw = APIGateway(db_path=os.path.join(td,"gw.db"),
                              jwt_secret=b"test-secret-key-for-jwt!!")

    def _req(self, method="GET", path="/", headers=None, query=None):
        return self.GReq(method=method, path=path,
                          headers=dict(headers or {}),
                          query=dict(query or {}))

    def test_route_match(self):
        async def h(req, ctx): return self.GResp(body={"ok":True})
        self.gw.register_route("GET","/ping", handler=h)
        resp = _run(self.gw.dispatch(self._req("GET","/ping")))
        self.assertEqual(resp.status, 200)

    def test_route_not_found(self):
        resp = _run(self.gw.dispatch(self._req("GET","/nope")))
        self.assertEqual(resp.status, 404)

    def test_path_params(self):
        captured = {}
        async def h(req, ctx):
            captured.update(req.path_params)
            return self.GResp(body={})
        self.gw.register_route("GET","/users/{id}", handler=h)
        _run(self.gw.dispatch(self._req("GET","/users/42")))
        self.assertEqual(captured.get("id"), "42")

    def test_method_mismatch(self):
        async def h(req, ctx): return self.GResp()
        self.gw.register_route("POST","/only-post", handler=h)
        resp = _run(self.gw.dispatch(self._req("GET","/only-post")))
        self.assertEqual(resp.status, 404)

    def test_apikey_auth_valid(self):
        self.gw.add_api_key("key-abc")
        async def h(req, ctx): return self.GResp(body={"auth":"ok"})
        self.gw.register_route("GET","/secure", handler=h,
                                 auth=self.AS.APIKEY)
        resp = _run(self.gw.dispatch(self._req(
            path="/secure", headers={"x-api-key":"key-abc"})))
        self.assertEqual(resp.status, 200)

    def test_apikey_auth_invalid(self):
        self.gw.add_api_key("real-key")
        async def h(req, ctx): return self.GResp()
        self.gw.register_route("GET","/guarded", handler=h,
                                 auth=self.AS.APIKEY)
        resp = _run(self.gw.dispatch(self._req(
            path="/guarded", headers={"x-api-key":"wrong"})))
        self.assertEqual(resp.status, 401)

    def test_mock_response(self):
        self.gw.register_route("GET","/mock",
                                 mock_response={"status":200,
                                                 "body":{"mocked":True}})
        resp = _run(self.gw.dispatch(self._req(path="/mock")))
        self.assertEqual(resp.body["mocked"], True)

    def test_rate_limiting(self):
        async def h(req, ctx): return self.GResp()
        self.gw.register_route("GET","/rl", handler=h,
                                 rate_limit=1)
        req = self._req(path="/rl", headers={"x-api-key":"u1"})
        req.client_id = "u1"
        r1 = _run(self.gw.dispatch(req))
        r2 = _run(self.gw.dispatch(req))
        statuses = {r1.status, r2.status}
        self.assertIn(429, statuses)

    def test_timeout(self):
        async def slow(req, ctx):
            await asyncio.sleep(10)
            return self.GResp()
        self.gw.register_route("GET","/slow", handler=slow, timeout_s=0.05)
        resp = _run(self.gw.dispatch(self._req(path="/slow")))
        self.assertEqual(resp.status, 504)

    def test_request_transform(self):
        transformed = []
        def req_tf(req):
            req.headers["X-Transformed"] = "yes"
            transformed.append(True)
            return req
        async def h(req, ctx):
            return self.GResp(body={"h": req.headers.get("X-Transformed")})
        self.gw.register_route("GET","/tf", handler=h,
                                 req_transform=req_tf)
        resp = _run(self.gw.dispatch(self._req(path="/tf")))
        self.assertEqual(resp.body["h"], "yes")

    def test_response_transform(self):
        async def h(req, ctx): return self.GResp(body={"v":1})
        def resp_tf(resp):
            resp.headers["X-Custom"] = "added"
            return resp
        self.gw.register_route("GET","/rtf", handler=h,
                                 resp_transform=resp_tf)
        resp = _run(self.gw.dispatch(self._req(path="/rtf")))
        self.assertEqual(resp.headers.get("X-Custom"), "added")

    def test_handler_error_returns_500(self):
        async def bad(req, ctx): raise RuntimeError("oops")
        self.gw.register_route("GET","/err", handler=bad)
        resp = _run(self.gw.dispatch(self._req(path="/err")))
        self.assertEqual(resp.status, 500)

    def test_on_request_hook(self):
        seen = []
        self.gw.on_request(lambda req: seen.append(req.path))
        async def h(req, ctx): return self.GResp()
        self.gw.register_route("GET","/hook", handler=h)
        _run(self.gw.dispatch(self._req(path="/hook")))
        self.assertIn("/hook", seen)

    def test_on_response_hook(self):
        seen = []
        self.gw.on_response(lambda req, resp: seen.append(resp.status))
        async def h(req, ctx): return self.GResp(status=201)
        self.gw.register_route("GET","/resp-hook", handler=h)
        _run(self.gw.dispatch(self._req(path="/resp-hook")))
        self.assertIn(201, seen)

    def test_latency_recorded(self):
        async def h(req, ctx): return self.GResp()
        self.gw.register_route("GET","/lat", handler=h)
        resp = _run(self.gw.dispatch(self._req(path="/lat")))
        self.assertGreater(resp.latency_ms, 0)

    def test_route_stats_increment(self):
        async def h(req, ctx): return self.GResp()
        rc = self.gw.register_route("GET","/sc", handler=h)
        _run(self.gw.dispatch(self._req(path="/sc")))
        self.assertEqual(rc.requests, 1)

    def test_query_param_route(self):
        captured = {}
        async def h(req, ctx):
            captured.update(req.query)
            return self.GResp()
        self.gw.register_route("GET","/qp", handler=h)
        req = self._req(path="/qp", query={"page":"2"})
        _run(self.gw.dispatch(req))
        self.assertEqual(captured.get("page"), "2")

    def test_stats(self):
        s = self.gw.stats()
        for k in ["routes","requests"]: self.assertIn(k, s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v44: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
