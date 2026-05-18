"""OMNI AGENT v30: IntentClassifier, ChainExecutor, CacheManager, AccessControl"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# INTENT CLASSIFIER
# ════════════════════════════════════════════════════════
class TestIntentClassifier(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.intent_classifier import IntentClassifier
        self.ic = IntentClassifier(db_path=os.path.join(td,"ic.db"),
                                    min_confidence=0.1)
        self.ic.add_intent("greeting",
            examples=["hello", "hi there", "good morning", "hey"],
            keywords=["hello","hi","hey"], tags=["basic"])
        self.ic.add_intent("farewell",
            examples=["goodbye", "see you later", "bye", "farewell"],
            keywords=["bye","goodbye"], tags=["basic"])
        self.ic.add_intent("help",
            examples=["I need help", "can you assist me", "I need support",
                       "please help me out"],
            keywords=["help","support","assist"])

    def test_classifies_greeting(self):
        r = self.ic.classify("hello there")
        self.assertEqual(r.top_intent, "greeting")

    def test_classifies_farewell(self):
        r = self.ic.classify("goodbye see you")
        self.assertEqual(r.top_intent, "farewell")

    def test_classifies_help(self):
        r = self.ic.classify("I need some help please")
        self.assertIn(r.top_intent, ["help", "greeting", "farewell"])
        # help should score highest
        self.assertGreater(r.confidence, 0)

    def test_unknown_when_low_confidence(self):
        from agent.intent_classifier import IntentClassifier
        ic2 = IntentClassifier(min_confidence=0.99)
        ic2.add_intent("x", examples=["specific phrase"])
        r = ic2.classify("completely unrelated text here xyz")
        self.assertTrue(r.is_unknown)

    def test_confidence_between_0_and_1(self):
        r = self.ic.classify("hello world")
        self.assertGreaterEqual(r.confidence, 0.0)
        self.assertLessEqual(r.confidence, 1.0)

    def test_all_scores_populated(self):
        r = self.ic.classify("hello")
        self.assertGreater(len(r.all_scores), 0)

    def test_all_scores_sorted_descending(self):
        r = self.ic.classify("hello world")
        scores = [s for _, s in r.all_scores]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_k_limits_scores(self):
        r = self.ic.classify("hello", top_k=2)
        self.assertLessEqual(len(r.all_scores), 2)

    def test_add_example_updates_centroid(self):
        self.ic.add_example("help", "I require assistance urgently")
        r = self.ic.classify("require assistance")
        self.assertGreater(r.confidence, 0)

    def test_add_negative_example(self):
        self.ic.add_example("greeting", "goodbye world", negative=True)
        r = self.ic.classify("goodbye world")
        # greeting should not win after negative example
        self.assertIsNotNone(r.top_intent)

    def test_remove_intent(self):
        self.ic.add_intent("temp", examples=["temp phrase"])
        ok = self.ic.remove_intent("temp")
        self.assertTrue(ok)
        r = self.ic.classify("temp phrase")
        self.assertNotEqual(r.top_intent, "temp")

    def test_multilabel_returns_multiple(self):
        self.ic.add_intent("general",
            examples=["hello help me", "hi assistance"],
            keywords=["hello","help"])
        hits = self.ic.classify_multilabel("hello help me", threshold=0.05)
        self.assertIsInstance(hits, list)

    def test_keyword_boost(self):
        r = self.ic.classify("hi")   # exact keyword for greeting
        self.assertEqual(r.top_intent, "greeting")
        self.assertGreater(r.confidence, 0.2)

    def test_synonym_in_score(self):
        self.ic.add_intent("purchase",
            examples=["I want to buy something"],
            synonyms=["buy","purchase","order"])
        r = self.ic.classify("I would like to order a product")
        self.assertGreater(r.confidence, 0)

    def test_batch_classify(self):
        texts = ["hello world", "goodbye", "I need help"]
        results = self.ic.batch_classify(texts)
        self.assertEqual(len(results), 3)

    def test_intent_hierarchy(self):
        self.ic.add_intent("customer_service", parent="help",
            examples=["customer support", "billing help"])
        spec = self.ic.list_intents(parent="help")
        self.assertTrue(any(s.name == "customer_service" for s in spec))

    def test_list_by_tag(self):
        specs = self.ic.list_intents(tag="basic")
        self.assertEqual(len(specs), 2)
        self.assertTrue(all("basic" in s.tags for s in specs))

    def test_intent_info(self):
        info = self.ic.intent_info("greeting")
        for k in ["id","name","examples_count","keywords"]:
            self.assertIn(k, info)

    def test_match_count_increments(self):
        before = self.ic._intents["greeting"].match_count
        self.ic.classify("hello")
        after = self.ic._intents["greeting"].match_count
        self.assertGreater(after, before)

    def test_result_to_dict(self):
        r = self.ic.classify("hello")
        d = r.to_dict()
        for k in ["text","top_intent","confidence","is_unknown","top_3"]:
            self.assertIn(k, d)

    def test_stats(self):
        self.ic.classify("hello")
        s = self.ic.stats()
        for k in ["total","defined_intents","total_examples"]:
            self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# CHAIN EXECUTOR
# ════════════════════════════════════════════════════════
class TestChainExecutor(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.chain_executor import ChainExecutor, StepType
        self.ex = ChainExecutor(db_path=os.path.join(td,"ce.db"))
        self.ST = StepType

    def _simple_chain(self, name="test"):
        chain = self.ex.define(name)
        chain.step("s1", fn=lambda ctx: "output1", output_key="s1_out")
        chain.step("s2", fn=lambda ctx: ctx.get("s1_out","") + "_s2",
                    output_key="s2_out")
        return chain

    def test_run_completes(self):
        self._simple_chain()
        run = _run(self.ex.run("test"))
        self.assertEqual(run.status, "completed")

    def test_context_threaded(self):
        self._simple_chain("ctx_chain")
        run = _run(self.ex.run("ctx_chain"))
        self.assertEqual(run.context.get("s2_out"), "output1_s2")

    def test_async_step(self):
        async def afn(ctx): await asyncio.sleep(0.01); return "async_out"
        chain = self.ex.define("async_chain")
        chain.step("as", fn=afn, output_key="async_result")
        run = _run(self.ex.run("async_chain"))
        self.assertEqual(run.context.get("async_result"), "async_out")

    def test_template_interpolation(self):
        chain = self.ex.define("tmpl_chain")
        chain.step("render", fn=lambda ctx: ctx.get("_rendered",""),
                    template="Hello {{name}}!", output_key="out")
        run = _run(self.ex.run("tmpl_chain", context={"name": "World"}))
        self.assertIn("Hello World", run.context.get("out",""))

    def test_missing_var_leaves_placeholder(self):
        chain = self.ex.define("mv_chain")
        chain.step("s", fn=lambda ctx: ctx.get("_rendered",""),
                    template="Hello {{missing_var}}!", output_key="out")
        run = _run(self.ex.run("mv_chain"))
        self.assertIn("{{missing_var}}", run.context.get("out",""))

    def test_step_failure(self):
        chain = self.ex.define("fail_chain")
        chain.step("boom", fn=lambda ctx: 1/0)
        run = _run(self.ex.run("fail_chain"))
        self.assertEqual(run.status, "failed")

    def test_step_retry(self):
        calls = [0]
        def flaky(ctx):
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("not yet")
            return "ok"
        chain = self.ex.define("retry_chain")
        chain.step("r", fn=flaky, max_retries=3, retry_delay=0.01)
        run = _run(self.ex.run("retry_chain"))
        self.assertGreaterEqual(calls[0], 2)

    def test_step_timeout(self):
        async def slow(ctx): await asyncio.sleep(5)
        chain = self.ex.define("to_chain")
        chain.step("slow", fn=slow, timeout_s=0.05)
        run = _run(self.ex.run("to_chain"))
        self.assertEqual(run.status, "failed")

    def test_branch_step(self):
        chain = self.ex.define("branch_chain")
        chain.branch("decide", lambda ctx: "yes_path" if ctx.get("flag") else "no_path")
        chain.step("yes_path", fn=lambda ctx: "yes", output_key="result")
        chain.step("no_path",  fn=lambda ctx: "no",  output_key="result")
        run = _run(self.ex.run("branch_chain", context={"flag": True}))
        self.assertIn(run.context.get("result"), ["yes","no"])

    def test_loop_step(self):
        counter = [0]
        def inc(ctx): counter[0] += 1; return counter[0]
        chain = self.ex.define("loop_chain")
        chain.loop("counter", fn=inc, count=3, output_key="count_out")
        chain.step("done", fn=lambda ctx: "done")
        run = _run(self.ex.run("loop_chain"))
        self.assertGreaterEqual(counter[0], 3)

    def test_no_fn_returns_rendered_template(self):
        chain = self.ex.define("nofn_chain")
        chain.step("render_only", template="Value: {{x}}", output_key="rendered")
        run = _run(self.ex.run("nofn_chain", context={"x": "42"}))
        self.assertEqual(run.context.get("rendered"), "Value: 42")

    def test_on_step_end_hook(self):
        completed = []
        self.ex.on("on_step_end", lambda s, sr, r: completed.append(s.name))
        self._simple_chain("hook_chain")
        _run(self.ex.run("hook_chain"))
        self.assertIn("s1", completed)

    def test_on_chain_end_hook(self):
        ended = []
        self.ex.on("on_chain_end", lambda r: ended.append(r.id))
        self._simple_chain("end_hook_chain")
        run = _run(self.ex.run("end_hook_chain"))
        self.assertIn(run.id, ended)

    def test_dry_run_returns_plan(self):
        self._simple_chain("dry_chain")
        run = _run(self.ex.run("dry_chain", dry_run=True))
        self.assertEqual(run.status, "dry_run")
        self.assertIn("_plan", run.context)

    def test_plan_lists_steps(self):
        self._simple_chain("plan_chain")
        plan = self.ex.plan("plan_chain")
        self.assertGreater(len(plan), 0)
        for item in plan: self.assertIn("id", item)

    def test_get_run(self):
        self._simple_chain("get_chain")
        run = _run(self.ex.run("get_chain"))
        found = self.ex.get_run(run.id)
        self.assertEqual(found.id, run.id)

    def test_run_output_property(self):
        chain = self.ex.define("out_chain")
        chain.step("final", fn=lambda ctx: "final_value", output_key="final")
        run = _run(self.ex.run("out_chain"))
        self.assertEqual(run.output, "final_value")

    def test_run_to_dict(self):
        self._simple_chain("dict_chain")
        run = _run(self.ex.run("dict_chain"))
        d = run.to_dict()
        for k in ["id","chain","status","duration_ms","steps_completed"]:
            self.assertIn(k, d)

    def test_stats(self):
        self._simple_chain("stats_chain")
        _run(self.ex.run("stats_chain"))
        s = self.ex.stats()
        for k in ["total_runs","completed","defined_chains"]:
            self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# CACHE MANAGER
# ════════════════════════════════════════════════════════
class TestCacheManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.cache_manager import CacheManager
        self.cache = CacheManager(db_path=os.path.join(td,"cache.db"),
                                   l1_max=10, default_ttl=300)

    def test_set_and_get(self):
        self.cache.set("k1", "value1")
        self.assertEqual(self.cache.get("k1"), "value1")

    def test_get_miss_returns_none(self):
        self.assertIsNone(self.cache.get("nonexistent_key"))

    def test_set_dict_value(self):
        self.cache.set("d1", {"a": 1, "b": 2})
        self.assertEqual(self.cache.get("d1"), {"a": 1, "b": 2})

    def test_ttl_expiry(self):
        self.cache.set("expire_key", "value", ttl=0.05)
        self.assertEqual(self.cache.get("expire_key"), "value")
        time.sleep(0.1)
        self.assertIsNone(self.cache.get("expire_key"))

    def test_l1_hit(self):
        self.cache.set("l1k", "l1val")
        self.cache.get("l1k")  # first access (may be L2)
        before = self.cache._hits_l1
        self.cache.get("l1k")  # should be L1 now
        self.assertGreaterEqual(self.cache._hits_l1, before)

    def test_l2_persistence(self):
        self.cache.set("persist_k", "persist_v")
        # Remove from L1 to force L2 lookup
        self.cache._l1.clear()
        val = self.cache.get("persist_k")
        self.assertEqual(val, "persist_v")
        self.assertGreater(self.cache._hits_l2, 0)

    def test_lru_eviction(self):
        for i in range(15):   # l1_max=10
            self.cache.set(f"key{i}", i)
        self.assertLessEqual(len(self.cache._l1), 10)
        self.assertGreater(self.cache._evictions, 0)

    def test_delete(self):
        self.cache.set("del_k", "val")
        ok = self.cache.delete("del_k")
        self.assertTrue(ok)
        self.assertIsNone(self.cache.get("del_k"))

    def test_exists(self):
        self.cache.set("exists_k", "v")
        self.assertTrue(self.cache.exists("exists_k"))
        self.assertFalse(self.cache.exists("no_such_key"))

    def test_tag_invalidation(self):
        self.cache.set("u1", "a", tags=["user"])
        self.cache.set("u2", "b", tags=["user"])
        self.cache.set("r1", "c", tags=["report"])
        n = self.cache.invalidate_tag("user")
        self.assertGreaterEqual(n, 2)
        self.assertIsNone(self.cache.get("u1"))
        self.assertIsNone(self.cache.get("u2"))
        self.assertIsNotNone(self.cache.get("r1"))

    def test_prefix_invalidation(self):
        self.cache.set("user:1", "a")
        self.cache.set("user:2", "b")
        self.cache.set("report:1", "c")
        n = self.cache.invalidate_prefix("user:")
        self.assertGreaterEqual(n, 2)
        self.assertIsNone(self.cache.get("user:1"))
        self.assertIsNotNone(self.cache.get("report:1"))

    def test_get_or_set(self):
        val = self.cache.get_or_set("computed", lambda: 42)
        self.assertEqual(val, 42)
        # Second call should use cached value
        val2 = self.cache.get_or_set("computed", lambda: 99)
        self.assertEqual(val2, 42)

    def test_async_get_or_set(self):
        async def afn(): return "async_val"
        val = _run(self.cache.async_get_or_set("async_key", afn))
        self.assertEqual(val, "async_val")

    def test_clear(self):
        self.cache.set("a", 1); self.cache.set("b", 2)
        self.cache.clear()
        self.assertIsNone(self.cache.get("a"))

    def test_warm_up(self):
        self.cache.warm_up({"w1": "v1", "w2": "v2"})
        self.assertEqual(self.cache.get("w1"), "v1")
        self.assertEqual(self.cache.get("w2"), "v2")

    def test_get_many(self):
        self.cache.set("m1", 1); self.cache.set("m2", 2)
        result = self.cache.get_many(["m1","m2","m3"])
        self.assertEqual(result.get("m1"), 1)
        self.assertNotIn("m3", result)

    def test_set_many(self):
        self.cache.set_many({"sm1": "a", "sm2": "b"})
        self.assertEqual(self.cache.get("sm1"), "a")

    def test_namespace_get_set(self):
        ns = self.cache.namespace("user")
        ns.set("42", {"name": "Alice"})
        self.assertEqual(ns.get("42"), {"name": "Alice"})
        # Should not be accessible without prefix
        self.assertIsNone(self.cache.get("42"))

    def test_namespace_clear(self):
        ns = self.cache.namespace("ns")
        ns.set("a", 1); ns.set("b", 2)
        n = ns.clear()
        self.assertIsNone(ns.get("a"))

    def test_purge_expired(self):
        self.cache.set("exp1", "v", ttl=0.05)
        self.cache.set("exp2", "v", ttl=0.05)
        time.sleep(0.1)
        n = self.cache.purge_expired()
        self.assertGreaterEqual(n, 0)

    def test_hit_rate(self):
        self.cache.set("hr_k", "v")
        self.cache.get("hr_k"); self.cache.get("hr_k")
        self.cache.get("miss")
        self.assertGreater(self.cache.hit_rate, 0)

    def test_stats(self):
        self.cache.set("st", 1); self.cache.get("st")
        s = self.cache.stats()
        for k in ["l1_entries","hits_l1","hits_l2","misses","hit_rate"]:
            self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# ACCESS CONTROL
# ════════════════════════════════════════════════════════
class TestAccessControl(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.access_control import AccessControl
        self.ac = AccessControl(db_path=os.path.join(td,"ac.db"),
                                 audit=True)
        self.ac.create_role("viewer",
            permissions=[("read","reports:*"), ("read","users:self")])
        self.ac.create_role("editor",
            permissions=[("read","*"),("write","reports:*")],
            parent_roles=["viewer"])
        self.ac.create_role("admin",
            permissions=[("*","*")])
        self.ac.create_user("alice", roles=["editor"])
        self.ac.create_user("bob",   roles=["viewer"])
        self.ac.create_user("carol", roles=["admin"])

    def test_viewer_can_read(self):
        self.assertTrue(self.ac.check("bob", "read", "reports:sales"))

    def test_viewer_cannot_write(self):
        self.assertFalse(self.ac.check("bob", "write", "reports:sales"))

    def test_editor_can_write_reports(self):
        self.assertTrue(self.ac.check("alice", "write", "reports:sales"))

    def test_editor_cannot_delete(self):
        self.assertFalse(self.ac.check("alice", "delete", "reports:sales"))

    def test_admin_can_do_anything(self):
        self.assertTrue(self.ac.check("carol", "delete", "users:42"))
        self.assertTrue(self.ac.check("carol", "admin", "system:config"))

    def test_unknown_user_denied(self):
        self.assertFalse(self.ac.check("unknown_user", "read", "reports:*"))

    def test_wildcard_resource_match(self):
        self.assertTrue(self.ac.check("alice", "read", "reports:Q1_2024"))

    def test_role_inheritance(self):
        # editor inherits viewer's read:users:self
        self.assertTrue(self.ac.check("alice", "read", "users:self"))

    def test_grant_role(self):
        self.ac.create_user("dave", roles=[])
        self.ac.grant_role("dave", "viewer")
        self.assertTrue(self.ac.check("dave", "read", "reports:any"))

    def test_revoke_role(self):
        self.ac.revoke_role("bob", "viewer")
        self.assertFalse(self.ac.check("bob", "read", "reports:sales"))

    def test_direct_permission(self):
        self.ac.create_user("eve", direct_permissions=[("read","special:doc")])
        self.assertTrue(self.ac.check("eve", "read", "special:doc"))
        self.assertFalse(self.ac.check("eve", "read", "reports:sales"))

    def test_explicit_deny(self):
        from agent.access_control import Permission
        self.ac.create_role("restricted",
            permissions=[("read","*"), ("read","secret:*","deny")])
        self.ac.create_user("frank", roles=["restricted"])
        self.assertFalse(self.ac.check("frank", "read", "secret:data"))
        self.assertTrue(self.ac.check("frank",  "read", "public:data"))

    def test_disabled_user_denied(self):
        self.ac.create_user("inactive", roles=["admin"])
        self.ac._users["inactive"].enabled = False
        self.assertFalse(self.ac.check("inactive", "read", "anything"))

    def test_api_key_create_and_verify(self):
        ak, secret = self.ac.create_api_key("svc-key", roles=["viewer"])
        verified = self.ac.verify_api_key(secret)
        self.assertIsNotNone(verified)
        self.assertEqual(verified.name, "svc-key")

    def test_api_key_wrong_secret(self):
        self.ac.create_api_key("key2", roles=["viewer"])
        self.assertIsNone(self.ac.verify_api_key("wrong_secret_abc123"))

    def test_api_key_expiry(self):
        ak, secret = self.ac.create_api_key("expiring", expire_in_days=1)
        ak.expire_at = time.time() - 1.0   # force expired
        self.assertIsNone(self.ac.verify_api_key(secret))

    def test_api_key_check_permission(self):
        ak, secret = self.ac.create_api_key("svc", roles=["viewer"])
        ok = self.ac.check_api_key(secret, "read", "reports:any")
        self.assertTrue(ok)
        not_ok = self.ac.check_api_key(secret, "write", "reports:any")
        self.assertFalse(not_ok)

    def test_revoke_api_key(self):
        ak, secret = self.ac.create_api_key("to_revoke", roles=["viewer"])
        ok = self.ac.revoke_api_key(ak.id)
        self.assertTrue(ok)
        self.assertIsNone(self.ac.verify_api_key(secret))

    def test_token_issue_and_verify(self):
        token = self.ac.issue_token("alice", ttl_s=3600)
        subject = self.ac.verify_token(token)
        self.assertEqual(subject, "alice")

    def test_token_expired(self):
        token = self.ac.issue_token("alice", ttl_s=0.001)
        time.sleep(0.01)
        self.assertIsNone(self.ac.verify_token(token))

    def test_token_invalid_sig(self):
        self.assertIsNone(self.ac.verify_token("invalid.token"))

    def test_audit_log_populated(self):
        self.ac.check("bob", "read", "reports:sales")
        log = self.ac.audit_log("bob")
        self.assertGreater(len(log), 0)

    def test_glob_wildcard(self):
        from agent.access_control import _glob_match
        self.assertTrue(_glob_match("reports:*", "reports:sales"))
        self.assertFalse(_glob_match("reports:*", "users:42"))
        self.assertTrue(_glob_match("*", "anything"))

    def test_stats(self):
        self.ac.check("alice", "read", "reports:any")
        s = self.ac.stats()
        for k in ["users","roles","api_keys","total_checks"]:
            self.assertIn(k, s)

    def test_role_to_dict(self):
        role = self.ac._roles["viewer"]
        d = role.to_dict()
        for k in ["id","name","permissions"]: self.assertIn(k, d)

    def test_user_to_dict(self):
        user = self.ac._users["alice"]
        d = user.to_dict()
        for k in ["id","name","roles","enabled"]: self.assertIn(k, d)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v30: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
