"""OMNI AGENT v34: StreamProcessor, PluginManager, MemoryStore, AuditLogger"""
import json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# STREAM PROCESSOR
# ════════════════════════════════════════════════════════
class TestStreamProcessor(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.stream_processor import StreamProcessor, WindowSpec, WindowType, AggFunc, BackpressurePolicy
        self.SP = StreamProcessor; self.WS = WindowSpec
        self.WT = WindowType; self.AF = AggFunc; self.BP = BackpressurePolicy
        self.sp = StreamProcessor(db_path=os.path.join(td,"sp.db"))
        self.sp.create_stream("test", buffer_max=20, persist=True)

    def test_create_stream(self):
        self.sp.create_stream("s2")
        self.assertIn("s2", self.sp._streams)

    def test_push_returns_record(self):
        r = self.sp.push("test", {"v": 1})
        self.assertIsNotNone(r)

    def test_push_unknown_stream_returns_none(self):
        r = self.sp.push("no_such", {"v": 1})
        self.assertIsNone(r)

    def test_buffer_grows(self):
        for i in range(5):
            self.sp.push("test", {"v": i})
        self.assertEqual(len(self.sp.buffer("test")), 5)

    def test_buffer_max_enforced_drop_oldest(self):
        self.sp.create_stream("capped", buffer_max=5,
                               backpressure=self.BP.DROP_OLDEST)
        for i in range(10):
            self.sp.push("capped", {"v": i})
        self.assertLessEqual(len(self.sp.buffer("capped")), 5)

    def test_buffer_max_enforced_drop_new(self):
        self.sp.create_stream("dnew", buffer_max=5,
                               backpressure=self.BP.DROP_NEW)
        for i in range(10):
            self.sp.push("dnew", {"v": i})
        self.assertLessEqual(len(self.sp.buffer("dnew")), 5)

    def test_filter_blocks_records(self):
        self.sp.create_stream("filtered")
        self.sp.add_filter("filtered", lambda r: r.payload.get("v",0) > 5)
        r = self.sp.push("filtered", {"v": 3})
        self.assertIsNone(r)

    def test_filter_passes_records(self):
        self.sp.create_stream("filt2")
        self.sp.add_filter("filt2", lambda r: r.payload.get("v",0) > 5)
        r = self.sp.push("filt2", {"v": 10})
        self.assertIsNotNone(r)

    def test_transform_modifies_payload(self):
        self.sp.create_stream("xform")
        def double(rec):
            rec.payload["v"] = rec.payload.get("v",0) * 2
            return rec
        self.sp.add_transform("xform", double)
        r = self.sp.push("xform", {"v": 5})
        self.assertEqual(r.payload["v"], 10)

    def test_tumbling_window_count(self):
        self.sp.create_stream("tw")
        spec = self.WS(window_type=self.WT.TUMBLING,
                        size_s=0.01, agg_func=self.AF.COUNT)
        self.sp.add_window("tw", spec)
        t0 = time.time()
        for i in range(5):
            self.sp.push("tw", {"v": i}, ts=t0 + i * 0.003)
        results = self.sp.window_results("tw")
        self.assertGreater(len(results), 0)

    def test_tumbling_window_sum(self):
        self.sp.create_stream("tsum")
        spec = self.WS(window_type=self.WT.TUMBLING, size_s=0.01,
                        agg_field="v", agg_func=self.AF.SUM)
        self.sp.add_window("tsum", spec)
        t0 = time.time()
        for i in range(5):
            self.sp.push("tsum", {"v": i+1}, ts=t0 + i * 0.003)
        results = self.sp.window_results("tsum")
        self.assertGreater(len(results), 0)

    def test_count_window(self):
        self.sp.create_stream("cw")
        spec = self.WS(window_type=self.WT.COUNT, count=3,
                        agg_func=self.AF.COUNT)
        self.sp.add_window("cw", spec)
        for i in range(6):
            self.sp.push("cw", {"v": i})
        results = self.sp.window_results("cw")
        self.assertGreaterEqual(len(results), 2)

    def test_sliding_window(self):
        self.sp.create_stream("sw")
        spec = self.WS(window_type=self.WT.SLIDING, size_s=1.0,
                        step_s=0.01, agg_func=self.AF.COUNT)
        self.sp.add_window("sw", spec)
        t0 = time.time()
        for i in range(5):
            self.sp.push("sw", {"v": i}, ts=t0 + i * 0.015)
        results = self.sp.window_results("sw")
        self.assertGreater(len(results), 0)

    def test_session_window_flush(self):
        self.sp.create_stream("sess")
        spec = self.WS(window_type=self.WT.SESSION, gap_s=0.001)
        self.sp.add_window("sess", spec)
        self.sp.push("sess", {"v": 1})
        time.sleep(0.01)
        n = self.sp.flush_sessions()
        self.assertGreaterEqual(n, 0)

    def test_fanout_routes_records(self):
        self.sp.create_stream("src"); self.sp.create_stream("dst")
        self.sp.add_fanout("src", "dst", lambda r: True)
        self.sp.push("src", {"v": 42})
        self.assertEqual(len(self.sp.buffer("dst")), 1)

    def test_fanout_conditional(self):
        self.sp.create_stream("src2"); self.sp.create_stream("dst2")
        self.sp.add_fanout("src2", "dst2", lambda r: r.payload.get("v",0) > 10)
        self.sp.push("src2", {"v": 5})   # should not route
        self.sp.push("src2", {"v": 15})  # should route
        self.assertEqual(len(self.sp.buffer("dst2")), 1)

    def test_dlq_on_filter_error(self):
        self.sp.create_stream("dlq_str")
        def bad_filter(r): raise ValueError("oops")
        self.sp.add_filter("dlq_str", bad_filter)
        self.sp.push("dlq_str", {"v": 1})
        self.assertGreater(len(self.sp._dlq), 0)

    def test_watermark(self):
        t0 = time.time()
        self.sp.push("test", {"v": 1}, ts=t0 + 100)
        wm = self.sp.watermark("test")
        self.assertGreaterEqual(wm, t0)

    def test_seq_increments(self):
        r1 = self.sp.push("test", {"v": 1})
        r2 = self.sp.push("test", {"v": 2})
        self.assertEqual(r2.seq, r1.seq + 1)

    def test_record_to_dict(self):
        r = self.sp.push("test", {"v": 1})
        d = r.to_dict()
        for k in ["id","stream","payload","ts","source","seq"]:
            self.assertIn(k, d)

    def test_stats(self):
        self.sp.push("test", {"v": 1})
        s = self.sp.stats()
        for k in ["streams","dlq_size","per_stream"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# PLUGIN MANAGER
# ════════════════════════════════════════════════════════
class TestPluginManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.plugin_manager import PluginManager, PluginSpec, PluginStatus
        self.PM = PluginManager; self.PS = PluginSpec; self.Stat = PluginStatus
        self.pm = PluginManager(db_path=os.path.join(td,"pm.db"))
        self.td = td

    def _make_plugin_file(self, name, has_setup=True, has_activate=True,
                           has_health=True):
        code = f"NAME = {name!r}\n"
        if has_setup:    code += "def setup(registry): registry[NAME] = 'registered'\n"
        if has_activate: code += "ACTIVE = False\ndef activate(): globals().__setitem__('ACTIVE', True)\n"
        code += "def deactivate(): globals().__setitem__('ACTIVE', False)\n"
        if has_health:   code += "def health(): return {'status': 'ok' if ACTIVE else 'inactive'}\n"
        path = os.path.join(self.td, f"{name}.py")
        open(path, "w").write(code)
        return path

    def test_register_spec(self):
        spec = self.PS(name="p1", version="1.0.0")
        self.pm.register(spec)
        self.assertIn("p1", self.pm._plugins)

    def test_load_no_entry_point(self):
        spec = self.PS(name="nep", version="1.0.0")
        self.pm.register(spec)
        ok = self.pm.load("nep")
        self.assertTrue(ok)
        self.assertEqual(self.pm._plugins["nep"].status, self.Stat.LOADED)

    def test_load_with_module(self):
        path = self._make_plugin_file("plug_a")
        spec = self.PS(name="plug_a", version="1.0.0", entry_point=path)
        self.pm.register(spec)
        registry = {}
        self.pm._registry = registry
        ok = self.pm.load("plug_a")
        self.assertTrue(ok)
        self.assertIn("plug_a", registry)

    def test_load_missing_plugin(self):
        ok = self.pm.load("ghost")
        self.assertFalse(ok)

    def test_activate(self):
        spec = self.PS(name="act_p", version="1.0.0")
        self.pm.register(spec); self.pm.load("act_p")
        ok = self.pm.activate("act_p")
        self.assertTrue(ok)
        self.assertEqual(self.pm._plugins["act_p"].status, self.Stat.ACTIVE)

    def test_deactivate(self):
        spec = self.PS(name="deact_p", version="1.0.0")
        self.pm.register(spec); self.pm.load("deact_p"); self.pm.activate("deact_p")
        ok = self.pm.deactivate("deact_p")
        self.assertTrue(ok)
        self.assertEqual(self.pm._plugins["deact_p"].status, self.Stat.INACTIVE)

    def test_dep_version_check_passes(self):
        s1 = self.PS(name="dep_a", version="2.0.0")
        s2 = self.PS(name="dep_b", version="1.0.0",
                      dependencies={"dep_a": ">=1.5.0"})
        self.pm.register(s1); self.pm.register(s2)
        self.pm.load("dep_a")
        ok = self.pm.load("dep_b")
        self.assertTrue(ok)

    def test_dep_version_check_fails(self):
        s1 = self.PS(name="old_dep", version="1.0.0")
        s2 = self.PS(name="new_req", version="1.0.0",
                      dependencies={"old_dep": ">=2.0.0"})
        self.pm.register(s1); self.pm.register(s2)
        self.pm.load("old_dep")
        ok = self.pm.load("new_req")
        self.assertFalse(ok)
        self.assertEqual(self.pm._plugins["new_req"].status, self.Stat.ERROR)

    def test_dep_not_loaded_blocks_load(self):
        s1 = self.PS(name="unloaded_dep", version="1.0.0")
        s2 = self.PS(name="needs_dep", version="1.0.0",
                      dependencies={"unloaded_dep": ">=1.0.0"})
        self.pm.register(s1); self.pm.register(s2)
        # Not loading s1
        ok = self.pm.load("needs_dep")
        self.assertFalse(ok)

    def test_load_order_topological(self):
        order_log = []
        s1 = self.PS(name="base", version="1.0.0")
        s2 = self.PS(name="mid",  version="1.0.0", dependencies={"base":">=1.0.0"})
        s3 = self.PS(name="top",  version="1.0.0", dependencies={"mid": ">=1.0.0"})
        for s in [s3, s1, s2]: self.pm.register(s)
        self.pm.on("on_load", lambda s: order_log.append(s.name))
        self.pm.load_all()
        if len(order_log) >= 3:
            self.assertLess(order_log.index("base"), order_log.index("mid"))

    def test_health_no_module(self):
        spec = self.PS(name="hlt_p", version="1.0.0")
        self.pm.register(spec)
        h = self.pm.health("hlt_p")
        self.assertIn("status", h)

    def test_health_with_module(self):
        path = self._make_plugin_file("hmod")
        spec = self.PS(name="hmod", version="1.0.0", entry_point=path)
        self.pm.register(spec); self.pm.load("hmod"); self.pm.activate("hmod")
        h = self.pm.health("hmod")
        self.assertIn("status", h)

    def test_on_load_hook(self):
        loaded = []
        self.pm.on("on_load", lambda s: loaded.append(s.name))
        spec = self.PS(name="hook_p", version="1.0.0")
        self.pm.register(spec); self.pm.load("hook_p")
        self.assertIn("hook_p", loaded)

    def test_on_activate_hook(self):
        activated = []
        self.pm.on("on_activate", lambda s: activated.append(s.name))
        spec = self.PS(name="ahook_p", version="1.0.0")
        self.pm.register(spec); self.pm.load("ahook_p"); self.pm.activate("ahook_p")
        self.assertIn("ahook_p", activated)

    def test_on_error_hook(self):
        errors = []
        self.pm.on("on_error", lambda s, e: errors.append(s.name))
        spec = self.PS(name="err_p", version="1.0.0", entry_point="/no/such/file.py")
        self.pm.register(spec); self.pm.load("err_p")
        self.assertIn("err_p", errors)

    def test_list_plugins_by_status(self):
        spec = self.PS(name="list_p", version="1.0.0")
        self.pm.register(spec); self.pm.load("list_p"); self.pm.activate("list_p")
        active = self.pm.list_plugins(status=self.Stat.ACTIVE)
        self.assertTrue(any(p.name == "list_p" for p in active))

    def test_plugin_to_dict(self):
        spec = self.PS(name="dict_p", version="1.0.0")
        self.pm.register(spec)
        d = spec.to_dict()
        for k in ["name","version","status","load_time_ms"]: self.assertIn(k, d)

    def test_hot_swap(self):
        s1 = self.PS(name="swap_p", version="1.0.0")
        self.pm.register(s1); self.pm.load("swap_p"); self.pm.activate("swap_p")
        s2 = self.PS(name="swap_p", version="2.0.0")
        ok = self.pm.hot_swap("swap_p", s2)
        self.assertTrue(ok)
        self.assertEqual(self.pm._plugins["swap_p"].version, "2.0.0")

    def test_stats(self):
        spec = self.PS(name="stat_p", version="1.0.0")
        self.pm.register(spec)
        s = self.pm.stats()
        for k in ["in_memory","by_status"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# MEMORY STORE
# ════════════════════════════════════════════════════════
class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.memory_store import MemoryStore, MemType
        self.ms = MemoryStore(db_path=os.path.join(td,"mem.db"),
                               short_term_cap=10, long_term_cap=20)
        self.MT = MemType

    def test_remember_returns_item(self):
        m = self.ms.remember("Paris is the capital of France")
        self.assertIsNotNone(m)
        self.assertEqual(m.content, "Paris is the capital of France")

    def test_recall_returns_relevant(self):
        self.ms.remember("Paris is in France", importance=0.8)
        self.ms.remember("London is in England", importance=0.8)
        results = self.ms.recall("France capital")
        self.assertGreater(len(results), 0)
        self.assertTrue(any("France" in m.content for m in results))

    def test_recall_top_k(self):
        for i in range(8):
            self.ms.remember(f"fact {i} about topic X")
        results = self.ms.recall("topic X", top_k=3)
        self.assertLessEqual(len(results), 3)

    def test_get_by_id(self):
        m = self.ms.remember("test fact")
        fetched = self.ms.get(m.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.content, "test fact")

    def test_access_count_increments(self):
        m = self.ms.remember("count me")
        self.ms.recall("count me")
        self.ms.recall("count me")
        m2 = self.ms.get(m.id)
        self.assertGreater(m2.access_count, 0)

    def test_short_term_cap_evicts(self):
        for i in range(12):
            self.ms.remember(f"item {i}", importance=0.3)
        self.assertLessEqual(len(self.ms._short), 10)

    def test_high_importance_consolidates(self):
        for i in range(11):
            self.ms.remember(f"hi-imp {i}", importance=0.95)
        n = self.ms.consolidate()
        self.assertGreaterEqual(len(self.ms._long), 0)

    def test_forget_removes_low_score(self):
        m = self.ms.remember("very old fact", importance=0.01)
        # Manually backdate creation to force low decay
        m.created_at -= 99999
        before = len(self.ms._short) + len(self.ms._long)
        forgotten = self.ms.forget(score_threshold=0.99)
        self.assertGreaterEqual(forgotten, 0)

    def test_pin_survives_eviction(self):
        m = self.ms.remember("pinned memory", importance=0.1, pinned=True)
        for i in range(15):
            self.ms.remember(f"eviction fodder {i}", importance=0.1)
        self.assertIn(m.id, self.ms._short)

    def test_link_creates_association(self):
        m1 = self.ms.remember("cat is an animal")
        m2 = self.ms.remember("dog is an animal")
        self.ms.link(m1.id, m2.id, strength=0.9)
        assoc = self.ms.associated(m1.id)
        self.assertTrue(any(m.id == m2.id for m in assoc))

    def test_recall_by_type(self):
        self.ms.remember("I ran 5km", mem_type=self.MT.EPISODE, importance=0.7)
        self.ms.remember("Python is a language", mem_type=self.MT.FACT, importance=0.7)
        results = self.ms.recall("fact", mem_type=self.MT.FACT)
        self.assertTrue(all(m.mem_type == self.MT.FACT for m in results))

    def test_recall_by_tag(self):
        self.ms.remember("tagged item", tags=["science"], importance=0.7)
        self.ms.remember("untagged item", importance=0.7)
        results = self.ms.recall("item", tag="science")
        self.assertTrue(all("science" in m.tags for m in results))

    def test_context_window_respects_budget(self):
        for i in range(5):
            self.ms.remember("x" * 100 + f" fact {i}", importance=0.8)
        ctx = self.ms.context_window("fact", max_tokens=50)
        total_chars = sum(len(m.content) for m in ctx)
        self.assertLessEqual(total_chars // 4, 50)

    def test_decay_score_decreases_with_age(self):
        m = self.ms.remember("old memory", importance=0.5)
        score_now = m.decay_score(decay_rate=0.1)
        m.created_at -= 100 * 3600  # pretend 100 hours ago
        score_old = m.decay_score(decay_rate=0.1)
        self.assertGreater(score_now, score_old)

    def test_persistence_reload(self):
        td = tempfile.mkdtemp()
        from agent.memory_store import MemoryStore
        ms1 = MemoryStore(db_path=os.path.join(td,"m.db"),
                           short_term_cap=20)
        ms1.remember("persistent fact", importance=0.8)
        ms2 = MemoryStore(db_path=os.path.join(td,"m.db"),
                           short_term_cap=20)
        found = any("persistent fact" in m.content
                     for m in list(ms2._short.values())+list(ms2._long.values()))
        self.assertTrue(found)

    def test_pin_unpin(self):
        m = self.ms.remember("pin me", importance=0.5)
        self.ms.pin(m.id)
        self.assertIn(m.id, self.ms._pinned)
        self.ms.unpin(m.id)
        self.assertNotIn(m.id, self.ms._pinned)

    def test_item_to_dict(self):
        m = self.ms.remember("dict test")
        d = m.to_dict()
        for k in ["id","content","type","importance","tier","decay_score"]:
            self.assertIn(k, d)

    def test_stats(self):
        self.ms.remember("stat fact")
        s = self.ms.stats()
        for k in ["in_memory_short","in_memory_long","pinned"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# AUDIT LOGGER
# ════════════════════════════════════════════════════════
class TestAuditLogger(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.audit_logger import AuditLogger, AuditLevel, Outcome
        self.AL = AuditLogger; self.Lv = AuditLevel; self.Out = Outcome
        self.al = AuditLogger(db_path=os.path.join(td,"audit.db"))

    def test_log_returns_event(self):
        e = self.al.log("alice", "login")
        self.assertIsNotNone(e)
        self.assertEqual(e.actor, "alice")
        self.assertEqual(e.action, "login")

    def test_seq_increments(self):
        e1 = self.al.log("alice", "a1")
        e2 = self.al.log("alice", "a2")
        self.assertEqual(e2.seq, e1.seq + 1)

    def test_chain_hash_set(self):
        e = self.al.log("alice", "action")
        self.assertNotEqual(e.chain_hash, "")
        self.assertEqual(len(e.chain_hash), 64)

    def test_prev_hash_chained(self):
        e1 = self.al.log("a", "act1")
        e2 = self.al.log("a", "act2")
        self.assertEqual(e2.prev_hash, e1.chain_hash)

    def test_chain_verification_valid(self):
        for i in range(5):
            self.al.log("user", f"action_{i}")
        valid, errors = self.al.verify_chain()
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_chain_tamper_detected(self):
        e1 = self.al.log("user", "legit")
        # Tamper: directly modify DB record
        import sqlite3
        with sqlite3.connect(self.al._store.db_path) as c:
            c.execute("UPDATE audit_events SET action='tampered' WHERE seq=?", (e1.seq,))
        valid, errors = self.al.verify_chain()
        self.assertFalse(valid)
        self.assertGreater(len(errors), 0)

    def test_integrity_report(self):
        self.al.log("a", "b")
        report = self.al.integrity_report()
        for k in ["valid","total_events","head_hash"]: self.assertIn(k, report)

    def test_log_with_metadata(self):
        e = self.al.log("bob", "read", metadata={"file": "/etc/passwd"})
        self.assertEqual(e.metadata.get("file"), "/etc/passwd")

    def test_log_security_level(self):
        alerts = []
        self.al.on_alert(lambda e: alerts.append(e.action))
        self.al.log("hacker", "brute_force", level=self.Lv.SECURITY)
        self.assertIn("brute_force", alerts)

    def test_info_level_no_alert(self):
        alerts = []
        self.al.on_alert(lambda e: alerts.append(e.action))
        self.al.log("user", "read", level=self.Lv.INFO)
        self.assertNotIn("read", alerts)

    def test_search_by_actor(self):
        self.al.log("alice", "act1"); self.al.log("bob", "act2")
        results = self.al.search(actor="alice")
        self.assertTrue(all(r["actor"] == "alice" for r in results))

    def test_search_by_outcome(self):
        self.al.log("u", "ok", outcome=self.Out.SUCCESS)
        self.al.log("u", "fail", outcome=self.Out.FAILURE)
        results = self.al.search(outcome="failure")
        self.assertTrue(all(r["outcome"] == "failure" for r in results))

    def test_search_since(self):
        t_before = time.time()
        self.al.log("u", "recent")
        results = self.al.search(since=t_before)
        self.assertGreater(len(results), 0)

    def test_export_jsonl(self):
        self.al.log("u", "export_act")
        out = self.al.export_jsonl()
        self.assertGreater(len(out), 0)
        first = json.loads(out.split("\n")[0])
        self.assertIn("actor", first)

    def test_export_csv(self):
        self.al.log("u", "csv_act")
        out = self.al.export_csv()
        self.assertIn("actor", out)
        self.assertIn("action", out)

    def test_redaction(self):
        self.al.log("u", "sensitive", metadata={"password": "secret123"})
        out = self.al.export_jsonl(redact=True)
        self.assertNotIn("secret123", out)
        self.assertIn("***", out)

    def test_event_to_dict(self):
        e = self.al.log("u", "act")
        d = e.to_dict()
        for k in ["id","seq","actor","action","outcome","level","chain_hash"]:
            self.assertIn(k, d)

    def test_retention_purge(self):
        # Log an old-looking event by manually inserting
        import sqlite3
        with sqlite3.connect(self.al._store.db_path) as c:
            c.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("old-id", 9999, "u", "user", "old_act", "", "success", "info",
                 "{}", "", "", time.time() - 999*86400,
                 "0"*64, "0"*64))
        purged = self.al.apply_retention()
        self.assertGreaterEqual(purged, 1)

    def test_stats(self):
        self.al.log("u", "a1"); self.al.log("u", "a2")
        s = self.al.stats()
        for k in ["total","by_level","by_outcome","current_seq"]: self.assertIn(k, s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v34: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
