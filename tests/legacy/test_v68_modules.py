"""OMNI AGENT v68: SignalBus, DataValidatorV2, AgentMemoryGraphV2, LoadBalancerV2"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# SIGNAL BUS
# ════════════════════════════════════════════════════════
class TestSignalBus(unittest.TestCase):
    def setUp(self):
        from agent.signal_bus import SignalBus
        self.sb = SignalBus(db_path=":memory:")

    def test_subscribe_and_emit(self):
        received = []
        self.sb.subscribe("test.event", lambda s: received.append(s.payload))
        self.sb.emit("test.event", payload=42)
        self.assertEqual(received, [42])

    def test_wildcard_star(self):
        received = []
        self.sb.subscribe("*", lambda s: received.append(s.name))
        self.sb.emit("anything")
        self.assertIn("anything", received)

    def test_prefix_wildcard(self):
        received = []
        self.sb.subscribe("auth.*", lambda s: received.append(s.name))
        self.sb.emit("auth.login")
        self.sb.emit("auth.logout")
        self.sb.emit("other.event")
        self.assertEqual(len(received), 2)

    def test_unsubscribe(self):
        received = []
        sub = self.sb.subscribe("ev", lambda s: received.append(1))
        self.sb.unsubscribe(sub.sub_id)
        self.sb.emit("ev")
        self.assertEqual(len(received), 0)

    def test_once_subscription(self):
        received = []
        self.sb.subscribe("once.ev", lambda s: received.append(1), once=True)
        self.sb.emit("once.ev")
        self.sb.emit("once.ev")
        self.assertEqual(len(received), 1)

    def test_priority_order(self):
        from agent.signal_bus import SignalPriority
        order = []
        self.sb.subscribe("prio", lambda s: order.append("low"),
                           priority=SignalPriority.LOW)
        self.sb.subscribe("prio", lambda s: order.append("high"),
                           priority=SignalPriority.HIGH)
        self.sb.subscribe("prio", lambda s: order.append("highest"),
                           priority=SignalPriority.HIGHEST)
        self.sb.emit("prio")
        self.assertEqual(order[0], "highest")
        self.assertEqual(order[-1], "low")

    def test_filter_fn(self):
        received = []
        self.sb.subscribe("filtered",
                           lambda s: received.append(s.payload),
                           filter_fn=lambda s: s.payload > 10)
        self.sb.emit("filtered", payload=5)
        self.sb.emit("filtered", payload=20)
        self.assertEqual(received, [20])

    def test_disable_enable(self):
        received = []
        sub = self.sb.subscribe("dis.ev", lambda s: received.append(1))
        self.sb.disable(sub.sub_id)
        self.sb.emit("dis.ev")
        self.assertEqual(len(received), 0)
        self.sb.enable(sub.sub_id)
        self.sb.emit("dis.ev")
        self.assertEqual(len(received), 1)

    def test_queue_and_drain(self):
        received = []
        self.sb.subscribe("q.ev", lambda s: received.append(s.payload))
        self.sb.queue("q.ev", payload=1)
        self.sb.queue("q.ev", payload=2)
        self.assertEqual(len(received), 0)
        self.sb.drain()
        self.assertEqual(len(received), 2)

    def test_correlation_id(self):
        received_corr = []
        self.sb.subscribe("corr.ev",
                           lambda s: received_corr.append(s.correlation_id))
        self.sb.emit("corr.ev", correlation_id="chain-123")
        self.assertEqual(received_corr[0], "chain-123")

    def test_handler_error_isolation(self):
        received = []
        self.sb.subscribe("err.ev",
                           lambda s: (_ for _ in ()).throw(RuntimeError("fail")))
        self.sb.subscribe("err.ev", lambda s: received.append(1))
        rec = self.sb.emit("err.ev")
        self.assertEqual(len(received), 1)
        self.assertGreater(len(rec.errors), 0)

    def test_history(self):
        self.sb.emit("h.ev", payload="a")
        self.sb.emit("h.ev", payload="b")
        h = self.sb.history("h.ev")
        self.assertEqual(len(h), 2)

    def test_dispatch_count(self):
        sub = self.sb.subscribe("dc.ev", lambda s: None)
        self.sb.emit("dc.ev")
        self.sb.emit("dc.ev")
        self.assertEqual(sub.dispatch_count, 2)

    def test_stats(self):
        self.sb.subscribe("st.ev", lambda s: None)
        self.sb.emit("st.ev")
        s = self.sb.stats()
        self.assertGreater(s["signals_emitted"], 0)
        self.assertGreater(s["subscriptions"], 0)

# ════════════════════════════════════════════════════════
# DATA VALIDATOR V2
# ════════════════════════════════════════════════════════
class TestDataValidatorV2(unittest.TestCase):
    def setUp(self):
        from agent.data_validator_v2 import DataValidatorV2, FieldType
        self.dv = DataValidatorV2(db_path=":memory:")
        s = self.dv.create_schema("user", allow_extra=False)
        self.schema_id = s.schema_id
        self.dv.add_field(s.schema_id, "name", FieldType.STRING,
                           min_len=2, max_len=50)
        self.dv.add_field(s.schema_id, "age", FieldType.INTEGER,
                           min_val=0, max_val=150)
        self.dv.add_field(s.schema_id, "email", FieldType.EMAIL)

    def test_valid_data(self):
        res = self.dv.validate(
            {"name": "Alice", "age": 30, "email": "alice@example.com"},
            self.schema_id)
        self.assertTrue(res.valid)

    def test_missing_required(self):
        res = self.dv.validate({"name": "Bob"}, self.schema_id)
        self.assertFalse(res.valid)

    def test_type_coercion(self):
        res = self.dv.validate(
            {"name": "Carol", "age": "25",
             "email": "carol@example.com"},
            self.schema_id)
        self.assertTrue(res.valid)
        self.assertEqual(res.transformed_data["age"], 25)

    def test_range_violation(self):
        res = self.dv.validate(
            {"name": "Dave", "age": 200,
             "email": "d@d.com"},
            self.schema_id)
        self.assertFalse(res.valid)

    def test_email_format(self):
        res = self.dv.validate(
            {"name": "Eve", "age": 20, "email": "not-an-email"},
            self.schema_id)
        self.assertFalse(res.valid)

    def test_min_len(self):
        res = self.dv.validate(
            {"name": "X", "age": 20, "email": "x@x.com"},
            self.schema_id)
        self.assertFalse(res.valid)

    def test_max_len(self):
        res = self.dv.validate(
            {"name": "A" * 100, "age": 20, "email": "a@a.com"},
            self.schema_id)
        self.assertFalse(res.valid)

    def test_extra_field_rejected(self):
        res = self.dv.validate(
            {"name": "Frank", "age": 30, "email": "f@f.com", "extra": "oops"},
            self.schema_id)
        self.assertFalse(res.valid)

    def test_allowed_values(self):
        from agent.data_validator_v2 import FieldType
        s = self.dv.create_schema("status_s", allow_extra=True)
        self.dv.add_field(s.schema_id, "status", FieldType.STRING,
                           allowed=["active", "inactive"])
        res = self.dv.validate({"status": "deleted"}, s.schema_id)
        self.assertFalse(res.valid)

    def test_regex_pattern(self):
        from agent.data_validator_v2 import FieldType
        s = self.dv.create_schema("phone_s", allow_extra=True)
        self.dv.add_field(s.schema_id, "phone", FieldType.STRING,
                           pattern=r"^\d{10}$")
        res = self.dv.validate({"phone": "123"}, s.schema_id)
        self.assertFalse(res.valid)

    def test_custom_validator(self):
        from agent.data_validator_v2 import FieldType
        s = self.dv.create_schema("cv_s", allow_extra=True)
        self.dv.add_field(s.schema_id, "score", FieldType.INTEGER,
                           custom_validators=[
                               lambda v: "Must be even" if v % 2 != 0 else None])
        res = self.dv.validate({"score": 3}, s.schema_id)
        self.assertFalse(res.valid)
        res2 = self.dv.validate({"score": 4}, s.schema_id)
        self.assertTrue(res2.valid)

    def test_transform_applied(self):
        from agent.data_validator_v2 import FieldType
        s = self.dv.create_schema("tr_s", allow_extra=True)
        self.dv.add_field(s.schema_id, "name", FieldType.STRING,
                           transform_fn=str.upper)
        res = self.dv.validate({"name": "alice"}, s.schema_id)
        self.assertEqual(res.transformed_data["name"], "ALICE")

    def test_cross_field_rule(self):
        s = self.dv.create_schema("cross_s", allow_extra=True)
        from agent.data_validator_v2 import FieldType
        self.dv.add_field(s.schema_id, "start", FieldType.INTEGER)
        self.dv.add_field(s.schema_id, "end",   FieldType.INTEGER)
        self.dv.add_cross_rule(s.schema_id,
            lambda d: "end must be > start"
                      if d.get("end", 0) <= d.get("start", 0) else None)
        res = self.dv.validate({"start": 10, "end": 5}, s.schema_id)
        self.assertFalse(res.valid)

    def test_default_value(self):
        from agent.data_validator_v2 import FieldType
        s = self.dv.create_schema("def_s", allow_extra=True)
        self.dv.add_field(s.schema_id, "role", FieldType.STRING,
                           required=True, default="user")
        res = self.dv.validate({}, s.schema_id)
        self.assertTrue(res.valid)
        self.assertEqual(res.transformed_data["role"], "user")

    def test_batch_validate(self):
        records = [
            {"name": "Alice", "age": 25, "email": "a@a.com"},
            {"name": "B", "age": -1, "email": "bad"},
        ]
        results = self.dv.validate_batch(records, self.schema_id)
        self.assertTrue(results[0].valid)
        self.assertFalse(results[1].valid)

    def test_stats(self):
        self.dv.validate({"name": "Alice", "age": 25, "email": "a@a.com"},
                          self.schema_id)
        s = self.dv.stats()
        self.assertGreater(s["runs"], 0)

# ════════════════════════════════════════════════════════
# AGENT MEMORY GRAPH V2
# ════════════════════════════════════════════════════════
class TestAgentMemoryGraphV2(unittest.TestCase):
    def setUp(self):
        from agent.agent_memory_graph_v2 import AgentMemoryGraphV2
        self.mg = AgentMemoryGraphV2(db_path=":memory:")

    def test_add_node(self):
        n = self.mg.add_node("Python", content="A programming language")
        self.assertIsNotNone(n.node_id)

    def test_get_node_boosts(self):
        n = self.mg.add_node("boost_node", importance=0.5)
        old_imp = n.importance
        self.mg.get_node(n.node_id)
        self.assertGreater(n.importance, old_imp)

    def test_remove_node(self):
        n = self.mg.add_node("to_remove")
        ok = self.mg.remove_node(n.node_id)
        self.assertTrue(ok)
        self.assertIsNone(self.mg.get_node(n.node_id))

    def test_add_edge(self):
        n1 = self.mg.add_node("n1")
        n2 = self.mg.add_node("n2")
        e  = self.mg.add_edge(n1.node_id, n2.node_id)
        self.assertIsNotNone(e.edge_id)

    def test_edge_requires_nodes(self):
        with self.assertRaises(KeyError):
            self.mg.add_edge("fake1", "fake2")

    def test_neighbors_out(self):
        n1 = self.mg.add_node("nb1")
        n2 = self.mg.add_node("nb2")
        n3 = self.mg.add_node("nb3")
        self.mg.add_edge(n1.node_id, n2.node_id)
        self.mg.add_edge(n1.node_id, n3.node_id)
        nbs = self.mg.neighbors(n1.node_id)
        self.assertEqual(len(nbs), 2)

    def test_bfs(self):
        n1 = self.mg.add_node("b1")
        n2 = self.mg.add_node("b2")
        n3 = self.mg.add_node("b3")
        self.mg.add_edge(n1.node_id, n2.node_id)
        self.mg.add_edge(n2.node_id, n3.node_id)
        path = self.mg.bfs(n1.node_id)
        self.assertEqual(path[0], n1.node_id)
        self.assertIn(n3.node_id, path)

    def test_dfs(self):
        n1 = self.mg.add_node("d1")
        n2 = self.mg.add_node("d2")
        self.mg.add_edge(n1.node_id, n2.node_id)
        path = self.mg.dfs(n1.node_id)
        self.assertIn(n1.node_id, path)
        self.assertIn(n2.node_id, path)

    def test_shortest_path(self):
        n1 = self.mg.add_node("sp1")
        n2 = self.mg.add_node("sp2")
        n3 = self.mg.add_node("sp3")
        self.mg.add_edge(n1.node_id, n2.node_id, weight=1.0)
        self.mg.add_edge(n2.node_id, n3.node_id, weight=1.0)
        path = self.mg.shortest_path(n1.node_id, n3.node_id)
        self.assertIsNotNone(path)
        self.assertEqual(path[0], n1.node_id)
        self.assertEqual(path[-1], n3.node_id)

    def test_keyword_search(self):
        self.mg.add_node("Python", content="programming language")
        self.mg.add_node("Java",   content="object oriented language")
        results = self.mg.search("python")
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].label, "Python")

    def test_decay(self):
        n = self.mg.add_node("decay_n", importance=1.0, decay_rate=100.0)
        time.sleep(0.01)
        self.assertLess(n.current_importance, 1.0)

    def test_prune(self):
        self.mg.add_node("low_imp", importance=0.001, decay_rate=0.0)
        self.mg.add_node("high_imp", importance=1.0, decay_rate=0.0)
        pruned = self.mg.prune(min_importance=0.01)
        self.assertGreater(pruned, 0)
        self.assertIn("high_imp",
                       [n.label for n in self.mg._nodes.values()])

    def test_contradiction_detection(self):
        from agent.agent_memory_graph_v2 import EdgeType
        n1 = self.mg.add_node("fact1")
        n2 = self.mg.add_node("fact2")
        self.mg.add_edge(n1.node_id, n2.node_id,
                          edge_type=EdgeType.CONTRADICTS)
        c = self.mg.contradictions()
        self.assertEqual(len(c), 1)

    def test_remove_edge(self):
        n1 = self.mg.add_node("re1")
        n2 = self.mg.add_node("re2")
        e  = self.mg.add_edge(n1.node_id, n2.node_id)
        ok = self.mg.remove_edge(e.edge_id)
        self.assertTrue(ok)
        self.assertEqual(len(self.mg.neighbors(n1.node_id)), 0)

    def test_stats(self):
        self.mg.add_node("sn1")
        s = self.mg.stats()
        self.assertGreater(s["nodes"], 0)

# ════════════════════════════════════════════════════════
# LOAD BALANCER V2
# ════════════════════════════════════════════════════════
class TestLoadBalancerV2(unittest.TestCase):
    def setUp(self):
        from agent.load_balancer_v2 import LoadBalancerV2, LBStrategy
        self.lb = LoadBalancerV2(strategy=LBStrategy.ROUND_ROBIN,
                                  db_path=":memory:", error_threshold=3)

    def test_add_backend(self):
        b = self.lb.add_backend("http://server1:8080")
        self.assertIsNotNone(b.backend_id)

    def test_route_success(self):
        b = self.lb.add_backend("http://s1")
        result, rec = self.lb.route(lambda b: "ok")
        self.assertEqual(result, "ok")
        self.assertTrue(rec.success)

    def test_round_robin(self):
        b1 = self.lb.add_backend("http://s1")
        b2 = self.lb.add_backend("http://s2")
        visited = set()
        for _ in range(4):
            _, rec = self.lb.route(lambda b: b.backend_id)
            visited.add(_)
        # Both backends should be used
        backends_used = set()
        for r in self.lb._records:
            backends_used.add(r.backend_id)
        self.assertEqual(len(backends_used), 2)

    def test_least_conn(self):
        from agent.load_balancer_v2 import LoadBalancerV2, LBStrategy
        lb = LoadBalancerV2(strategy=LBStrategy.LEAST_CONN,
                             db_path=":memory:")
        lb.add_backend("http://s1")
        lb.add_backend("http://s2")
        result, _ = lb.route(lambda b: b.address)
        self.assertIsNotNone(result)

    def test_weighted_rr(self):
        from agent.load_balancer_v2 import LoadBalancerV2, LBStrategy
        lb = LoadBalancerV2(strategy=LBStrategy.WEIGHTED_RR,
                             db_path=":memory:")
        lb.add_backend("http://heavy", weight=3)
        lb.add_backend("http://light", weight=1)
        results = []
        for _ in range(8):
            res, _ = lb.route(lambda b: b.address)
            results.append(res)
        heavy_count = results.count("http://heavy")
        self.assertGreater(heavy_count, results.count("http://light"))

    def test_circuit_breaker_opens(self):
        from agent.load_balancer_v2 import BackendStatus, CircuitState
        b = self.lb.add_backend("http://flaky", )
        for _ in range(3):
            self.lb.route(
                lambda b: (_ for _ in ()).throw(RuntimeError("fail")))
        self.assertEqual(b.circuit_state, CircuitState.OPEN)

    def test_no_backend_available(self):
        b = self.lb.add_backend("http://s1")
        self.lb.disable_backend(b.backend_id)
        result, rec = self.lb.route(lambda b: "ok")
        self.assertIsNone(result)
        self.assertFalse(rec.success)

    def test_drain_backend(self):
        from agent.load_balancer_v2 import BackendStatus
        b = self.lb.add_backend("http://drain")
        self.lb.drain_backend(b.backend_id)
        self.assertEqual(b.status, BackendStatus.DRAINING)

    def test_sticky_session(self):
        b1 = self.lb.add_backend("http://a")
        b2 = self.lb.add_backend("http://b")
        results = []
        for _ in range(3):
            res, _ = self.lb.route(lambda b: b.address,
                                    client_id="user-123", sticky=True)
            results.append(res)
        # All 3 should go to same backend after first
        self.assertEqual(results[1], results[2])

    def test_health_check(self):
        from agent.load_balancer_v2 import LoadBalancerV2, LBStrategy
        lb = LoadBalancerV2(
            strategy=LBStrategy.ROUND_ROBIN,
            db_path=":memory:",
            health_check_fn=lambda b: "good" in b.address)
        lb.add_backend("http://good-server")
        lb.add_backend("http://bad-server")
        results = lb.health_check_all()
        self.assertTrue(results[list(results.keys())[0]]
                         if "good" in lb._backends[list(results.keys())[0]].address
                         else not results[list(results.keys())[0]])

    def test_error_tracking(self):
        b = self.lb.add_backend("http://err")
        self.lb.route(
            lambda b: (_ for _ in ()).throw(RuntimeError("e")))
        self.assertEqual(b.total_errors, 1)

    def test_latency_tracking(self):
        b = self.lb.add_backend("http://lat")
        self.lb.route(lambda b: time.sleep(0.001) or "ok")
        self.assertGreater(b.total_ms, 0.0)

    def test_stats(self):
        b = self.lb.add_backend("http://st")
        self.lb.route(lambda b: "ok")
        s = self.lb.stats()
        self.assertGreater(s["backends"], 0)
        self.assertGreater(s["total_requests"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v68: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
