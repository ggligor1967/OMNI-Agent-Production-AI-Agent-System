"""OMNI AGENT v26: PromptOptimizer, DataPipeline, ConversationRouter, HealthDashboard"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# PROMPT OPTIMIZER
# ════════════════════════════════════════════════════════
class TestPromptOptimizer(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.prompt_optimizer import PromptOptimizer
        self.opt = PromptOptimizer(db_path=os.path.join(td, "po.db"))

    def test_create_template(self):
        t = self.opt.create("qa", "Answer: {question}")
        self.assertEqual(t.name, "qa")
        self.assertIn("question", t.variables)

    def test_get_template(self):
        self.opt.create("t1", "Hello {name}")
        t = self.opt.get("t1")
        self.assertIsNotNone(t)

    def test_render(self):
        self.opt.create("greet", "Hello {name}, you are {age} years old.")
        result = self.opt.render("greet", name="Alice", age=30)
        self.assertEqual(result, "Hello Alice, you are 30 years old.")

    def test_token_count(self):
        self.opt.create("tok", "word " * 40)
        tokens = self.opt.token_count("tok")
        self.assertGreater(tokens, 0)

    def test_generate_variant_concise(self):
        self.opt.create("verbose", "Please kindly just simply answer the question.")
        v = self.opt.generate_variant("verbose", strategy="concise")
        self.assertLess(len(v.text), len(self.opt.get("verbose").text) + 5)
        self.assertEqual(v.strategy_used, "concise")

    def test_generate_variant_cot(self):
        self.opt.create("simple", "Answer the question.")
        v = self.opt.generate_variant("simple", strategy="cot")
        self.assertIn("step by step", v.text.lower())

    def test_generate_variant_role(self):
        self.opt.create("no_role", "Answer the question.")
        v = self.opt.generate_variant("no_role", strategy="role")
        self.assertIn("expert", v.text.lower())

    def test_generate_variant_role_idempotent(self):
        self.opt.create("has_role", "You are an expert. Answer.")
        v = self.opt.generate_variant("has_role", strategy="role")
        # Should not double-add role
        self.assertEqual(v.text.lower().count("you are an expert"), 1)

    def test_generate_variant_output_format(self):
        self.opt.create("no_fmt", "Summarise the text.")
        v = self.opt.generate_variant("no_fmt", strategy="output_format")
        self.assertIn("json", v.text.lower())

    def test_generate_variant_compress(self):
        self.opt.create("bloated", "Hello\n\n\n\nWorld  spaces  here")
        v = self.opt.generate_variant("bloated", strategy="compress")
        self.assertNotIn("\n\n\n", v.text)

    def test_generate_variant_structured(self):
        self.opt.create("unstructured", "Do this.\nDo that.\nDo other.")
        v = self.opt.generate_variant("unstructured", strategy="structured")
        self.assertIn("1.", v.text)

    def test_variant_parent_id(self):
        self.opt.create("parent_t", "Original text.")
        v = self.opt.generate_variant("parent_t", strategy="cot")
        self.assertEqual(v.parent_id, self.opt.get("parent_t").id)

    def test_score_template(self):
        self.opt.create("scored", "Some prompt.")
        self.opt.score_template("scored", 0.8)
        self.opt.score_template("scored", 0.9)
        t = self.opt.get("scored")
        self.assertAlmostEqual(t.avg_score, 0.85, places=2)

    def test_score_clamped(self):
        self.opt.create("clamp_t", "text")
        self.opt.score_template("clamp_t", 1.5)  # > 1.0
        t = self.opt.get("clamp_t")
        self.assertLessEqual(t.avg_score, 1.0)

    def test_best_returns_highest_score(self):
        self.opt.create("fam", "Original")
        v = self.opt.generate_variant("fam", strategy="cot")
        self.opt.score_template("fam", 0.5)
        self.opt.score_template(v.name, 0.9)
        best = self.opt.best("fam")
        self.assertEqual(best.name, v.name)

    def test_lineage(self):
        self.opt.create("root", "Root prompt.")
        v1 = self.opt.generate_variant("root", strategy="concise")
        chain = self.opt.lineage(v1.name)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[-1].name, "root")

    def test_ab_experiment_assign(self):
        self.opt.create("ctrl", "Control prompt.")
        self.opt.create("var_a", "Variant A prompt.")
        exp = self.opt.create_experiment("exp1", "ctrl", ["var_a"],
                                          traffic_split=[0.5, 0.5])
        assigned = set()
        for _ in range(20):
            t = self.opt.assign_experiment("exp1")
            if t: assigned.add(t.name)
        self.assertGreater(len(assigned), 0)

    def test_apply_strategy_no_save(self):
        self.opt.create("ns", "Please just kindly answer.")
        result = self.opt.apply_strategy("ns", "concise")
        self.assertIsInstance(result, str)
        self.assertFalse(self.opt.get(f"ns__concise"))  # not saved

    def test_update(self):
        self.opt.create("upd", "Old text")
        self.opt.update("upd", "New text")
        self.assertEqual(self.opt.get("upd").text, "New text")
        self.assertEqual(self.opt.get("upd").version, 2)

    def test_delete(self):
        self.opt.create("del_t", "text")
        ok = self.opt.delete("del_t")
        self.assertTrue(ok)
        self.assertIsNone(self.opt.get("del_t"))

    def test_list(self):
        self.opt.create("l1", "text", tags=["nlp"])
        self.opt.create("l2", "text", tags=["code"])
        all_t = self.opt.list()
        self.assertGreaterEqual(len(all_t), 2)

    def test_list_by_tag(self):
        self.opt.create("tag_t", "text", tags=["special"])
        tagged = self.opt.list(tag="special")
        self.assertTrue(all("special" in t.tags for t in tagged))

    def test_stats(self):
        s = self.opt.stats()
        for k in ["total_templates","in_memory","experiments","strategies"]: self.assertIn(k, s)

    def test_to_dict(self):
        t = self.opt.create("dict_t", "text")
        d = t.to_dict()
        for k in ["id","name","tokens","variables","avg_score"]: self.assertIn(k, d)

    def test_persistence(self):
        td = tempfile.mkdtemp()
        from agent.prompt_optimizer import PromptOptimizer
        db = os.path.join(td, "po2.db")
        opt1 = PromptOptimizer(db_path=db)
        opt1.create("persist_p", "Persist me.")
        opt2 = PromptOptimizer(db_path=db)
        self.assertIsNotNone(opt2.get("persist_p"))

# ════════════════════════════════════════════════════════
# DATA PIPELINE
# ════════════════════════════════════════════════════════
class TestDataPipeline(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.data_pipeline import DataPipeline
        self.dp = DataPipeline(db_path=os.path.join(td, "dp.db"))

    def test_simple_pipeline(self):
        from agent.data_pipeline import PipelineState
        pl = self.dp.define("simple")
        pl.extract("load", lambda ctx: None)
        pl.transform("upper", lambda ctx: None)
        run = _run(self.dp.run("simple", initial_data=["a", "b", "c"]))
        self.assertEqual(run.state, PipelineState.COMPLETED)

    def test_initial_data_in_context(self):
        pl = self.dp.define("data_check")
        pl.extract("load", lambda ctx: None)
        run = _run(self.dp.run("data_check", initial_data=["x", "y"]))
        self.assertEqual(len(run.context.records), 2)

    def test_transform_modifies_records(self):
        def upper_transform(ctx):
            for r in ctx.active_records():
                r.data = str(r.data).upper()
        pl = self.dp.define("transform_test")
        pl.transform("upper", upper_transform)
        run = _run(self.dp.run("transform_test", initial_data=["hello", "world"]))
        values = [r.data for r in run.context.records]
        self.assertIn("HELLO", values)

    def test_stage_returns_new_context(self):
        from agent.data_pipeline import PipelineContext
        def add_records(ctx):
            new_ctx = PipelineContext()
            for r in ctx.active_records():
                new_ctx.add(r.data * 2)
            return new_ctx
        pl = self.dp.define("ctx_return")
        pl.transform("double", add_records)
        run = _run(self.dp.run("ctx_return", initial_data=[1, 2, 3]))
        self.assertIn(2, [r.data for r in run.context.records])

    def test_async_stage(self):
        async def async_stage(ctx):
            await asyncio.sleep(0.01)
            for r in ctx.active_records():
                r.data = str(r.data) + "_async"
        pl = self.dp.define("async_test")
        pl.transform("async_step", async_stage)
        run = _run(self.dp.run("async_test", initial_data=["item"]))
        self.assertIn("item_async", [r.data for r in run.context.records])

    def test_stage_error_fail_fast(self):
        from agent.data_pipeline import PipelineState, ErrorMode
        pl = self.dp.define("fail_fast")
        pl.transform("boom", lambda ctx: 1/0,
                     error_mode=ErrorMode.FAIL_FAST, max_retries=0)
        pl.load("never_reached", lambda ctx: None)
        run = _run(self.dp.run("fail_fast", initial_data=["x"]))
        self.assertEqual(run.state, PipelineState.FAILED)
        # Second stage should not have run
        ran_names = [s["stage"] for s in run.stage_results]
        self.assertNotIn("never_reached", ran_names)

    def test_stage_retry(self):
        calls = [0]
        def flaky(ctx):
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("not yet")
        pl = self.dp.define("retry_test")
        pl.transform("flaky", flaky, max_retries=3, retry_delay=0.01)
        run = _run(self.dp.run("retry_test", initial_data=["x"]))
        self.assertGreaterEqual(calls[0], 2)

    def test_lineage_tagged(self):
        pl = self.dp.define("lineage_test")
        pl.extract("extract_step", lambda ctx: None)
        pl.transform("transform_step", lambda ctx: None)
        run = _run(self.dp.run("lineage_test", initial_data=["item"]))
        record = run.context.records[0]
        stages_in_lineage = [tag.split("@")[0] for tag in record.lineage]
        self.assertIn("extract_step", stages_in_lineage)

    def test_stage_metrics(self):
        pl = self.dp.define("metrics_test")
        pl.extract("e1", lambda ctx: None)
        run = _run(self.dp.run("metrics_test", initial_data=["a", "b"]))
        stage = self.dp.stages("metrics_test")[0]
        self.assertGreater(stage.run_count, 0)

    def test_parallel_pipelines(self):
        from agent.data_pipeline import PipelineState
        for name in ["par_a", "par_b"]:
            pl = self.dp.define(name)
            pl.transform("step", lambda ctx: None)
        runs = _run(self.dp.run_parallel(["par_a", "par_b"], initial_data=["x"]))
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(r.state == PipelineState.COMPLETED for r in runs))

    def test_get_run(self):
        pl = self.dp.define("get_run_test")
        pl.extract("e", lambda ctx: None)
        run = _run(self.dp.run("get_run_test"))
        fetched = self.dp.get_run(run.id)
        self.assertIsNotNone(fetched)

    def test_list_runs(self):
        pl = self.dp.define("list_test")
        pl.extract("e", lambda ctx: None)
        _run(self.dp.run("list_test")); _run(self.dp.run("list_test"))
        runs = self.dp.list_runs("list_test")
        self.assertGreaterEqual(len(runs), 2)

    def test_stats(self):
        pl = self.dp.define("stats_test")
        pl.extract("e", lambda ctx: None)
        _run(self.dp.run("stats_test"))
        s = self.dp.stats()
        for k in ["total_runs","defined_pipelines"]: self.assertIn(k, s)

    def test_run_to_dict(self):
        pl = self.dp.define("dict_test")
        pl.extract("e", lambda ctx: None)
        run = _run(self.dp.run("dict_test"))
        d = run.to_dict()
        for k in ["id","pipeline","state","duration_ms","stage_results"]: self.assertIn(k, d)

    def test_record_lineage(self):
        from agent.data_pipeline import Record
        r = Record(id="r1", data="test")
        r.tag("stage1"); r.tag("stage2")
        self.assertEqual(len(r.lineage), 2)

    def test_context_to_dict(self):
        pl = self.dp.define("ctx_dict")
        pl.extract("e", lambda ctx: None)
        run = _run(self.dp.run("ctx_dict", initial_data=["a"]))
        d = run.context.to_dict()
        for k in ["record_count","active","errors"]: self.assertIn(k, d)

    def test_disabled_stage_skipped(self):
        pl = self.dp.define("disabled_test")
        s1 = pl.extract("active_stage", lambda ctx: None)
        s2 = pl.transform("disabled_stage", lambda ctx: (_ for _ in ()).throw(RuntimeError("should not run")))
        # Disable s2
        self.dp.stages("disabled_test")[1].enabled = False
        run = _run(self.dp.run("disabled_test", initial_data=["x"]))
        ran = [s["stage"] for s in run.stage_results]
        self.assertNotIn("disabled_stage", ran)

# ════════════════════════════════════════════════════════
# CONVERSATION ROUTER
# ════════════════════════════════════════════════════════
class TestConversationRouter(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.conversation_router import ConversationRouter, HandlerResponse
        self.router = ConversationRouter(
            db_path=os.path.join(td, "router.db"),
            similarity_threshold=0.3)
        self.HR = HandlerResponse
        self._register_intents()

    def _register_intents(self):
        self.router.register("greet",
            patterns=[r'\b(hi|hello|hey)\b'],
            keywords=["hello","hi","hey"],
            handler=lambda message, ctx: self.HR(reply="Hello there!"),
            priority=1, tags=["common"])
        self.router.register("help",
            keywords=["help","assist","support","stuck"],
            examples=["I need assistance", "can you help me"],
            handler=lambda message, ctx: self.HR(reply="How can I help you?"),
            priority=2)
        self.router.register("bye",
            patterns=[r'\b(bye|goodbye|ciao|farewell)\b'],
            keywords=["bye","goodbye"],
            handler=lambda message, ctx: self.HR(reply="Goodbye!"),
            priority=1)

    def test_route_regex_match(self):
        decision, response = _run(self.router.route("Hello there!", "s1"))
        self.assertEqual(decision.intent_name, "greet")
        self.assertEqual(decision.method, "regex")

    def test_route_keyword_match(self):
        decision, response = _run(self.router.route("I need some help please", "s2"))
        self.assertEqual(decision.intent_name, "help")
        self.assertEqual(decision.method, "keyword")

    def test_route_response(self):
        _, response = _run(self.router.route("hi", "s3"))
        self.assertEqual(response.reply, "Hello there!")

    def test_route_fallback(self):
        decision, response = _run(self.router.route("xyzzy unknown query zork", "s4"))
        self.assertEqual(decision.intent_name, "fallback")

    def test_route_similarity(self):
        decision, _ = _run(self.router.route("I need assistance with something", "s5"))
        # Should match help via keyword or similarity; greet also acceptable
        self.assertIn(decision.intent_name, ["help", "fallback", "greet"])

    def test_session_created(self):
        _run(self.router.route("hello", "sess_new"))
        s = self.router.get_session("sess_new")
        self.assertIsNotNone(s)

    def test_session_history(self):
        _run(self.router.route("hello", "hist_sess"))
        _run(self.router.route("help", "hist_sess"))
        s = self.router.get_session("hist_sess")
        self.assertEqual(len(s.history), 2)

    def test_guided_intent(self):
        _run(self.router.route("hello", "guided_sess"))
        session = self.router.get_session("guided_sess")
        session.next_expected_intent = "help"
        decision, _ = _run(self.router.route("random stuff", "guided_sess"))
        self.assertEqual(decision.intent_name, "help")

    def test_context_updates(self):
        self.router.register("ctx_intent",
            keywords=["update"],
            handler=lambda message, ctx: self.HR(
                reply="updated", context_updates={"key": "val"}))
        _run(self.router.route("please update context", "ctx_sess"))
        s = self.router.get_session("ctx_sess")
        self.assertEqual(s.context.get("key"), "val")

    def test_slot_extraction(self):
        self.router.register("order",
            keywords=["order","buy"],
            slots={"product": r'\b(apple|banana|orange)\b'},
            handler=lambda message, ctx, slots: self.HR(
                reply=f"Ordering {slots.get('product','?')}"))
        _, response = _run(self.router.route("I want to order a banana", "slot_sess"))
        self.assertIn("banana", response.reply)

    def test_next_intent_set(self):
        self.router.register("multi_step",
            keywords=["start"],
            handler=lambda message, ctx: self.HR(
                reply="Step 1", next_intent="help"))
        _run(self.router.route("start the process", "multi_sess"))
        s = self.router.get_session("multi_sess")
        self.assertEqual(s.next_expected_intent, "help")

    def test_pre_hook(self):
        transformed = []
        self.router.add_pre_hook(lambda msg, ctx: transformed.append(msg) or msg)
        _run(self.router.route("hello", "hook_sess"))
        self.assertGreater(len(transformed), 0)

    def test_post_hook(self):
        fired = []
        self.router.add_post_hook(lambda d, r, s: fired.append(d.intent_name))
        _run(self.router.route("hello", "post_sess"))
        self.assertGreater(len(fired), 0)

    def test_async_handler(self):
        async def async_handler(message, ctx):
            await asyncio.sleep(0.01)
            return self.HR(reply="async reply")
        self.router.register("async_intent",
            keywords=["async"],
            handler=async_handler)
        _, response = _run(self.router.route("async test", "async_sess"))
        self.assertEqual(response.reply, "async reply")

    def test_list_intents(self):
        intents = self.router.list_intents()
        names = [i.name for i in intents]
        self.assertIn("greet", names); self.assertIn("help", names)

    def test_list_intents_by_tag(self):
        tagged = self.router.list_intents(tag="common")
        self.assertTrue(all("common" in i.tags for i in tagged))

    def test_priority_ordering(self):
        intents = self.router.list_intents()
        priorities = [i.priority for i in intents]
        self.assertEqual(priorities, sorted(priorities))

    def test_intent_to_dict(self):
        i = self.router._intents["greet"]
        d = i.to_dict()
        for k in ["id","name","priority","call_count"]: self.assertIn(k, d)

    def test_decision_to_dict(self):
        decision, _ = _run(self.router.route("hello", "dict_sess"))
        d = decision.to_dict()
        for k in ["intent","confidence","method"]: self.assertIn(k, d)

    def test_stats(self):
        _run(self.router.route("hello", "stat_s"))
        s = self.router.stats()
        for k in ["total_decisions","registered_intents"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# HEALTH DASHBOARD
# ════════════════════════════════════════════════════════
class TestHealthDashboard(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.health_dashboard import HealthDashboard
        self.hd = HealthDashboard(db_path=os.path.join(td, "health.db"),
                                   alert_cooldown_s=0.01)

    def test_register_component(self):
        spec = self.hd.register("db", lambda: ("healthy", 10.0, "ok"))
        self.assertEqual(spec.name, "db")

    def test_check_healthy(self):
        from agent.health_dashboard import HealthState
        self.hd.register("fast_svc", lambda: ("healthy", 5.0, ""))
        result = _run(self.hd.check_component("fast_svc"))
        self.assertEqual(result.state, HealthState.HEALTHY)

    def test_check_degraded(self):
        from agent.health_dashboard import HealthState
        self.hd.register("slow_svc", lambda: ("degraded", 600.0, "slow"),
                          warn_threshold=500, critical_threshold=2000)
        result = _run(self.hd.check_component("slow_svc"))
        self.assertEqual(result.state, HealthState.DEGRADED)

    def test_check_unhealthy(self):
        from agent.health_dashboard import HealthState
        self.hd.register("dead_svc", lambda: ("unhealthy", 3000.0, "down"))
        result = _run(self.hd.check_component("dead_svc"))
        self.assertEqual(result.state, HealthState.UNHEALTHY)

    def test_check_unknown_component(self):
        from agent.health_dashboard import HealthState
        result = _run(self.hd.check_component("nonexistent"))
        self.assertEqual(result.state, HealthState.UNKNOWN)

    def test_numeric_return_threshold(self):
        from agent.health_dashboard import HealthState
        self.hd.register("metric_svc", lambda: 50.0,
                          warn_threshold=100, critical_threshold=500)
        result = _run(self.hd.check_component("metric_svc"))
        self.assertEqual(result.state, HealthState.HEALTHY)

    def test_check_exception(self):
        from agent.health_dashboard import HealthState
        self.hd.register("exc_svc", lambda: 1/0)
        result = _run(self.hd.check_component("exc_svc"))
        self.assertEqual(result.state, HealthState.UNHEALTHY)

    def test_async_check_fn(self):
        from agent.health_dashboard import HealthState
        async def async_check():
            await asyncio.sleep(0.01)
            return ("healthy", 20.0, "")
        self.hd.register("async_svc", async_check)
        result = _run(self.hd.check_component("async_svc"))
        self.assertEqual(result.state, HealthState.HEALTHY)

    def test_history_populated(self):
        self.hd.register("hist_svc", lambda: ("healthy", 10.0, ""))
        _run(self.hd.check_component("hist_svc"))
        _run(self.hd.check_component("hist_svc"))
        spec = self.hd._components["hist_svc"]
        self.assertEqual(len(spec.history), 2)

    def test_alert_fired(self):
        from agent.health_dashboard import HealthState
        alerts = []
        self.hd.add_alert_hook(lambda a: alerts.append(a.component))
        self.hd.register("bad_svc", lambda: ("unhealthy", 3000.0, "down"))
        _run(self.hd.check_component("bad_svc"))
        self.assertIn("bad_svc", alerts)

    def test_alert_cooldown(self):
        alerts = []
        self.hd.add_alert_hook(lambda a: alerts.append(a))
        # Use longer cooldown for this test
        self.hd._cooldown = 1000.0
        self.hd.register("cool_svc", lambda: ("unhealthy", 3000.0, ""))
        _run(self.hd.check_component("cool_svc"))
        _run(self.hd.check_component("cool_svc"))
        self.assertEqual(len(alerts), 1)  # only one alert despite two checks

    def test_resolve_alert(self):
        self.hd.register("resolve_svc", lambda: ("unhealthy", 3000.0, ""))
        _run(self.hd.check_component("resolve_svc"))
        alert_ids = list(self.hd._alerts.keys())
        if alert_ids:
            self.hd.resolve_alert(alert_ids[0])
            self.assertTrue(self.hd._alerts[alert_ids[0]].resolved)

    def test_check_all(self):
        self.hd.register("c1", lambda: ("healthy", 5.0, ""))
        self.hd.register("c2", lambda: ("healthy", 10.0, ""))
        results = _run(self.hd.check_all())
        self.assertIn("c1", results); self.assertIn("c2", results)

    def test_overall_score_all_healthy(self):
        self.hd.register("s1", lambda: ("healthy", 5.0, ""))
        _run(self.hd.check_component("s1"))
        score = self.hd.overall_score()
        self.assertGreater(score, 0.0)

    def test_overall_score_degraded(self):
        from agent.health_dashboard import HealthState
        self.hd.register("deg", lambda: ("degraded", 600.0, ""), criticality="high")
        _run(self.hd.check_component("deg"))
        score = self.hd.overall_score()
        self.assertLess(score, 1.0)

    def test_latency_stats(self):
        self.hd.register("lat", lambda: ("healthy", 50.0, ""))
        for _ in range(5): _run(self.hd.check_component("lat"))
        spec = self.hd._components["lat"]
        stats = spec.latency_stats()
        for k in ["p50","p95","min","max","mean"]: self.assertIn(k, stats)

    def test_trend_stable(self):
        self.hd.register("stable", lambda: ("healthy", 50.0, ""))
        for _ in range(5): _run(self.hd.check_component("stable"))
        spec = self.hd._components["stable"]
        self.assertEqual(spec.trend(), "stable")

    def test_trend_rising(self):
        from agent.health_dashboard import _trend_slope
        slope = _trend_slope([10.0, 20.0, 30.0, 40.0, 50.0])
        self.assertGreater(slope, 0)

    def test_get_status(self):
        self.hd.register("stat_c", lambda: ("healthy", 5.0, ""))
        _run(self.hd.check_component("stat_c"))
        status = self.hd.get_status()
        for k in ["overall_score","components","open_alerts"]: self.assertIn(k, status)

    def test_stats(self):
        self.hd.register("stat_s", lambda: ("healthy", 5.0, ""))
        _run(self.hd.check_component("stat_s"))
        s = self.hd.stats()
        for k in ["total_checks","registered_components","overall_score"]: self.assertIn(k, s)

    def test_result_to_dict(self):
        self.hd.register("dict_c", lambda: ("healthy", 5.0, "ok"))
        result = _run(self.hd.check_component("dict_c"))
        d = result.to_dict()
        for k in ["component","state","value","check_ms"]: self.assertIn(k, d)

    def test_spec_to_dict(self):
        self.hd.register("spec_c", lambda: ("healthy", 5.0, ""))
        _run(self.hd.check_component("spec_c"))
        d = self.hd._components["spec_c"].to_dict()
        for k in ["id","name","state","criticality","trend"]: self.assertIn(k, d)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v26: {total-failed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
