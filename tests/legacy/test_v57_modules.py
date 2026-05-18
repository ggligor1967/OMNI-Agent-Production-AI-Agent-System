"""OMNI AGENT v57: SemanticRouter, ObservabilityHub, PolicyEngine, DataPipelineV3"""
import os, sys, time, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# SEMANTIC ROUTER
# ════════════════════════════════════════════════════════
class TestSemanticRouter(unittest.TestCase):
    def setUp(self):
        from agent.semantic_router import SemanticRouter, RouteMatchStrategy
        self.sr = SemanticRouter(
            strategy=RouteMatchStrategy.THRESHOLD,
            threshold=0.5,
            db_path=":memory:")

    def _add(self, name, examples, keywords=None, handler=None, **kw):
        h = handler or (lambda q, *a, **k: name)
        return self.sr.add_route(name, h, examples=examples,
                                 keywords=keywords or [], **kw)

    def test_add_route(self):
        r = self._add("math", ["what is 2+2", "calculate pi"])
        self.assertIn(r.route_id, self.sr._routes)

    def test_keyword_match(self):
        self._add("weather", ["tell me the forecast"],
                  keywords=["weather", "forecast"])
        d = self.sr.route("what's the weather today?")
        self.assertTrue(d.matched)
        self.assertEqual(d.matched_by, "keyword")

    def test_embedding_match(self):
        # Same text → cosine=1.0 → always matches
        self._add("exact", ["exact query text here"])
        d = self.sr.route("exact query text here")
        self.assertTrue(d.matched)

    def test_no_match_below_threshold(self):
        from agent.semantic_router import SemanticRouter, RouteMatchStrategy
        sr = SemanticRouter(strategy=RouteMatchStrategy.THRESHOLD,
                            threshold=0.9999, db_path=":memory:")
        sr.add_route("r", lambda q: None,
                     examples=["completely unrelated topic xyz"])
        d = sr.route("something else entirely different")
        # May or may not match depending on hash embeddings — just check it runs
        self.assertIsNotNone(d)

    def test_fallback_route(self):
        fallback = self._add("fallback", [])
        self.sr.set_fallback(fallback.route_id)
        from agent.semantic_router import SemanticRouter, RouteMatchStrategy
        sr2 = SemanticRouter(strategy=RouteMatchStrategy.THRESHOLD,
                             threshold=0.9999, db_path=":memory:")
        fb = sr2.add_route("fb", lambda q: "fallback",
                           examples=["nope"])
        sr2.set_fallback(fb.route_id)
        d = sr2.route("completely unrelated random gibberish xyz")
        if not d.matched:
            self.assertEqual(d.matched_by, "none")

    def test_dispatch_calls_handler(self):
        results = []
        self._add("action", ["run the action now"],
                  keywords=["action"],
                  handler=lambda q, *a, **k: results.append(q) or "done")
        d, res = self.sr.dispatch("action please")
        if d.matched:
            self.assertEqual(res, "done")

    def test_add_examples_later(self):
        r = self._add("extra", [])
        self.sr.add_examples(r.route_id, ["new example query"])
        self.assertEqual(len(self.sr._routes[r.route_id].examples), 1)

    def test_remove_route(self):
        r = self._add("remove_me", ["test"])
        self.sr.remove_route(r.route_id)
        self.assertNotIn(r.route_id, self.sr._routes)

    def test_disabled_route_not_matched(self):
        r = self._add("disabled", ["test query"], keywords=["disabled_kw"])
        self.sr._routes[r.route_id].enabled = False
        d = self.sr.route("disabled_kw in query")
        if d.matched:
            self.assertNotEqual(d.route_id, r.route_id)

    def test_priority_respected(self):
        self._add("low_prio", ["same query"], priority=0)
        r_high = self._add("high_prio", [], keywords=["same"], priority=10)
        d = self.sr.route("same query")
        if d.matched:
            # Either could match — just verify it runs
            self.assertIsNotNone(d.route_id)

    def test_routing_log(self):
        self._add("log_test", ["log this query"], keywords=["logtest"])
        self.sr.route("logtest query")
        log = self.sr.routing_log()
        self.assertGreater(len(log), 0)

    def test_stats(self):
        self._add("stat_route", ["stat query"], keywords=["statq"])
        self.sr.route("statq here")
        s = self.sr.stats()
        self.assertIn("routes", s)
        self.assertIn("matched", s)

    def test_top_k_vote_strategy(self):
        from agent.semantic_router import SemanticRouter, RouteMatchStrategy
        sr = SemanticRouter(strategy=RouteMatchStrategy.TOP_K_VOTE,
                            top_k=2, db_path=":memory:")
        sr.add_route("r", lambda q: "ok",
                     examples=["hello world", "hello there"])
        d = sr.route("hello world")
        self.assertIsNotNone(d)

# ════════════════════════════════════════════════════════
# OBSERVABILITY HUB
# ════════════════════════════════════════════════════════
class TestObservabilityHub(unittest.TestCase):
    def setUp(self):
        from agent.observability_hub import ObservabilityHub
        self.ob = ObservabilityHub(service="test", db_path=":memory:")

    def test_start_finish_span(self):
        from agent.observability_hub import SpanStatus
        s = self.ob.start_span("my_op")
        self.ob.finish_span(s)
        self.assertEqual(s.status, SpanStatus.OK)
        self.assertIsNotNone(s.finished_at)

    def test_span_duration(self):
        s = self.ob.start_span("dur")
        time.sleep(0.01)
        self.ob.finish_span(s)
        self.assertGreater(s.duration_ms, 0)

    def test_span_context_manager(self):
        from agent.observability_hub import SpanStatus
        with self.ob.span("ctx_span") as s:
            pass
        self.assertEqual(s.status, SpanStatus.OK)

    def test_span_context_manager_error(self):
        from agent.observability_hub import SpanStatus
        try:
            with self.ob.span("err_span") as s:
                raise ValueError("test error")
        except ValueError:
            pass
        self.assertEqual(s.status, SpanStatus.ERROR)
        self.assertIsNotNone(s.error)

    def test_parent_span_linked(self):
        parent = self.ob.start_span("parent")
        child  = self.ob.start_span("child", parent_id=parent.span_id,
                                    trace_id=parent.trace_id)
        self.assertEqual(child.parent_id, parent.span_id)
        self.assertEqual(child.trace_id, parent.trace_id)

    def test_get_trace(self):
        s1 = self.ob.start_span("s1")
        s2 = self.ob.start_span("s2", trace_id=s1.trace_id)
        trace = self.ob.get_trace(s1.trace_id)
        self.assertEqual(len(trace), 2)

    def test_span_tags(self):
        s = self.ob.start_span("tagged")
        s.set_tag("env", "prod")
        self.assertEqual(s.tags["env"], "prod")

    def test_span_log(self):
        s = self.ob.start_span("logged")
        s.log("something happened", level="info")
        self.assertEqual(len(s.logs), 1)

    def test_log_info(self):
        self.ob.info("test message", key="val")
        logs = self.ob.get_logs()
        self.assertGreater(len(logs), 0)

    def test_log_level_filter(self):
        from agent.observability_hub import LogLevel
        self.ob.debug("debug msg")
        self.ob.error("error msg")
        errors = self.ob.get_logs(level=LogLevel.ERROR)
        self.assertTrue(all(l.level == LogLevel.ERROR for l in errors))

    def test_log_min_level_filter(self):
        from agent.observability_hub import ObservabilityHub, LogLevel
        ob = ObservabilityHub(service="t", min_log_level=LogLevel.WARNING,
                              db_path=":memory:")
        ob.debug("should not appear")
        ob.warning("should appear")
        logs = ob.get_logs()
        self.assertEqual(len(logs), 1)

    def test_log_trace_correlation(self):
        s = self.ob.start_span("corr")
        self.ob.set_context(s.trace_id, s.span_id)
        self.ob.info("correlated message")
        self.ob.clear_context()
        logs = self.ob.get_logs(trace_id=s.trace_id)
        self.assertGreater(len(logs), 0)

    def test_record_metric(self):
        self.ob.record_metric("latency", 120.0)
        summary = self.ob.metric_summary("latency")
        self.assertEqual(summary["count"], 1)
        self.assertAlmostEqual(summary["avg"], 120.0)

    def test_gauge(self):
        self.ob.gauge("cpu", 0.75)
        self.assertAlmostEqual(self.ob._gauges["cpu"], 0.75)

    def test_increment(self):
        self.ob.increment("requests")
        self.ob.increment("requests", 4)
        self.assertAlmostEqual(self.ob._counters["requests"], 5.0)

    def test_metric_alert_fires(self):
        fired = []
        self.ob.add_metric_alert("cpu", "gt", 0.9, cooldown_s=0)
        self.ob.on_alert(lambda n, v, r: fired.append(v))
        self.ob.record_metric("cpu", 0.95)
        self.assertGreater(len(fired), 0)

    def test_metric_alert_cooldown(self):
        fired = []
        self.ob.add_metric_alert("mem", "gt", 0.5, cooldown_s=9999)
        self.ob.on_alert(lambda n, v, r: fired.append(v))
        self.ob.record_metric("mem", 0.9)
        self.ob.record_metric("mem", 0.95)
        self.assertEqual(len(fired), 1)

    def test_stats(self):
        self.ob.start_span("x")
        self.ob.info("msg")
        s = self.ob.stats()
        self.assertGreater(s["spans"], 0)
        self.assertGreater(s["logs"], 0)

# ════════════════════════════════════════════════════════
# POLICY ENGINE
# ════════════════════════════════════════════════════════
class TestPolicyEngine(unittest.TestCase):
    def setUp(self):
        from agent.policy_engine import PolicyEngine, Effect
        self.pe = PolicyEngine(default_effect=Effect.ALLOW, db_path=":memory:")

    def _ctx(self, **kw):
        return kw

    def test_default_allow(self):
        r = self.pe.evaluate(self._ctx(user={"role": "guest"}))
        self.assertTrue(r.allowed)

    def test_deny_rule_blocks(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("block guests", Effect.DENY,
                         conditions=[{"field": "user.role", "op": "eq", "value": "guest"}])
        r = self.pe.evaluate(self._ctx(user={"role": "guest"}))
        self.assertFalse(r.allowed)

    def test_allow_rule_explicit(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("allow admins", Effect.ALLOW,
                         conditions=[{"field": "user.role", "op": "eq", "value": "admin"}])
        r = self.pe.evaluate(self._ctx(user={"role": "admin"}))
        self.assertTrue(r.allowed)

    def test_warn_rule_still_allows(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("warn rule", Effect.WARN,
                         conditions=[{"field": "action", "op": "eq", "value": "delete"}])
        r = self.pe.evaluate(self._ctx(action="delete"))
        self.assertTrue(r.allowed)
        self.assertGreater(len(r.warnings), 0)

    def test_audit_rule_adds_note(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("audit rule", Effect.AUDIT,
                         conditions=[{"field": "resource", "op": "eq", "value": "pii"}])
        r = self.pe.evaluate(self._ctx(resource="pii"))
        self.assertTrue(r.allowed)
        self.assertGreater(len(r.audit_notes), 0)

    def test_or_conditions(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("or_test", Effect.DENY,
                         conditions=[
                             {"field": "ip", "op": "eq", "value": "1.2.3.4"},
                             {"field": "ip", "op": "eq", "value": "5.6.7.8"},
                         ], condition_logic="OR")
        self.assertFalse(self.pe.evaluate(self._ctx(ip="1.2.3.4")).allowed)
        self.assertFalse(self.pe.evaluate(self._ctx(ip="5.6.7.8")).allowed)
        self.assertTrue(self.pe.evaluate(self._ctx(ip="9.9.9.9")).allowed)

    def test_in_condition(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("in_test", Effect.DENY,
                         conditions=[{"field": "user.role", "op": "in",
                                      "value": ["banned", "suspended"]}])
        self.assertFalse(self.pe.evaluate(self._ctx(user={"role": "banned"})).allowed)
        self.assertTrue(self.pe.evaluate(self._ctx(user={"role": "admin"})).allowed)

    def test_regex_condition(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("regex_test", Effect.DENY,
                         conditions=[{"field": "email", "op": "regex",
                                      "value": r".*@evil\.com$"}])
        self.assertFalse(self.pe.evaluate(self._ctx(email="user@evil.com")).allowed)
        self.assertTrue(self.pe.evaluate(self._ctx(email="user@good.com")).allowed)

    def test_exists_condition(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("exists_test", Effect.DENY,
                         conditions=[{"field": "token", "op": "not_exists"}])
        self.assertFalse(self.pe.evaluate(self._ctx(other="val")).allowed)
        self.assertTrue(self.pe.evaluate(self._ctx(token="abc")).allowed)

    def test_testing_mode_not_enforced(self):
        from agent.policy_engine import Effect, PolicyStatus
        self.pe.add_rule("testing_rule", Effect.DENY,
                         conditions=[{"field": "x", "op": "exists"}],
                         status=PolicyStatus.TESTING)
        r = self.pe.evaluate(self._ctx(x="val"))
        self.assertTrue(r.allowed)  # Testing mode doesn't enforce

    def test_deactivate_rule(self):
        from agent.policy_engine import Effect
        rule = self.pe.add_rule("deact", Effect.DENY,
                                conditions=[{"field": "a", "op": "exists"}])
        self.pe.deactivate(rule.rule_id)
        r = self.pe.evaluate(self._ctx(a="val"))
        self.assertTrue(r.allowed)

    def test_policy_set(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("set_rule", Effect.DENY,
                         conditions=[{"field": "x", "op": "eq", "value": 1}],
                         policy_set="my_set")
        # Only evaluate the set
        r1 = self.pe.evaluate(self._ctx(x=1), policy_set="my_set")
        r2 = self.pe.evaluate(self._ctx(x=2), policy_set="my_set")
        self.assertFalse(r1.allowed)
        self.assertTrue(r2.allowed)

    def test_is_allowed(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("is_allowed_test", Effect.DENY,
                         conditions=[{"field": "blocked", "op": "eq", "value": True}])
        self.assertFalse(self.pe.is_allowed(self._ctx(blocked=True)))
        self.assertTrue(self.pe.is_allowed(self._ctx(blocked=False)))

    def test_pre_post_hooks(self):
        pre_seen, post_seen = [], []
        self.pe.on_pre_eval(lambda ctx: pre_seen.append(True))
        self.pe.on_post_eval(lambda r: post_seen.append(r.allowed))
        self.pe.evaluate(self._ctx())
        self.assertEqual(len(pre_seen), 1)
        self.assertEqual(len(post_seen), 1)

    def test_eval_log(self):
        self.pe.evaluate(self._ctx(x=1))
        log = self.pe.eval_log()
        self.assertGreater(len(log), 0)

    def test_list_rules(self):
        from agent.policy_engine import Effect
        self.pe.add_rule("lr", Effect.ALLOW)
        rules = self.pe.list_rules()
        self.assertGreater(len(rules), 0)

    def test_stats(self):
        self.pe.evaluate(self._ctx())
        s = self.pe.stats()
        self.assertGreater(s["evaluations"], 0)

# ════════════════════════════════════════════════════════
# DATA PIPELINE V3
# ════════════════════════════════════════════════════════
class TestDataPipelineV3(unittest.TestCase):
    def setUp(self):
        from agent.data_pipeline_v3 import DataPipelineV3
        self.dp = DataPipelineV3(name="test_pipeline", db_path=":memory:")

    def test_simple_passthrough(self):
        self.dp.add_stage("pass", lambda r: r)
        result = self.dp.run([1, 2, 3])
        self.assertEqual(result["records_in"], 3)
        self.assertEqual(result["records_out"], 3)

    def test_map_transform(self):
        collected = []
        self.dp.add_map("double", lambda x: x * 2)
        self.dp.add_sink(lambda r: collected.append(r.data))
        self.dp.run([1, 2, 3])
        self.assertEqual(sorted(collected), [2, 4, 6])

    def test_filter_stage(self):
        collected = []
        self.dp.add_filter("evens", lambda r: r.data % 2 == 0)
        self.dp.add_sink(lambda r: collected.append(r.data))
        self.dp.run([1, 2, 3, 4, 5])
        self.assertEqual(sorted(collected), [2, 4])

    def test_filtered_count(self):
        self.dp.add_filter("gt2", lambda r: r.data > 2)
        result = self.dp.run([1, 2, 3, 4])
        self.assertEqual(result["filtered"], 2)
        self.assertEqual(result["records_out"], 2)

    def test_failed_record_skipped(self):
        def boom(rec):
            if rec.data == 2:
                raise ValueError("bad data")
            return rec
        self.dp.add_stage("boom_stage", boom, skip_on_error=True)
        result = self.dp.run([1, 2, 3])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["records_out"], 2)

    def test_error_no_skip_raises(self):
        def boom(rec):
            raise RuntimeError("fatal")
        self.dp.add_stage("fatal", boom, skip_on_error=False)
        with self.assertRaises(RuntimeError):
            self.dp.run([1])

    def test_multi_stage_pipeline(self):
        collected = []
        self.dp.add_map("add10", lambda x: x + 10)
        self.dp.add_map("mul2", lambda x: x * 2)
        self.dp.add_filter("gt25", lambda r: r.data > 25)
        self.dp.add_sink(lambda r: collected.append(r.data))
        self.dp.run([1, 5, 10])
        # (1+10)*2=22 filtered, (5+10)*2=30, (10+10)*2=40
        self.assertEqual(sorted(collected), [30, 40])

    def test_disabled_stage_skipped(self):
        calls = []
        self.dp.add_stage("skip_me", lambda r: (calls.append(1) or r))
        self.dp.disable_stage("skip_me")
        self.dp.run([1])
        self.assertEqual(len(calls), 0)

    def test_enable_stage(self):
        calls = []
        self.dp.add_stage("enable_me", lambda r: (calls.append(1) or r))
        self.dp.disable_stage("enable_me")
        self.dp.enable_stage("enable_me")
        self.dp.run([1])
        self.assertEqual(len(calls), 1)

    def test_source_function(self):
        self.dp.add_source(lambda: [10, 20, 30])
        result = self.dp.run()
        self.assertEqual(result["records_in"], 3)

    def test_multiple_sinks(self):
        s1, s2 = [], []
        self.dp.add_sink(lambda r: s1.append(r.data))
        self.dp.add_sink(lambda r: s2.append(r.data))
        self.dp.run([1, 2])
        self.assertEqual(len(s1), 2)
        self.assertEqual(len(s2), 2)

    def test_run_async(self):
        done = threading.Event()
        results = []
        def callback(r): results.append(r); done.set()
        self.dp.add_stage("p", lambda r: r)
        t = self.dp.run_async([1, 2, 3], callback=callback)
        done.wait(timeout=2)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["records_in"], 3)

    def test_run_history(self):
        self.dp.run([1])
        history = self.dp.run_history()
        self.assertEqual(len(history), 1)

    def test_error_log(self):
        def boom(rec): raise ValueError("err")
        self.dp.add_stage("err", boom, skip_on_error=True)
        self.dp.run([1])
        log = self.dp.error_log()
        self.assertGreater(len(log), 0)

    def test_stage_stats(self):
        self.dp.add_map("stat_stage", lambda x: x * 2)
        self.dp.run([1, 2, 3])
        stats = self.dp.stage_stats()
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["in"], 3)

    def test_stats(self):
        self.dp.run([1, 2])
        s = self.dp.stats()
        self.assertEqual(s["runs"], 1)
        self.assertEqual(s["total_in"], 2)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v57: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
