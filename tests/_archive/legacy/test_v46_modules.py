"""OMNI AGENT v46: MultiTenancy, ContextualBandit, DataLineage, HotReloader"""
import asyncio, os, sys, tempfile, time, types, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# MULTI-TENANCY
# ════════════════════════════════════════════════════════
class TestMultiTenancy(unittest.TestCase):
    def setUp(self):
        from agent.multi_tenancy import TenantManager, Plan
        self.Plan = Plan
        self.mgr = TenantManager(db_path=":memory:")

    def test_create_tenant(self):
        t = self.mgr.create_tenant("Acme Corp")
        self.assertIsNotNone(t.tenant_id)
        self.assertEqual(t.name, "Acme Corp")

    def test_get_tenant(self):
        t = self.mgr.create_tenant("Beta Inc")
        got = self.mgr.get_tenant(t.tenant_id)
        self.assertEqual(got.tenant_id, t.tenant_id)

    def test_not_found_raises(self):
        from agent.multi_tenancy import TenantNotFound
        with self.assertRaises(TenantNotFound):
            self.mgr.get_tenant("nonexistent")

    def test_list_tenants(self):
        self.mgr.create_tenant("A"); self.mgr.create_tenant("B")
        self.assertEqual(len(self.mgr.list_tenants()), 2)

    def test_list_active_only(self):
        t1 = self.mgr.create_tenant("Active")
        t2 = self.mgr.create_tenant("Inactive")
        self.mgr.deactivate_tenant(t2.tenant_id)
        active = self.mgr.list_tenants(active_only=True)
        self.assertTrue(all(t.active for t in active))

    def test_update_plan(self):
        t = self.mgr.create_tenant("Upgrade Me", plan=self.Plan.FREE)
        self.mgr.update_plan(t.tenant_id, self.Plan.PRO)
        self.assertEqual(self.mgr.get_tenant(t.tenant_id).plan, self.Plan.PRO)

    def test_deactivate_tenant(self):
        t = self.mgr.create_tenant("Deactivate Me")
        self.mgr.deactivate_tenant(t.tenant_id)
        self.assertFalse(self.mgr.get_tenant(t.tenant_id).active)

    def test_delete_tenant(self):
        from agent.multi_tenancy import TenantNotFound
        t = self.mgr.create_tenant("Delete Me")
        self.mgr.delete_tenant(t.tenant_id)
        with self.assertRaises(TenantNotFound):
            self.mgr.get_tenant(t.tenant_id)

    def test_plan_limits(self):
        t = self.mgr.create_tenant("Free User", plan=self.Plan.FREE)
        self.assertEqual(t.limits["requests_per_day"], 100)

    def test_enterprise_unlimited(self):
        t = self.mgr.create_tenant("Big Corp", plan=self.Plan.ENTERPRISE)
        self.assertEqual(t.limits["requests_per_day"], -1)

    def test_check_quota_passes(self):
        t = self.mgr.create_tenant("OK User", plan=self.Plan.PRO)
        self.assertTrue(self.mgr.check_quota(t.tenant_id, tokens=100))

    def test_quota_exceeded_requests(self):
        from agent.multi_tenancy import QuotaExceeded
        t = self.mgr.create_tenant("Over Limit", plan=self.Plan.FREE)
        usage = self.mgr._usage[t.tenant_id]
        usage.requests = 100  # at limit
        with self.assertRaises(QuotaExceeded):
            self.mgr.check_quota(t.tenant_id)

    def test_quota_exceeded_tokens(self):
        from agent.multi_tenancy import QuotaExceeded
        t = self.mgr.create_tenant("Token Hog", plan=self.Plan.FREE)
        usage = self.mgr._usage[t.tenant_id]
        usage.tokens = 49_999
        with self.assertRaises(QuotaExceeded):
            self.mgr.check_quota(t.tenant_id, tokens=10)

    def test_inactive_tenant_quota_raises(self):
        from agent.multi_tenancy import QuotaExceeded
        t = self.mgr.create_tenant("Banned")
        self.mgr.deactivate_tenant(t.tenant_id)
        with self.assertRaises(QuotaExceeded):
            self.mgr.check_quota(t.tenant_id)

    def test_record_usage(self):
        t = self.mgr.create_tenant("Usage Tracker")
        self.mgr.record_usage(t.tenant_id, tokens=500, cost_usd=0.01)
        u = self.mgr.get_usage(t.tenant_id)
        self.assertEqual(u["requests"], 1)
        self.assertEqual(u["tokens"], 500)

    def test_record_error(self):
        t = self.mgr.create_tenant("Error Prone")
        self.mgr.record_usage(t.tenant_id, error=True)
        u = self.mgr.get_usage(t.tenant_id)
        self.assertEqual(u["errors"], 1)

    def test_concurrent_tracking(self):
        t = self.mgr.create_tenant("Concurrent User")
        self.mgr.enter_request(t.tenant_id)
        self.mgr.enter_request(t.tenant_id)
        self.assertEqual(self.mgr._concurrent[t.tenant_id], 2)
        self.mgr.exit_request(t.tenant_id)
        self.assertEqual(self.mgr._concurrent[t.tenant_id], 1)

    def test_namespace(self):
        t = self.mgr.create_tenant("NS Corp")
        ns = self.mgr.namespace(t.tenant_id, "session:abc")
        self.assertIn(t.tenant_id, ns)
        self.assertIn("session:abc", ns)

    def test_flush_billing(self):
        t = self.mgr.create_tenant("Billed User")
        self.mgr.record_usage(t.tenant_id, tokens=1000, cost_usd=0.05)
        record = self.mgr.flush_billing(t.tenant_id)
        self.assertIn("cost_usd", record)

    def test_billing_history(self):
        t = self.mgr.create_tenant("History User")
        self.mgr.record_usage(t.tenant_id, tokens=100, cost_usd=0.01)
        self.mgr.flush_billing(t.tenant_id)
        hist = self.mgr.billing_history(t.tenant_id)
        self.assertEqual(len(hist), 1)

    def test_stats(self):
        self.mgr.create_tenant("A", plan=self.Plan.FREE)
        self.mgr.create_tenant("B", plan=self.Plan.PRO)
        s = self.mgr.stats()
        self.assertEqual(s["total_tenants"], 2)
        self.assertEqual(s["plans"]["free"], 1)

    def test_tenant_to_dict(self):
        t = self.mgr.create_tenant("Dict Test")
        d = t.to_dict()
        for k in ["tenant_id", "name", "plan", "active", "limits"]:
            self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# CONTEXTUAL BANDIT
# ════════════════════════════════════════════════════════
class TestContextualBandit(unittest.TestCase):
    def setUp(self):
        from agent.contextual_bandit import Bandit, Strategy
        self.Strategy = Strategy
        self.bandit = Bandit(strategy=Strategy.UCB1, db_path=":memory:")
        for arm_id in ["model_a", "model_b", "model_c"]:
            self.bandit.add_arm(arm_id)

    def test_add_arm(self):
        from agent.contextual_bandit import Bandit
        b = Bandit(db_path=":memory:")
        arm = b.add_arm("test", "Test Arm")
        self.assertEqual(arm.arm_id, "test")

    def test_select_unpulled_first(self):
        # With 3 unpulled arms, should always pick one
        arm_id = self.bandit.select()
        self.assertIn(arm_id, ["model_a", "model_b", "model_c"])

    def test_update_reward(self):
        self.bandit.select()
        self.bandit.update("model_a", 0.8)
        arm = self.bandit.get_arm("model_a")
        self.assertEqual(arm.n, 1)
        self.assertAlmostEqual(arm.total_reward, 0.8)

    def test_mean_reward(self):
        self.bandit.update("model_a", 0.6)
        self.bandit.update("model_a", 0.8)
        arm = self.bandit.get_arm("model_a")
        self.assertAlmostEqual(arm.mean_reward, 0.7)

    def test_ucb1_selects_best(self):
        # Give model_a high reward consistently
        for _ in range(10):
            self.bandit.update("model_a", 1.0)
            self.bandit.update("model_b", 0.1)
            self.bandit.update("model_c", 0.1)
        # After enough pulls, UCB should favour model_a
        counts = {"model_a": 0, "model_b": 0, "model_c": 0}
        for _ in range(20):
            counts[self.bandit.select()] += 1
        self.assertGreater(counts["model_a"], counts["model_b"])

    def test_thompson_strategy(self):
        from agent.contextual_bandit import Bandit, Strategy
        b = Bandit(strategy=Strategy.THOMPSON, db_path=":memory:")
        b.add_arm("x"); b.add_arm("y")
        b.update("x", 1.0); b.update("y", 0.0)
        arm_id = b.select()
        self.assertIn(arm_id, ["x", "y"])

    def test_epsilon_greedy(self):
        from agent.contextual_bandit import Bandit, Strategy
        b = Bandit(strategy=Strategy.EPSILON, epsilon=0.0, db_path=":memory:")
        b.add_arm("best"); b.add_arm("worst")
        b.update("best",  1.0)
        b.update("worst", 0.0)
        # epsilon=0 → pure greedy
        self.assertEqual(b.select(), "best")

    def test_greedy_strategy(self):
        from agent.contextual_bandit import Bandit, Strategy
        b = Bandit(strategy=Strategy.GREEDY, db_path=":memory:")
        b.add_arm("g1"); b.add_arm("g2")
        b.update("g1", 0.9)
        b.update("g2", 0.1)
        self.assertEqual(b.select(), "g1")

    def test_best_arm(self):
        self.bandit.update("model_a", 0.9)
        self.bandit.update("model_b", 0.3)
        self.bandit.update("model_c", 0.5)
        self.assertEqual(self.bandit.best_arm(), "model_a")

    def test_regret_non_negative(self):
        self.bandit.update("model_a", 0.5)
        r = self.bandit.regret(1.0)
        self.assertGreaterEqual(r, 0)

    def test_select_and_update(self):
        arm_id, reward = self.bandit.select_and_update(lambda _: 1.0)
        self.assertIn(arm_id, ["model_a", "model_b", "model_c"])
        self.assertEqual(reward, 1.0)

    def test_history_tracked(self):
        self.bandit.update("model_a", 0.7)
        self.bandit.update("model_b", 0.5)
        hist = self.bandit.history()
        self.assertEqual(len(hist), 2)

    def test_db_history(self):
        self.bandit.update("model_a", 0.9)
        rows = self.bandit.db_history()
        self.assertGreater(len(rows), 0)

    def test_remove_arm(self):
        self.bandit.remove_arm("model_c")
        self.assertIsNone(self.bandit.get_arm("model_c"))

    def test_unknown_arm_update_raises(self):
        with self.assertRaises(KeyError):
            self.bandit.update("unknown", 1.0)

    def test_empty_bandit_raises(self):
        from agent.contextual_bandit import Bandit
        b = Bandit(db_path=":memory:")
        with self.assertRaises(ValueError):
            b.select()

    def test_arm_to_dict(self):
        arm = self.bandit.get_arm("model_a")
        d = arm.to_dict()
        for k in ["arm_id", "label", "n", "mean_reward"]:
            self.assertIn(k, d)

    def test_stats(self):
        self.bandit.update("model_a", 0.8)
        s = self.bandit.stats()
        self.assertIn("total_pulls", s)
        self.assertIn("best_arm", s)

    def test_bandit_registry(self):
        from agent.contextual_bandit import BanditRegistry
        reg = BanditRegistry()
        b = reg.get_or_create("routing")
        b.add_arm("fast"); b.add_arm("smart")
        self.assertIn("routing", reg.list_bandits())

    def test_registry_stats_all(self):
        from agent.contextual_bandit import BanditRegistry
        reg = BanditRegistry()
        reg.get_or_create("b1").add_arm("a1")
        reg.get_or_create("b2").add_arm("a2")
        s = reg.stats_all()
        self.assertIn("b1", s); self.assertIn("b2", s)

# ════════════════════════════════════════════════════════
# DATA LINEAGE
# ════════════════════════════════════════════════════════
class TestDataLineage(unittest.TestCase):
    def setUp(self):
        from agent.data_lineage import LineageGraph, NodeType
        self.NodeType = NodeType
        self.g = LineageGraph(db_path=":memory:")

    def test_add_node(self):
        n = self.g.add_node("raw_data", self.NodeType.SOURCE)
        self.assertIsNotNone(n.node_id)
        self.assertEqual(n.name, "raw_data")

    def test_get_node(self):
        n = self.g.add_node("x")
        got = self.g.get_node(n.node_id)
        self.assertEqual(got.node_id, n.node_id)

    def test_add_edge(self):
        a = self.g.add_node("A"); b = self.g.add_node("B")
        e = self.g.add_edge(a.node_id, b.node_id, transform="filter")
        self.assertEqual(e.transform, "filter")

    def test_edge_invalid_source_raises(self):
        b = self.g.add_node("B")
        with self.assertRaises(KeyError):
            self.g.add_edge("bad_source", b.node_id)

    def test_edge_invalid_target_raises(self):
        a = self.g.add_node("A")
        with self.assertRaises(KeyError):
            self.g.add_edge(a.node_id, "bad_target")

    def test_upstream(self):
        a = self.g.add_node("A"); b = self.g.add_node("B"); c = self.g.add_node("C")
        self.g.add_edge(a.node_id, b.node_id)
        self.g.add_edge(b.node_id, c.node_id)
        up = self.g.upstream(c.node_id)
        self.assertIn(a.node_id, up)
        self.assertIn(b.node_id, up)

    def test_downstream(self):
        a = self.g.add_node("A"); b = self.g.add_node("B"); c = self.g.add_node("C")
        self.g.add_edge(a.node_id, b.node_id)
        self.g.add_edge(b.node_id, c.node_id)
        down = self.g.downstream(a.node_id)
        self.assertIn(b.node_id, down)
        self.assertIn(c.node_id, down)

    def test_upstream_empty_for_root(self):
        a = self.g.add_node("A")
        self.assertEqual(self.g.upstream(a.node_id), [])

    def test_path_direct(self):
        a = self.g.add_node("A"); b = self.g.add_node("B")
        self.g.add_edge(a.node_id, b.node_id)
        p = self.g.path(a.node_id, b.node_id)
        self.assertEqual(p, [a.node_id, b.node_id])

    def test_path_multi_hop(self):
        a = self.g.add_node("A"); b = self.g.add_node("B"); c = self.g.add_node("C")
        self.g.add_edge(a.node_id, b.node_id)
        self.g.add_edge(b.node_id, c.node_id)
        p = self.g.path(a.node_id, c.node_id)
        self.assertEqual(p[0], a.node_id)
        self.assertEqual(p[-1], c.node_id)

    def test_path_none_if_disconnected(self):
        a = self.g.add_node("A"); b = self.g.add_node("B")
        self.assertIsNone(self.g.path(a.node_id, b.node_id))

    def test_root_nodes(self):
        a = self.g.add_node("A"); b = self.g.add_node("B")
        self.g.add_edge(a.node_id, b.node_id)
        roots = [n.node_id for n in self.g.root_nodes()]
        self.assertIn(a.node_id, roots)
        self.assertNotIn(b.node_id, roots)

    def test_leaf_nodes(self):
        a = self.g.add_node("A"); b = self.g.add_node("B")
        self.g.add_edge(a.node_id, b.node_id)
        leaves = [n.node_id for n in self.g.leaf_nodes()]
        self.assertIn(b.node_id, leaves)
        self.assertNotIn(a.node_id, leaves)

    def test_no_cycle_dag(self):
        a = self.g.add_node("A"); b = self.g.add_node("B")
        self.g.add_edge(a.node_id, b.node_id)
        self.assertFalse(self.g.has_cycle())

    def test_find_nodes_by_name(self):
        self.g.add_node("sales_data"); self.g.add_node("user_data")
        found = self.g.find_nodes(name="sales")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "sales_data")

    def test_find_nodes_by_type(self):
        self.g.add_node("src", self.NodeType.SOURCE)
        self.g.add_node("xfm", self.NodeType.TRANSFORM)
        found = self.g.find_nodes(node_type=self.NodeType.SOURCE)
        self.assertTrue(all(n.node_type == self.NodeType.SOURCE for n in found))

    def test_find_nodes_by_tag(self):
        self.g.add_node("tagged", tags=["pii"])
        self.g.add_node("untagged")
        found = self.g.find_nodes(tag="pii")
        self.assertEqual(len(found), 1)

    def test_delete_node_removes_edges(self):
        a = self.g.add_node("A"); b = self.g.add_node("B")
        self.g.add_edge(a.node_id, b.node_id)
        self.g.delete_node(a.node_id)
        self.assertEqual(len(self.g._edges), 0)

    def test_to_dict(self):
        a = self.g.add_node("A"); b = self.g.add_node("B")
        self.g.add_edge(a.node_id, b.node_id)
        d = self.g.to_dict()
        self.assertEqual(len(d["nodes"]), 2)
        self.assertEqual(len(d["edges"]), 1)

    def test_stats(self):
        a = self.g.add_node("A"); b = self.g.add_node("B")
        self.g.add_edge(a.node_id, b.node_id)
        s = self.g.stats()
        self.assertEqual(s["nodes"], 2)
        self.assertEqual(s["edges"], 1)
        self.assertFalse(s["has_cycle"])

    def test_depth_limited_upstream(self):
        a = self.g.add_node("A"); b = self.g.add_node("B"); c = self.g.add_node("C")
        self.g.add_edge(a.node_id, b.node_id)
        self.g.add_edge(b.node_id, c.node_id)
        up = self.g.upstream(c.node_id, depth=1)
        self.assertIn(b.node_id, up)
        self.assertNotIn(a.node_id, up)

# ════════════════════════════════════════════════════════
# HOT RELOADER
# ════════════════════════════════════════════════════════
class TestHotReloader(unittest.TestCase):
    def setUp(self):
        from agent.hot_reloader import HotReloader
        self.HotReloader = HotReloader
        self.td = tempfile.mkdtemp()

    def _write_module(self, name: str, content: str) -> str:
        path = os.path.join(self.td, f"{name}.py")
        with open(path, "w") as f:
            f.write(content)
        return path

    def _load_module(self, reloader, name: str, path: str):
        """Register and do initial load."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        import sys; sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return reloader.register(name, path)

    def test_register_module(self):
        hr = self.HotReloader()
        path = self._write_module("mod_a", "VALUE = 1\n")
        self._load_module(hr, "mod_a", path)
        self.assertTrue(hr.is_registered("mod_a"))

    def test_unregister_module(self):
        hr = self.HotReloader()
        path = self._write_module("mod_b", "VALUE = 1\n")
        self._load_module(hr, "mod_b", path)
        hr.unregister("mod_b")
        self.assertFalse(hr.is_registered("mod_b"))

    def test_reload_module(self):
        hr = self.HotReloader()
        path = self._write_module("mod_c", "VALUE = 1\n")
        self._load_module(hr, "mod_c", path)
        with open(path, "w") as f:
            f.write("VALUE = 42\n")
        success = hr.reload("mod_c")
        self.assertTrue(success)

    def test_reload_increments_count(self):
        hr = self.HotReloader()
        path = self._write_module("mod_d", "X = 0\n")
        rec = self._load_module(hr, "mod_d", path)
        hr.reload("mod_d")
        self.assertEqual(rec.reload_count, 1)

    def test_reload_updates_attr(self):
        import sys
        hr = self.HotReloader()
        path = self._write_module("mod_e", "VALUE = 'old'\n")
        self._load_module(hr, "mod_e", path)
        with open(path, "w") as f:
            f.write("VALUE = 'new'\n")
        hr.reload("mod_e")
        self.assertEqual(sys.modules["mod_e"].VALUE, "new")

    def test_reload_error_returns_false(self):
        hr = self.HotReloader()
        path = self._write_module("mod_err", "X = 1\n")
        self._load_module(hr, "mod_err", path)
        with open(path, "w") as f:
            f.write("raise SyntaxError('bad')\n")
        result = hr.reload("mod_err")
        self.assertFalse(result)

    def test_reload_error_stored(self):
        hr = self.HotReloader()
        path = self._write_module("mod_err2", "X = 1\n")
        rec = self._load_module(hr, "mod_err2", path)
        with open(path, "w") as f:
            f.write("this is not valid python !!! @@@\n")
        hr.reload("mod_err2")
        self.assertIsNotNone(rec.last_error)

    def test_reload_unknown_raises(self):
        hr = self.HotReloader()
        with self.assertRaises(KeyError):
            hr.reload("does_not_exist")

    def test_check_changed_false_no_change(self):
        hr = self.HotReloader()
        path = self._write_module("mod_f", "X = 1\n")
        self._load_module(hr, "mod_f", path)
        self.assertFalse(hr.check_changed("mod_f"))

    def test_check_changed_true_after_write(self):
        hr = self.HotReloader()
        path = self._write_module("mod_g", "X = 1\n")
        self._load_module(hr, "mod_g", path)
        time.sleep(0.01)
        with open(path, "w") as f:
            f.write("X = 2\n")
        self.assertTrue(hr.check_changed("mod_g"))

    def test_poll_once_detects_change(self):
        hr = self.HotReloader()
        path = self._write_module("mod_h", "X = 1\n")
        self._load_module(hr, "mod_h", path)
        with open(path, "w") as f:
            f.write("X = 99\n")
        reloaded = hr.poll_once()
        self.assertIn("mod_h", reloaded)

    def test_reload_all(self):
        hr = self.HotReloader()
        p1 = self._write_module("mod_i", "X=1\n")
        p2 = self._write_module("mod_j", "Y=2\n")
        self._load_module(hr, "mod_i", p1)
        self._load_module(hr, "mod_j", p2)
        results = hr.reload_all()
        self.assertIn("mod_i", results)
        self.assertIn("mod_j", results)

    def test_on_reload_hook_called(self):
        called = []
        hr = self.HotReloader()
        hr.on_reload(lambda rec: called.append(rec.module_name))
        path = self._write_module("mod_k", "X=1\n")
        self._load_module(hr, "mod_k", path)
        hr.reload("mod_k")
        self.assertIn("mod_k", called)

    def test_on_error_hook_called(self):
        errors = []
        hr = self.HotReloader()
        hr.on_error(lambda rec, exc: errors.append(rec.module_name))
        path = self._write_module("mod_l", "X=1\n")
        self._load_module(hr, "mod_l", path)
        with open(path, "w") as f:
            f.write("invalid python syntax !!!\n")
        hr.reload("mod_l")
        self.assertIn("mod_l", errors)

    def test_clear_hooks(self):
        called = []
        hr = self.HotReloader()
        hr.on_reload(lambda rec: called.append(1))
        hr.clear_hooks()
        path = self._write_module("mod_m", "X=1\n")
        self._load_module(hr, "mod_m", path)
        hr.reload("mod_m")
        self.assertEqual(called, [])

    def test_start_stop_watching(self):
        hr = self.HotReloader(poll_interval=0.05)
        hr.start_watching()
        self.assertTrue(hr.is_watching())
        hr.stop_watching()
        self.assertFalse(hr.is_watching())

    def test_list_modules(self):
        hr = self.HotReloader()
        path = self._write_module("mod_n", "X=1\n")
        self._load_module(hr, "mod_n", path)
        modules = hr.list_modules()
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0]["module_name"], "mod_n")

    def test_stats(self):
        hr = self.HotReloader()
        path = self._write_module("mod_o", "X=1\n")
        self._load_module(hr, "mod_o", path)
        hr.reload("mod_o")
        s = hr.stats()
        self.assertEqual(s["registered"], 1)
        self.assertEqual(s["total_reloads"], 1)

    def test_get_module(self):
        import sys
        hr = self.HotReloader()
        path = self._write_module("mod_p", "VAL=77\n")
        self._load_module(hr, "mod_p", path)
        mod = hr.get_module("mod_p")
        self.assertIsNotNone(mod)

    def test_get_attr(self):
        hr = self.HotReloader()
        path = self._write_module("mod_q", "ANSWER=42\n")
        self._load_module(hr, "mod_q", path)
        val = hr.get_attr("mod_q", "ANSWER")
        self.assertEqual(val, 42)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v46: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
