"""OMNI AGENT v59: CacheManagerV2, AlertManager, ResponseStreamerV2, PersonaEngineV2"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# CACHE MANAGER V2
# ════════════════════════════════════════════════════════
class TestCacheManagerV2(unittest.TestCase):
    def setUp(self):
        from agent.cache_manager_v2 import CacheManagerV2, EvictionPolicy
        self.cm = CacheManagerV2(max_l1_size=10, db_path=":memory:",
                                  eviction_policy=EvictionPolicy.LRU)

    def test_set_and_get(self):
        self.cm.set("k1", "val1")
        self.assertEqual(self.cm.get("k1"), "val1")

    def test_miss_returns_default(self):
        self.assertIsNone(self.cm.get("missing"))
        self.assertEqual(self.cm.get("missing", default="x"), "x")

    def test_ttl_expiry(self):
        self.cm.set("exp_k", "val", ttl_s=0.01)
        time.sleep(0.02)
        self.assertIsNone(self.cm.get("exp_k"))

    def test_delete(self):
        self.cm.set("del_k", "v")
        self.assertTrue(self.cm.delete("del_k"))
        self.assertIsNone(self.cm.get("del_k"))

    def test_partition_isolation(self):
        self.cm.set("key", "v1", partition="p1")
        self.cm.set("key", "v2", partition="p2")
        self.assertEqual(self.cm.get("key", "p1"), "v1")
        self.assertEqual(self.cm.get("key", "p2"), "v2")

    def test_invalidate_partition(self):
        self.cm.set("a", 1, partition="wipe")
        self.cm.set("b", 2, partition="wipe")
        n = self.cm.invalidate_partition("wipe")
        self.assertEqual(n, 2)
        self.assertIsNone(self.cm.get("a", "wipe"))

    def test_lru_eviction(self):
        from agent.cache_manager_v2 import CacheManagerV2, EvictionPolicy
        cm = CacheManagerV2(max_l1_size=3, eviction_policy=EvictionPolicy.LRU,
                             write_through=False, db_path=":memory:")
        cm.set("a", 1); cm.set("b", 2); cm.set("c", 3)
        cm.get("a"); cm.get("a")  # make 'a' recently used
        cm.set("d", 4)  # should evict 'b' (LRU)
        self.assertIsNotNone(cm.get("a"))
        self.assertIsNotNone(cm.get("d"))

    def test_lfu_eviction(self):
        from agent.cache_manager_v2 import CacheManagerV2, EvictionPolicy
        cm = CacheManagerV2(max_l1_size=3, eviction_policy=EvictionPolicy.LFU,
                             write_through=False, db_path=":memory:")
        cm.set("a", 1); cm.set("b", 2); cm.set("c", 3)
        cm.get("a"); cm.get("a"); cm.get("b")  # c has lowest count
        cm.set("d", 4)
        self.assertIsNotNone(cm.get("a"))

    def test_fifo_eviction(self):
        from agent.cache_manager_v2 import CacheManagerV2, EvictionPolicy
        cm = CacheManagerV2(max_l1_size=3, eviction_policy=EvictionPolicy.FIFO,
                             write_through=False, db_path=":memory:")
        cm.set("first", 1); cm.set("second", 2); cm.set("third", 3)
        cm.set("fourth", 4)  # evicts "first"
        self.assertIsNone(cm.get("first"))
        self.assertIsNotNone(cm.get("fourth"))

    def test_write_through_to_l2(self):
        self.cm.set("wt_key", "wt_val")
        # Evict from L1 then check L2
        self.cm._l1.clear()
        val = self.cm.get("wt_key")
        self.assertEqual(val, "wt_val")

    def test_l2_promotion_to_l1(self):
        self.cm.set("promote_k", "pval")
        self.cm._l1.pop(self.cm._full_key("promote_k", "default"), None)
        v = self.cm.get("promote_k")
        self.assertEqual(v, "pval")
        self.assertIn(self.cm._full_key("promote_k", "default"), self.cm._l1)

    def test_loader_on_miss(self):
        self.cm.register_loader("db", lambda k: f"loaded_{k}")
        v = self.cm.get("missing_key", partition="db")
        self.assertEqual(v, "loaded_missing_key")

    def test_set_many_get_many(self):
        self.cm.set_many({"x": 10, "y": 20})
        result = self.cm.get_many(["x", "y", "z"])
        self.assertEqual(result["x"], 10)
        self.assertEqual(result["y"], 20)
        self.assertIsNone(result["z"])

    def test_exists(self):
        self.cm.set("ex_k", "v")
        self.assertTrue(self.cm.exists("ex_k"))
        self.assertFalse(self.cm.exists("no_key"))

    def test_ttl_query(self):
        self.cm.set("ttl_k", "v", ttl_s=60)
        t = self.cm.ttl("ttl_k")
        self.assertIsNotNone(t)
        self.assertGreater(t, 50)

    def test_clear_all(self):
        self.cm.set("c1", 1); self.cm.set("c2", 2)
        self.cm.clear()
        self.assertIsNone(self.cm.get("c1"))

    def test_stats(self):
        self.cm.set("sk", "sv")
        self.cm.get("sk")
        self.cm.get("missing")
        s = self.cm.stats()
        self.assertEqual(s["hits"], 1)
        self.assertEqual(s["misses"], 1)
        self.assertEqual(s["writes"], 1)

# ════════════════════════════════════════════════════════
# ALERT MANAGER
# ════════════════════════════════════════════════════════
class TestAlertManager(unittest.TestCase):
    def setUp(self):
        from agent.alert_manager import AlertManager
        self.am = AlertManager(db_path=":memory:")
        self.fired = []
        self.am.add_channel("test_ch",
                            lambda a: self.fired.append(a),
                            channel_id="test_ch")

    def test_rule_fires(self):
        from agent.alert_manager import Severity
        self.am.add_rule("cpu_high",
                         condition=lambda ctx: ctx.get("cpu", 0) > 0.9,
                         severity=Severity.HIGH, cooldown_s=0)
        alerts = self.am.evaluate({"cpu": 0.95})
        self.assertGreater(len(alerts), 0)

    def test_rule_does_not_fire_below_threshold(self):
        self.am.add_rule("cpu_high",
                         condition=lambda ctx: ctx.get("cpu", 0) > 0.9,
                         cooldown_s=0)
        alerts = self.am.evaluate({"cpu": 0.5})
        self.assertEqual(len(alerts), 0)

    def test_channel_notified(self):
        from agent.alert_manager import Severity
        self.am.add_rule("notify_test",
                         condition=lambda ctx: True,
                         severity=Severity.HIGH, cooldown_s=0)
        self.am.evaluate({})
        self.assertGreater(len(self.fired), 0)

    def test_severity_filter_on_channel(self):
        from agent.alert_manager import AlertManager, Severity
        am = AlertManager(db_path=":memory:")
        high_only = []
        am.add_channel("high_ch", lambda a: high_only.append(a),
                       min_severity=Severity.HIGH, channel_id="hch")
        am.add_rule("low_alert", condition=lambda ctx: True,
                    severity=Severity.LOW, cooldown_s=0)
        am.evaluate({})
        self.assertEqual(len(high_only), 0)

    def test_cooldown_respected(self):
        self.am.add_rule("cooldown_test",
                         condition=lambda ctx: True,
                         cooldown_s=999)
        self.am.evaluate({})
        pre = len(self.fired)
        self.am.evaluate({})
        self.assertEqual(len(self.fired), pre)

    def test_silence_suppresses(self):
        self.am.add_rule("silenced_rule",
                         condition=lambda ctx: True,
                         cooldown_s=0,
                         labels={"env": "staging"})
        self.am.add_silence({"env": "staging"}, duration_s=3600)
        self.am.evaluate({})
        staging_fired = [a for a in self.fired
                         if a.labels.get("env") == "staging"]
        self.assertEqual(len(staging_fired), 0)

    def test_expire_silence(self):
        s = self.am.add_silence({"env": "prod"}, duration_s=3600)
        self.assertTrue(s.is_active)
        self.am.expire_silence(s.silence_id)
        self.assertFalse(s.is_active)

    def test_resolve_alert(self):
        from agent.alert_manager import AlertStatus
        rule = self.am.add_rule("resolve_me",
                                condition=lambda ctx: True,
                                cooldown_s=0)
        self.am.evaluate({})
        resolved = self.am.resolve_rule(rule.rule_id)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.status, AlertStatus.RESOLVED)

    def test_pending_threshold(self):
        rule = self.am.add_rule("pending_rule",
                                condition=lambda ctx: True,
                                cooldown_s=0, pending_threshold=3)
        self.am.evaluate({})
        self.am.evaluate({})
        before = len(self.am.active_alerts())
        self.am.evaluate({})   # 3rd trigger → FIRING
        after = len(self.am.active_alerts())
        self.assertGreaterEqual(after, before)

    def test_active_alerts_list(self):
        from agent.alert_manager import Severity
        self.am.add_rule("active_test",
                         condition=lambda ctx: True,
                         severity=Severity.CRITICAL, cooldown_s=0)
        self.am.evaluate({})
        alerts = self.am.active_alerts()
        self.assertGreater(len(alerts), 0)

    def test_active_alerts_min_severity(self):
        from agent.alert_manager import Severity
        self.am.add_rule("low_r", condition=lambda ctx: True,
                         severity=Severity.LOW, cooldown_s=0)
        self.am.evaluate({})
        highs = self.am.active_alerts(min_severity=Severity.CRITICAL)
        self.assertEqual(len(highs), 0)

    def test_disable_rule(self):
        rule = self.am.add_rule("dis_rule",
                                condition=lambda ctx: True,
                                cooldown_s=0)
        self.am.disable_rule(rule.rule_id)
        self.am.evaluate({})
        self.assertEqual(len(self.am.active_alerts()), 0)

    def test_enable_rule(self):
        rule = self.am.add_rule("en_rule",
                                condition=lambda ctx: True,
                                cooldown_s=0)
        self.am.disable_rule(rule.rule_id)
        self.am.enable_rule(rule.rule_id)
        self.am.evaluate({})
        self.assertGreater(len(self.am.active_alerts()), 0)

    def test_alert_history(self):
        self.am.add_rule("hist_rule", condition=lambda ctx: True, cooldown_s=0)
        self.am.evaluate({})
        h = self.am.alert_history()
        self.assertGreater(len(h), 0)

    def test_stats(self):
        self.am.add_rule("stat_r", condition=lambda ctx: True, cooldown_s=0)
        self.am.evaluate({})
        s = self.am.stats()
        self.assertEqual(s["rules"], 1)
        self.assertGreater(s["total_fired"], 0)

# ════════════════════════════════════════════════════════
# RESPONSE STREAMER V2
# ════════════════════════════════════════════════════════
class TestResponseStreamerV2(unittest.TestCase):
    def setUp(self):
        from agent.response_streamer_v2 import ResponseStreamerV2
        self.rs = ResponseStreamerV2()

    def _gen(self, tokens):
        return iter(tokens)

    def test_basic_stream(self):
        from agent.response_streamer_v2 import StreamEvent
        chunks = list(self.rs.stream(self._gen(["hello", " ", "world"])))
        tokens = [c for c in chunks if c.event == StreamEvent.TOKEN]
        self.assertEqual(len(tokens), 3)

    def test_stream_ends_with_done(self):
        from agent.response_streamer_v2 import StreamEvent
        chunks = list(self.rs.stream(self._gen(["hi"])))
        self.assertEqual(chunks[-1].event, StreamEvent.DONE)

    def test_collect_assembles_string(self):
        result = self.rs.collect(self._gen(["Hello", " ", "World"]))
        self.assertEqual(result, "Hello World")

    def test_collect_chunks(self):
        chunks = self.rs.collect_chunks(self._gen(["a", "b"]))
        self.assertGreater(len(chunks), 0)

    def test_stream_text(self):
        from agent.response_streamer_v2 import StreamEvent
        chunks = list(self.rs.stream_text("Hello World", chunk_size=5))
        tokens = [c for c in chunks if c.event == StreamEvent.TOKEN]
        self.assertGreater(len(tokens), 0)

    def test_sse_format(self):
        sse_chunks = list(self.rs.stream_sse(self._gen(["test"])))
        self.assertTrue(any("event:" in s for s in sse_chunks))

    def test_token_map_transform(self):
        self.rs.add_token_map(lambda t: t.upper())
        result = self.rs.collect(self._gen(["hello"]))
        self.assertEqual(result, "HELLO")

    def test_token_filter_transform(self):
        from agent.response_streamer_v2 import StreamEvent
        self.rs.add_token_filter(lambda t: t != "bad")
        chunks = list(self.rs.stream(self._gen(["good", "bad", "good"])))
        tokens = [c for c in chunks if c.event == StreamEvent.TOKEN]
        self.assertEqual(len(tokens), 2)

    def test_redact_transform(self):
        self.rs.add_redact([r"\d{3}-\d{2}-\d{4}"])
        result = self.rs.collect(self._gen(["SSN: 123-45-6789"]))
        self.assertNotIn("123-45-6789", result)
        self.assertIn("REDACTED", result)

    def test_metadata_chunk_emitted(self):
        from agent.response_streamer_v2 import StreamEvent
        chunks = list(self.rs.stream(self._gen(["hi"]),
                                     metadata={"model": "gpt"}))
        meta = [c for c in chunks if c.event == StreamEvent.METADATA]
        self.assertEqual(len(meta), 1)
        self.assertEqual(meta[0].data["model"], "gpt")

    def test_stream_id_propagated(self):
        chunks = list(self.rs.stream(self._gen(["a"]), stream_id="test_sid"))
        self.assertTrue(all(c.stream_id == "test_sid" for c in chunks))

    def test_chunk_index_increments(self):
        from agent.response_streamer_v2 import StreamEvent
        chunks = list(self.rs.stream(self._gen(["a", "b", "c"])))
        tokens = [c for c in chunks if c.event == StreamEvent.TOKEN]
        indices = [c.index for c in tokens]
        self.assertEqual(indices, list(range(len(indices))))

    def test_error_chunk_on_exception(self):
        from agent.response_streamer_v2 import StreamEvent
        def bad_gen():
            yield "ok"
            raise RuntimeError("stream error")
        chunks = list(self.rs.stream(bad_gen()))
        has_error = any(c.event == StreamEvent.ERROR for c in chunks)
        self.assertTrue(has_error)

    def test_tool_call_events(self):
        from agent.response_streamer_v2 import StreamEvent
        items = [
            "Hello ",
            {"type": "tool_call", "name": "search"},
            {"type": "tool_result", "result": "data"},
            "World",
        ]
        chunks = list(self.rs.stream_with_tool_calls(iter(items)))
        types = {c.event for c in chunks}
        self.assertIn(StreamEvent.TOOL_CALL, types)
        self.assertIn(StreamEvent.TOOL_RESULT, types)

    def test_stats(self):
        list(self.rs.stream(self._gen(["x"])))
        s = self.rs.stats()
        self.assertEqual(s["total_streams"], 1)

# ════════════════════════════════════════════════════════
# PERSONA ENGINE V2
# ════════════════════════════════════════════════════════
class TestPersonaEngineV2(unittest.TestCase):
    def setUp(self):
        from agent.persona_engine_v2 import PersonaEngineV2
        self.pe = PersonaEngineV2(db_path=":memory:")

    def _create(self, name="TestBot", **kw):
        return self.pe.create_persona(name=name, **kw)

    def test_create_persona(self):
        p = self._create("Bot1")
        self.assertIsNotNone(p.persona_id)
        self.assertEqual(p.name, "Bot1")

    def test_get_persona(self):
        p = self._create("GetMe")
        got = self.pe.get_persona(p.persona_id)
        self.assertEqual(got.name, "GetMe")

    def test_find_by_name(self):
        self._create("FindMe")
        found = self.pe.find_by_name("FindMe")
        self.assertIsNotNone(found)

    def test_update_persona(self):
        p = self._create("UpdateMe")
        self.pe.update_persona(p.persona_id, description="Updated desc")
        self.assertEqual(p.description, "Updated desc")
        self.assertEqual(p.version, 2)

    def test_set_trait_new(self):
        p = self._create("TraitBot")
        self.pe.set_trait(p.persona_id, "friendliness", 0.9)
        self.assertAlmostEqual(p.get_trait_value("friendliness"), 0.9)

    def test_set_trait_update(self):
        p = self._create("TraitUpdate", traits=[
            {"name": "formal", "category": "communication", "value": 0.3}])
        self.pe.set_trait(p.persona_id, "formal", 0.8)
        self.assertAlmostEqual(p.get_trait_value("formal"), 0.8)

    def test_build_system_prompt(self):
        p = self._create("PromptBot",
                         base_prompt="You are helpful.",
                         traits=[{"name": "concise",
                                   "category": "verbosity",
                                   "value": 0.9,
                                   "prompt_fragment": "Be concise."}])
        prompt = self.pe.build_prompt(p.persona_id)
        self.assertIn("You are helpful.", prompt)
        self.assertIn("Be concise.", prompt)

    def test_clone_persona(self):
        p = self._create("Original")
        clone = self.pe.clone_persona(p.persona_id, "Clone")
        self.assertIsNotNone(clone)
        self.assertNotEqual(clone.persona_id, p.persona_id)
        self.assertEqual(clone.name, "Clone")
        self.assertEqual(clone.version, 1)

    def test_deactivate(self):
        p = self._create("Deactivate")
        self.pe.deactivate(p.persona_id)
        self.assertFalse(p.active)

    def test_list_active_only(self):
        p1 = self._create("Active1")
        p2 = self._create("Inactive")
        self.pe.deactivate(p2.persona_id)
        active = self.pe.list_personas(active_only=True)
        names = [x["name"] for x in active]
        self.assertIn("Active1", names)
        self.assertNotIn("Inactive", names)

    def test_list_by_tag(self):
        self._create("Tagged", tags=["ai", "bot"])
        self._create("Untagged")
        tagged = self.pe.list_personas(tag="ai")
        self.assertEqual(len(tagged), 1)

    def test_session_assignment(self):
        p = self._create("SessionBot")
        self.pe.assign_session("sess1", p.persona_id)
        got = self.pe.get_session_persona("sess1")
        self.assertEqual(got.persona_id, p.persona_id)

    def test_release_session(self):
        p = self._create("ReleaseBot")
        self.pe.assign_session("sess2", p.persona_id)
        self.pe.release_session("sess2")
        self.assertIsNone(self.pe.get_session_persona("sess2"))

    def test_blend_personas(self):
        p1 = self._create("P1", traits=[
            {"name": "formal", "category": "communication",
             "value": 0.8, "prompt_fragment": "Be formal."}])
        p2 = self._create("P2", traits=[
            {"name": "formal", "category": "communication",
             "value": 0.2, "prompt_fragment": "Be formal."}])
        blend = self.pe.create_blend("Mix",
                                     [(p1.persona_id, 1.0),
                                      (p2.persona_id, 1.0)])
        self.assertIsNotNone(blend)
        resolved = self.pe.resolve_blend(blend.blend_id)
        self.assertIsNotNone(resolved)
        formal_val = resolved.get_trait_value("formal")
        self.assertAlmostEqual(formal_val, 0.5, places=1)

    def test_ab_assignment_deterministic(self):
        p1 = self._create("AB1")
        p2 = self._create("AB2")
        self.pe.set_ab_pool([p1.persona_id, p2.persona_id])
        got1 = self.pe.get_ab_persona("user_abc")
        got2 = self.pe.get_ab_persona("user_abc")
        self.assertEqual(got1.persona_id, got2.persona_id)

    def test_consistency_check_conflict(self):
        p = self._create("Conflicted", traits=[
            {"name": "formal",  "category": "communication", "value": 0.9},
            {"name": "casual",  "category": "communication", "value": 0.9},
        ])
        issues = self.pe.check_consistency(p.persona_id)
        self.assertGreater(len(issues), 0)

    def test_consistency_check_no_conflict(self):
        p = self._create("Clean", traits=[
            {"name": "formal", "category": "communication", "value": 0.9},
        ])
        issues = self.pe.check_consistency(p.persona_id)
        self.assertEqual(len(issues), 0)

    def test_stats(self):
        self._create("StatsBot")
        s = self.pe.stats()
        self.assertGreater(s["personas"], 0)
        self.assertIn("blends", s)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v59: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
