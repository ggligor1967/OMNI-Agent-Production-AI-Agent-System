"""OMNI AGENT v39: EventBus, SessionManager, TemplateEngine, ObjectStorage"""
import asyncio, json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# EVENT BUS
# ════════════════════════════════════════════════════════
class TestEventBus(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.event_bus import EventBus, Event, DeliveryMode
        self.EB = EventBus; self.Ev = Event; self.DM = DeliveryMode
        self.bus = EventBus(db_path=os.path.join(td,"eb.db"))

    def test_publish_returns_event(self):
        ev = _run(self.bus.publish("test.topic", {"x": 1}))
        self.assertIsNotNone(ev)
        self.assertEqual(ev.topic, "test.topic")

    def test_subscribe_handler_called(self):
        received = []
        async def handler(ev): received.append(ev.payload)
        self.bus.subscribe("sub1","test.*", handler)
        _run(self.bus.publish("test.event", {"msg":"hello"}))
        self.assertEqual(received, [{"msg":"hello"}])

    def test_wildcard_pattern(self):
        received = []
        async def h(ev): received.append(ev.topic)
        self.bus.subscribe("wc_sub","user.*", h)
        _run(self.bus.publish("user.created", {}))
        _run(self.bus.publish("user.deleted", {}))
        _run(self.bus.publish("order.created", {}))
        self.assertEqual(set(received), {"user.created","user.deleted"})

    def test_exact_pattern(self):
        received = []
        async def h(ev): received.append(ev.topic)
        self.bus.subscribe("exact","order.created", h)
        _run(self.bus.publish("order.created", {}))
        _run(self.bus.publish("order.updated", {}))
        self.assertEqual(received, ["order.created"])

    def test_filter_fn(self):
        received = []
        async def h(ev): received.append(ev.payload)
        self.bus.subscribe("filtered","data.*", h,
                            filter_fn=lambda ev: ev.payload.get("ok"))
        _run(self.bus.publish("data.in", {"ok": True}))
        _run(self.bus.publish("data.in", {"ok": False}))
        self.assertEqual(len(received), 1)
        self.assertTrue(received[0]["ok"])

    def test_priority_ordering(self):
        order = []
        async def h1(ev): order.append(1)
        async def h2(ev): order.append(2)
        self.bus.subscribe("p1","pri.*", h1, priority=2)
        self.bus.subscribe("p2","pri.*", h2, priority=1)
        _run(self.bus.publish("pri.test", {}))
        self.assertEqual(order[0], 2)  # priority 1 (lowest number) first

    def test_retry_on_error(self):
        calls = [0]
        async def flaky(ev):
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("fail")
        self.bus.subscribe("retry_sub","retry.*", flaky,
                            max_retries=3, retry_delay_s=0.001)
        _run(self.bus.publish("retry.test", {}))
        self.assertGreaterEqual(calls[0], 3)

    def test_dlq_on_exhausted_retry(self):
        async def always_fail(ev): raise RuntimeError("dlq test")
        self.bus.subscribe("dlq_sub","dlq.*", always_fail, max_retries=1)
        _run(self.bus.publish("dlq.test", {}))
        dlq = self.bus.dlq()
        self.assertGreater(len(dlq), 0)

    def test_unsubscribe(self):
        received = []
        async def h(ev): received.append(ev)
        self.bus.subscribe("unsub","unsub.*", h)
        self.bus.unsubscribe("unsub")
        _run(self.bus.publish("unsub.test", {}))
        self.assertEqual(received, [])

    def test_pause_resume_topic(self):
        received = []
        async def h(ev): received.append(ev)
        self.bus.subscribe("pause_sub","paused.*", h)
        self.bus.pause("paused.topic")
        _run(self.bus.publish("paused.topic", {}))
        self.assertEqual(received, [])
        self.bus.resume("paused.topic")
        _run(self.bus.publish("paused.topic", {}))
        self.assertEqual(len(received), 1)

    def test_event_buffer(self):
        _run(self.bus.publish("buf.topic", {"n": 1}))
        _run(self.bus.publish("buf.topic", {"n": 2}))
        buf = self.bus.get_buffer("buf.topic")
        self.assertEqual(len(buf), 2)

    def test_replay(self):
        received = []
        async def h(ev): received.append(ev)
        self.bus.subscribe("replay_sub","replay.*", h)
        _run(self.bus.publish("replay.test", {"n": 1}))
        _run(self.bus.publish("replay.test", {"n": 2}))
        count = _run(self.bus.replay("replay_sub", from_seq=0))
        self.assertGreaterEqual(count, 2)

    def test_middleware_applied(self):
        transformed = []
        def mw(ev):
            ev.headers["mw"] = "yes"; return ev
        self.bus.add_middleware(mw)
        async def h(ev): transformed.append(ev.headers.get("mw"))
        self.bus.subscribe("mw_sub","mw.*", h)
        _run(self.bus.publish("mw.test", {}))
        self.assertIn("yes", transformed)

    def test_on_publish_hook(self):
        published = []
        self.bus.on_publish(lambda ev: published.append(ev.topic))
        _run(self.bus.publish("hook.test", {}))
        self.assertIn("hook.test", published)

    def test_event_to_dict(self):
        ev = _run(self.bus.publish("dict.test", {"a":1}))
        d = ev.to_dict()
        for k in ["id","topic","payload","ts","seq"]: self.assertIn(k,d)

    def test_seq_increments(self):
        ev1 = _run(self.bus.publish("seq.test", {}))
        ev2 = _run(self.bus.publish("seq.test", {}))
        self.assertGreater(ev2.seq, ev1.seq)

    def test_sync_subscriber(self):
        received = []
        def sync_h(ev): received.append(ev.payload)
        self.bus.subscribe("sync_sub","sync.*", sync_h)
        _run(self.bus.publish("sync.event", {"val": 42}))
        self.assertEqual(received, [{"val": 42}])

    def test_stats(self):
        _run(self.bus.publish("stats.test", {}))
        s = self.bus.stats()
        for k in ["subscriptions","topics"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# SESSION MANAGER
# ════════════════════════════════════════════════════════
class TestSessionManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.session_manager import SessionManager, SessionStatus
        self.sm = SessionManager(db_path=os.path.join(td,"sm.db"),
                                  default_ttl_s=3600)
        self.SS = SessionStatus

    def test_create_session(self):
        s = self.sm.create("user_1")
        self.assertIsNotNone(s.token)
        self.assertIsNotNone(s.refresh_token)
        self.assertEqual(s.user_id, "user_1")

    def test_validate_valid_token(self):
        s = self.sm.create("user_2")
        v = self.sm.validate(s.token)
        self.assertIsNotNone(v)
        self.assertEqual(v.user_id, "user_2")

    def test_validate_invalid_token(self):
        self.assertIsNone(self.sm.validate("fake_token_xyz"))

    def test_token_uniqueness(self):
        s1 = self.sm.create("u"); s2 = self.sm.create("u2")
        self.assertNotEqual(s1.token, s2.token)

    def test_ttl_expiry(self):
        s = self.sm.create("u_exp", ttl_s=0.01)
        time.sleep(0.02)
        self.assertIsNone(self.sm.validate(s.token))

    def test_sliding_window_extends(self):
        sm2_td = tempfile.mkdtemp()
        from agent.session_manager import SessionManager
        sm2 = SessionManager(db_path=os.path.join(sm2_td,"sm2.db"),
                              default_ttl_s=0.05, sliding=True)
        s = sm2.create("slide_u")
        time.sleep(0.03); sm2.validate(s.token)  # touch → extend
        time.sleep(0.03)  # within new TTL
        self.assertIsNotNone(sm2.validate(s.token))

    def test_revoke(self):
        s = self.sm.create("u_rev")
        ok = self.sm.revoke(s.token)
        self.assertTrue(ok)
        self.assertIsNone(self.sm.validate(s.token))

    def test_revoke_nonexistent(self):
        self.assertFalse(self.sm.revoke("no_such_token"))

    def test_refresh_token(self):
        s = self.sm.create("u_ref")
        new_s = self.sm.refresh(s.refresh_token)
        self.assertIsNotNone(new_s)
        self.assertNotEqual(new_s.token, s.token)
        self.assertEqual(new_s.user_id, "u_ref")

    def test_old_token_invalid_after_refresh(self):
        s = self.sm.create("u_old")
        self.sm.refresh(s.refresh_token)
        self.assertIsNone(self.sm.validate(s.token))

    def test_rotation_count_increments(self):
        s = self.sm.create("u_rot")
        new_s = self.sm.refresh(s.refresh_token)
        self.assertEqual(new_s.rotation_count, 1)

    def test_claims_stored(self):
        s = self.sm.create("u_cl", claims={"role":"admin","plan":"pro"})
        v = self.sm.validate(s.token)
        self.assertEqual(v.claims["role"], "admin")

    def test_set_claim(self):
        s = self.sm.create("u_sc")
        self.sm.set_claim(s.token, "feature_x", True)
        v = self.sm.validate(s.token)
        self.assertTrue(v.claims.get("feature_x"))

    def test_device_info_stored(self):
        s = self.sm.create("u_dev", device={"ip":"1.2.3.4","ua":"Chrome"})
        v = self.sm.validate(s.token)
        self.assertEqual(v.device["ip"], "1.2.3.4")

    def test_max_sessions_enforced(self):
        from agent.session_manager import SessionManager
        td2 = tempfile.mkdtemp()
        sm2 = SessionManager(db_path=os.path.join(td2,"sm2.db"),
                              max_sessions_per_user=2, default_ttl_s=3600)
        s1 = sm2.create("max_u"); s2 = sm2.create("max_u")
        s3 = sm2.create("max_u")  # should evict s1
        self.assertIsNone(sm2.validate(s1.token))
        self.assertIsNotNone(sm2.validate(s3.token))

    def test_revoke_all(self):
        s1 = self.sm.create("bulk_u"); s2 = self.sm.create("bulk_u")
        n = self.sm.revoke_all("bulk_u")
        self.assertGreaterEqual(n, 2)
        self.assertIsNone(self.sm.validate(s1.token))

    def test_get_user_sessions(self):
        self.sm.create("list_u"); self.sm.create("list_u")
        sessions = self.sm.get_user_sessions("list_u")
        self.assertGreaterEqual(len(sessions), 2)

    def test_sweep_expired(self):
        s = self.sm.create("swp_u", ttl_s=0.01)
        time.sleep(0.02)
        n = self.sm.sweep_expired()
        self.assertGreaterEqual(n, 1)

    def test_on_create_hook(self):
        created = []
        self.sm.on_create(lambda s: created.append(s.user_id))
        self.sm.create("hook_u")
        self.assertIn("hook_u", created)

    def test_on_revoke_hook(self):
        revoked = []
        self.sm.on_revoke(lambda s: revoked.append(s.user_id))
        s = self.sm.create("rev_u")
        self.sm.revoke(s.token)
        self.assertIn("rev_u", revoked)

    def test_stats(self):
        self.sm.create("stat_u")
        s = self.sm.stats()
        for k in ["by_status","in_memory"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# TEMPLATE ENGINE
# ════════════════════════════════════════════════════════
class TestTemplateEngine(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.template_engine import TemplateEngine
        self.te = TemplateEngine(db_path=os.path.join(td,"te.db"),
                                  auto_escape=False)

    def _r(self, src, ctx=None):
        return self.te.render_string(src, ctx or {})

    def test_variable_substitution(self):
        self.assertEqual(self._r("Hello {{ name }}!", {"name":"World"}),
                          "Hello World!")

    def test_dot_access(self):
        self.assertEqual(self._r("{{ user.name }}", {"user":{"name":"Alice"}}),
                          "Alice")

    def test_filter_upper(self):
        self.assertEqual(self._r("{{ name | upper }}", {"name":"hello"}), "HELLO")

    def test_filter_lower(self):
        self.assertEqual(self._r("{{ name | lower }}", {"name":"WORLD"}), "world")

    def test_filter_length(self):
        self.assertEqual(self._r("{{ items | length }}", {"items":[1,2,3]}), "3")

    def test_filter_default(self):
        self.assertEqual(self._r("{{ x | default('N/A') }}", {}), "N/A")

    def test_filter_truncate(self):
        s = "a" * 100
        out = self._r("{{ s | truncate(10) }}", {"s":s})
        self.assertLessEqual(len(out), 10)
        self.assertTrue(out.endswith("..."))

    def test_filter_join(self):
        self.assertEqual(
            self._r("{{ items | join(', ') }}", {"items":["a","b","c"]}),
            "a, b, c")

    def test_filter_replace(self):
        self.assertEqual(
            self._r("{{ s | replace('foo','bar') }}", {"s":"foo baz foo"}),
            "bar baz bar")

    def test_if_true(self):
        out = self._r("{% if x %}yes{% endif %}", {"x": True})
        self.assertEqual(out, "yes")

    def test_if_false(self):
        out = self._r("{% if x %}yes{% endif %}", {"x": False})
        self.assertEqual(out, "")

    def test_if_else(self):
        out = self._r("{% if x %}yes{% else %}no{% endif %}", {"x": False})
        self.assertEqual(out, "no")

    def test_elif(self):
        out = self._r(
            "{% if x == 1 %}one{% elif x == 2 %}two{% else %}other{% endif %}",
            {"x": 2})
        self.assertEqual(out, "two")

    def test_for_loop(self):
        out = self._r("{% for i in items %}{{ i }}{% endfor %}",
                       {"items":[1,2,3]})
        self.assertEqual(out, "123")

    def test_loop_index(self):
        out = self._r(
            "{% for i in items %}{{ loop.index }}{% endfor %}",
            {"items":["a","b","c"]})
        self.assertEqual(out, "123")

    def test_loop_first_last(self):
        out = self._r(
            "{% for i in items %}{% if loop.first %}F{% endif %}"
            "{% if loop.last %}L{% endif %}{% endfor %}",
            {"items":[1,2,3]})
        self.assertEqual(out, "FL")

    def test_for_else_empty(self):
        out = self._r(
            "{% for i in items %}{{ i }}{% else %}empty{% endfor %}",
            {"items":[]})
        self.assertEqual(out, "empty")

    def test_nested_loop(self):
        out = self._r(
            "{% for r in rows %}{% for c in r %}{{ c }}{% endfor %}|{% endfor %}",
            {"rows":[[1,2],[3,4]]})
        self.assertEqual(out, "12|34|")

    def test_comparison_eq(self):
        out = self._r("{% if x == 5 %}yes{% endif %}", {"x":5})
        self.assertEqual(out, "yes")

    def test_comparison_not_in(self):
        out = self._r("{% if x not in items %}missing{% endif %}",
                       {"x":"d","items":["a","b","c"]})
        self.assertEqual(out, "missing")

    def test_macro(self):
        src = ('{% macro greet(name, greeting="Hello") %}'
               '{{ greeting }}, {{ name }}!'
               '{% endmacro %}'
               '{{ greet("Alice") }} and {{ greet("Bob", "Hi") }}')
        out = self._r(src, {})
        self.assertIn("Hello, Alice!", out)
        self.assertIn("Hi, Bob!", out)

    def test_template_registry(self):
        self.te.register("tmpl1", "Value: {{ x }}")
        out = self.te.render("tmpl1", {"x": 42})
        self.assertEqual(out, "Value: 42")

    def test_include(self):
        self.te.register("partial", "PARTIAL:{{ v }}")
        out = self._r('{% include "partial" %}', {"v":"X"})
        self.assertEqual(out, "PARTIAL:X")

    def test_inheritance(self):
        self.te.register("base_t",
                          "HEADER|{% block content %}DEFAULT{% endblock %}|FOOTER")
        src = '{% extends "base_t" %}{% block content %}CHILD{% endblock %}'
        out = self._r(src, {})
        self.assertEqual(out, "HEADER|CHILD|FOOTER")

    def test_auto_escape(self):
        from agent.template_engine import TemplateEngine
        td = tempfile.mkdtemp()
        te_safe = TemplateEngine(db_path=os.path.join(td,"tse.db"),
                                  auto_escape=True)
        out = te_safe.render_string("{{ s }}", {"s":"<script>alert(1)</script>"})
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;", out)

    def test_custom_filter(self):
        self.te.add_filter("shout", lambda v, *a: str(v).upper() + "!!!")
        out = self._r("{{ msg | shout }}", {"msg":"hello"})
        self.assertEqual(out, "HELLO!!!")

    def test_comment_stripped(self):
        out = self._r("before{# this is a comment #}after", {})
        self.assertEqual(out, "beforeafter")

    def test_stats(self):
        self.te.register("s_tmpl","x")
        self.te.render("s_tmpl",{})
        s = self.te.stats()
        for k in ["in_memory","renders","filters"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# OBJECT STORAGE
# ════════════════════════════════════════════════════════
class TestObjectStorage(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.object_storage import ObjectStorage, ACL
        self.os = ObjectStorage(db_path=os.path.join(td,"os.db"))
        self.ACL = ACL
        self.os.create_bucket("test-bucket")

    def test_put_and_get(self):
        self.os.put("test-bucket","k1",b"hello world")
        obj = self.os.get("test-bucket","k1")
        self.assertIsNotNone(obj)
        self.assertEqual(obj.content, b"hello world")

    def test_get_missing(self):
        self.assertIsNone(self.os.get("test-bucket","no-such-key"))

    def test_put_string_content(self):
        self.os.put("test-bucket","str_key","text content")
        obj = self.os.get("test-bucket","str_key")
        self.assertEqual(obj.content, b"text content")

    def test_metadata_stored(self):
        self.os.put("test-bucket","meta_k",b"v",
                     metadata={"author":"alice"})
        obj = self.os.get("test-bucket","meta_k")
        self.assertEqual(obj.metadata["author"],"alice")

    def test_content_type_stored(self):
        self.os.put("test-bucket","ct_k",b"{}",
                     content_type="application/json")
        obj = self.os.get("test-bucket","ct_k")
        self.assertEqual(obj.content_type,"application/json")

    def test_etag_computed(self):
        self.os.put("test-bucket","etag_k",b"data")
        obj = self.os.get("test-bucket","etag_k")
        self.assertIsNotNone(obj.etag)
        self.assertEqual(len(obj.etag), 32)

    def test_delete(self):
        self.os.put("test-bucket","del_k",b"v")
        ok = self.os.delete("test-bucket","del_k")
        self.assertTrue(ok)
        self.assertIsNone(self.os.get("test-bucket","del_k"))

    def test_delete_nonexistent(self):
        ok = self.os.delete("test-bucket","ghost")
        self.assertFalse(ok)

    def test_versioning(self):
        self.os.create_bucket("versioned", versioning=True)
        v1 = self.os.put("versioned","k",b"v1")
        v2 = self.os.put("versioned","k",b"v2")
        obj = self.os.get("versioned","k")
        self.assertEqual(obj.content, b"v2")
        old = self.os.get("versioned","k", version_id=v1.version_id)
        self.assertEqual(old.content, b"v1")

    def test_overwrite_non_versioned(self):
        self.os.put("test-bucket","ow_k",b"old")
        self.os.put("test-bucket","ow_k",b"new")
        obj = self.os.get("test-bucket","ow_k")
        self.assertEqual(obj.content, b"new")

    def test_list_objects(self):
        self.os.put("test-bucket","a/1",b"x")
        self.os.put("test-bucket","a/2",b"y")
        self.os.put("test-bucket","b/1",b"z")
        result = self.os.list("test-bucket", prefix="a/")
        keys = [r["key"] for r in result]
        self.assertIn("a/1",keys); self.assertIn("a/2",keys)
        self.assertNotIn("b/1",keys)

    def test_head_no_content(self):
        self.os.put("test-bucket","head_k",b"data")
        h = self.os.head("test-bucket","head_k")
        self.assertIsNotNone(h)
        self.assertNotIn("content",h)

    def test_copy_object(self):
        self.os.create_bucket("dst-bucket")
        self.os.put("test-bucket","src_k",b"copy me")
        self.os.copy("test-bucket","src_k","dst-bucket","dst_k")
        obj = self.os.get("dst-bucket","dst_k")
        self.assertEqual(obj.content, b"copy me")

    def test_multipart_upload(self):
        uid = self.os.initiate_multipart("test-bucket","mp_key","text/plain")
        self.os.upload_part(uid, 1, b"part one ")
        self.os.upload_part(uid, 2, b"part two")
        obj = self.os.complete_multipart(uid)
        self.assertEqual(obj.content, b"part one part two")

    def test_abort_multipart(self):
        uid = self.os.initiate_multipart("test-bucket","ab_mp","text/plain")
        ok = self.os.abort_multipart(uid)
        self.assertTrue(ok)
        with self.assertRaises(KeyError):
            self.os.complete_multipart(uid)

    def test_presign_get(self):
        self.os.put("test-bucket","ps_k",b"secret")
        token = self.os.presign("test-bucket","ps_k","get",expires_s=60)
        obj = self.os.use_presigned(token)
        self.assertIsNotNone(obj)
        self.assertEqual(obj.content, b"secret")

    def test_presign_expired(self):
        self.os.put("test-bucket","exp_k",b"v")
        token = self.os.presign("test-bucket","exp_k","get",expires_s=0.01)
        time.sleep(0.02)
        self.assertIsNone(self.os.use_presigned(token))

    def test_missing_bucket_raises(self):
        with self.assertRaises(KeyError):
            self.os.put("no-bucket","k",b"v")

    def test_lifecycle_sweep(self):
        self.os.create_bucket("lifecycle", lifecycle_days=0)
        self.os.put("lifecycle","exp_obj",b"v")
        # Manually expire
        with self.os._store._conn() as c:
            c.execute("UPDATE objects SET expires_at=1 WHERE key='exp_obj'")
        n = self.os.sweep_lifecycle()
        self.assertGreaterEqual(n, 1)

    def test_bucket_stats(self):
        self.os.put("test-bucket","sz_k",b"12345")
        s = self.os.bucket_stats("test-bucket")
        self.assertIn("used_bytes",s)
        self.assertGreater(s["used_bytes"], 0)

    def test_global_stats(self):
        s = self.os.stats()
        for k in ["buckets","objects","in_memory_buckets"]: self.assertIn(k,s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v39: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
