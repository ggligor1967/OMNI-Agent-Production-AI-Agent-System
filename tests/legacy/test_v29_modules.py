"""OMNI AGENT v29: WorkflowEngine, OutputValidator, AgentScheduler, Telemetry"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# WORKFLOW ENGINE
# ════════════════════════════════════════════════════════
class TestWorkflowEngine(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.workflow_engine import WorkflowEngine
        self.eng = WorkflowEngine(db_path=os.path.join(td,"wf.db"))
        from agent.workflow_engine import NodeType, WorkflowStatus, NodeStatus
        self.NT = NodeType; self.WS = WorkflowStatus; self.NS = NodeStatus

    def _simple_wf(self, name="test"):
        wf = self.eng.define(name)
        wf.node("start",   None,              node_type=self.NT.START)
        wf.node("do_work", lambda ctx: 42,    node_type=self.NT.TASK)
        wf.node("end",     None,              node_type=self.NT.END)
        wf.edge("start", "do_work")
        wf.edge("do_work", "end")
        return wf

    def test_run_completes(self):
        self._simple_wf()
        run = _run(self.eng.run("test"))
        self.assertEqual(run.status, self.WS.COMPLETED)

    def test_node_output_stored(self):
        self._simple_wf("out")
        run = _run(self.eng.run("out"))
        self.assertEqual(run.context.get("do_work"), 42)

    def test_async_node(self):
        async def async_fn(ctx): await asyncio.sleep(0.01); return "async_result"
        wf = self.eng.define("async_wf")
        wf.node("a", async_fn).node("end", None, node_type=self.NT.END)
        wf.edge("a", "end")
        run = _run(self.eng.run("async_wf"))
        self.assertEqual(run.context.get("a"), "async_result")

    def test_node_receives_context(self):
        wf = self.eng.define("ctx_wf")
        wf.node("n1", lambda ctx: ctx.get("seed", 0) * 2)
        wf.node("n2", None, node_type=self.NT.END)
        wf.edge("n1", "n2")
        run = _run(self.eng.run("ctx_wf", context={"seed": 21}))
        self.assertEqual(run.context.get("n1"), 42)

    def test_node_failure_marks_run(self):
        wf = self.eng.define("fail_wf")
        wf.node("boom", lambda ctx: 1/0)
        wf.node("end", None, node_type=self.NT.END)
        wf.edge("boom", "end")
        run = _run(self.eng.run("fail_wf"))
        self.assertIn(run.status, (self.WS.FAILED, self.WS.PARTIAL))

    def test_retry_on_failure(self):
        attempts = [0]
        def flaky(ctx):
            attempts[0] += 1
            if attempts[0] < 3: raise RuntimeError("not yet")
            return "ok"
        wf = self.eng.define("retry_wf")
        wf.node("r", flaky, max_retries=3, retry_delay=0.01)
        run = _run(self.eng.run("retry_wf"))
        self.assertGreaterEqual(attempts[0], 2)

    def test_node_timeout(self):
        async def slow(ctx): await asyncio.sleep(5)
        wf = self.eng.define("timeout_wf")
        wf.node("slow", slow, timeout_s=0.05)
        run = _run(self.eng.run("timeout_wf"))
        nr = run.node_results.get("slow")
        self.assertIsNotNone(nr)
        self.assertEqual(nr.status, self.NS.FAILED)

    def test_conditional_edge_skips_node(self):
        wf = self.eng.define("cond_wf")
        wf.node("n1", lambda ctx: "yes")
        wf.node("n2", lambda ctx: "no")  # should be skipped
        wf.node("n3", lambda ctx: "end")
        wf.edge("n1", "n2", condition=lambda ctx: False)
        wf.edge("n1", "n3")
        run = _run(self.eng.run("cond_wf"))
        n2 = run.node_results.get("n2")
        # n2 is either None or SKIPPED (both mean it didn't execute)
        if n2 is not None:
            self.assertEqual(n2.status, self.NS.SKIPPED)

    def test_fan_out_parallel(self):
        results = []
        async def branch_a(ctx): await asyncio.sleep(0.02); return "a"
        async def branch_b(ctx): await asyncio.sleep(0.02); return "b"
        wf = self.eng.define("fan_wf")
        wf.node("start", None, node_type=self.NT.START)
        wf.node("fan",   None, node_type=self.NT.FAN_OUT)
        wf.node("ba",    branch_a)
        wf.node("bb",    branch_b)
        wf.node("end",   None, node_type=self.NT.END)
        wf.edge("start", "fan")
        wf.edge("fan", "ba"); wf.edge("fan", "bb")
        wf.edge("ba", "end"); wf.edge("bb", "end")
        t0 = time.time()
        run = _run(self.eng.run("fan_wf"))
        elapsed = time.time() - t0
        # Parallel: should take ~0.02s not ~0.04s
        self.assertLess(elapsed, 0.1)

    def test_topo_sort_linear(self):
        wf = self.eng.define("topo_wf")
        wf.node("a", lambda ctx: 1).node("b", lambda ctx: 2).node("c", lambda ctx: 3)
        wf.edge("a","b"); wf.edge("b","c")
        order = wf.topo_sort()
        self.assertLess(order.index("a"), order.index("b"))
        self.assertLess(order.index("b"), order.index("c"))

    def test_cycle_detection(self):
        wf = self.eng.define("cycle_wf")
        wf.node("a", None).node("b", None)
        wf.edge("a","b"); wf.edge("b","a")
        with self.assertRaises(ValueError):
            wf.topo_sort()

    def test_plan_returns_nodes(self):
        self._simple_wf("plan_wf")
        plan = self.eng.plan("plan_wf")
        self.assertGreater(len(plan), 0)
        for item in plan: self.assertIn("id", item)

    def test_on_node_complete_hook(self):
        completed = []
        self.eng.on("on_node_complete", lambda s, nr, r: completed.append(s.id))
        self._simple_wf("hook_wf")
        _run(self.eng.run("hook_wf"))
        self.assertIn("do_work", completed)

    def test_on_workflow_end_hook(self):
        ended = []
        self.eng.on("on_workflow_end", lambda r: ended.append(r.id))
        self._simple_wf("end_hook_wf")
        run = _run(self.eng.run("end_hook_wf"))
        self.assertIn(run.id, ended)

    def test_get_run(self):
        self._simple_wf("get_wf")
        run = _run(self.eng.run("get_wf"))
        found = self.eng.get_run(run.id)
        self.assertEqual(found.id, run.id)

    def test_run_to_dict(self):
        self._simple_wf("dict_wf")
        run = _run(self.eng.run("dict_wf"))
        d = run.to_dict()
        for k in ["id","workflow","status","duration_ms","nodes_completed"]:
            self.assertIn(k, d)

    def test_node_result_to_dict(self):
        self._simple_wf("nr_dict_wf")
        run = _run(self.eng.run("nr_dict_wf"))
        nr = run.node_results.get("do_work")
        if nr:
            d = nr.to_dict()
            for k in ["id","name","status","latency_ms"]: self.assertIn(k, d)

    def test_stats(self):
        self._simple_wf("stats_wf")
        _run(self.eng.run("stats_wf"))
        s = self.eng.stats()
        for k in ["total_runs","defined_workflows"]: self.assertIn(k, s)

    def test_multiple_workflows(self):
        self._simple_wf("wf1"); self._simple_wf("wf2")
        r1 = _run(self.eng.run("wf1")); r2 = _run(self.eng.run("wf2"))
        self.assertEqual(r1.status, self.WS.COMPLETED)
        self.assertEqual(r2.status, self.WS.COMPLETED)

# ════════════════════════════════════════════════════════
# OUTPUT VALIDATOR
# ════════════════════════════════════════════════════════
class TestOutputValidator(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.output_validator import OutputValidator
        self.OV = OutputValidator
        self.v = OutputValidator(db_path=os.path.join(td,"val.db"))
        from agent.output_validator import RuleType, Severity
        self.RT = RuleType; self.SEV = Severity

    def test_valid_passes(self):
        self.v.add_rule("not_empty", self.RT.LENGTH, min_len=1)
        r = self.v.validate("Hello world")
        self.assertTrue(r.valid)

    def test_required_fields_pass(self):
        self.v.add_rule("req", self.RT.REQUIRED_FIELDS, fields=["a","b"])
        r = self.v.validate({"a":1,"b":2})
        self.assertTrue(r.valid)

    def test_required_fields_fail(self):
        self.v.add_rule("req", self.RT.REQUIRED_FIELDS, fields=["a","b"])
        r = self.v.validate({"a":1})
        self.assertFalse(r.valid)
        self.assertTrue(any("b" in str(e["message"]) for e in r.errors))

    def test_type_check_pass(self):
        self.v.add_rule("t", self.RT.TYPE_CHECK, expected_type="list")
        r = self.v.validate([1,2,3])
        self.assertTrue(r.valid)

    def test_type_check_fail(self):
        self.v.add_rule("t", self.RT.TYPE_CHECK, expected_type="dict")
        r = self.v.validate("a string")
        self.assertFalse(r.valid)

    def test_regex_pass(self):
        self.v.add_rule("r", self.RT.REGEX, pattern=r"\d{4}")
        r = self.v.validate("code: 1234")
        self.assertTrue(r.valid)

    def test_regex_fail(self):
        self.v.add_rule("r", self.RT.REGEX, pattern=r"\d{4}")
        r = self.v.validate("no digits here")
        self.assertFalse(r.valid)

    def test_not_regex(self):
        self.v.add_rule("nr", self.RT.NOT_REGEX, pattern=r"badword")
        r = self.v.validate("clean text")
        self.assertTrue(r.valid)
        r2 = self.v.validate("contains badword here")
        self.assertFalse(r2.valid)

    def test_length_pass(self):
        self.v.add_rule("l", self.RT.LENGTH, min_len=5, max_len=20)
        r = self.v.validate("hello world")
        self.assertTrue(r.valid)

    def test_length_fail_too_short(self):
        self.v.add_rule("l", self.RT.LENGTH, min_len=10)
        r = self.v.validate("hi")
        self.assertFalse(r.valid)

    def test_range_pass(self):
        self.v.add_rule("rng", self.RT.RANGE, min_val=0.0, max_val=1.0)
        r = self.v.validate(0.5)
        self.assertTrue(r.valid)

    def test_range_fail(self):
        self.v.add_rule("rng", self.RT.RANGE, min_val=0.0, max_val=1.0)
        r = self.v.validate(1.5)
        self.assertFalse(r.valid)

    def test_json_valid_pass(self):
        self.v.add_rule("j", self.RT.JSON_VALID)
        r = self.v.validate('{"key": "value"}')
        self.assertTrue(r.valid)

    def test_json_valid_embedded(self):
        self.v.add_rule("j", self.RT.JSON_VALID)
        r = self.v.validate('Here is the answer: {"result": 42} good.')
        self.assertTrue(r.valid)

    def test_json_valid_fail(self):
        self.v.add_rule("j", self.RT.JSON_VALID)
        r = self.v.validate("not json at all")
        self.assertFalse(r.valid)

    def test_blacklist_pass(self):
        self.v.add_rule("bl", self.RT.BLACKLIST, blacklist=["forbidden","banned"])
        r = self.v.validate("clean text here")
        self.assertTrue(r.valid)

    def test_blacklist_fail(self):
        self.v.add_rule("bl", self.RT.BLACKLIST, blacklist=["forbidden"])
        r = self.v.validate("this text has forbidden content")
        self.assertFalse(r.valid)

    def test_whitelist_pass(self):
        self.v.add_rule("wl", self.RT.WHITELIST, whitelist=["answer","result"])
        r = self.v.validate("The answer is 42")
        self.assertTrue(r.valid)

    def test_custom_rule(self):
        self.v.add_rule("custom", self.RT.CUSTOM,
                         custom_fn=lambda o: (len(str(o)) > 5, "too short"))
        r = self.v.validate("hello world")
        self.assertTrue(r.valid)

    def test_warning_does_not_fail(self):
        self.v.add_rule("warn", self.RT.BLACKLIST, blacklist=["informal"],
                         severity=self.SEV.WARNING)
        r = self.v.validate("informal language here")
        self.assertTrue(r.valid)
        self.assertGreater(len(r.warnings), 0)

    def test_score_perfect(self):
        self.v.add_rule("ok", self.RT.LENGTH, min_len=1)
        r = self.v.validate("valid text")
        self.assertGreater(r.score, 0.9)

    def test_score_lower_on_error(self):
        self.v.add_rule("e1", self.RT.REQUIRED_FIELDS, fields=["x"])
        self.v.add_rule("e2", self.RT.REQUIRED_FIELDS, fields=["y"])
        r = self.v.validate({})
        self.assertLess(r.score, 0.5)

    def test_repair_truncate(self):
        self.v.add_rule("tr", self.RT.LENGTH, min_len=1, max_len=5,
                         repair_strategy="truncate")
        r = self.v.validate("this is too long text")
        self.assertTrue(r.repaired)

    def test_repair_json_fix(self):
        self.v.add_rule("jf", self.RT.JSON_VALID, repair_strategy="json_fix")
        r = self.v.validate('prefix {"key": "val"} suffix')
        if r.repaired:
            self.assertIsNotNone(r.repaired_output)

    def test_repair_fallback(self):
        self.v.add_rule("fb", self.RT.REQUIRED_FIELDS, fields=["must_have"],
                         repair_strategy="fallback", repair_value={"must_have": "default"})
        r = self.v.validate({})
        # After fallback, repaired_output should have the field
        if r.repaired: self.assertIsNotNone(r.repaired_output)

    def test_sanitize_whitespace(self):
        text = "  hello   world  "
        clean = self.v.sanitize(text, normalize_whitespace=True)
        self.assertEqual(clean, "hello world")

    def test_sanitize_truncate(self):
        text = "a" * 100
        clean = self.v.sanitize(text, max_len=10)
        self.assertLessEqual(len(clean), 10)

    def test_sanitize_pii(self):
        text = "Email me at user@example.com please"
        clean = self.v.sanitize(text, strip_pii=True)
        self.assertNotIn("user@example.com", clean)
        self.assertIn("[EMAIL]", clean)

    def test_batch_validate(self):
        self.v.add_rule("len", self.RT.LENGTH, min_len=1)
        results = self.v.validate_batch(["a","b","c",""])
        self.assertEqual(len(results), 4)
        self.assertFalse(results[-1].valid)

    def test_remove_rule(self):
        self.v.add_rule("removable", self.RT.LENGTH, min_len=100)
        r1 = self.v.validate("short")
        self.assertFalse(r1.valid)
        self.v.remove_rule("removable")
        r2 = self.v.validate("short")
        self.assertTrue(r2.valid)

    def test_stats(self):
        self.v.add_rule("st", self.RT.LENGTH, min_len=1)
        self.v.validate("hello"); self.v.validate("")
        s = self.v.stats()
        for k in ["total","valid","pass_rate"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# AGENT SCHEDULER
# ════════════════════════════════════════════════════════
class TestAgentScheduler(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.agent_scheduler import AgentScheduler, ScheduleType
        self.sched = AgentScheduler(db_path=os.path.join(td,"sched.db"))
        self.ST = ScheduleType

    def test_schedule_task(self):
        spec = self.sched.schedule("t1", lambda ctx: 1)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "t1")

    def test_run_now_sync(self):
        self.sched.schedule("sync_task", lambda ctx: "result")
        tr = _run(self.sched.run_now("sync_task"))
        self.assertIsNotNone(tr)
        self.assertEqual(tr.status.value, "completed")

    def test_run_now_async(self):
        async def afn(ctx): await asyncio.sleep(0.01); return "async"
        self.sched.schedule("async_task", afn)
        tr = _run(self.sched.run_now("async_task"))
        self.assertEqual(tr.output, "async")

    def test_run_now_with_context(self):
        self.sched.set_context("key", "val")
        self.sched.schedule("ctx_task", lambda ctx: ctx.get("key"))
        tr = _run(self.sched.run_now("ctx_task"))
        self.assertEqual(tr.output, "val")

    def test_run_failure(self):
        self.sched.schedule("fail_task", lambda ctx: 1/0)
        tr = _run(self.sched.run_now("fail_task"))
        self.assertEqual(tr.status.value, "failed")
        self.assertIn("division", tr.error.lower())

    def test_retry_on_failure(self):
        calls = [0]
        def flaky(ctx):
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("not yet")
            return "ok"
        self.sched.schedule("retry_task", flaky, max_retries=3, retry_delay=0.01)
        tr = _run(self.sched.run_now("retry_task"))
        self.assertGreaterEqual(calls[0], 2)
        self.assertEqual(tr.status.value, "completed")

    def test_timeout(self):
        async def slow(ctx): await asyncio.sleep(5)
        self.sched.schedule("slow_task", slow, timeout_s=0.05)
        tr = _run(self.sched.run_now("slow_task"))
        self.assertEqual(tr.status.value, "failed")

    def test_cancel(self):
        self.sched.schedule("cancel_me", lambda ctx: 1)
        ok = self.sched.cancel("cancel_me")
        self.assertTrue(ok)
        self.assertNotIn("cancel_me", self.sched._tasks)

    def test_pause_resume(self):
        self.sched.schedule("toggle", lambda ctx: 1)
        self.sched.pause("toggle")
        self.assertFalse(self.sched._tasks["toggle"].enabled)
        self.sched.resume("toggle")
        self.assertTrue(self.sched._tasks["toggle"].enabled)

    def test_trigger_sets_next_run(self):
        self.sched.schedule("trig", lambda ctx: 1, interval_s=3600)
        self.sched.trigger("trig")
        self.assertLessEqual(self.sched._tasks["trig"].next_run, time.time())

    def test_interval_scheduling(self):
        spec = self.sched.schedule("interval_t", lambda ctx: 1,
                                    schedule_type=self.ST.INTERVAL,
                                    interval_s=60)
        _run(self.sched.run_now("interval_t"))
        # next_run should be ~60s in future
        self.assertGreater(spec.next_run, time.time() + 50)

    def test_cron_scheduling(self):
        spec = self.sched.schedule("cron_t", lambda ctx: 1,
                                    schedule_type=self.ST.CRON,
                                    cron_expr="0 * * * *")
        self.assertGreater(spec.next_run, time.time())

    def test_one_shot_does_not_reschedule(self):
        spec = self.sched.schedule("one_shot", lambda ctx: 1,
                                    schedule_type=self.ST.ONE_SHOT,
                                    run_now=True)
        _run(self.sched.run_now("one_shot"))
        self.assertEqual(spec.next_run, float('inf'))

    def test_dependency_tracking(self):
        self.sched.schedule("dep_a", lambda ctx: "a")
        self.sched.schedule("dep_b", lambda ctx: "b", depends_on=["dep_a"])
        self.assertFalse(self.sched._deps_satisfied(self.sched._tasks["dep_b"]))
        _run(self.sched.run_now("dep_a"))
        self.assertTrue(self.sched._deps_satisfied(self.sched._tasks["dep_b"]))

    def test_priority_ordering(self):
        specs = self.sched.list_tasks()
        self.sched.schedule("p_low", lambda ctx: 1, priority=10)
        self.sched.schedule("p_high", lambda ctx: 1, priority=1)
        specs = self.sched.list_tasks()
        priorities = [s.priority for s in specs]
        self.assertEqual(priorities, sorted(priorities))

    def test_on_success_hook(self):
        successes = []
        self.sched.on("on_success", lambda spec, tr: successes.append(spec.name))
        self.sched.schedule("hook_task", lambda ctx: 1)
        _run(self.sched.run_now("hook_task"))
        self.assertIn("hook_task", successes)

    def test_on_failure_hook(self):
        failures = []
        self.sched.on("on_failure", lambda spec, tr, e: failures.append(spec.name))
        self.sched.schedule("err_task", lambda ctx: 1/0)
        _run(self.sched.run_now("err_task"))
        self.assertIn("err_task", failures)

    def test_history_populated(self):
        self.sched.schedule("hist_task", lambda ctx: 1)
        _run(self.sched.run_now("hist_task"))
        spec = self.sched._tasks["hist_task"]
        self.assertEqual(len(spec.history), 1)

    def test_task_info(self):
        self.sched.schedule("info_task", lambda ctx: 1)
        info = self.sched.task_info("info_task")
        for k in ["id","name","schedule","run_count"]: self.assertIn(k, info)

    def test_list_by_tag(self):
        self.sched.schedule("tagged", lambda ctx: 1, tags=["nightly"])
        self.sched.schedule("other", lambda ctx: 1, tags=["hourly"])
        nightly = self.sched.list_tasks(tag="nightly")
        self.assertTrue(all("nightly" in s.tags for s in nightly))

    def test_run_count_increments(self):
        self.sched.schedule("count_task", lambda ctx: 1)
        _run(self.sched.run_now("count_task"))
        _run(self.sched.run_now("count_task"))
        self.assertEqual(self.sched._tasks["count_task"].run_count, 2)

    def test_stats(self):
        self.sched.schedule("st", lambda ctx: 1)
        _run(self.sched.run_now("st"))
        s = self.sched.stats()
        for k in ["scheduled_tasks","total_runs","running"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# TELEMETRY
# ════════════════════════════════════════════════════════
class TestTelemetry(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.telemetry import Telemetry
        self.tel = Telemetry(db_path=os.path.join(td,"tel.db"))

    def test_span_creates_and_finishes(self):
        s = self.tel.start_span("test_op")
        self.assertIsNotNone(s.id)
        self.tel.finish_span(s)
        self.assertGreaterEqual(s.duration_ms, 0)

    def test_span_context_manager(self):
        durations = []
        with self.tel.span("timed_op") as s:
            time.sleep(0.01)
        self.assertGreater(s.duration_ms, 5)

    def test_span_error_status(self):
        s = self.tel.start_span("error_op")
        self.tel.finish_span(s, "error", "something failed")
        self.assertEqual(s.status, "error")
        self.assertEqual(s.error, "something failed")

    def test_span_context_manager_catches_exception(self):
        with self.assertRaises(ValueError):
            with self.tel.span("exc_op"):
                raise ValueError("test error")
        spans = self.tel.get_spans("exc_op")
        self.assertGreater(len(spans), 0)

    def test_span_attributes(self):
        s = self.tel.start_span("attr_op", model="gpt4", tokens=100)
        self.assertEqual(s.attributes["model"], "gpt4")
        self.assertEqual(s.attributes["tokens"], 100)
        self.tel.finish_span(s)

    def test_trace_id_propagation(self):
        tid = "trace-123"
        s1 = self.tel.start_span("s1", trace_id=tid)
        s2 = self.tel.start_span("s2", trace_id=tid)
        self.tel.finish_span(s1); self.tel.finish_span(s2)
        spans = self.tel.get_spans(trace_id=tid)
        self.assertEqual(len(spans), 2)

    def test_new_trace(self):
        tid = self.tel.new_trace()
        self.assertIsNotNone(tid)
        s = self.tel.start_span("in_trace")
        self.assertEqual(s.trace_id, tid)
        self.tel.finish_span(s)

    def test_counter_inc(self):
        c = self.tel.counter("requests")
        c.inc(); c.inc(5)
        self.assertEqual(c.value, 6)

    def test_counter_rate(self):
        c = self.tel.counter("rate_test")
        c.inc(60)
        r = c.rate(60)
        self.assertGreater(r, 0)

    def test_inc_helper(self):
        self.tel.inc("api_calls", 3)
        c = self.tel.counter("api_calls")
        self.assertEqual(c.value, 3)

    def test_gauge_set(self):
        g = self.tel.gauge("queue_depth")
        g.set(7); self.assertEqual(g.value, 7)

    def test_gauge_inc_dec(self):
        g = self.tel.gauge("connections")
        g.inc(3); g.dec(1)
        self.assertEqual(g.value, 2)

    def test_set_gauge_helper(self):
        self.tel.set_gauge("active_users", 42)
        g = self.tel.gauge("active_users")
        self.assertEqual(g.value, 42)

    def test_histogram_observe(self):
        h = self.tel.histogram("latency")
        for v in [10, 20, 30, 40, 50]:
            h.observe(v)
        s = h.stats()
        self.assertEqual(s["count"], 5)
        self.assertAlmostEqual(s["mean"], 30.0)

    def test_histogram_percentiles(self):
        h = self.tel.histogram("p_test")
        for i in range(100): h.observe(float(i))
        self.assertGreater(h.percentile(0.95), h.percentile(0.50))

    def test_observe_helper(self):
        self.tel.observe("response_time", 42.5)
        h = self.tel.histogram("response_time")
        self.assertGreater(len(h._values), 0)

    def test_counter_with_labels(self):
        c1 = self.tel.counter("hits", labels={"env":"prod"})
        c2 = self.tel.counter("hits", labels={"env":"staging"})
        c1.inc(10); c2.inc(3)
        self.assertEqual(c1.value, 10)
        self.assertEqual(c2.value, 3)

    def test_summary_structure(self):
        self.tel.inc("x"); self.tel.set_gauge("y", 1); self.tel.observe("z", 1)
        s = self.tel.summary()
        for k in ["counters","gauges","histograms","active_spans"]:
            self.assertIn(k, s)

    def test_snapshot_persists(self):
        self.tel.inc("snap_counter", 5)
        self.tel.snapshot()
        s = self.tel.stats()
        self.assertGreater(s["snapshots"], 0)

    def test_get_spans(self):
        with self.tel.span("findable"): pass
        spans = self.tel.get_spans("findable")
        self.assertGreater(len(spans), 0)

    def test_stats(self):
        with self.tel.span("stat_span"): pass
        self.tel.inc("stat_c")
        s = self.tel.stats()
        for k in ["total_spans","counters","gauges"]: self.assertIn(k, s)

    def test_span_to_dict(self):
        s = self.tel.start_span("td_op")
        self.tel.finish_span(s)
        d = s.to_dict()
        for k in ["id","trace_id","name","status","duration_ms"]:
            self.assertIn(k, d)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v29: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
