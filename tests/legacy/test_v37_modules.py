"""OMNI AGENT v37: CacheManager, FeatureFlags, AccessControl, WorkflowEngine"""
import json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# CACHE MANAGER
# ════════════════════════════════════════════════════════
class TestCacheManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.cache_manager import CacheManager, EvictionPolicy, WritePolicy
        self.cm = CacheManager(db_path=os.path.join(td,"cm.db"))
        self.EP = EvictionPolicy; self.WP = WritePolicy

    def test_set_and_get(self):
        self.cm.set("k1", "value1")
        self.assertEqual(self.cm.get("k1"), "value1")

    def test_get_miss_returns_none(self):
        self.assertIsNone(self.cm.get("missing"))

    def test_ttl_expiry(self):
        self.cm.set("k_ttl", "val", ttl_s=0.01)
        time.sleep(0.02)
        self.assertIsNone(self.cm.get("k_ttl"))

    def test_ttl_not_expired(self):
        self.cm.set("k_ttl2", "val2", ttl_s=60)
        self.assertEqual(self.cm.get("k_ttl2"), "val2")

    def test_delete(self):
        self.cm.set("k_del", "v")
        ok = self.cm.delete("k_del")
        self.assertTrue(ok)
        self.assertIsNone(self.cm.get("k_del"))

    def test_delete_nonexistent(self):
        self.assertFalse(self.cm.delete("ghost"))

    def test_namespace_isolation(self):
        self.cm.create_namespace("ns_a"); self.cm.create_namespace("ns_b")
        self.cm.set("k", "a_val", namespace="ns_a")
        self.cm.set("k", "b_val", namespace="ns_b")
        self.assertEqual(self.cm.get("k","ns_a"), "a_val")
        self.assertEqual(self.cm.get("k","ns_b"), "b_val")

    def test_lru_eviction(self):
        self.cm.create_namespace("lru", max_size=3,
                                  eviction=self.EP.LRU)
        for i in range(4):
            self.cm.set(f"k{i}", i, namespace="lru")
        # k0 should be evicted (least recently used)
        self.assertIsNone(self.cm.get("k0", "lru"))

    def test_lfu_eviction(self):
        self.cm.create_namespace("lfu", max_size=3,
                                  eviction=self.EP.LFU)
        for i in range(3):
            self.cm.set(f"k{i}", i, namespace="lfu")
        # Access k1 and k2 more than k0
        for _ in range(5): self.cm.get("k1","lfu")
        for _ in range(5): self.cm.get("k2","lfu")
        self.cm.set("k3", 3, namespace="lfu")
        # k0 should be evicted (least frequent)
        self.assertIsNone(self.cm.get("k0","lfu"))

    def test_hit_miss_stats(self):
        self.cm.set("sk", "sv")
        self.cm.get("sk")   # hit
        self.cm.get("sk")   # hit
        self.cm.get("miss") # miss
        s = self.cm.stats("default")
        self.assertEqual(s["hits"], 2)
        self.assertEqual(s["misses"], 1)

    def test_hit_rate(self):
        self.cm.set("hk", "hv")
        for _ in range(3): self.cm.get("hk")
        self.cm.get("nope")
        s = self.cm.stats("default")
        self.assertGreater(s["hit_rate"], 0)

    def test_get_or_set(self):
        loader_calls = [0]
        def loader():
            loader_calls[0] += 1
            return "loaded_value"
        val1 = self.cm.get_or_set("gos_k", loader, ttl_s=60)
        val2 = self.cm.get_or_set("gos_k", loader, ttl_s=60)
        self.assertEqual(val1, "loaded_value")
        self.assertEqual(val2, "loaded_value")
        self.assertEqual(loader_calls[0], 1)  # loader called only once

    def test_delete_prefix(self):
        for i in range(5):
            self.cm.set(f"user:{i}", i)
        n = self.cm.delete_prefix("user:")
        self.assertEqual(n, 5)
        self.assertIsNone(self.cm.get("user:0"))

    def test_delete_by_tag(self):
        self.cm.set("t1", "v1", tags={"group_a"})
        self.cm.set("t2", "v2", tags={"group_a"})
        self.cm.set("t3", "v3", tags={"group_b"})
        n = self.cm.delete_tag("group_a")
        self.assertEqual(n, 2)
        self.assertIsNone(self.cm.get("t1"))
        self.assertEqual(self.cm.get("t3"), "v3")

    def test_get_many(self):
        self.cm.set("gm1","a"); self.cm.set("gm2","b")
        result = self.cm.get_many(["gm1","gm2","gm3"])
        self.assertEqual(result["gm1"], "a")
        self.assertEqual(result["gm2"], "b")
        self.assertIsNone(result["gm3"])

    def test_set_many(self):
        self.cm.set_many({"sm1":1,"sm2":2,"sm3":3})
        self.assertEqual(self.cm.get("sm1"), 1)
        self.assertEqual(self.cm.get("sm3"), 3)

    def test_flush_namespace(self):
        for i in range(5): self.cm.set(f"fl{i}", i)
        n = self.cm.flush()
        self.assertEqual(n, 5)
        self.assertIsNone(self.cm.get("fl0"))

    def test_write_through_backing(self):
        backing = {}
        self.cm.create_namespace("wt",
                                  write_policy=self.WP.WRITE_THROUGH,
                                  backing_set=lambda k,v: backing.__setitem__(k,v))
        self.cm.set("bk1","bv1",namespace="wt")
        self.assertIn("bk1", backing)

    def test_write_back_dirty(self):
        self.cm.create_namespace("wb", write_policy=self.WP.WRITE_BACK)
        entry = self.cm.set("wb_k","wb_v",namespace="wb")
        self.assertTrue(entry.dirty)

    def test_sweep_expired(self):
        for i in range(3):
            self.cm.set(f"exp{i}","v",ttl_s=0.01)
        time.sleep(0.02)
        n = self.cm.sweep()
        self.assertGreaterEqual(n, 3)

    def test_stats_all_namespaces(self):
        s = self.cm.stats()
        self.assertIn("default", s)

# ════════════════════════════════════════════════════════
# FEATURE FLAGS
# ════════════════════════════════════════════════════════
class TestFeatureFlags(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.feature_flags import FeatureFlags, FlagType, Variant, SegmentRule, RuleOp
        self.ff = FeatureFlags(db_path=os.path.join(td,"ff.db"))
        self.FT = FlagType; self.V = Variant
        self.SR = SegmentRule; self.RO = RuleOp

    def test_boolean_flag_enabled(self):
        self.ff.define("f1", self.FT.BOOLEAN, enabled=True)
        self.assertTrue(self.ff.is_enabled("f1","user1"))

    def test_boolean_flag_disabled(self):
        self.ff.define("f2", self.FT.BOOLEAN, enabled=False)
        self.assertFalse(self.ff.is_enabled("f2","user1"))

    def test_kill_switch(self):
        self.ff.define("f3", self.FT.BOOLEAN, enabled=True)
        self.ff.kill("f3")
        self.assertFalse(self.ff.is_enabled("f3","user1"))

    def test_revive_after_kill(self):
        self.ff.define("f4", self.FT.BOOLEAN, enabled=True)
        self.ff.kill("f4"); self.ff.revive("f4")
        self.assertTrue(self.ff.is_enabled("f4","user1"))

    def test_percentage_deterministic(self):
        self.ff.define("pct", self.FT.PERCENTAGE, percentage=50.0)
        r1 = self.ff.is_enabled("pct","user_abc")
        r2 = self.ff.is_enabled("pct","user_abc")
        self.assertEqual(r1, r2)

    def test_percentage_100_everyone_in(self):
        self.ff.define("pct100", self.FT.PERCENTAGE, percentage=100.0)
        for uid in ["a","b","c","d","e"]:
            self.assertTrue(self.ff.is_enabled("pct100", uid))

    def test_percentage_0_noone_in(self):
        self.ff.define("pct0", self.FT.PERCENTAGE, percentage=0.0)
        for uid in ["a","b","c","d","e"]:
            self.assertFalse(self.ff.is_enabled("pct0", uid))

    def test_set_percentage(self):
        self.ff.define("pct_change", self.FT.PERCENTAGE, percentage=10.0)
        self.ff.set_percentage("pct_change", 90.0)
        self.assertEqual(self.ff._flags["pct_change"].percentage, 90.0)

    def test_variant_assignment(self):
        self.ff.define("ab", self.FT.VARIANT,
                        variants=[self.V("control",50), self.V("treatment",50)])
        v = self.ff.get_variant("ab","user1")
        self.assertIn(v, ["control","treatment"])

    def test_variant_deterministic(self):
        self.ff.define("ab2", self.FT.VARIANT,
                        variants=[self.V("A",50), self.V("B",50)])
        v1 = self.ff.get_variant("ab2","user_xyz")
        v2 = self.ff.get_variant("ab2","user_xyz")
        self.assertEqual(v1, v2)

    def test_user_override(self):
        self.ff.define("ov", self.FT.BOOLEAN, enabled=False)
        self.ff.set_override("ov","alice",True)
        self.assertTrue(self.ff.is_enabled("ov","alice"))
        self.assertFalse(self.ff.is_enabled("ov","bob"))

    def test_segment_rule_eq(self):
        self.ff.define("seg", self.FT.BOOLEAN, enabled=True,
                        rules=[self.SR("plan", self.RO.EQ, "premium")])
        self.assertTrue(self.ff.is_enabled("seg","u1",{"plan":"premium"}))
        self.assertFalse(self.ff.is_enabled("seg","u2",{"plan":"free"}))

    def test_segment_rule_in(self):
        self.ff.define("seg2", self.FT.BOOLEAN, enabled=True,
                        rules=[self.SR("country", self.RO.IN, ["US","CA"])])
        self.assertTrue(self.ff.is_enabled("seg2","u1",{"country":"US"}))
        self.assertFalse(self.ff.is_enabled("seg2","u2",{"country":"UK"}))

    def test_dependency_blocks(self):
        self.ff.define("parent_f", self.FT.BOOLEAN, enabled=False)
        self.ff.define("child_f",  self.FT.BOOLEAN, enabled=True,
                        depends_on="parent_f")
        self.assertFalse(self.ff.is_enabled("child_f","u1"))

    def test_dependency_passes(self):
        self.ff.define("parent2", self.FT.BOOLEAN, enabled=True)
        self.ff.define("child2",  self.FT.BOOLEAN, enabled=True,
                        depends_on="parent2")
        self.assertTrue(self.ff.is_enabled("child2","u1"))

    def test_unknown_flag_returns_false(self):
        self.assertFalse(self.ff.is_enabled("no_such_flag","u1"))

    def test_get_value_with_default(self):
        val = self.ff.get_value("missing_flag","u1",default="fallback")
        self.assertEqual(val,"fallback")

    def test_evaluate_all(self):
        self.ff.define("ea1",self.FT.BOOLEAN,enabled=True)
        self.ff.define("ea2",self.FT.BOOLEAN,enabled=False)
        result = self.ff.evaluate_all("u1")
        self.assertIn("ea1",result); self.assertIn("ea2",result)

    def test_evaluation_reason(self):
        self.ff.define("reason_f",self.FT.BOOLEAN,enabled=True)
        ev = self.ff.evaluate("reason_f","u1")
        self.assertEqual(ev.reason,"enabled")

    def test_stats(self):
        self.ff.define("stat_f",self.FT.BOOLEAN)
        s = self.ff.stats()
        for k in ["flags","exposures","in_memory"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# ACCESS CONTROL
# ════════════════════════════════════════════════════════
class TestAccessControl(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.access_control import (AccessControl, Permission, Role,
                                           Policy, Effect, Condition, Subject)
        self.ac = AccessControl(db_path=os.path.join(td,"ac.db"))
        self.Perm = Permission; self.Pol = Policy; self.Eff = Effect
        self.Cond = Condition

    def test_rbac_allow(self):
        self.ac.define_role("reader", permissions=[self.Perm("read","*")])
        self.ac.add_subject("alice", roles=["reader"])
        self.assertTrue(self.ac.can("alice","read","docs/1"))

    def test_rbac_deny_no_permission(self):
        self.ac.define_role("reader2", permissions=[self.Perm("read","*")])
        self.ac.add_subject("bob", roles=["reader2"])
        self.assertFalse(self.ac.can("bob","write","docs/1"))

    def test_wildcard_action(self):
        self.ac.define_role("admin", permissions=[self.Perm("*","*")])
        self.ac.add_subject("admin_user", roles=["admin"])
        self.assertTrue(self.ac.can("admin_user","delete","anything"))

    def test_wildcard_resource(self):
        self.ac.define_role("writer", permissions=[self.Perm("write","*")])
        self.ac.add_subject("writer_user", roles=["writer"])
        self.assertTrue(self.ac.can("writer_user","write","any/resource"))

    def test_specific_resource_permission(self):
        self.ac.define_role("doc_reader",
                             permissions=[self.Perm("read","documents")])
        self.ac.add_subject("dr_user", roles=["doc_reader"])
        self.assertTrue(self.ac.can("dr_user","read","documents"))
        self.assertFalse(self.ac.can("dr_user","read","invoices"))

    def test_role_inheritance(self):
        self.ac.define_role("base_r", permissions=[self.Perm("read","*")])
        self.ac.define_role("extended", parents=["base_r"],
                             permissions=[self.Perm("write","*")])
        self.ac.add_subject("ext_user", roles=["extended"])
        self.assertTrue(self.ac.can("ext_user","read","anything"))
        self.assertTrue(self.ac.can("ext_user","write","anything"))

    def test_assign_revoke_role(self):
        self.ac.define_role("temp_role", permissions=[self.Perm("read","x")])
        self.ac.add_subject("temp_user")
        self.ac.assign_role("temp_user","temp_role")
        self.assertTrue(self.ac.can("temp_user","read","x"))
        self.ac.remove_role("temp_user","temp_role")
        self.assertFalse(self.ac.can("temp_user","read","x"))

    def test_owner_can_read_write(self):
        self.ac.add_subject("owner_u")
        self.ac.add_resource("doc:99", owner_id="owner_u")
        self.assertTrue(self.ac.can("owner_u","read","doc:99"))
        self.assertTrue(self.ac.can("owner_u","write","doc:99"))

    def test_non_owner_denied(self):
        self.ac.add_subject("other_u")
        self.ac.add_resource("doc:100", owner_id="owner2")
        self.assertFalse(self.ac.can("other_u","delete","doc:100"))

    def test_policy_allow(self):
        self.ac.add_subject("pol_u")
        self.ac.add_policy(self.Pol("allow_all_reads",self.Eff.ALLOW,
                                     subjects=["pol_u"],
                                     actions=["read"]))
        self.assertTrue(self.ac.can("pol_u","read","anything"))

    def test_policy_deny_wins(self):
        self.ac.define_role("full_r2", permissions=[self.Perm("*","*")])
        self.ac.add_subject("deny_u", roles=["full_r2"])
        self.ac.add_policy(self.Pol("deny_delete",self.Eff.DENY,
                                     subjects=["deny_u"],
                                     actions=["delete"],
                                     priority=10))
        self.assertFalse(self.ac.can("deny_u","delete","any"))
        self.assertTrue(self.ac.can("deny_u","read","any"))

    def test_policy_condition(self):
        self.ac.add_subject("cond_u", attributes={"dept":"engineering"})
        self.ac.add_policy(self.Pol("eng_only",self.Eff.ALLOW,
                                     subjects=["cond_u"],
                                     actions=["deploy"],
                                     conditions=[
                                         self.Cond("dept","eq","engineering",
                                                    source="subject")]))
        self.assertTrue(self.ac.can("cond_u","deploy","prod"))

    def test_policy_condition_fails(self):
        self.ac.add_subject("wrong_u", attributes={"dept":"marketing"})
        self.ac.add_policy(self.Pol("eng_only2",self.Eff.ALLOW,
                                     subjects=["wrong_u"],
                                     actions=["deploy"],
                                     conditions=[
                                         self.Cond("dept","eq","engineering",
                                                    source="subject")]))
        self.assertFalse(self.ac.can("wrong_u","deploy","prod"))

    def test_default_deny_unknown_subject(self):
        self.assertFalse(self.ac.can("unknown_nobody","read","anything"))

    def test_explain_returns_reason(self):
        self.ac.define_role("xplr", permissions=[self.Perm("read","*")])
        self.ac.add_subject("xplain_u", roles=["xplr"])
        result = self.ac.explain("xplain_u","read","docs")
        self.assertIn("decision",result); self.assertIn("reason",result)

    def test_subject_permissions_list(self):
        self.ac.define_role("sr", permissions=[self.Perm("read","*"),
                                                self.Perm("write","docs")])
        self.ac.add_subject("sp_u", roles=["sr"])
        perms = self.ac.subject_permissions("sp_u")
        self.assertIn("read:*", perms)

    def test_decision_caching(self):
        self.ac.define_role("cache_r", permissions=[self.Perm("read","*")])
        self.ac.add_subject("cache_u", roles=["cache_r"])
        self.ac.can("cache_u","read","x")
        # Second call should hit cache
        self.ac.can("cache_u","read","x")
        self.assertGreater(len(self.ac._cache), 0)

    def test_cache_invalidated_on_role_change(self):
        self.ac.define_role("temp_r2", permissions=[self.Perm("read","*")])
        self.ac.add_subject("ci_u", roles=["temp_r2"])
        self.ac.can("ci_u","read","x")
        self.ac.remove_role("ci_u","temp_r2")
        key = self.ac._cache_key("ci_u","read","x")
        self.assertNotIn(key, self.ac._cache)

    def test_stats(self):
        s = self.ac.stats()
        for k in ["roles","subjects","policies","cache_size"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# WORKFLOW ENGINE
# ════════════════════════════════════════════════════════
class TestWorkflowEngine(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.workflow_engine import (WorkflowEngine, WorkflowDefinition,
                                            State, Transition, WFStatus)
        self.WE = WorkflowEngine; self.WD = WorkflowDefinition
        self.St = State; self.Tr = Transition; self.WS = WFStatus
        self.engine = WorkflowEngine(db_path=os.path.join(td,"wf.db"))
        self._build_order_wf()

    def _build_order_wf(self):
        wf = self.WD("order")
        wf.add_state(self.St("pending", is_initial=True))
        wf.add_state(self.St("paid"))
        wf.add_state(self.St("shipped"))
        wf.add_state(self.St("delivered", is_terminal=True))
        wf.add_state(self.St("cancelled", is_terminal=True))
        wf.add_transition(self.Tr("pending","paid","pay"))
        wf.add_transition(self.Tr("paid","shipped","ship"))
        wf.add_transition(self.Tr("shipped","delivered","deliver"))
        wf.add_transition(self.Tr("pending","cancelled","cancel"))
        wf.add_transition(self.Tr("paid","cancelled","cancel"))
        self.engine.register(wf)

    def test_start_workflow(self):
        inst = self.engine.start("order")
        self.assertEqual(inst.current_state,"pending")
        self.assertEqual(inst.status, self.WS.ACTIVE)

    def test_send_event(self):
        inst = self.engine.start("order")
        inst = self.engine.send(inst.id,"pay")
        self.assertEqual(inst.current_state,"paid")

    def test_multi_step_transition(self):
        inst = self.engine.start("order")
        self.engine.send(inst.id,"pay")
        self.engine.send(inst.id,"ship")
        inst = self.engine.send(inst.id,"deliver")
        self.assertEqual(inst.current_state,"delivered")
        self.assertEqual(inst.status, self.WS.COMPLETED)

    def test_terminal_state_completes(self):
        inst = self.engine.start("order")
        inst = self.engine.send(inst.id,"cancel")
        self.assertEqual(inst.status, self.WS.COMPLETED)

    def test_guard_blocks_transition(self):
        wf = self.WD("guarded")
        wf.add_state(self.St("start", is_initial=True))
        wf.add_state(self.St("end", is_terminal=True))
        wf.add_transition(self.Tr("start","end","proceed",
                                    guard=lambda ctx: ctx.get("ok")==True))
        self.engine.register(wf)
        inst = self.engine.start("guarded")
        inst = self.engine.send(inst.id,"proceed")
        self.assertEqual(inst.current_state,"start")  # guard blocked

    def test_guard_allows_with_context(self):
        wf = self.WD("guarded2")
        wf.add_state(self.St("start", is_initial=True))
        wf.add_state(self.St("end", is_terminal=True))
        wf.add_transition(self.Tr("start","end","proceed",
                                    guard=lambda ctx: ctx.get("ok")==True))
        self.engine.register(wf)
        inst = self.engine.start("guarded2",{"ok":True})
        inst = self.engine.send(inst.id,"proceed")
        self.assertEqual(inst.current_state,"end")

    def test_action_updates_context(self):
        wf = self.WD("action_wf")
        wf.add_state(self.St("a", is_initial=True))
        wf.add_state(self.St("b", is_terminal=True))
        def set_flag(ctx):
            ctx["flag"] = True; return ctx
        wf.add_transition(self.Tr("a","b","go",action=set_flag))
        self.engine.register(wf)
        inst = self.engine.start("action_wf")
        inst = self.engine.send(inst.id,"go")
        self.assertTrue(inst.context.get("flag"))

    def test_history_recorded(self):
        inst = self.engine.start("order")
        self.engine.send(inst.id,"pay")
        self.engine.send(inst.id,"ship")
        inst = self.engine.status(inst.id)
        self.assertGreaterEqual(len(inst.history), 2)

    def test_history_entry_fields(self):
        inst = self.engine.start("order")
        self.engine.send(inst.id,"pay")
        h = inst.history[0]
        self.assertEqual(h.from_state,"pending")
        self.assertEqual(h.to_state,"paid")
        self.assertEqual(h.event,"pay")

    def test_context_update_on_send(self):
        inst = self.engine.start("order")
        inst = self.engine.send(inst.id,"pay",{"amount":99.99})
        self.assertEqual(inst.context.get("amount"),99.99)

    def test_invalid_event_no_change(self):
        inst = self.engine.start("order")
        inst = self.engine.send(inst.id,"ship")  # invalid from pending
        self.assertEqual(inst.current_state,"pending")

    def test_entry_action_called(self):
        entries = []
        wf = self.WD("entry_wf")
        wf.add_state(self.St("start",is_initial=True,
                               entry_action=lambda ctx: entries.append("start") or ctx))
        wf.add_state(self.St("done",is_terminal=True,
                               entry_action=lambda ctx: entries.append("done") or ctx))
        wf.add_transition(self.Tr("start","done","go"))
        self.engine.register(wf)
        inst = self.engine.start("entry_wf")
        self.engine.send(inst.id,"go")
        self.assertIn("start",entries); self.assertIn("done",entries)

    def test_on_transition_hook(self):
        events = []
        wf = self.engine._defs.get("order")
        if wf:
            wf.on_transition(lambda f,t,e,ctx: events.append(e))
        inst = self.engine.start("order")
        self.engine.send(inst.id,"pay")
        self.assertIn("pay",events)

    def test_cancel_instance(self):
        inst = self.engine.start("order")
        self.engine.cancel(inst.id)
        inst2 = self.engine.status(inst.id)
        self.assertEqual(inst2.status, self.WS.CANCELLED)

    def test_available_events(self):
        inst = self.engine.start("order")
        events = self.engine.available_events(inst.id)
        self.assertIn("pay",events)
        self.assertIn("cancel",events)

    def test_can_transition(self):
        inst = self.engine.start("order")
        self.assertTrue(self.engine.can_transition(inst.id,"pay"))
        self.assertFalse(self.engine.can_transition(inst.id,"ship"))

    def test_mermaid_export(self):
        diagram = self.engine.mermaid("order")
        self.assertIn("stateDiagram-v2",diagram)
        self.assertIn("pending",diagram)

    def test_unknown_definition_raises(self):
        with self.assertRaises(KeyError):
            self.engine.start("no_such_wf")

    def test_instance_to_dict(self):
        inst = self.engine.start("order")
        d = inst.to_dict()
        for k in ["id","definition","current_state","status"]: self.assertIn(k,d)

    def test_stats(self):
        self.engine.start("order")
        s = self.engine.stats()
        for k in ["definitions","in_memory"]: self.assertIn(k,s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v37: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
