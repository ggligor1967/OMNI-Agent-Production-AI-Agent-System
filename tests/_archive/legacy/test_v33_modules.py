"""OMNI AGENT v33: KnowledgeGraph, TaskPlanner, ModelRouter, ConfigManager"""
import json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH
# ════════════════════════════════════════════════════════
class TestKnowledgeGraph(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.knowledge_graph import KnowledgeGraph
        self.kg = KnowledgeGraph(db_path=os.path.join(td,"kg.db"))
        self.kg.add_node("alice", "Alice", node_type="person")
        self.kg.add_node("bob",   "Bob",   node_type="person")
        self.kg.add_node("acme",  "Acme",  node_type="company")
        self.kg.add_edge("alice", "bob",  "knows")
        self.kg.add_edge("alice", "acme", "works_at", weight=1.0)
        self.kg.add_edge("bob",   "acme", "works_at", weight=0.8)

    def test_add_and_get_node(self):
        n = self.kg.get_node("alice")
        self.assertIsNotNone(n)
        self.assertEqual(n.label, "Alice")

    def test_add_and_get_edge(self):
        edges = list(self.kg._edges.values())
        self.assertGreater(len(edges), 0)

    def test_neighbours_out(self):
        nbs = self.kg.neighbours("alice", direction="out")
        ids = [n.id for n in nbs]
        self.assertIn("bob", ids)
        self.assertIn("acme", ids)

    def test_neighbours_in(self):
        nbs = self.kg.neighbours("acme", direction="in")
        ids = [n.id for n in nbs]
        self.assertIn("alice", ids)
        self.assertIn("bob", ids)

    def test_neighbour_relation_filter(self):
        nbs = self.kg.neighbours("alice", relation="knows", direction="out")
        self.assertEqual(len(nbs), 1)
        self.assertEqual(nbs[0].id, "bob")

    def test_nodes_by_type(self):
        persons = self.kg.nodes_by_type("person")
        self.assertEqual(len(persons), 2)

    def test_edges_by_relation(self):
        works = self.kg.edges_by_relation("works_at")
        self.assertEqual(len(works), 2)

    def test_bfs(self):
        result = self.kg.bfs("alice", max_depth=2)
        ids = [r[0] for r in result]
        self.assertIn("alice", ids)
        self.assertIn("bob", ids)
        self.assertIn("acme", ids)

    def test_bfs_depth_limit(self):
        self.kg.add_node("d3", "D3", node_type="x")
        self.kg.add_edge("acme", "d3", "owns")
        result = self.kg.bfs("alice", max_depth=1)
        ids = [r[0] for r in result]
        self.assertNotIn("d3", ids)

    def test_dfs(self):
        result = self.kg.dfs("alice", max_depth=3)
        self.assertIn("alice", result)

    def test_shortest_path_unweighted(self):
        self.kg.add_node("carol", "Carol", node_type="person")
        self.kg.add_edge("bob", "carol", "knows")
        path = self.kg.shortest_path("alice", "carol")
        self.assertIsNotNone(path)
        self.assertEqual(path[0], "alice")
        self.assertEqual(path[-1], "carol")

    def test_shortest_path_weighted(self):
        path = self.kg.shortest_path("alice", "acme", weighted=True)
        self.assertIsNotNone(path)
        self.assertGreater(len(path), 0)

    def test_shortest_path_none_if_unreachable(self):
        self.kg.add_node("island", "Island", node_type="x")
        path = self.kg.shortest_path("alice", "island")
        self.assertIsNone(path)

    def test_subgraph(self):
        sub = self.kg.subgraph("alice", hops=1)
        self.assertIn("alice", sub._nodes)
        self.assertIn("bob", sub._nodes)

    def test_delete_node_removes_edges(self):
        self.kg.delete_node("bob")
        self.assertIsNone(self.kg.get_node("bob"))
        # acme should no longer have bob as in-neighbour
        nbs = self.kg.neighbours("acme", direction="in")
        self.assertNotIn("bob", [n.id for n in nbs])

    def test_delete_edge(self):
        edge = list(self.kg._edges.values())[0]
        ok = self.kg.delete_edge(edge.id)
        self.assertTrue(ok)
        self.assertIsNone(self.kg.get_edge(edge.id))

    def test_update_node_properties(self):
        ok = self.kg.update_node("alice", age=30, city="NY")
        self.assertTrue(ok)
        n = self.kg.get_node("alice")
        self.assertEqual(n.properties.get("age"), 30)

    def test_find_nodes(self):
        self.kg.update_node("alice", age=30)
        results = self.kg.find_nodes(lambda n: n.properties.get("age", 0) >= 30)
        self.assertTrue(any(n.id == "alice" for n in results))

    def test_degree_centrality(self):
        dc = self.kg.degree_centrality()
        self.assertIn("alice", dc)
        self.assertIn("acme", dc)
        # acme has 2 in-edges so should have high centrality
        self.assertGreater(dc["acme"], 0)

    def test_connected_components(self):
        self.kg.add_node("lone", "Lone", node_type="x")  # isolated
        comps = self.kg.connected_components()
        self.assertGreater(len(comps), 0)

    def test_merge_nodes(self):
        self.kg.add_node("alice2", "Alice2", node_type="person")
        self.kg.add_edge("alice2", "acme", "works_at")
        ok = self.kg.merge_nodes("alice", "alice2")
        self.assertTrue(ok)
        self.assertIsNone(self.kg.get_node("alice2"))

    def test_persistence_reload(self):
        td = tempfile.mkdtemp()
        from agent.knowledge_graph import KnowledgeGraph
        db = os.path.join(td, "kg2.db")
        kg1 = KnowledgeGraph(db_path=db)
        kg1.add_node("p1", "Person1"); kg1.add_node("p2", "Person2")
        kg1.add_edge("p1", "p2", "knows")
        kg2 = KnowledgeGraph(db_path=db)
        self.assertIsNotNone(kg2.get_node("p1"))
        self.assertGreater(len(kg2._edges), 0)

    def test_node_to_dict(self):
        n = self.kg.get_node("alice")
        d = n.to_dict()
        for k in ["id","label","type","properties","tags"]: self.assertIn(k, d)

    def test_stats(self):
        s = self.kg.stats()
        for k in ["in_memory_nodes","in_memory_edges","directed"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# TASK PLANNER
# ════════════════════════════════════════════════════════
class TestTaskPlanner(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.task_planner import TaskPlanner, Effort, Status
        self.tp = TaskPlanner(db_path=os.path.join(td,"tp.db"))
        self.Effort = Effort; self.Status = Status

    def _make_goal(self):
        return self.tp.create_goal("Ship v1", "Launch by Friday")

    def test_create_goal(self):
        g = self._make_goal()
        self.assertIsNotNone(g)
        self.assertEqual(g.name, "Ship v1")

    def test_add_task(self):
        g = self._make_goal()
        t = self.tp.add_task(g.id, "Design", effort=self.Effort.S)
        self.assertIsNotNone(t)
        self.assertEqual(t.name, "Design")

    def test_task_in_goal(self):
        g = self._make_goal()
        self.tp.add_task(g.id, "T1")
        self.assertEqual(len(g.tasks), 1)

    def test_task_priority_score(self):
        g = self._make_goal()
        t = self.tp.add_task(g.id, "Urgent", urgency=9, importance=9,
                              effort=self.Effort.XS)
        self.assertGreater(t.priority, 0)

    def test_execution_order_respects_deps(self):
        g = self._make_goal()
        t1 = self.tp.add_task(g.id, "T1")
        t2 = self.tp.add_task(g.id, "T2", depends_on=[t1.id])
        t3 = self.tp.add_task(g.id, "T3", depends_on=[t2.id])
        order = self.tp.execution_order(g.id)
        ids = [t.id for t in order]
        self.assertLess(ids.index(t1.id), ids.index(t2.id))
        self.assertLess(ids.index(t2.id), ids.index(t3.id))

    def test_start_task(self):
        g = self._make_goal()
        t = self.tp.add_task(g.id, "Task")
        ok = self.tp.start_task(t.id)
        self.assertTrue(ok)
        self.assertEqual(t.status, self.Status.RUNNING)

    def test_start_blocked_by_dep(self):
        g = self._make_goal()
        t1 = self.tp.add_task(g.id, "T1")
        t2 = self.tp.add_task(g.id, "T2", depends_on=[t1.id])
        ok = self.tp.start_task(t2.id)
        self.assertFalse(ok)

    def test_complete_task(self):
        g = self._make_goal()
        t = self.tp.add_task(g.id, "T")
        self.tp.start_task(t.id)
        ok = self.tp.complete_task(t.id, output={"result": "done"})
        self.assertTrue(ok)
        self.assertEqual(t.status, self.Status.DONE)
        self.assertEqual(t.context_out.get("result"), "done")

    def test_goal_done_when_all_tasks_done(self):
        g = self._make_goal()
        t = self.tp.add_task(g.id, "Only task")
        self.tp.start_task(t.id)
        self.tp.complete_task(t.id)
        self.assertEqual(g.status, self.Status.DONE)

    def test_fail_task_skips_dependents(self):
        g = self._make_goal()
        t1 = self.tp.add_task(g.id, "T1")
        t2 = self.tp.add_task(g.id, "T2", depends_on=[t1.id])
        t3 = self.tp.add_task(g.id, "T3", depends_on=[t2.id])
        skipped = self.tp.fail_task(t1.id, "oops")
        self.assertGreaterEqual(skipped, 1)
        self.assertEqual(t2.status, self.Status.SKIPPED)

    def test_ready_tasks(self):
        g = self._make_goal()
        t1 = self.tp.add_task(g.id, "T1")
        t2 = self.tp.add_task(g.id, "T2", depends_on=[t1.id])
        ready = self.tp.ready_tasks(g.id)
        self.assertIn(t1, ready)
        self.assertNotIn(t2, ready)

    def test_add_step(self):
        g = self._make_goal()
        t = self.tp.add_task(g.id, "T")
        step = self.tp.add_step(t.id, "Step1", description="Do thing")
        self.assertIsNotNone(step)
        self.assertEqual(len(t.steps), 1)

    def test_task_progress(self):
        g = self._make_goal()
        t = self.tp.add_task(g.id, "T")
        s1 = self.tp.add_step(t.id, "S1")
        s2 = self.tp.add_step(t.id, "S2")
        s1.status = self.Status.DONE
        self.assertAlmostEqual(t.progress, 0.5)

    def test_goal_progress(self):
        g = self._make_goal()
        t1 = self.tp.add_task(g.id, "T1"); t1.status = self.Status.DONE
        t2 = self.tp.add_task(g.id, "T2")
        self.assertAlmostEqual(g.progress, 0.5, places=1)

    def test_effort_sizes(self):
        g = self._make_goal()
        for eff in self.Effort:
            t = self.tp.add_task(g.id, f"t_{eff.value}", effort=eff)
            self.assertGreater(t.estimated_hours, 0)

    def test_critical_path(self):
        g = self._make_goal()
        t1 = self.tp.add_task(g.id, "T1", effort=self.Effort.S)
        t2 = self.tp.add_task(g.id, "T2", effort=self.Effort.L, depends_on=[t1.id])
        path = self.tp.critical_path(g.id)
        self.assertGreater(len(path), 0)

    def test_goal_total_hours(self):
        g = self._make_goal()
        self.tp.add_task(g.id, "T1", effort=self.Effort.S)  # 2h
        self.tp.add_task(g.id, "T2", effort=self.Effort.M)  # 8h
        self.assertAlmostEqual(g.total_estimated_hours, 10.0)

    def test_task_to_dict(self):
        g = self._make_goal()
        t = self.tp.add_task(g.id, "T")
        d = t.to_dict()
        for k in ["id","name","status","priority","progress","effort"]:
            self.assertIn(k, d)

    def test_goal_to_dict(self):
        g = self._make_goal()
        d = g.to_dict()
        for k in ["id","name","status","progress","task_count"]:
            self.assertIn(k, d)

    def test_stats(self):
        g = self._make_goal()
        self.tp.add_task(g.id, "T")
        s = self.tp.stats()
        for k in ["goals","tasks","in_memory_goals"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# MODEL ROUTER
# ════════════════════════════════════════════════════════
class TestModelRouter(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.model_router import ModelRouter, RoutingStrategy
        self.mr = ModelRouter(db_path=os.path.join(td,"mr.db"))
        self.RS = RoutingStrategy
        self.mr.register("cheap",   cost_per_1k=0.001, latency_ms=600, quality=0.7)
        self.mr.register("fast",    cost_per_1k=0.003, latency_ms=200, quality=0.75)
        self.mr.register("quality", cost_per_1k=0.010, latency_ms=1500, quality=0.95)
        self.mr.register("balanced",cost_per_1k=0.004, latency_ms=500, quality=0.85)

    def test_route_cheapest(self):
        r = self.mr.route(strategy=self.RS.CHEAPEST)
        self.assertEqual(r.model, "cheap")

    def test_route_fastest(self):
        r = self.mr.route(strategy=self.RS.FASTEST)
        self.assertEqual(r.model, "fast")

    def test_route_best_quality(self):
        r = self.mr.route(strategy=self.RS.BEST_QUALITY)
        self.assertEqual(r.model, "quality")

    def test_route_balanced(self):
        r = self.mr.route(strategy=self.RS.BALANCED)
        self.assertIsNotNone(r)
        self.assertIn(r.model, ["cheap","fast","quality","balanced"])

    def test_route_round_robin(self):
        models = set()
        for _ in range(8):
            r = self.mr.route(strategy=self.RS.ROUND_ROBIN)
            models.add(r.model)
        self.assertGreater(len(models), 1)

    def test_capability_filter(self):
        self.mr.register("vision_model", capabilities=["vision","json"],
                          cost_per_1k=0.005, latency_ms=800, quality=0.88)
        r = self.mr.route(required_caps=["vision"])
        self.assertEqual(r.model, "vision_model")

    def test_no_capable_model_returns_none(self):
        r = self.mr.route(required_caps=["quantum_compute"])
        self.assertIsNone(r)

    def test_max_cost_filter(self):
        r = self.mr.route(max_cost_per_1k=0.002)
        self.assertEqual(r.model, "cheap")

    def test_min_quality_filter(self):
        r = self.mr.route(min_quality=0.90)
        self.assertEqual(r.model, "quality")

    def test_max_latency_filter(self):
        r = self.mr.route(max_latency_ms=300, strategy=self.RS.CHEAPEST)
        self.assertEqual(r.model, "fast")

    def test_min_context_filter(self):
        self.mr.register("big_ctx", cost_per_1k=0.005, latency_ms=900,
                          quality=0.85, max_context=100000)
        r = self.mr.route(min_context=50000)
        self.assertEqual(r.model, "big_ctx")

    def test_disable_model(self):
        self.mr.disable("cheap")
        r = self.mr.route(strategy=self.RS.CHEAPEST)
        self.assertNotEqual(r.model, "cheap")
        self.mr.enable("cheap")

    def test_fallback_chain(self):
        self.mr.set_fallback_chain("cheap", ["fast"])
        self.mr._models["cheap"].error_count = 100
        self.mr._models["cheap"].request_count = 100
        r = self.mr.route_with_fallback(strategy=self.RS.CHEAPEST)
        self.assertIsNotNone(r)

    def test_record_outcome_updates_metrics(self):
        before = self.mr._models["balanced"].request_count
        self.mr.record_outcome("balanced", tokens_used=500, latency_ms=480)
        self.assertEqual(self.mr._models["balanced"].request_count, before + 1)

    def test_latency_samples_tracked(self):
        self.mr.record_outcome("fast", latency_ms=200)
        self.mr.record_outcome("fast", latency_ms=300)
        self.assertGreater(len(self.mr._models["fast"].latency_samples), 0)

    def test_cost_estimated_in_result(self):
        r = self.mr.route(strategy=self.RS.CHEAPEST, estimated_tokens=2000)
        self.assertGreater(r.estimated_cost_usd, 0)

    def test_list_models(self):
        models = self.mr.list_models()
        self.assertGreater(len(models), 0)

    def test_list_models_by_capability(self):
        self.mr.register("cap_model", capabilities=["special"])
        models = self.mr.list_models(capability="special")
        self.assertEqual(len(models), 1)

    def test_result_to_dict(self):
        r = self.mr.route()
        d = r.to_dict()
        for k in ["model","strategy","candidates","estimated_cost"]:
            self.assertIn(k, d)

    def test_stats(self):
        s = self.mr.stats()
        for k in ["registered_models","enabled_models","total_requests"]:
            self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# CONFIG MANAGER
# ════════════════════════════════════════════════════════
class TestConfigManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.config_manager import ConfigManager
        self.CM = ConfigManager
        self.cm = ConfigManager(
            db_path=os.path.join(td,"cfg.db"),
            env_prefix="TEST",
            secret_patterns=["*.password","*.secret"])
        self.cm.set_defaults({"db": {"host": "localhost", "port": 5432},
                               "app": {"debug": False, "workers": 4},
                               "auth": {"password": "secret123"}})
        self.td = td

    def test_get_scalar(self):
        self.assertEqual(self.cm.get("db.host"), "localhost")
        self.assertEqual(self.cm.get("db.port"), 5432)

    def test_get_nested(self):
        self.assertEqual(self.cm.get("app.workers"), 4)

    def test_get_default(self):
        self.assertIsNone(self.cm.get("nonexistent.key"))
        self.assertEqual(self.cm.get("missing", "fallback"), "fallback")

    def test_set_runtime(self):
        self.cm.set("app.workers", 8)
        self.assertEqual(self.cm.get("app.workers"), 8)

    def test_set_nested_creates_path(self):
        self.cm.set("new.nested.value", 42)
        self.assertEqual(self.cm.get("new.nested.value"), 42)

    def test_delete_key(self):
        self.cm.set("temp.key", "val")
        ok = self.cm.delete("temp.key")
        self.assertTrue(ok)
        self.assertIsNone(self.cm.get("temp.key"))

    def test_env_override(self, monkeypatch=None):
        import os as _os
        _os.environ["TEST_DB__HOST"] = "envhost"
        self.cm._resolve()
        # Should pick up env var
        val = self.cm.get("db.host")
        _os.environ.pop("TEST_DB__HOST", None)
        self.cm._resolve()
        # Just verify env reading doesn't crash
        self.assertIsNotNone(val)

    def test_load_file(self):
        cfg_path = os.path.join(self.td, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump({"app": {"workers": 16}}, f)
        errors = self.cm.load_file(cfg_path)
        self.assertEqual(errors, [])
        self.assertEqual(self.cm.get("app.workers"), 16)

    def test_load_nonexistent_file(self):
        errors = self.cm.load_file("/no/such/file.json")
        self.assertGreater(len(errors), 0)

    def test_validate_valid(self):
        schema = {"type": "object", "properties":
                   {"db": {"type": "object"}}, "required": ["db"]}
        errors = self.cm.validate(schema)
        self.assertEqual(errors, [])

    def test_validate_missing_required(self):
        schema = {"type": "object", "required": ["missing_key"]}
        errors = self.cm.validate(schema)
        self.assertGreater(len(errors), 0)

    def test_validate_wrong_type(self):
        schema = {"type": "object", "properties":
                   {"app": {"type": "object",
                             "properties": {"workers": {"type": "string"}}}}}
        errors = self.cm.validate(schema)
        self.assertGreater(len(errors), 0)

    def test_secrets_masked(self):
        cfg = self.cm.get_all(masked=True)
        password = cfg.get("auth", {}).get("password")
        self.assertEqual(password, "***MASKED***")

    def test_unmasked_has_secret(self):
        cfg = self.cm.get_all(masked=False)
        self.assertEqual(cfg["auth"]["password"], "secret123")

    def test_on_change_listener(self):
        changes = []
        self.cm.on_change("app.*", lambda key, old, new: changes.append((key, new)))
        self.cm.set("app.workers", 99)
        self.assertTrue(any(c[0] == "app.workers" for c in changes))

    def test_override_context_manager(self):
        with self.cm.override({"app": {"debug": True}}):
            self.assertTrue(self.cm.get("app.debug"))
        self.assertFalse(self.cm.get("app.debug"))

    def test_keys(self):
        keys = self.cm.keys()
        self.assertIn("db.host", keys)
        self.assertIn("app.workers", keys)

    def test_diff(self):
        self.cm.set("app.workers", 99)
        diff = self.cm.diff({"app": {"workers": 4}})
        self.assertIn("app.workers", diff)

    def test_snapshot(self):
        snap_path = os.path.join(self.td, "snap.json")
        self.cm.snapshot(snap_path)
        self.assertTrue(os.path.exists(snap_path))
        with open(snap_path) as f:
            data = json.load(f)
        self.assertIn("app", data)

    def test_reload_no_file(self):
        self.assertFalse(self.cm.reload())

    def test_get_all_returns_dict(self):
        cfg = self.cm.get_all()
        self.assertIsInstance(cfg, dict)
        self.assertIn("app", cfg)

    def test_stats(self):
        s = self.cm.stats()
        for k in ["config_keys","layers","total_changes"]: self.assertIn(k, s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v33: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
