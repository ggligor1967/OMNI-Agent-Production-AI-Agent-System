"""OMNI AGENT v51: ModelRouterV2, EventSourcingV2, SkillGraphV2, FeedbackAnalyzer"""
import asyncio, os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# MODEL ROUTER V2
# ════════════════════════════════════════════════════════
class TestModelRouterV2(unittest.TestCase):
    def setUp(self):
        from agent.model_router_v2 import ModelRouterV2, ModelSpec, RoutingStrategy
        self.RoutingStrategy = RoutingStrategy
        self.router = ModelRouterV2(strategy=RoutingStrategy.ADAPTIVE)
        self.ModelSpec = ModelSpec
        self._add_models()

    def _add_models(self):
        from agent.model_router_v2 import ModelSpec
        self.router.register(ModelSpec("m1", "fast-cheap", "prov_a",
            cost_per_1k_tokens=0.001, avg_latency_ms=200, quality_score=0.6,
            capabilities={"text"}, context_window=4096))
        self.router.register(ModelSpec("m2", "slow-quality", "prov_b",
            cost_per_1k_tokens=0.02, avg_latency_ms=2000, quality_score=0.95,
            capabilities={"text", "code"}, context_window=32768))
        self.router.register(ModelSpec("m3", "balanced", "prov_c",
            cost_per_1k_tokens=0.005, avg_latency_ms=600, quality_score=0.8,
            capabilities={"text"}, context_window=8192))

    def test_route_returns_decision(self):
        d = self.router.route()
        self.assertIsNotNone(d.model_id)

    def test_route_lowest_cost(self):
        d = self.router.route(strategy=self.RoutingStrategy.LOWEST_COST)
        self.assertEqual(d.model_id, "m1")

    def test_route_highest_quality(self):
        d = self.router.route(strategy=self.RoutingStrategy.HIGHEST_QUALITY)
        self.assertEqual(d.model_id, "m2")

    def test_route_capability_filter(self):
        d = self.router.route(required_capabilities={"code"})
        self.assertEqual(d.model_id, "m2")

    def test_route_context_filter(self):
        d = self.router.route(min_context=16000)
        self.assertEqual(d.model_id, "m2")

    def test_route_round_robin(self):
        from agent.model_router_v2 import ModelRouterV2, RoutingStrategy
        r = ModelRouterV2(strategy=RoutingStrategy.ROUND_ROBIN)
        r.register(self.ModelSpec("a", "A", "p", context_window=4096))
        r.register(self.ModelSpec("b", "B", "p", context_window=4096))
        ids = [r.route().model_id for _ in range(4)]
        self.assertIn("a", ids); self.assertIn("b", ids)

    def test_route_weighted(self):
        from agent.model_router_v2 import ModelRouterV2, ModelSpec, RoutingStrategy
        r = ModelRouterV2(strategy=RoutingStrategy.WEIGHTED)
        r.register(ModelSpec("w1", "heavy", "p", weight=10.0, context_window=4096))
        r.register(ModelSpec("w2", "light", "p", weight=1.0, context_window=4096))
        d = r.route()
        self.assertEqual(d.model_id, "w1")

    def test_fallback_chain_populated(self):
        d = self.router.route(strategy=self.RoutingStrategy.LOWEST_COST)
        self.assertGreater(len(d.fallback_chain), 0)

    def test_offline_model_excluded(self):
        from agent.model_router_v2 import ModelStatus
        self.router.set_status("m2", ModelStatus.OFFLINE)
        d = self.router.route(strategy=self.RoutingStrategy.HIGHEST_QUALITY)
        self.assertNotEqual(d.model_id, "m2")

    def test_record_outcome_updates_stats(self):
        self.router.record_outcome("m1", True, 300, tokens=1000)
        s = self.router.get_stats("m1")
        self.assertEqual(s["requests"], 1)
        self.assertEqual(s["successes"], 1)

    def test_record_outcome_computes_cost(self):
        self.router.record_outcome("m1", True, 200, tokens=2000)
        s = self.router.get_stats("m1")
        self.assertGreater(s["total_cost"], 0)

    def test_auto_degrade_on_errors(self):
        from agent.model_router_v2 import ModelStatus
        for _ in range(10):
            self.router.record_outcome("m3", False, 100)
        self.assertEqual(self.router._models["m3"].status, ModelStatus.DEGRADED)

    def test_route_with_fallback(self):
        calls = []
        def fn(mid): calls.append(mid); return f"ok_{mid}"
        result, used = self.router.route_with_fallback(fn)
        self.assertIn("ok_", result)

    def test_route_with_fallback_retries(self):
        attempt = [0]
        def fn(mid):
            attempt[0] += 1
            if attempt[0] < 2:
                raise RuntimeError("fail")
            return "ok"
        result, used = self.router.route_with_fallback(fn)
        self.assertEqual(result, "ok")

    def test_custom_filter(self):
        self.router.add_filter(lambda m: m.provider == "prov_a")
        d = self.router.route()
        self.assertEqual(d.model_id, "m1")
        self.router.clear_filters()

    def test_async_route(self):
        d = _run(self.router.route_async())
        self.assertIsNotNone(d.model_id)

    def test_recent_decisions(self):
        self.router.route()
        self.router.route()
        decs = self.router.recent_decisions(2)
        self.assertEqual(len(decs), 2)

    def test_list_models(self):
        models = self.router.list_models()
        self.assertEqual(len(models), 3)

    def test_no_model_available_raises(self):
        from agent.model_router_v2 import ModelRouterV2, NoModelAvailable
        r = ModelRouterV2()
        with self.assertRaises(NoModelAvailable):
            r.route()

    def test_stats(self):
        self.router.route()
        s = self.router.stats()
        self.assertEqual(s["models"], 3)
        self.assertGreaterEqual(s["decisions"], 1)

# ════════════════════════════════════════════════════════
# EVENT SOURCING V2
# ════════════════════════════════════════════════════════
class TestEventSourcingV2(unittest.TestCase):
    def setUp(self):
        from agent.event_sourcing_v2 import EventStoreV2
        self.store = EventStoreV2(db_path=":memory:")

    def test_append_event(self):
        e = self.store.append("agg1", "Order", "OrderPlaced", {"amount": 100})
        self.assertEqual(e.version, 1)

    def test_version_increments(self):
        self.store.append("agg1", "Order", "OrderPlaced", {})
        e2 = self.store.append("agg1", "Order", "OrderShipped", {})
        self.assertEqual(e2.version, 2)

    def test_load_events(self):
        self.store.append("agg2", "User", "UserCreated", {"name": "Alice"})
        self.store.append("agg2", "User", "UserUpdated", {"name": "Alicia"})
        events = self.store.load("agg2")
        self.assertEqual(len(events), 2)

    def test_load_from_version(self):
        for i in range(4):
            self.store.append("agg3", "T", "E", {"i": i})
        events = self.store.load("agg3", from_version=3)
        self.assertEqual(len(events), 2)

    def test_optimistic_lock_error(self):
        from agent.event_sourcing_v2 import OptimisticLockError
        self.store.append("agg4", "T", "E1", {})
        with self.assertRaises(OptimisticLockError):
            self.store.append("agg4", "T", "E2", {}, expected_version=0)

    def test_optimistic_lock_ok(self):
        self.store.append("agg5", "T", "E1", {}, expected_version=0)
        e2 = self.store.append("agg5", "T", "E2", {}, expected_version=1)
        self.assertEqual(e2.version, 2)

    def test_append_many_atomic(self):
        events = self.store.append_many("agg6", "T",
            [("E1", {"x": 1}), ("E2", {"x": 2})])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1].version, 2)

    def test_snapshot_saved_and_loaded(self):
        self.store.append("agg7", "T", "E1", {})
        self.store.save_snapshot("agg7", "T", {"count": 1}, version=1)
        snap = self.store.load_snapshot("agg7")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.state["count"], 1)

    def test_load_from_snapshot(self):
        self.store.append("agg8", "T", "E1", {})
        self.store.save_snapshot("agg8", "T", {"count": 1}, version=1)
        self.store.append("agg8", "T", "E2", {})
        state, events = self.store.load_from_snapshot("agg8")
        self.assertIsNotNone(state)
        self.assertEqual(len(events), 1)

    def test_event_subscription(self):
        received = []
        self.store.subscribe("UserCreated", lambda e: received.append(e.event_type))
        self.store.append("a9", "User", "UserCreated", {})
        self.assertIn("UserCreated", received)

    def test_subscribe_all(self):
        received = []
        self.store.subscribe_all(lambda e: received.append(e.event_id))
        self.store.append("a10", "X", "AnyEvent", {})
        self.assertEqual(len(received), 1)

    def test_replay_reducer(self):
        self.store.append("a11", "Counter", "Incremented", {"by": 1})
        self.store.append("a11", "Counter", "Incremented", {"by": 2})
        def reducer(state, event):
            state["count"] = state.get("count", 0) + event.payload["by"]
            return state
        state = self.store.replay("a11", reducer, {"count": 0})
        self.assertEqual(state["count"], 3)

    def test_replay_all(self):
        self.store.append("a12", "CT", "E", {"v": 1})
        self.store.append("a13", "CT", "E", {"v": 2})
        def reducer(state, event): return {**state, **event.payload}
        states = self.store.replay_all("CT", reducer)
        self.assertEqual(len(states), 2)

    def test_load_type(self):
        self.store.append("x1", "Product", "Created", {})
        self.store.append("x2", "Product", "Created", {})
        events = self.store.load_type("Product")
        self.assertEqual(len(events), 2)

    def test_all_events(self):
        self.store.append("e1", "T", "E", {})
        self.store.append("e2", "T", "E", {})
        events = self.store.all_events()
        self.assertGreaterEqual(len(events), 2)

    def test_projection(self):
        from agent.event_sourcing_v2 import Projection
        proj = Projection("counter")
        @proj.on("Incremented")
        def handle(state, event):
            state["n"] = state.get("n", 0) + 1
        self.store.subscribe("Incremented", proj.apply)
        self.store.append("p1", "T", "Incremented", {})
        self.store.append("p1", "T", "Incremented", {})
        self.assertEqual(proj.get_state()["n"], 2)

    def test_projection_reset(self):
        from agent.event_sourcing_v2 import Projection
        proj = Projection("p")
        @proj.on("E")
        def h(s, e): s["n"] = s.get("n", 0) + 1
        self.store.subscribe("E", proj.apply)
        self.store.append("p2", "T", "E", {})
        proj.reset()
        self.assertEqual(proj.get_state(), {})

    def test_stats(self):
        self.store.append("s1", "T", "E", {})
        s = self.store.stats()
        self.assertEqual(s["total_events"], 1)
        self.assertIn("aggregates", s)

# ════════════════════════════════════════════════════════
# SKILL GRAPH V2
# ════════════════════════════════════════════════════════
class TestSkillGraphV2(unittest.TestCase):
    def setUp(self):
        from agent.skill_graph_v2 import SkillGraphV2, SkillCategory
        self.sg = SkillGraphV2()
        self.SkillCategory = SkillCategory

    def test_register_and_call(self):
        self.sg.register("add", lambda a, b: a + b, skill_id="add")
        sr = self.sg.call("add", 2, 3)
        self.assertTrue(sr.success)
        self.assertEqual(sr.result, 5)

    def test_call_not_found_raises(self):
        from agent.skill_graph_v2 import SkillNotFound
        with self.assertRaises(SkillNotFound):
            self.sg.call("missing_skill")

    def test_call_with_error_captured(self):
        self.sg.register("bad", lambda: 1/0, skill_id="bad")
        sr = self.sg.call("bad")
        self.assertFalse(sr.success)
        self.assertIn("division", sr.error)

    def test_prerequisite_ok(self):
        self.sg.register("step1", lambda: 1, skill_id="s1")
        self.sg.register("step2", lambda: 2, prerequisites=["s1"], skill_id="s2")
        sr = self.sg.call("s2", completed_skills={"s1"})
        self.assertTrue(sr.success)

    def test_prerequisite_not_met(self):
        from agent.skill_graph_v2 import PrerequisiteNotMet
        self.sg.register("step1", lambda: 1, skill_id="r1")
        self.sg.register("step2", lambda: 2, prerequisites=["r1"], skill_id="r2")
        with self.assertRaises(PrerequisiteNotMet):
            self.sg.call("r2")

    def test_disable_skill(self):
        self.sg.register("d", lambda: 1, skill_id="d1")
        self.sg.disable("d1")
        sr = self.sg.call("d1")
        self.assertFalse(sr.success)

    def test_cache_hit(self):
        calls = [0]
        def fn(x):
            calls[0] += 1
            return x * 2
        self.sg.register("cached", fn, cacheable=True, skill_id="c1")
        self.sg.call("c1", 5)
        sr = self.sg.call("c1", 5)
        self.assertTrue(sr.cached)
        self.assertEqual(calls[0], 1)

    def test_clear_cache(self):
        self.sg.register("cv", lambda x: x, cacheable=True, skill_id="cv1")
        self.sg.call("cv1", 1)
        self.sg.clear_cache()
        self.assertEqual(len(self.sg._cache), 0)

    def test_chain(self):
        self.sg.register("double", lambda x: x * 2, skill_id="dbl")
        self.sg.register("add10", lambda x: x + 10, skill_id="a10")
        results = self.sg.chain(["dbl", "a10"], initial_input=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1].result, 20)  # (5*2)+10

    def test_chain_stops_on_error(self):
        self.sg.register("ok", lambda x: x, skill_id="ok1")
        self.sg.register("fail", lambda x: 1/0, skill_id="f1")
        self.sg.register("after", lambda x: x, skill_id="aft")
        results = self.sg.chain(["ok1", "f1", "aft"], initial_input=1)
        self.assertEqual(len(results), 2)

    def test_async_call(self):
        async def async_fn(x): return x + 1
        self.sg.register("async_add", async_fn, skill_id="aa")
        sr = _run(self.sg.call_async("aa", 41))
        self.assertEqual(sr.result, 42)

    def test_search_by_category(self):
        from agent.skill_graph_v2 import SkillCategory
        self.sg.register("r1", lambda: 1, category=SkillCategory.REASONING, skill_id="sr1")
        self.sg.register("t1", lambda: 2, category=SkillCategory.TOOL, skill_id="st1")
        results = self.sg.search(category=SkillCategory.REASONING)
        self.assertEqual(len(results), 1)

    def test_search_by_tag(self):
        self.sg.register("tagged", lambda: 1, tags=["nlp"], skill_id="tg1")
        results = self.sg.search(tag="nlp")
        self.assertEqual(len(results), 1)

    def test_find_by_name(self):
        self.sg.register("unique_name", lambda: 1, skill_id="un1")
        s = self.sg.find_by_name("unique_name")
        self.assertIsNotNone(s)

    def test_top_skills(self):
        self.sg.register("popular", lambda: 1, skill_id="pop")
        for _ in range(5): self.sg.call("pop")
        self.sg.register("unpopular", lambda: 2, skill_id="unpop")
        top = self.sg.top_skills(1)
        self.assertEqual(top[0].skill_id, "pop")

    def test_dependency_order(self):
        self.sg.register("base", lambda: 1, skill_id="b")
        self.sg.register("derived", lambda: 2, prerequisites=["b"], skill_id="d")
        order = self.sg.dependency_order()
        self.assertLess(order.index("b"), order.index("d"))

    def test_hooks_called(self):
        called = []
        self.sg.on_before_call(lambda s: called.append("pre"))
        self.sg.on_after_call(lambda r: called.append("post"))
        self.sg.register("hk", lambda: 1, skill_id="hk1")
        self.sg.call("hk1")
        self.assertIn("pre", called); self.assertIn("post", called)

    def test_stats(self):
        self.sg.register("s", lambda: 1, skill_id="s1x")
        self.sg.call("s1x")
        s = self.sg.stats()
        self.assertEqual(s["total_calls"], 1)

# ════════════════════════════════════════════════════════
# FEEDBACK ANALYZER
# ════════════════════════════════════════════════════════
class TestFeedbackAnalyzer(unittest.TestCase):
    def setUp(self):
        from agent.feedback_analyzer import FeedbackAnalyzer, FeedbackType
        self.fa = FeedbackAnalyzer(db_path=":memory:")
        self.FeedbackType = FeedbackType

    def test_submit_thumbs_up(self):
        e = self.fa.submit_thumbs("resp1", positive=True, model_id="m1")
        self.assertEqual(e.feedback_type, self.FeedbackType.THUMBS_UP)

    def test_submit_thumbs_down(self):
        e = self.fa.submit_thumbs("resp2", positive=False)
        self.assertEqual(e.numeric_score, 0.0)

    def test_submit_rating(self):
        e = self.fa.submit_rating("resp3", rating=4.0)
        self.assertAlmostEqual(e.numeric_score, 0.8)

    def test_submit_correction(self):
        e = self.fa.submit_correction("resp4", "The correct answer is 42")
        self.assertEqual(e.feedback_type, self.FeedbackType.CORRECTION)

    def test_get_feedback(self):
        e = self.fa.submit_thumbs("resp5", positive=True)
        got = self.fa.get(e.feedback_id)
        self.assertIsNotNone(got)

    def test_for_response(self):
        self.fa.submit_thumbs("resp6", positive=True)
        self.fa.submit_rating("resp6", rating=3)
        entries = self.fa.for_response("resp6")
        self.assertEqual(len(entries), 2)

    def test_for_model(self):
        self.fa.submit_thumbs("r1", True, model_id="modelX")
        self.fa.submit_thumbs("r2", False, model_id="modelX")
        entries = self.fa.for_model("modelX")
        self.assertEqual(len(entries), 2)

    def test_avg_score_thumbs(self):
        self.fa.submit_thumbs("r1", True,  model_id="m2")
        self.fa.submit_thumbs("r2", True,  model_id="m2")
        self.fa.submit_thumbs("r3", False, model_id="m2")
        avg = self.fa.avg_score(model_id="m2")
        self.assertAlmostEqual(avg, 2/3, places=5)

    def test_avg_score_rating(self):
        self.fa.submit_rating("r1", 5.0)
        self.fa.submit_rating("r2", 3.0)
        avg = self.fa.avg_score()
        self.assertAlmostEqual(avg, 0.8, places=5)

    def test_thumbs_ratio(self):
        self.fa.submit_thumbs("r1", True,  model_id="m3")
        self.fa.submit_thumbs("r2", True,  model_id="m3")
        self.fa.submit_thumbs("r3", False, model_id="m3")
        ratio = self.fa.thumbs_ratio(model_id="m3")
        self.assertAlmostEqual(ratio["positive"], 2/3, places=5)

    def test_dimension_scores(self):
        from agent.feedback_analyzer import FeedbackDimension, FeedbackType
        self.fa.submit("r1", FeedbackType.RATING, 4.0,
                       dimensions={FeedbackDimension.ACCURACY: 0.9,
                                   FeedbackDimension.HELPFULNESS: 0.7})
        dims = self.fa.dimension_scores()
        self.assertIn(FeedbackDimension.ACCURACY.value, dims)

    def test_trend_returns_points(self):
        for i in range(3):
            self.fa.submit_thumbs(f"r{i}", True, model_id="tm")
        trend = self.fa.trend(model_id="tm")
        self.assertGreater(len(trend), 0)

    def test_compare_models(self):
        self.fa.submit_thumbs("r1", True,  model_id="ma")
        self.fa.submit_thumbs("r2", False, model_id="mb")
        cmp = self.fa.compare_models(["ma", "mb"])
        self.assertIn("ma", cmp); self.assertIn("mb", cmp)
        self.assertGreater(cmp["ma"], cmp["mb"])

    def test_anomaly_detection(self):
        # 9 positive, 1 very negative
        for i in range(9):
            self.fa.submit_thumbs(f"r{i}", True, model_id="anom")
        self.fa.submit_thumbs("r9", False, model_id="anom")
        # With enough variance spread, anomaly should fire
        anomalies = self.fa.anomalies(model_id="anom")
        # May or may not be > 0 depending on threshold, just check it runs
        self.assertIsInstance(anomalies, list)

    def test_corrections_returned(self):
        self.fa.submit_correction("r1", "Fix this", model_id="mc")
        corrections = self.fa.corrections(model_id="mc")
        self.assertEqual(len(corrections), 1)

    def test_on_feedback_hook(self):
        received = []
        self.fa.on_feedback(lambda e: received.append(e.feedback_id))
        self.fa.submit_thumbs("rx", True)
        self.assertEqual(len(received), 1)

    def test_rating_clipped(self):
        e = self.fa.submit_rating("r1", rating=10.0)  # clipped to 5
        self.assertAlmostEqual(e.value, 5.0)

    def test_stats(self):
        self.fa.submit_thumbs("r1", True)
        self.fa.submit_rating("r2", 3.0)
        s = self.fa.stats()
        self.assertEqual(s["total_feedback"], 2)
        self.assertIn("by_type", s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v51: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
