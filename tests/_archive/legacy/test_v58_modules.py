"""OMNI AGENT v58: ToolRegistryV2, ModelEvaluator, SessionStoreV2, WorkflowEngineV2"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# TOOL REGISTRY V2
# ════════════════════════════════════════════════════════
class TestToolRegistryV2(unittest.TestCase):
    def setUp(self):
        from agent.tool_registry_v2 import ToolRegistryV2
        self.tr = ToolRegistryV2(db_path=":memory:")

    def test_register_and_get(self):
        self.tr.register("add", lambda a, b: a + b,
                         params=[{"name":"a","type":"integer"},
                                 {"name":"b","type":"integer"}])
        t = self.tr.get_tool("add")
        self.assertIsNotNone(t)
        self.assertEqual(t.name, "add")

    def test_call_tool(self):
        self.tr.register("greet", lambda name: f"Hello {name}",
                         params=[{"name":"name","type":"string"}])
        tc = self.tr.call("greet", {"name": "Alice"})
        self.assertTrue(tc.success)
        self.assertEqual(tc.output, "Hello Alice")

    def test_param_validation_missing(self):
        self.tr.register("req", lambda x: x,
                         params=[{"name":"x","type":"string","required":True}])
        tc = self.tr.call("req", {})
        self.assertFalse(tc.success)
        self.assertIsNotNone(tc.error)

    def test_param_coercion_int(self):
        results = []
        self.tr.register("typed", lambda n: results.append(n) or n,
                         params=[{"name":"n","type":"integer"}])
        self.tr.call("typed", {"n": "42"})
        self.assertEqual(results[0], 42)

    def test_param_coercion_bool(self):
        results = []
        self.tr.register("boolp", lambda flag: results.append(flag) or flag,
                         params=[{"name":"flag","type":"boolean"}])
        self.tr.call("boolp", {"flag": "true"})
        self.assertTrue(results[0])

    def test_param_enum_valid(self):
        self.tr.register("colors",
                         lambda c: c,
                         params=[{"name":"c","type":"string",
                                  "enum":["red","green","blue"]}])
        tc = self.tr.call("colors", {"c": "red"})
        self.assertTrue(tc.success)

    def test_param_enum_invalid(self):
        self.tr.register("colors2",
                         lambda c: c,
                         params=[{"name":"c","type":"string",
                                  "enum":["red","green"]}])
        tc = self.tr.call("colors2", {"c": "purple"})
        self.assertFalse(tc.success)

    def test_param_range(self):
        self.tr.register("range_t", lambda n: n,
                         params=[{"name":"n","type":"number",
                                  "minimum":0.0,"maximum":1.0}])
        self.assertFalse(self.tr.call("range_t", {"n": 2.0}).success)
        self.assertTrue(self.tr.call("range_t", {"n": 0.5}).success)

    def test_disabled_tool_rejected(self):
        spec = self.tr.register("dis", lambda: 1)
        self.tr.disable(spec.tool_id)
        tc = self.tr.call("dis")
        self.assertFalse(tc.success)

    def test_enable_tool(self):
        spec = self.tr.register("en", lambda: 1)
        self.tr.disable(spec.tool_id)
        self.tr.enable(spec.tool_id)
        tc = self.tr.call("en")
        self.assertTrue(tc.success)

    def test_unregister(self):
        spec = self.tr.register("unreg", lambda: 1)
        self.assertTrue(self.tr.unregister(spec.tool_id))
        self.assertIsNone(self.tr.get_tool("unreg"))

    def test_deprecate(self):
        from agent.tool_registry_v2 import ToolStatus
        spec = self.tr.register("depr", lambda: 1)
        self.tr.deprecate(spec.tool_id, replacement="new_tool")
        self.assertEqual(spec.status, ToolStatus.DEPRECATED)

    def test_register_from_function(self):
        def multiply(a: int, b: int) -> int:
            """Multiply two numbers."""
            return a * b
        spec = self.tr.register_from_function(multiply)
        self.assertEqual(spec.name, "multiply")
        self.assertEqual(len(spec.params), 2)

    def test_openai_spec_export(self):
        self.tr.register("chat", lambda msg: msg,
                         description="Chat tool",
                         params=[{"name":"msg","type":"string",
                                  "description":"message"}])
        specs = self.tr.to_openai_specs()
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["type"], "function")

    def test_pre_post_hooks(self):
        pre, post = [], []
        self.tr.on_pre_call(lambda s, i: pre.append(s.name))
        self.tr.on_post_call(lambda s, tc: post.append(tc.success))
        self.tr.register("hook_t", lambda: "ok")
        self.tr.call("hook_t")
        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)

    def test_call_history(self):
        self.tr.register("hist_t", lambda: 1)
        self.tr.call("hist_t")
        hist = self.tr.call_history("hist_t")
        self.assertEqual(len(hist), 1)

    def test_list_tools_by_namespace(self):
        self.tr.register("ns_tool", lambda: 1, namespace="prod")
        tools = self.tr.list_tools(namespace="prod")
        self.assertEqual(len(tools), 1)

    def test_list_tools_by_tag(self):
        self.tr.register("tag_tool", lambda: 1, tags=["ai"])
        tools = self.tr.list_tools(tag="ai")
        self.assertEqual(len(tools), 1)

    def test_stats(self):
        self.tr.register("s", lambda: 1)
        self.tr.call("s")
        s = self.tr.stats()
        self.assertEqual(s["tools"], 1)
        self.assertEqual(s["total_calls"], 1)

# ════════════════════════════════════════════════════════
# MODEL EVALUATOR
# ════════════════════════════════════════════════════════
class TestModelEvaluator(unittest.TestCase):
    def setUp(self):
        from agent.model_evaluator import ModelEvaluator
        self.me = ModelEvaluator(pass_threshold=0.5, db_path=":memory:")
        self.me.register_model("perfect", lambda p: p.split("|")[1] if "|" in p else p)
        self.me.register_model("random",  lambda p: "random_output")

    def _case(self, prompt, expected, **kw):
        return self.me.add_case(prompt, expected, **kw)

    def test_exact_match_pass(self):
        from agent.model_evaluator import ScoringMethod, EvalStatus
        self.me.register_model("echo", lambda p: p)
        c = self._case("hello", "hello")
        r = self.me.eval_case(c.case_id, "echo", ScoringMethod.EXACT_MATCH)
        self.assertEqual(r.status, EvalStatus.PASS)
        self.assertAlmostEqual(r.score, 1.0)

    def test_exact_match_fail(self):
        from agent.model_evaluator import ScoringMethod, EvalStatus
        c = self._case("q", "expected_answer")
        r = self.me.eval_case(c.case_id, "random", ScoringMethod.EXACT_MATCH)
        self.assertEqual(r.status, EvalStatus.FAIL)

    def test_contains_match(self):
        from agent.model_evaluator import ScoringMethod, EvalStatus
        self.me.register_model("m", lambda p: "The answer is 42 here")
        c = self._case("q", "42")
        r = self.me.eval_case(c.case_id, "m", ScoringMethod.CONTAINS)
        self.assertEqual(r.status, EvalStatus.PASS)

    def test_regex_match(self):
        from agent.model_evaluator import ScoringMethod, EvalStatus
        self.me.register_model("digits", lambda p: "Order 12345 confirmed")
        c = self._case("q", r"\d{5}")
        r = self.me.eval_case(c.case_id, "digits", ScoringMethod.REGEX)
        self.assertEqual(r.status, EvalStatus.PASS)

    def test_semantic_match_identical(self):
        from agent.model_evaluator import ScoringMethod, EvalStatus
        self.me.register_model("sem", lambda p: "hello world")
        c = self._case("q", "hello world")
        r = self.me.eval_case(c.case_id, "sem", ScoringMethod.SEMANTIC)
        self.assertGreater(r.score, 0.5)

    def test_custom_scorer(self):
        from agent.model_evaluator import ScoringMethod, EvalStatus
        self.me.add_custom_scorer("length_match",
                                   lambda o, e: 1.0 if len(str(o)) == len(str(e)) else 0.0)
        self.me.register_model("fixed_len", lambda p: "abc")
        c = self._case("q", "xyz")
        r = self.me.eval_case(c.case_id, "fixed_len",
                              ScoringMethod.CUSTOM, custom_scorer="length_match")
        self.assertAlmostEqual(r.score, 1.0)

    def test_rubric_scoring(self):
        from agent.model_evaluator import ScoringMethod
        self.me.add_rubric("quality", [
            {"name": "relevance", "weight": 2.0,
             "scorer": lambda o, e: 1.0 if e.lower() in o.lower() else 0.0},
            {"name": "length", "weight": 1.0,
             "scorer": lambda o, e: 1.0 if len(o) > 5 else 0.0},
        ])
        self.me.register_model("rubric_m", lambda p: "contains expected output here")
        c = self._case("q", "expected")
        r = self.me.eval_case(c.case_id, "rubric_m",
                              ScoringMethod.RUBRIC, rubric_name="quality")
        self.assertGreater(r.score, 0)

    def test_suite_benchmark(self):
        from agent.model_evaluator import ScoringMethod
        self.me.register_model("echo2", lambda p: p)
        for i in range(3):
            self._case(f"q{i}", f"q{i}", suite="my_suite")
        report = self.me.run_benchmark("echo2", suite="my_suite",
                                        method=ScoringMethod.EXACT_MATCH)
        self.assertEqual(report.total_cases, 3)
        self.assertEqual(report.passed, 3)

    def test_compare_models(self):
        from agent.model_evaluator import ScoringMethod
        self.me.register_model("a", lambda p: p)
        self.me.register_model("b", lambda p: "wrong")
        self._case("x", "x", suite="cmp")
        reports = self.me.compare_models(["a", "b"], suite="cmp",
                                          method=ScoringMethod.EXACT_MATCH)
        self.assertIn("a", reports)
        self.assertIn("b", reports)
        self.assertGreater(reports["a"].avg_score,
                           reports["b"].avg_score)

    def test_model_error_handled(self):
        from agent.model_evaluator import ScoringMethod, EvalStatus
        self.me.register_model("boom", lambda p: (_ for _ in ()).throw(RuntimeError("err")))
        c = self._case("q", "a")
        r = self.me.eval_case(c.case_id, "boom", ScoringMethod.EXACT_MATCH)
        self.assertEqual(r.status, EvalStatus.FAIL)

    def test_case_not_found_skip(self):
        from agent.model_evaluator import EvalStatus
        r = self.me.eval_case("nonexistent", "random")
        self.assertEqual(r.status, EvalStatus.SKIP)

    def test_benchmark_history(self):
        self.me.register_model("hist_m", lambda p: p)
        self._case("a", "a", suite="hist_s")
        from agent.model_evaluator import ScoringMethod
        self.me.run_benchmark("hist_m", suite="hist_s",
                               method=ScoringMethod.EXACT_MATCH)
        h = self.me.benchmark_history()
        self.assertGreater(len(h), 0)

    def test_stats(self):
        s = self.me.stats()
        self.assertIn("cases", s)
        self.assertIn("models", s)

# ════════════════════════════════════════════════════════
# SESSION STORE V2
# ════════════════════════════════════════════════════════
class TestSessionStoreV2(unittest.TestCase):
    def setUp(self):
        from agent.session_store_v2 import SessionStoreV2
        self.ss = SessionStoreV2(default_ttl_s=3600, db_path=":memory:")

    def test_create_session(self):
        s = self.ss.create(user_id="u1")
        self.assertIsNotNone(s.session_id)
        self.assertTrue(s.is_active)

    def test_get_session(self):
        s = self.ss.create()
        got = self.ss.get(s.session_id)
        self.assertIsNotNone(got)

    def test_expired_session_not_returned(self):
        s = self.ss.create(ttl_s=0.01)
        time.sleep(0.02)
        got = self.ss.get(s.session_id)
        self.assertIsNone(got)

    def test_update_merges_data(self):
        s = self.ss.create(data={"a": 1})
        self.ss.update(s.session_id, {"b": 2})
        self.assertEqual(s.data["a"], 1)
        self.assertEqual(s.data["b"], 2)

    def test_update_replace_data(self):
        s = self.ss.create(data={"a": 1})
        self.ss.update(s.session_id, {"b": 2}, merge=False)
        self.assertNotIn("a", s.data)
        self.assertEqual(s.data["b"], 2)

    def test_set_get_key(self):
        s = self.ss.create()
        self.ss.set_key(s.session_id, "theme", "dark")
        v = self.ss.get_key(s.session_id, "theme")
        self.assertEqual(v, "dark")

    def test_extend_ttl(self):
        s = self.ss.create(ttl_s=10)
        old_expiry = s.expires_at
        self.ss.extend(s.session_id, by_s=3600)
        self.assertGreater(s.expires_at, old_expiry)

    def test_revoke_session(self):
        from agent.session_store_v2 import SessionStatus
        s = self.ss.create()
        self.ss.revoke(s.session_id)
        self.assertEqual(s.status, SessionStatus.REVOKED)
        self.assertIsNone(self.ss.get(s.session_id))

    def test_lock_and_unlock(self):
        from agent.session_store_v2 import SessionStatus
        s = self.ss.create()
        self.ss.lock(s.session_id)
        self.assertEqual(s.status, SessionStatus.LOCKED)
        self.ss.unlock(s.session_id)
        self.assertEqual(s.status, SessionStatus.ACTIVE)

    def test_locked_session_not_updatable(self):
        s = self.ss.create(data={"x": 1})
        self.ss.lock(s.session_id)
        result = self.ss.update(s.session_id, {"x": 99})
        self.assertIsNone(result)
        self.assertEqual(s.data["x"], 1)

    def test_delete_session(self):
        s = self.ss.create()
        self.assertTrue(self.ss.delete(s.session_id))
        self.assertIsNone(self.ss.get(s.session_id))

    def test_token_verification(self):
        s = self.ss.create()
        verified = self.ss.verify(s.session_id, s.token)
        self.assertIsNotNone(verified)
        bad = self.ss.verify(s.session_id, "wrong_token")
        self.assertIsNone(bad)

    def test_revoke_user_sessions(self):
        self.ss.create(user_id="u99")
        self.ss.create(user_id="u99")
        n = self.ss.revoke_user_sessions("u99")
        self.assertEqual(n, 2)

    def test_revoke_by_tag(self):
        self.ss.create(tags=["temp"])
        self.ss.create(tags=["temp"])
        self.ss.create(tags=["permanent"])
        n = self.ss.revoke_by_tag("temp")
        self.assertEqual(n, 2)

    def test_cleanup_expired(self):
        self.ss.create(ttl_s=0.01)
        time.sleep(0.02)
        removed = self.ss.cleanup_expired()
        self.assertGreater(removed, 0)

    def test_namespace_isolation(self):
        self.ss.create(namespace="ns1")
        self.ss.create(namespace="ns2")
        ns1 = self.ss.list_sessions(namespace="ns1")
        self.assertEqual(len(ns1), 1)

    def test_lifecycle_hook(self):
        from agent.session_store_v2 import SessionEvent
        created = []
        self.ss.on_event(SessionEvent.CREATED, lambda s: created.append(s.session_id))
        s = self.ss.create()
        self.assertIn(s.session_id, created)

    def test_event_log(self):
        s = self.ss.create()
        self.ss.get(s.session_id)
        log = self.ss.event_log(s.session_id)
        self.assertGreater(len(log), 0)

    def test_count_active(self):
        self.ss.create()
        self.ss.create()
        self.assertEqual(self.ss.count_active(), 2)

    def test_stats(self):
        self.ss.create()
        st = self.ss.stats()
        self.assertEqual(st["total_sessions"], 1)
        self.assertEqual(st["created"], 1)

# ════════════════════════════════════════════════════════
# WORKFLOW ENGINE V2
# ════════════════════════════════════════════════════════
class TestWorkflowEngineV2(unittest.TestCase):
    def setUp(self):
        from agent.workflow_engine_v2 import WorkflowEngineV2
        self.we = WorkflowEngineV2(db_path=":memory:")

    def _wf(self, name="test"):
        return self.we.define(name)

    def test_simple_action_workflow(self):
        from agent.workflow_engine_v2 import StepType, WorkflowStatus
        wf = self._wf()
        self.we.add_step(wf.workflow_id, name="s1",
                         step_type=StepType.ACTION,
                         fn=lambda ctx: ctx.update({"done": True}) or True)
        run = self.we.run(wf.workflow_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertTrue(run.context.get("done"))

    def test_multi_step_chain(self):
        from agent.workflow_engine_v2 import StepType, WorkflowStatus
        wf = self._wf()
        s1 = self.we.add_step(wf.workflow_id, name="s1",
                               step_type=StepType.ACTION,
                               fn=lambda ctx: ctx.update({"s1": True}))
        s2 = self.we.add_step(wf.workflow_id, name="s2",
                               step_type=StepType.ACTION,
                               fn=lambda ctx: ctx.update({"s2": True}))
        s1.next_step = s2.step_id
        run = self.we.run(wf.workflow_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertTrue(run.context.get("s1"))
        self.assertTrue(run.context.get("s2"))

    def test_condition_branch_true(self):
        from agent.workflow_engine_v2 import StepType, WorkflowStatus
        wf = self._wf()
        cond = self.we.add_step(wf.workflow_id, name="cond",
                                step_type=StepType.CONDITION,
                                condition=lambda ctx: ctx.get("val", 0) > 5)
        true_s = self.we.add_step(wf.workflow_id, name="true_branch",
                                   step_type=StepType.ACTION,
                                   fn=lambda ctx: ctx.update({"branch": "true"}))
        false_s = self.we.add_step(wf.workflow_id, name="false_branch",
                                    step_type=StepType.ACTION,
                                    fn=lambda ctx: ctx.update({"branch": "false"}))
        cond.on_true  = true_s.step_id
        cond.on_false = false_s.step_id
        run = self.we.run(wf.workflow_id, context={"val": 10})
        self.assertEqual(run.context.get("branch"), "true")

    def test_condition_branch_false(self):
        from agent.workflow_engine_v2 import StepType
        wf = self._wf()
        cond = self.we.add_step(wf.workflow_id, name="cond",
                                step_type=StepType.CONDITION,
                                condition=lambda ctx: ctx.get("val", 0) > 5)
        true_s = self.we.add_step(wf.workflow_id, name="t",
                                   step_type=StepType.ACTION,
                                   fn=lambda ctx: ctx.update({"branch": "true"}))
        false_s = self.we.add_step(wf.workflow_id, name="f",
                                    step_type=StepType.ACTION,
                                    fn=lambda ctx: ctx.update({"branch": "false"}))
        cond.on_true  = true_s.step_id
        cond.on_false = false_s.step_id
        run = self.we.run(wf.workflow_id, context={"val": 1})
        self.assertEqual(run.context.get("branch"), "false")

    def test_set_var_step(self):
        from agent.workflow_engine_v2 import StepType, WorkflowStatus
        wf = self._wf()
        self.we.add_step(wf.workflow_id, name="sv",
                         step_type=StepType.SET_VAR,
                         var_name="greeting", var_value="hello")
        run = self.we.run(wf.workflow_id)
        self.assertEqual(run.context.get("greeting"), "hello")
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)

    def test_set_var_callable(self):
        from agent.workflow_engine_v2 import StepType
        wf = self._wf()
        self.we.add_step(wf.workflow_id, name="sv2",
                         step_type=StepType.SET_VAR,
                         var_name="doubled",
                         var_value=lambda ctx: ctx.get("n", 0) * 2)
        run = self.we.run(wf.workflow_id, context={"n": 5})
        self.assertEqual(run.context.get("doubled"), 10)

    def test_loop_step(self):
        from agent.workflow_engine_v2 import StepType
        wf = self._wf()
        body = self.we.add_step(wf.workflow_id, name="body",
                                step_type=StepType.ACTION,
                                fn=lambda ctx: ctx.get("_loop_item"))
        loop = self.we.add_step(wf.workflow_id, name="loop",
                                step_type=StepType.LOOP,
                                loop_over="items",
                                loop_body=body.step_id)
        self.we.set_start(wf.workflow_id, loop.step_id)
        run = self.we.run(wf.workflow_id, context={"items": [1, 2, 3]})
        self.assertEqual(run.context.get("_loop_results"), [1, 2, 3])

    def test_emit_event(self):
        from agent.workflow_engine_v2 import StepType
        fired = []
        self.we.on_event("my_event", lambda ctx, r: fired.append(True))
        wf = self._wf()
        self.we.add_step(wf.workflow_id, name="emit",
                         step_type=StepType.EMIT,
                         var_name="my_event")
        self.we.run(wf.workflow_id)
        self.assertGreater(len(fired), 0)

    def test_step_on_error_continue(self):
        from agent.workflow_engine_v2 import StepType, WorkflowStatus
        wf = self._wf()
        s1 = self.we.add_step(wf.workflow_id, name="fail_step",
                               step_type=StepType.ACTION,
                               fn=lambda ctx: (_ for _ in ()).throw(RuntimeError("err")),
                               on_error="continue")
        s2 = self.we.add_step(wf.workflow_id, name="after",
                               step_type=StepType.ACTION,
                               fn=lambda ctx: ctx.update({"after": True}))
        s1.next_step = s2.step_id
        run = self.we.run(wf.workflow_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertTrue(run.context.get("after"))

    def test_step_on_error_fail(self):
        from agent.workflow_engine_v2 import StepType, WorkflowStatus
        wf = self._wf()
        self.we.add_step(wf.workflow_id, name="fatal",
                         step_type=StepType.ACTION,
                         fn=lambda ctx: (_ for _ in ()).throw(RuntimeError("fatal")),
                         on_error="fail")
        run = self.we.run(wf.workflow_id)
        self.assertEqual(run.status, WorkflowStatus.FAILED)

    def test_retry_on_action(self):
        from agent.workflow_engine_v2 import StepType, WorkflowStatus
        attempts = [0]
        def flaky(ctx):
            attempts[0] += 1
            if attempts[0] < 3:
                raise RuntimeError("retry me")
            ctx["ok"] = True
        wf = self._wf()
        self.we.add_step(wf.workflow_id, name="retry_s",
                         step_type=StepType.ACTION,
                         fn=flaky, max_retries=3, retry_delay_s=0.0)
        run = self.we.run(wf.workflow_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertTrue(run.context.get("ok"))

    def test_subflow(self):
        from agent.workflow_engine_v2 import StepType, WorkflowStatus
        sub = self._wf("sub")
        self.we.add_step(sub.workflow_id, name="sub_step",
                         step_type=StepType.ACTION,
                         fn=lambda ctx: ctx.update({"sub_ran": True}))
        main = self._wf("main")
        self.we.add_step(main.workflow_id, name="main_step",
                         step_type=StepType.SUBFLOW,
                         subflow_id=sub.workflow_id)
        run = self.we.run(main.workflow_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)

    def test_cancel_workflow(self):
        from agent.workflow_engine_v2 import WorkflowStatus
        wf = self._wf()
        run = self.we.run(wf.workflow_id)
        # Can't cancel after completion, test cancel on existing run
        run.status = WorkflowStatus.RUNNING
        cancelled = self.we.cancel(run.run_id)
        self.assertTrue(cancelled)

    def test_run_history(self):
        from agent.workflow_engine_v2 import StepType
        wf = self._wf()
        self.we.add_step(wf.workflow_id, name="s",
                         step_type=StepType.ACTION, fn=lambda ctx: None)
        self.we.run(wf.workflow_id)
        h = self.we.run_history()
        self.assertGreater(len(h), 0)

    def test_stats(self):
        from agent.workflow_engine_v2 import StepType
        wf = self._wf()
        self.we.add_step(wf.workflow_id, name="s",
                         step_type=StepType.ACTION, fn=lambda ctx: None)
        self.we.run(wf.workflow_id)
        s = self.we.stats()
        self.assertEqual(s["workflows"], 1)
        self.assertGreater(s["runs"], 0)
        self.assertGreater(s["completed"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v58: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
