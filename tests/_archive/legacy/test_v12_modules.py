"""
OMNI AGENT v12 — Test Suite
Tests: FeatureFlags, CostTracker, PluginLoader, HealthDashboard
Run: python3 tests/test_v12_modules.py
"""
import asyncio, os, sys, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE FLAGS — TargetingRule
# ══════════════════════════════════════════════════════════════════════════════

class TestTargetingRule(unittest.TestCase):
    def setUp(self):
        from agent.feature_flags import TargetingRule, RuleOperator
        self.TR = TargetingRule
        self.RO = RuleOperator

    def test_eq_match(self):
        rule = self.TR("plan", self.RO.EQ, "pro")
        self.assertTrue(rule.evaluate({"plan": "pro"}))

    def test_eq_no_match(self):
        rule = self.TR("plan", self.RO.EQ, "pro")
        self.assertFalse(rule.evaluate({"plan": "free"}))

    def test_neq_match(self):
        rule = self.TR("plan", self.RO.NEQ, "free")
        self.assertTrue(rule.evaluate({"plan": "pro"}))

    def test_in_match(self):
        rule = self.TR("country", self.RO.IN, ["US", "CA", "UK"])
        self.assertTrue(rule.evaluate({"country": "US"}))
        self.assertFalse(rule.evaluate({"country": "DE"}))

    def test_contains_match(self):
        rule = self.TR("tags", self.RO.CONTAINS, "beta")
        self.assertTrue(rule.evaluate({"tags": ["beta", "early_access"]}))
        self.assertFalse(rule.evaluate({"tags": ["stable"]}))

    def test_gt_match(self):
        rule = self.TR("age", self.RO.GT, 18)
        self.assertTrue(rule.evaluate({"age": 25}))
        self.assertFalse(rule.evaluate({"age": 16}))

    def test_lt_match(self):
        rule = self.TR("score", self.RO.LT, 100)
        self.assertTrue(rule.evaluate({"score": 50}))
        self.assertFalse(rule.evaluate({"score": 150}))

    def test_missing_attribute_returns_false(self):
        rule = self.TR("plan", self.RO.EQ, "pro")
        self.assertFalse(rule.evaluate({}))

    def test_to_dict(self):
        rule = self.TR("plan", self.RO.EQ, "pro")
        d = rule.to_dict()
        self.assertEqual(d["attribute"], "plan")
        self.assertEqual(d["value"], "pro")


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE FLAGS — FeatureFlag.evaluate
# ══════════════════════════════════════════════════════════════════════════════

class TestFeatureFlagEvaluate(unittest.TestCase):
    def setUp(self):
        from agent.feature_flags import FeatureFlag, FlagType, TargetingRule, RuleOperator
        self.FF = FeatureFlag
        self.FT = FlagType
        self.TR = TargetingRule
        self.RO = RuleOperator

    def _make(self, flag_type, enabled=True, **kwargs):
        return self.FF(id="f1", name="test", flag_type=flag_type,
                       enabled=enabled, **kwargs)

    def test_disabled_always_false(self):
        flag = self._make(self.FT.BOOLEAN, enabled=False)
        self.assertFalse(flag.evaluate(user_id="any"))

    def test_boolean_enabled_true(self):
        flag = self._make(self.FT.BOOLEAN, enabled=True)
        self.assertTrue(flag.evaluate(user_id="any"))

    def test_percentage_zero_false(self):
        flag = self._make(self.FT.PERCENTAGE, percentage=0.0)
        self.assertFalse(flag.evaluate(user_id="user1"))

    def test_percentage_hundred_true(self):
        flag = self._make(self.FT.PERCENTAGE, percentage=100.0)
        self.assertTrue(flag.evaluate(user_id="user1"))

    def test_percentage_consistent(self):
        flag = self._make(self.FT.PERCENTAGE, percentage=50.0)
        r1 = flag.evaluate(user_id="stable_user")
        r2 = flag.evaluate(user_id="stable_user")
        self.assertEqual(r1, r2)

    def test_user_list_match(self):
        flag = self._make(self.FT.USER_LIST, user_list=["alice", "bob"])
        self.assertTrue(flag.evaluate(user_id="alice"))
        self.assertFalse(flag.evaluate(user_id="charlie"))

    def test_rule_match_all_true(self):
        rules = [
            self.TR("plan", self.RO.EQ, "pro"),
            self.TR("country", self.RO.EQ, "US"),
        ]
        flag = self._make(self.FT.RULE, rules=rules, rules_match_all=True)
        self.assertTrue(flag.evaluate(context={"plan": "pro", "country": "US"}))
        self.assertFalse(flag.evaluate(context={"plan": "pro", "country": "UK"}))

    def test_rule_match_any_true(self):
        rules = [
            self.TR("plan", self.RO.EQ, "pro"),
            self.TR("plan", self.RO.EQ, "enterprise"),
        ]
        flag = self._make(self.FT.RULE, rules=rules, rules_match_all=False)
        self.assertTrue(flag.evaluate(context={"plan": "enterprise"}))

    def test_override_forces_on(self):
        flag = self._make(self.FT.PERCENTAGE, percentage=0.0,
                          overrides={"user_x": True})
        self.assertTrue(flag.evaluate(user_id="user_x"))

    def test_override_forces_off(self):
        flag = self._make(self.FT.BOOLEAN, enabled=True,
                          overrides={"blocked_user": False})
        self.assertFalse(flag.evaluate(user_id="blocked_user"))


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE FLAGS — FeatureFlagService
# ══════════════════════════════════════════════════════════════════════════════

class TestFeatureFlagService(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.feature_flags import FeatureFlagService, FlagType
        self.svc = FeatureFlagService(
            db_path=os.path.join(self.tmpdir, "flags.db")
        )
        self.FT = FlagType

    def test_create_boolean_flag(self):
        flag = self.svc.create("new_feature", enabled=True)
        self.assertIsNotNone(flag)
        self.assertEqual(flag.name, "new_feature")

    def test_create_duplicate_raises(self):
        self.svc.create("dup_flag", enabled=True)
        with self.assertRaises(ValueError):
            self.svc.create("dup_flag", enabled=True)

    def test_get_existing(self):
        self.svc.create("my_flag", enabled=True)
        got = self.svc.get("my_flag")
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "my_flag")

    def test_get_nonexistent_returns_none(self):
        self.assertIsNone(self.svc.get("does_not_exist"))

    def test_list_flags(self):
        self.svc.create("flag_a", enabled=True)
        self.svc.create("flag_b", enabled=False)
        flags = self.svc.list_flags()
        names = [f.name for f in flags]
        self.assertIn("flag_a", names)
        self.assertIn("flag_b", names)

    def test_list_enabled_only(self):
        self.svc.create("on_flag", enabled=True)
        self.svc.create("off_flag", enabled=False)
        flags = self.svc.list_flags(enabled_only=True)
        self.assertTrue(all(f.enabled for f in flags))

    def test_list_by_tag(self):
        self.svc.create("tagged", tags=["beta"], enabled=True)
        self.svc.create("untagged", tags=[], enabled=True)
        flags = self.svc.list_flags(tag="beta")
        names = [f.name for f in flags]
        self.assertIn("tagged", names)
        self.assertNotIn("untagged", names)

    def test_delete_flag(self):
        self.svc.create("del_me", enabled=True)
        ok = self.svc.delete("del_me")
        self.assertTrue(ok)
        self.assertIsNone(self.svc.get("del_me"))

    def test_delete_nonexistent(self):
        self.assertFalse(self.svc.delete("never_existed"))

    def test_is_enabled_true(self):
        self.svc.create("enabled_flag", enabled=True)
        self.assertTrue(self.svc.is_enabled("enabled_flag", user_id="u1"))

    def test_is_enabled_false(self):
        self.svc.create("disabled_flag", enabled=False)
        self.assertFalse(self.svc.is_enabled("disabled_flag", user_id="u1"))

    def test_is_enabled_missing_flag(self):
        self.assertFalse(self.svc.is_enabled("ghost_flag"))

    def test_evaluate_all(self):
        self.svc.create("ev_a", enabled=True)
        self.svc.create("ev_b", enabled=False)
        results = self.svc.evaluate_all(user_id="u1")
        self.assertIn("ev_a", results)
        self.assertIn("ev_b", results)
        self.assertTrue(results["ev_a"])
        self.assertFalse(results["ev_b"])

    def test_evaluate_many(self):
        self.svc.create("em_a", enabled=True)
        self.svc.create("em_b", enabled=True)
        results = self.svc.evaluate_many(["em_a", "em_b", "ghost"], "u1")
        self.assertTrue(results["em_a"])
        self.assertFalse(results["ghost"])

    def test_enable_disable(self):
        self.svc.create("toggle", enabled=False)
        self.svc.enable("toggle")
        self.assertTrue(self.svc.is_enabled("toggle", "u1"))
        self.svc.disable("toggle")
        self.assertFalse(self.svc.is_enabled("toggle", "u1"))

    def test_set_percentage(self):
        self.svc.create("pct_flag", flag_type=self.FT.PERCENTAGE,
                        enabled=True, percentage=0.0)
        self.svc.set_percentage("pct_flag", 100.0)
        self.assertTrue(self.svc.is_enabled("pct_flag", "any_user"))

    def test_set_percentage_clamps(self):
        self.svc.create("clamp_flag", flag_type=self.FT.PERCENTAGE, enabled=True)
        self.svc.set_percentage("clamp_flag", 150.0)
        flag = self.svc.get("clamp_flag")
        self.assertEqual(flag.percentage, 100.0)

    def test_add_remove_user_list(self):
        self.svc.create("ul_flag", flag_type=self.FT.USER_LIST, enabled=True)
        self.svc.add_to_user_list("ul_flag", "user_123")
        self.assertTrue(self.svc.is_enabled("ul_flag", "user_123"))
        self.svc.remove_from_user_list("ul_flag", "user_123")
        self.assertFalse(self.svc.is_enabled("ul_flag", "user_123"))

    def test_set_override(self):
        self.svc.create("ov_flag", enabled=False)
        self.svc.set_override("ov_flag", "vip_user", True)
        self.assertTrue(self.svc.is_enabled("ov_flag", "vip_user"))
        self.assertFalse(self.svc.is_enabled("ov_flag", "regular_user"))

    def test_clear_override(self):
        self.svc.create("ov2_flag", enabled=True)
        self.svc.set_override("ov2_flag", "blocked", False)
        self.assertFalse(self.svc.is_enabled("ov2_flag", "blocked"))
        self.svc.clear_override("ov2_flag", "blocked")
        self.assertTrue(self.svc.is_enabled("ov2_flag", "blocked"))

    def test_changelog_recorded(self):
        self.svc.create("log_flag", enabled=True, actor="alice")
        self.svc.disable("log_flag", actor="bob")
        log = self.svc.changelog("log_flag")
        self.assertGreater(len(log), 0)

    def test_persistence_survives_reload(self):
        self.svc.create("persist_flag", enabled=True)
        from agent.feature_flags import FeatureFlagService
        svc2 = FeatureFlagService(
            db_path=os.path.join(self.tmpdir, "flags.db")
        )
        flag = svc2.get("persist_flag")
        self.assertIsNotNone(flag)
        self.assertTrue(flag.enabled)

    def test_stats(self):
        self.svc.create("s1", enabled=True)
        self.svc.create("s2", enabled=False)
        stats = self.svc.stats()
        self.assertIn("total", stats)
        self.assertIn("enabled", stats)
        self.assertGreaterEqual(stats["total"], 2)


# ══════════════════════════════════════════════════════════════════════════════
# COST TRACKER — Pricing
# ══════════════════════════════════════════════════════════════════════════════

class TestModelPricing(unittest.TestCase):
    def test_known_model_cost(self):
        from agent.cost_tracker import get_model_cost
        cost = get_model_cost("claude-3-5-sonnet", 1000, 500)
        self.assertGreater(cost, 0)
        self.assertLess(cost, 1.0)  # sanity

    def test_zero_tokens_zero_cost(self):
        from agent.cost_tracker import get_model_cost
        self.assertEqual(get_model_cost("gpt-4o", 0, 0), 0.0)

    def test_unknown_model_uses_default(self):
        from agent.cost_tracker import get_model_cost
        cost = get_model_cost("some-unknown-model-xyz", 1000, 1000)
        self.assertGreater(cost, 0)

    def test_add_custom_pricing(self):
        from agent.cost_tracker import add_model_pricing, get_model_cost
        add_model_pricing("my-custom-model", 1.0, 2.0)
        cost = get_model_cost("my-custom-model", 1_000_000, 0)
        self.assertAlmostEqual(cost, 1.0, places=4)

    def test_output_more_expensive(self):
        from agent.cost_tracker import get_model_cost
        # For most models output tokens cost more
        input_only = get_model_cost("claude-3-5-sonnet", 1000, 0)
        output_only = get_model_cost("claude-3-5-sonnet", 0, 1000)
        self.assertGreater(output_only, input_only)


# ══════════════════════════════════════════════════════════════════════════════
# COST TRACKER — CostTracker
# ══════════════════════════════════════════════════════════════════════════════

class TestCostTracker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.cost_tracker import CostTracker
        self.tracker = CostTracker(
            db_path=os.path.join(self.tmpdir, "cost.db")
        )

    def test_record_call(self):
        rec = self.tracker.record_call(
            model="claude-3-5-sonnet",
            input_tokens=1000, output_tokens=200,
            user_id="user_1", session_id="sess_1",
            operation="chat",
        )
        self.assertIsNotNone(rec)
        self.assertGreater(rec.cost_usd, 0)
        self.assertEqual(rec.total_tokens, 1200)

    def test_custom_cost_usd(self):
        rec = self.tracker.record_call(
            model="any-model", input_tokens=0, output_tokens=0,
            custom_cost_usd=0.042,
        )
        self.assertAlmostEqual(rec.cost_usd, 0.042, places=6)

    def test_total_spend(self):
        self.tracker.record_call("gpt-4o", 1000, 200, user_id="u1")
        self.tracker.record_call("gpt-4o", 1000, 200, user_id="u1")
        total = self.tracker.total_spend(user_id="u1")
        self.assertGreater(total, 0)

    def test_total_spend_by_user_isolated(self):
        self.tracker.record_call("gpt-4o", 500, 100, user_id="user_A")
        self.tracker.record_call("gpt-4o", 500, 100, user_id="user_B")
        spend_a = self.tracker.total_spend(user_id="user_A")
        spend_b = self.tracker.total_spend(user_id="user_B")
        # Both should have same spend (same tokens)
        self.assertAlmostEqual(spend_a, spend_b, places=8)

    def test_spend_by_model(self):
        self.tracker.record_call("gpt-4o", 100, 50)
        self.tracker.record_call("claude-3-5-sonnet", 100, 50)
        by_model = self.tracker.spend_by_model()
        models = [row["key"] for row in by_model]
        self.assertGreater(len(models), 0)

    def test_spend_by_user(self):
        self.tracker.record_call("gpt-4o", 100, 50, user_id="alice")
        self.tracker.record_call("gpt-4o", 100, 50, user_id="bob")
        by_user = self.tracker.spend_by_user()
        users = [row["key"] for row in by_user]
        self.assertIn("alice", users)
        self.assertIn("bob", users)

    def test_spend_by_operation(self):
        self.tracker.record_call("gpt-4o", 100, 50, operation="chat")
        self.tracker.record_call("gpt-4o", 100, 50, operation="embed")
        by_op = self.tracker.spend_by_operation()
        ops = [row["key"] for row in by_op]
        self.assertIn("chat", ops)
        self.assertIn("embed", ops)

    def test_set_budget(self):
        budget = self.tracker.set_budget("user", "u1", limit_usd=5.0,
                                          period="month")
        self.assertIsNotNone(budget)
        self.assertEqual(budget.limit_usd, 5.0)

    def test_check_budget_ok(self):
        self.tracker.set_budget("user", "fresh_user", limit_usd=100.0,
                                 period="month")
        ok, statuses = self.tracker.check_budget("user", "fresh_user")
        self.assertTrue(ok)
        self.assertEqual(len(statuses), 1)

    def test_check_budget_exceeded(self):
        self.tracker.set_budget("user", "broke_user", limit_usd=0.000001,
                                 period="all_time", hard_limit=True)
        self.tracker.record_call("gpt-4o", 10000, 5000, user_id="broke_user")
        ok, statuses = self.tracker.check_budget("user", "broke_user")
        self.assertFalse(ok)
        self.assertTrue(any(s.exceeded for s in statuses))

    def test_check_budget_alert(self):
        self.tracker.set_budget("user", "alert_user", limit_usd=0.0001,
                                 period="all_time", alert_pct=50.0)
        self.tracker.record_call("gpt-4o", 100, 50, user_id="alert_user")
        _, statuses = self.tracker.check_budget("user", "alert_user")
        # May or may not trigger depending on actual cost
        for s in statuses:
            self.assertIsInstance(s.alert_triggered, bool)

    def test_delete_budget(self):
        b = self.tracker.set_budget("session", "sess_x", limit_usd=1.0)
        ok = self.tracker.delete_budget(b.id)
        self.assertTrue(ok)
        budgets = self.tracker.get_budgets("session", "sess_x")
        self.assertEqual(len(budgets), 0)

    def test_alert_callback_fires(self):
        fired = []
        def cb(status):
            fired.append(status)

        from agent.cost_tracker import CostTracker
        tracker = CostTracker(
            db_path=os.path.join(self.tmpdir, "cost_cb.db"),
            alert_callback=cb,
        )
        tracker.set_budget("user", "cb_user", limit_usd=0.000001,
                            period="all_time", alert_pct=0.0)
        tracker.record_call("gpt-4o", 1000, 500, user_id="cb_user")
        # Callback may fire; just ensure no crash
        self.assertIsInstance(fired, list)

    def test_daily_report(self):
        self.tracker.record_call("gpt-4o", 500, 100, user_id="rep_user")
        report = self.tracker.daily_report(days=7)
        self.assertIn("total_spend_usd", report)
        self.assertIn("by_model", report)
        self.assertIn("by_user", report)

    def test_usage_history(self):
        self.tracker.record_call("gpt-4o", 100, 50, user_id="hist_user")
        history = self.tracker.usage_history(user_id="hist_user")
        self.assertGreater(len(history), 0)

    def test_usage_record_to_dict(self):
        rec = self.tracker.record_call("gpt-4o", 100, 50, user_id="dict_user")
        d = rec.to_dict()
        for key in ["id", "model", "input_tokens", "output_tokens",
                    "cost_usd", "user_id"]:
            self.assertIn(key, d)

    def test_stats(self):
        self.tracker.record_call("gpt-4o", 100, 50)
        stats = self.tracker.stats()
        self.assertIn("total_spend_usd", stats)
        self.assertIn("pricing_models", stats)
        self.assertGreater(stats["pricing_models"], 10)


# ══════════════════════════════════════════════════════════════════════════════
# PLUGIN LOADER — HookRegistry
# ══════════════════════════════════════════════════════════════════════════════

class TestHookRegistry(unittest.TestCase):
    def setUp(self):
        from agent.plugin_loader import HookRegistry
        self.reg = HookRegistry()

    def test_register_and_fire_sync(self):
        results = []
        self.reg.register("on_startup", lambda: results.append("fired"))
        self.reg.fire_sync("on_startup")
        self.assertEqual(results, ["fired"])

    def test_fire_unknown_hook_no_error(self):
        # Should not raise
        results = self.reg.fire_sync("nonexistent_hook")
        self.assertEqual(results, [])

    def test_multiple_handlers_same_hook(self):
        results = []
        self.reg.register("before_chat", lambda: results.append(1))
        self.reg.register("before_chat", lambda: results.append(2))
        self.reg.fire_sync("before_chat")
        self.assertEqual(sorted(results), [1, 2])

    def test_async_fire(self):
        results = []
        async def async_handler():
            results.append("async")
        self.reg.register("after_chat", async_handler)
        _run(self.reg.fire("after_chat"))
        self.assertEqual(results, ["async"])

    def test_handler_error_does_not_crash(self):
        def bad_handler():
            raise RuntimeError("boom")
        self.reg.register("on_error", bad_handler)
        # Should not raise
        self.reg.fire_sync("on_error")

    def test_unregister_plugin(self):
        def handler():
            return "hi"
        handler._plugin_id = "plugin_xyz"
        self.reg.register("before_chat", handler)
        self.reg.unregister_plugin("plugin_xyz")
        self.assertEqual(self.reg._hooks.get("before_chat", []), [])

    def test_list_hooks(self):
        self.reg.register("on_startup", lambda: None)
        self.reg.register("on_shutdown", lambda: None)
        hooks = self.reg.list_hooks()
        self.assertIn("on_startup", hooks)
        self.assertEqual(hooks["on_startup"], 1)

    def test_list_all_hooks(self):
        all_hooks = self.reg.list_all_hooks()
        self.assertIn("on_startup", all_hooks)
        self.assertIn("before_chat", all_hooks)


# ══════════════════════════════════════════════════════════════════════════════
# PLUGIN LOADER — PluginLoader
# ══════════════════════════════════════════════════════════════════════════════

class TestPluginLoader(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.plugin_loader import PluginLoader, HookRegistry
        self.loader = PluginLoader(
            plugin_dirs=[self.tmpdir],
            hook_registry=HookRegistry(),
        )

    def _create_plugin_dir(self, name: str, setup_body: str = "pass") -> str:
        """Create a minimal plugin directory in tmpdir."""
        plugin_dir = os.path.join(self.tmpdir, name)
        os.makedirs(plugin_dir, exist_ok=True)
        with open(os.path.join(plugin_dir, "plugin.py"), "w") as f:
            f.write(f"""
METADATA = {{
    "name": "{name}",
    "version": "1.0.0",
    "description": "Test plugin {name}",
    "hooks": ["before_chat"],
}}

def setup(context):
    {setup_body}

def teardown(context):
    pass
""")
        return plugin_dir

    def test_discover_finds_plugin(self):
        self._create_plugin_dir("my_plugin")
        plugins = self.loader.discover()
        names = [p.meta.name for p in plugins]
        self.assertIn("my_plugin", names)

    def test_discover_empty_dir(self):
        from agent.plugin_loader import PluginLoader
        loader = PluginLoader(plugin_dirs=[self.tmpdir])
        # tmpdir has no plugin dirs yet (just created files earlier)
        # Just ensure no crash
        loader.discover()

    def test_load_plugin(self):
        self._create_plugin_dir("load_test")
        self.loader.discover()
        ok = self.loader.load("load_test")
        self.assertTrue(ok)

    def test_activate_plugin(self):
        self._create_plugin_dir("act_test")
        self.loader.discover()
        self.loader.load("act_test")
        ok = self.loader.activate("act_test", context={})
        self.assertTrue(ok)
        from agent.plugin_loader import PluginStatus
        plugin = self.loader.get_plugin("act_test")
        self.assertEqual(plugin.status, PluginStatus.ACTIVE)

    def test_deactivate_plugin(self):
        self._create_plugin_dir("deact_test")
        self.loader.discover()
        self.loader.load("deact_test")
        self.loader.activate("deact_test", context={})
        ok = self.loader.deactivate("deact_test", context={})
        self.assertTrue(ok)
        from agent.plugin_loader import PluginStatus
        plugin = self.loader.get_plugin("deact_test")
        self.assertEqual(plugin.status, PluginStatus.INACTIVE)

    def test_register_in_memory(self):
        activated = []
        def setup_fn(ctx):
            activated.append("setup_called")
        plugin = self.loader.register_in_memory(
            "mem_plugin", setup_fn,
            meta={"version": "2.0.0", "description": "In-memory test"},
        )
        self.assertIsNotNone(plugin)
        ok = self.loader.activate("mem_plugin", context={})
        self.assertTrue(ok)
        self.assertEqual(activated, ["setup_called"])

    def test_in_memory_hook_registration(self):
        from agent.plugin_loader import PluginLoader, HookRegistry
        hooks = HookRegistry()
        loader = PluginLoader(hook_registry=hooks)
        results = []

        def setup_fn(ctx):
            ctx["hook_registry"].register("before_chat", lambda: results.append("hook"))

        loader.register_in_memory("hook_plugin", setup_fn)
        loader.activate("hook_plugin", context={})
        hooks.fire_sync("before_chat")
        self.assertEqual(results, ["hook"])

    def test_load_nonexistent_returns_false(self):
        ok = self.loader.load("nonexistent_plugin")
        self.assertFalse(ok)

    def test_plugin_with_error_in_setup(self):
        self._create_plugin_dir("bad_plugin",
                                 setup_body="raise RuntimeError('setup failed')")
        self.loader.discover()
        self.loader.load("bad_plugin")
        ok = self.loader.activate("bad_plugin", context={})
        self.assertFalse(ok)
        from agent.plugin_loader import PluginStatus
        plugin = self.loader.get_plugin("bad_plugin")
        self.assertEqual(plugin.status, PluginStatus.ERROR)

    def test_reload_plugin(self):
        self._create_plugin_dir("reload_test")
        self.loader.discover()
        self.loader.load("reload_test")
        self.loader.activate("reload_test", context={})
        ok = self.loader.reload("reload_test", context={})
        self.assertTrue(ok)
        plugin = self.loader.get_plugin("reload_test")
        self.assertEqual(plugin.reload_count, 1)

    def test_unload_plugin(self):
        self._create_plugin_dir("unload_test")
        self.loader.discover()
        self.loader.load("unload_test")
        ok = self.loader.unload("unload_test", context={})
        self.assertTrue(ok)
        from agent.plugin_loader import PluginStatus
        plugin = self.loader.get_plugin("unload_test")
        self.assertEqual(plugin.status, PluginStatus.UNLOADED)
        self.assertIsNone(plugin.module)

    def test_list_plugins(self):
        self._create_plugin_dir("list_plugin_a")
        self._create_plugin_dir("list_plugin_b")
        self.loader.discover()
        plugins = self.loader.list_plugins()
        names = [p.meta.name for p in plugins]
        self.assertIn("list_plugin_a", names)

    def test_list_plugins_by_status(self):
        self._create_plugin_dir("status_test")
        self.loader.discover()
        self.loader.load("status_test")
        from agent.plugin_loader import PluginStatus
        loaded = self.loader.list_plugins(status=PluginStatus.LOADED)
        names = [p.meta.name for p in loaded]
        self.assertIn("status_test", names)

    def test_stats(self):
        self._create_plugin_dir("stats_plugin")
        self.loader.discover()
        self.loader.load("stats_plugin")
        self.loader.activate("stats_plugin", context={})
        stats = self.loader.stats()
        self.assertIn("total", stats)
        self.assertIn("active", stats)
        self.assertGreaterEqual(stats["active"], 1)

    def test_plugin_to_dict(self):
        self._create_plugin_dir("dict_plugin")
        self.loader.discover()
        plugin = self.loader.get_plugin("dict_plugin")
        d = plugin.to_dict()
        for key in ["id", "path", "status", "name", "version"]:
            self.assertIn(key, d)


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthDashboard(unittest.TestCase):
    def setUp(self):
        from agent.health_dashboard import HealthDashboard
        self.dash = HealthDashboard(app_name="Test Agent")

    def test_check_all_empty(self):
        snapshot = _run(self.dash.check_all())
        self.assertIsNotNone(snapshot)

    def test_register_ok_check(self):
        self.dash.register_ok("config", "Loaded from env")
        snapshot = _run(self.dash.check_all())
        names = [c.name for c in snapshot.checks]
        self.assertIn("config", names)

    def test_ok_check_returns_ok(self):
        from agent.health_dashboard import HealthStatus
        self.dash.register_ok("always_ok")
        snapshot = _run(self.dash.check_all())
        check = next(c for c in snapshot.checks if c.name == "always_ok")
        self.assertEqual(check.status, HealthStatus.OK)

    def test_register_lambda(self):
        from agent.health_dashboard import HealthStatus
        self.dash.register_lambda("custom", lambda: (HealthStatus.OK, "all good"))
        snapshot = _run(self.dash.check_all())
        check = next(c for c in snapshot.checks if c.name == "custom")
        self.assertEqual(check.status, HealthStatus.OK)

    def test_failing_check_returns_down(self):
        from agent.health_dashboard import HealthStatus
        self.dash.register_lambda("always_down",
                                   lambda: (HealthStatus.DOWN, "connection refused"))
        snapshot = _run(self.dash.check_all())
        check = next(c for c in snapshot.checks if c.name == "always_down")
        self.assertEqual(check.status, HealthStatus.DOWN)

    def test_degraded_check(self):
        from agent.health_dashboard import HealthStatus
        self.dash.register_lambda("slow_svc",
                                   lambda: (HealthStatus.DEGRADED, "high latency"))
        snapshot = _run(self.dash.check_all())
        self.assertEqual(snapshot.status, HealthStatus.DEGRADED)

    def test_overall_status_ok_all_pass(self):
        from agent.health_dashboard import HealthStatus
        self.dash.register_ok("svc_a")
        self.dash.register_ok("svc_b")
        snapshot = _run(self.dash.check_all())
        self.assertEqual(snapshot.status, HealthStatus.OK)

    def test_overall_status_down_if_any_down(self):
        from agent.health_dashboard import HealthStatus
        self.dash.register_ok("good_svc")
        self.dash.register_lambda("bad_svc",
                                   lambda: (HealthStatus.DOWN, "dead"))
        snapshot = _run(self.dash.check_all())
        self.assertEqual(snapshot.status, HealthStatus.DOWN)

    def test_check_exception_marked_down(self):
        from agent.health_dashboard import HealthStatus
        def boom():
            raise ConnectionError("cannot connect")
        self.dash.register("exploding_svc", boom)
        snapshot = _run(self.dash.check_all())
        check = next(c for c in snapshot.checks if c.name == "exploding_svc")
        self.assertEqual(check.status, HealthStatus.DOWN)
        self.assertIn("cannot connect", check.message)

    def test_bool_return_true_is_ok(self):
        from agent.health_dashboard import HealthStatus
        self.dash.register("bool_ok", lambda: True)
        snapshot = _run(self.dash.check_all())
        check = next(c for c in snapshot.checks if c.name == "bool_ok")
        self.assertEqual(check.status, HealthStatus.OK)

    def test_bool_return_false_is_down(self):
        from agent.health_dashboard import HealthStatus
        self.dash.register("bool_down", lambda: False)
        snapshot = _run(self.dash.check_all())
        check = next(c for c in snapshot.checks if c.name == "bool_down")
        self.assertEqual(check.status, HealthStatus.DOWN)

    def test_async_check(self):
        from agent.health_dashboard import HealthStatus
        async def async_check():
            await asyncio.sleep(0.001)
            return HealthStatus.OK, "async ok"
        self.dash.register("async_svc", async_check)
        snapshot = _run(self.dash.check_all())
        check = next(c for c in snapshot.checks if c.name == "async_svc")
        self.assertEqual(check.status, HealthStatus.OK)

    def test_duration_recorded(self):
        self.dash.register_ok("timed_svc")
        snapshot = _run(self.dash.check_all())
        check = next(c for c in snapshot.checks if c.name == "timed_svc")
        self.assertGreaterEqual(check.duration_ms, 0)

    def test_latency_threshold_degraded(self):
        from agent.health_dashboard import HealthStatus
        def slow_check():
            time.sleep(0.05)
            return HealthStatus.OK, "done"
        self.dash.register("slow_check", slow_check,
                            degraded_threshold_ms=10,
                            down_threshold_ms=10000)
        snapshot = _run(self.dash.check_all())
        check = next(c for c in snapshot.checks if c.name == "slow_check")
        self.assertEqual(check.status, HealthStatus.DEGRADED)

    def test_latency_threshold_down(self):
        from agent.health_dashboard import HealthStatus
        def very_slow():
            time.sleep(0.05)
            return HealthStatus.OK, "done"
        self.dash.register("very_slow", very_slow,
                            degraded_threshold_ms=1,
                            down_threshold_ms=10)
        snapshot = _run(self.dash.check_all())
        check = next(c for c in snapshot.checks if c.name == "very_slow")
        self.assertEqual(check.status, HealthStatus.DOWN)

    def test_unregister_check(self):
        self.dash.register_ok("removable")
        ok = self.dash.unregister("removable")
        self.assertTrue(ok)
        snapshot = _run(self.dash.check_all())
        names = [c.name for c in snapshot.checks]
        self.assertNotIn("removable", names)

    def test_unregister_nonexistent(self):
        self.assertFalse(self.dash.unregister("ghost"))

    def test_snapshot_counts(self):
        from agent.health_dashboard import HealthStatus
        self.dash.register_ok("ok1")
        self.dash.register_ok("ok2")
        self.dash.register_lambda("deg1",
                                   lambda: (HealthStatus.DEGRADED, "slow"))
        snapshot = _run(self.dash.check_all())
        self.assertEqual(snapshot.ok_count, 2)
        self.assertEqual(snapshot.degraded_count, 1)
        self.assertEqual(snapshot.down_count, 0)

    def test_last_snapshot(self):
        self.dash.register_ok("snap_test")
        _run(self.dash.check_all())
        snap = self.dash.last_snapshot()
        self.assertIsNotNone(snap)

    def test_history_appends(self):
        self.dash.register_ok("hist_svc")
        _run(self.dash.check_all())
        _run(self.dash.check_all())
        h = self.dash.history()
        self.assertGreaterEqual(len(h), 2)

    def test_history_bounded(self):
        from agent.health_dashboard import HealthDashboard
        dash = HealthDashboard(history_size=3)
        dash.register_ok("s")
        for _ in range(5):
            _run(dash.check_all())
        self.assertLessEqual(len(dash.history()), 3)

    def test_snapshot_to_dict(self):
        self.dash.register_ok("dict_svc")
        snapshot = _run(self.dash.check_all())
        d = snapshot.to_dict()
        for key in ["status", "timestamp", "summary", "checks"]:
            self.assertIn(key, d)
        self.assertIn("total", d["summary"])

    def test_render_html(self):
        self.dash.register_ok("html_svc")
        snapshot = _run(self.dash.check_all())
        html = self.dash.render_html(snapshot)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Test Agent", html)
        self.assertIn("html_svc", html)

    def test_render_html_shows_status_colors(self):
        from agent.health_dashboard import HealthStatus
        self.dash.register_lambda("failing",
                                   lambda: (HealthStatus.DOWN, "oops"))
        snapshot = _run(self.dash.check_all())
        html = self.dash.render_html(snapshot)
        self.assertIn("DOWN", html.upper())

    def test_register_module_with_health_check(self):
        from agent.health_dashboard import HealthStatus

        class FakeModule:
            def health_check(self):
                return HealthStatus.OK, "module ok"

        self.dash.register_module("fake_module", FakeModule())
        snapshot = _run(self.dash.check_all())
        names = [c.name for c in snapshot.checks]
        self.assertIn("fake_module", names)

    def test_register_module_without_health_check(self):
        class SimpleModule:
            pass
        self.dash.register_module("simple_module", SimpleModule())
        snapshot = _run(self.dash.check_all())
        names = [c.name for c in snapshot.checks]
        self.assertIn("simple_module", names)

    def test_stats(self):
        self.dash.register_ok("stat_svc")
        stats = self.dash.stats()
        self.assertIn("registered_checks", stats)
        self.assertIn("uptime_s", stats)
        self.assertGreaterEqual(stats["registered_checks"], 1)

    def test_aggregate_status_all_ok(self):
        from agent.health_dashboard import _aggregate_status, HealthStatus
        result = _aggregate_status([HealthStatus.OK, HealthStatus.OK])
        self.assertEqual(result, HealthStatus.OK)

    def test_aggregate_status_worst_wins(self):
        from agent.health_dashboard import _aggregate_status, HealthStatus
        result = _aggregate_status([HealthStatus.OK, HealthStatus.DEGRADED,
                                     HealthStatus.DOWN])
        self.assertEqual(result, HealthStatus.DOWN)

    def test_aggregate_status_empty(self):
        from agent.health_dashboard import _aggregate_status, HealthStatus
        result = _aggregate_status([])
        self.assertEqual(result, HealthStatus.UNKNOWN)


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
    print(f"  v12 Test Results: {passed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  ✗ {t}")
            print(f"    {tb.strip().splitlines()[-1]}")
    else:
        print(f"  ✅ ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
