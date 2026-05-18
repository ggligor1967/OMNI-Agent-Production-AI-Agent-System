"""OMNI AGENT v17 Tests: ContextComposer, ResponseStreamer, ExperimentTracker, TokenBudgetManager"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# CONTEXT COMPOSER
# ════════════════════════════════════════════════════════
class TestContextComposer(unittest.TestCase):
    def setUp(self):
        from agent.context_composer import ContextComposer
        self.ctx = ContextComposer(token_budget=1000)

    def test_set_slot(self):
        self.ctx.set_slot("system", "You are helpful.")
        self.assertEqual(self.ctx.get_slot("system").content, "You are helpful.")

    def test_token_count(self):
        self.ctx.set_slot("system", "A" * 400)
        self.assertGreater(self.ctx.total_tokens, 0)

    def test_add_turn(self):
        t = self.ctx.add_turn("user", "Hello!")
        self.assertEqual(t.role, "user")
        self.assertEqual(len(self.ctx._turns), 1)

    def test_pop_turn(self):
        self.ctx.add_turn("user", "Hello")
        t = self.ctx.pop_turn()
        self.assertEqual(t.role, "user")
        self.assertEqual(len(self.ctx._turns), 0)

    def test_clear_turns(self):
        self.ctx.add_turn("user", "A"); self.ctx.add_turn("assistant", "B")
        self.ctx.clear_turns(); self.assertEqual(len(self.ctx._turns), 0)

    def test_inject_retrieval(self):
        self.ctx.inject_retrieval(["Passage 1.", "Passage 2."])
        self.assertIn("Passage 1", self.ctx.get_slot("retrieved").content)

    def test_render_chat_format(self):
        self.ctx.set_slot("system", "Be helpful.")
        self.ctx.add_turn("user", "Hi")
        rendered = self.ctx.render("chat")
        self.assertIn("system", rendered); self.assertIn("user", rendered)

    def test_render_xml(self):
        self.ctx.set_slot("system", "Test")
        rendered = self.ctx.render("xml")
        self.assertIn("<context>", rendered); self.assertIn("<system>", rendered)

    def test_render_markdown(self):
        self.ctx.set_slot("system", "Test")
        rendered = self.ctx.render("markdown")
        self.assertIn("SYSTEM", rendered)

    def test_render_plain(self):
        self.ctx.set_slot("system", "Hello")
        rendered = self.ctx.render("plain")
        self.assertIn("Hello", rendered)

    def test_prune_drops_low_priority(self):
        self.ctx.set_slot("system", "S", pinned=True)
        self.ctx.set_slot("scratchpad", "X" * 5000)
        pruned = self.ctx.prune_to_budget()
        self.assertGreater(len(pruned), 0)

    def test_pinned_slot_not_pruned(self):
        self.ctx.set_slot("system", "Critical", pinned=True)
        self.ctx.set_slot("scratchpad", "X" * 10000)
        self.ctx.prune_to_budget()
        self.assertEqual(self.ctx.get_slot("system").content, "Critical")

    def test_remaining_tokens(self):
        self.ctx.set_slot("system", "Hello")
        self.assertLess(self.ctx.remaining_tokens, self.ctx.token_budget)

    def test_token_breakdown(self):
        self.ctx.set_slot("system", "Hello")
        b = self.ctx.token_breakdown()
        self.assertIn("_total", b); self.assertIn("_remaining", b)

    def test_clear_slot(self):
        self.ctx.set_slot("scratchpad", "Data")
        self.ctx.clear_slot("scratchpad")
        self.assertEqual(self.ctx.get_slot("scratchpad").content, "")

    def test_pin_unpin(self):
        self.ctx.set_slot("system", "S"); self.ctx.pin_slot("system")
        self.assertTrue(self.ctx.get_slot("system").pinned)
        self.ctx.unpin_slot("system")
        self.assertFalse(self.ctx.get_slot("system").pinned)

    def test_last_n_turns(self):
        for i in range(5): self.ctx.add_turn("user", f"msg{i}")
        last3 = self.ctx.last_n_turns(3)
        self.assertEqual(len(last3), 3)

    def test_diff(self):
        from agent.context_composer import ContextComposer
        ctx2 = ContextComposer(token_budget=1000)
        self.ctx.set_slot("system", "A"); ctx2.set_slot("system", "B")
        diff = self.ctx.diff(ctx2)
        self.assertIn("system", diff)

    def test_to_dict(self):
        self.ctx.set_slot("system", "Hello"); self.ctx.add_turn("user", "Hi")
        d = self.ctx.to_dict()
        for k in ["token_budget","slots","turns","total_tokens"]: self.assertIn(k,d)

    def test_from_dict_roundtrip(self):
        from agent.context_composer import ContextComposer
        self.ctx.set_slot("system", "Restored"); self.ctx.add_turn("user","Test")
        d = self.ctx.to_dict()
        ctx2 = ContextComposer.from_dict(d)
        self.assertEqual(ctx2.get_slot("system").content, "Restored")
        self.assertEqual(len(ctx2._turns), 1)

    def test_stats(self):
        self.ctx.set_slot("system", "Hello")
        s = self.ctx.stats()
        for k in ["total_tokens","token_budget","remaining_tokens","utilisation_pct"]:
            self.assertIn(k, s)

    def test_truncate_slot(self):
        from agent.context_composer import Slot
        s = Slot(name="test", content="word " * 200)
        t = s.truncate(10)
        self.assertLessEqual(t.token_count, 12)

# ════════════════════════════════════════════════════════
# RESPONSE STREAMER
# ════════════════════════════════════════════════════════
class TestResponseStreamer(unittest.TestCase):
    def setUp(self):
        from agent.response_streamer import ResponseStreamer
        self.rs = ResponseStreamer()

    def _tokens(self, text="Hello world this is a test stream"):
        return text.split()

    def test_stream_list(self):
        text, metrics = _run(self.rs.stream(self._tokens()))
        self.assertGreater(len(text), 0)

    def test_stream_all_tokens_present(self):
        words = ["alpha","beta","gamma","delta"]
        text, _ = _run(self.rs.stream(words))
        for w in words: self.assertIn(w, text)

    def test_stream_metrics_populated(self):
        _, metrics = _run(self.rs.stream(self._tokens()))
        self.assertGreater(metrics.total_tokens, 0)
        self.assertGreater(metrics.total_latency_ms, 0)

    def test_on_chunk_callback(self):
        chunks = []
        def collect(c): chunks.append(c)
        _run(self.rs.stream(self._tokens(), on_chunk=collect))
        self.assertGreater(len(chunks), 0)

    def test_flush_modes(self):
        for mode in ["token","word","sentence","chunk"]:
            _, m = _run(self.rs.stream(self._tokens(), flush_mode=mode))
            self.assertGreater(m.total_tokens, 0)

    def test_stream_async_generator(self):
        async def gen():
            for w in ["the","quick","brown","fox"]:
                yield w + " "
        text, _ = _run(self.rs.stream(gen()))
        self.assertIn("fox", text)

    def test_injection(self):
        from agent.response_streamer import InjectionPoint
        tokens = ["Hello", "[[cite]]", "world"]
        injections = [InjectionPoint("[[cite]]", "[REF1]")]
        text, metrics = _run(self.rs.stream(tokens, injections=injections))
        self.assertIn("REF1", text)
        self.assertEqual(metrics.injections, 1)

    def test_max_tokens(self):
        long_tokens = ["word"] * 50
        _, metrics = _run(self.rs.stream(long_tokens, max_tokens=5))
        self.assertLessEqual(metrics.total_tokens, 6)

    def test_subscribe(self):
        sid, q = self.rs.subscribe("test_sub")
        self.assertEqual(sid, "test_sub")
        self.assertIsNotNone(q)

    def test_unsubscribe(self):
        sid, _ = self.rs.subscribe("unsub_test")
        self.rs.unsubscribe(sid)
        self.assertNotIn(sid, self.rs._subscribers)

    def test_detect_format_json(self):
        self.assertEqual(self.rs.detect_format('{"key":"value"}'), "json")

    def test_detect_format_code(self):
        self.assertEqual(self.rs.detect_format("```python\ncode\n```"), "code")

    def test_detect_format_markdown(self):
        self.assertEqual(self.rs.detect_format("## Title\nContent"), "markdown")

    def test_detect_format_plain(self):
        self.assertEqual(self.rs.detect_format("just plain text here"), "plain")

    def test_metrics_tokens_per_second(self):
        _, metrics = _run(self.rs.stream(["a","b","c","d","e"]))
        self.assertGreaterEqual(metrics.tokens_per_second, 0)

    def test_stream_history(self):
        _run(self.rs.stream(self._tokens())); _run(self.rs.stream(self._tokens()))
        self.assertGreaterEqual(len(self.rs.history()), 2)

    def test_stats(self):
        _run(self.rs.stream(self._tokens()))
        s = self.rs.stats()
        for k in ["total_streams","avg_tokens_per_second"]: self.assertIn(k, s)

    def test_stream_buffer_flush_modes(self):
        from agent.response_streamer import StreamBuffer
        buf = StreamBuffer("word", 5)
        result = buf.push("hello ")
        self.assertIsNotNone(result)

    def test_chunk_to_dict(self):
        from agent.response_streamer import StreamChunk
        c = StreamChunk("hello", 0, False, 5, 100.0)
        d = c.to_dict()
        for k in ["text","chunk_id","is_final","tokens_so_far"]: self.assertIn(k,d)

    def test_metrics_to_dict(self):
        _, m = _run(self.rs.stream(self._tokens()))
        d = m.to_dict()
        for k in ["total_tokens","total_chunks","total_latency_ms","tokens_per_second"]:
            self.assertIn(k, d)

    def test_progress_callback(self):
        milestones_hit = []
        async def cb(pct): milestones_hit.append(pct)
        tokens = ["word"] * 20
        _run(self.rs.stream(tokens, max_tokens=20, progress_callbacks={50: cb}))
        self.assertGreater(len(milestones_hit), 0)

# ════════════════════════════════════════════════════════
# EXPERIMENT TRACKER
# ════════════════════════════════════════════════════════
class TestExperimentTracker(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.experiment_tracker import ExperimentTracker
        self.et = ExperimentTracker(db_path=os.path.join(td,"et.db"))

    def test_create_run(self):
        run = self.et.create_run("baseline")
        self.assertIsNotNone(run.id)

    def test_run_params(self):
        run = self.et.create_run("test", params={"lr":0.001,"epochs":10})
        self.assertEqual(run.params["lr"], 0.001)

    def test_start_finish(self):
        run = self.et.create_run("r1")
        self.et.start_run(run.id); self.et.finish_run(run.id)
        loaded = self.et.get_run(run.id)
        self.assertEqual(loaded.status.value, "completed")

    def test_fail_run(self):
        run = self.et.create_run("r_fail")
        self.et.fail_run(run.id, reason="OOM")
        loaded = self.et.get_run(run.id)
        self.assertEqual(loaded.status.value, "failed")

    def test_log_metric(self):
        run = self.et.create_run("r_metric")
        self.et.log_metric(run.id, "loss", 0.9, step=0)
        self.et.log_metric(run.id, "loss", 0.5, step=1)
        loaded = self.et.get_run(run.id)
        self.assertEqual(len(loaded.metrics["loss"]), 2)

    def test_log_params(self):
        run = self.et.create_run("r_params")
        self.et.log_params(run.id, {"batch_size": 32})
        loaded = self.et.get_run(run.id)
        self.assertEqual(loaded.params["batch_size"], 32)

    def test_add_artefact(self):
        run = self.et.create_run("r_art")
        self.et.add_artefact(run.id, "/path/to/model.pt")
        loaded = self.et.get_run(run.id)
        self.assertIn("/path/to/model.pt", loaded.artefacts)

    def test_best_metric_max(self):
        run = self.et.create_run("r_best")
        self.et.log_metric(run.id, "acc", 0.7)
        self.et.log_metric(run.id, "acc", 0.9)
        self.assertEqual(run.best_metric("acc","max"), 0.9)

    def test_best_metric_min(self):
        run = self.et.create_run("r_min")
        self.et.log_metric(run.id, "loss", 0.5)
        self.et.log_metric(run.id, "loss", 0.2)
        self.assertEqual(run.best_metric("loss","min"), 0.2)

    def test_list_runs(self):
        self.et.create_run("r1", experiment="exp1")
        self.et.create_run("r2", experiment="exp1")
        runs = self.et.list_runs(experiment="exp1")
        self.assertEqual(len(runs), 2)

    def test_best_run(self):
        r1 = self.et.create_run("r1", experiment="cmp")
        r2 = self.et.create_run("r2", experiment="cmp")
        self.et.start_run(r1.id); self.et.log_metric(r1.id,"acc",0.7); self.et.finish_run(r1.id)
        self.et.start_run(r2.id); self.et.log_metric(r2.id,"acc",0.9); self.et.finish_run(r2.id)
        best = self.et.best_run("cmp","acc","max")
        self.assertEqual(best.id, r2.id)

    def test_compare_runs(self):
        r1 = self.et.create_run("r1",params={"lr":0.01})
        r2 = self.et.create_run("r2",params={"lr":0.001})
        self.et.log_metric(r1.id,"loss",0.5); self.et.log_metric(r2.id,"loss",0.3)
        cmp = self.et.compare_runs([r1.id,r2.id])
        self.assertIn("params",cmp); self.assertIn("metrics",cmp)

    def test_compare_significance(self):
        r1 = self.et.create_run("r1"); r2 = self.et.create_run("r2")
        for v in [0.9,0.85,0.88]: self.et.log_metric(r1.id,"acc",v)
        for v in [0.7,0.72,0.71]: self.et.log_metric(r2.id,"acc",v)
        cmp = self.et.compare_runs([r1.id,r2.id])
        self.assertIn("acc",cmp.get("significance_tests",{}))

    def test_tags_filtering(self):
        self.et.create_run("r1",tags=["production"])
        self.et.create_run("r2",tags=["experiment"])
        runs = self.et.list_runs(tags=["production"])
        self.assertTrue(all("production" in r.tags for r in runs))

    def test_duration_recorded(self):
        run = self.et.create_run("r_dur")
        self.et.start_run(run.id)
        time.sleep(0.05)
        self.et.finish_run(run.id)
        loaded = self.et.get_run(run.id)
        self.assertGreater(loaded.duration_s, 0)

    def test_to_dict(self):
        run = self.et.create_run("r_dict",params={"p":1})
        d = run.to_dict()
        for k in ["id","name","experiment","params","status"]: self.assertIn(k,d)

    def test_stats(self):
        self.et.create_run("r_stats")
        s = self.et.stats()
        for k in ["total_runs","total_metric_points","by_status"]: self.assertIn(k,s)

    def test_persistence(self):
        from agent.experiment_tracker import ExperimentTracker
        td = tempfile.mkdtemp(); db = os.path.join(td,"et.db")
        et1 = ExperimentTracker(db_path=db)
        r = et1.create_run("persist_run"); et1.finish_run(r.id)
        et2 = ExperimentTracker(db_path=db)
        loaded = et2.get_run(r.id)
        self.assertEqual(loaded.status.value,"completed")

# ════════════════════════════════════════════════════════
# TOKEN BUDGET MANAGER
# ════════════════════════════════════════════════════════
class TestTokenBudgetManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.token_budget_manager import TokenBudgetManager
        self.mgr = TokenBudgetManager(db_path=os.path.join(td,"tb.db"))

    def test_set_quota(self):
        q = self.mgr.set_quota("alice", hard_limit=10000, soft_limit=8000)
        self.assertIsNotNone(q.id)

    def test_check_under_budget(self):
        self.mgr.set_quota("bob", hard_limit=10000)
        result = self.mgr.check("bob", tokens_needed=100)
        self.assertTrue(result.allowed)

    def test_check_no_quota(self):
        result = self.mgr.check("unknown_actor", tokens_needed=100)
        self.assertTrue(result.allowed)

    def test_check_hard_limit_blocks(self):
        self.mgr.set_quota("carol", hard_limit=100, overage_policy="block")
        # Record usage to exhaust budget
        self.mgr.record("carol", "gpt4", input_tokens=90, output_tokens=20)
        result = self.mgr.check("carol", tokens_needed=50, model="gpt4")
        self.assertFalse(result.allowed)

    def test_check_warn_policy(self):
        self.mgr.set_quota("dave", hard_limit=100, soft_limit=50, overage_policy="warn")
        self.mgr.record("dave","gpt4",input_tokens=60,output_tokens=0)
        result = self.mgr.check("dave",tokens_needed=10)
        self.assertTrue(result.allowed); self.assertTrue(result.warning)

    def test_record_usage(self):
        self.mgr.set_quota("eve", hard_limit=10000)
        event = self.mgr.record("eve","claude-sonnet-4-6",input_tokens=100,output_tokens=200)
        self.assertEqual(event.total_tokens, 300)

    def test_usage_counted(self):
        self.mgr.set_quota("frank", hard_limit=10000)
        self.mgr.record("frank","gpt4",input_tokens=500,output_tokens=500)
        result = self.mgr.check("frank",tokens_needed=0)
        self.assertGreaterEqual(result.used, 1000)

    def test_report_returns_data(self):
        self.mgr.record("grace","gpt4",input_tokens=100,output_tokens=50)
        report = self.mgr.report()
        self.assertIn("total_tokens",report); self.assertIn("by_actor",report)

    def test_report_by_actor(self):
        self.mgr.record("heidi","gpt4",input_tokens=200,output_tokens=100)
        report = self.mgr.report(actor="heidi")
        self.assertIn("heidi",report["by_actor"])

    def test_cost_calculated(self):
        self.mgr.set_quota("ivan",hard_limit=10000,cost_per_token=0.00001)
        event = self.mgr.record("ivan","gpt4",input_tokens=100,output_tokens=100,
                                  quota_id=f"q_ivan_daily_all")
        self.assertGreaterEqual(event.cost_usd,0)

    def test_window_types(self):
        for window in ["hourly","daily","monthly","total"]:
            q = self.mgr.set_quota(f"user_{window}",hard_limit=1000,window=window)
            self.assertEqual(q.window.value,window)

    def test_disable_quota(self):
        q = self.mgr.set_quota("judy",hard_limit=100,overage_policy="block")
        self.mgr.record("judy","gpt4",input_tokens=90,output_tokens=20)
        self.mgr.disable_quota(q.id)
        result = self.mgr.check("judy",tokens_needed=200)
        self.assertTrue(result.allowed)

    def test_get_quotas(self):
        self.mgr.set_quota("kate",hard_limit=5000)
        quotas = self.mgr.get_quotas(actor="kate")
        self.assertGreater(len(quotas),0)

    def test_model_filter_applies(self):
        self.mgr.set_quota("leo",hard_limit=100,model_filter="gpt4",overage_policy="block")
        self.mgr.record("leo","gpt4",input_tokens=90,output_tokens=20)
        # Check for gpt4 — should hit limit
        result_gpt4 = self.mgr.check("leo",model="gpt4",tokens_needed=50)
        # Check for other model — quota doesn't apply
        result_claude = self.mgr.check("leo",model="claude",tokens_needed=50)
        self.assertFalse(result_gpt4.allowed); self.assertTrue(result_claude.allowed)

    def test_alert_callback(self):
        alerts = []
        def alert_cb(result): alerts.append(result.actor)
        self.mgr.on_alert(alert_cb)
        self.assertIsNotNone(self.mgr._alert_callbacks)

    def test_stats(self):
        self.mgr.record("mia","gpt4",input_tokens=10,output_tokens=10)
        s = self.mgr.stats()
        for k in ["total_quotas","total_usage_events","total_tokens_all_time"]:
            self.assertIn(k,s)

    def test_check_result_to_dict(self):
        self.mgr.set_quota("nia",hard_limit=1000)
        result = self.mgr.check("nia",tokens_needed=50)
        d = result.to_dict()
        for k in ["allowed","actor","used","limit","remaining"]: self.assertIn(k,d)

    def test_persistence(self):
        from agent.token_budget_manager import TokenBudgetManager
        td = tempfile.mkdtemp(); db = os.path.join(td,"tb.db")
        m1 = TokenBudgetManager(db_path=db)
        m1.set_quota("persist_user",hard_limit=5000)
        m1.record("persist_user","gpt4",input_tokens=100,output_tokens=100)
        m2 = TokenBudgetManager(db_path=db)
        quotas = m2.get_quotas(actor="persist_user")
        self.assertGreater(len(quotas),0)

if __name__=="__main__":
    loader=unittest.TestLoader()
    suite=loader.loadTestsFromModule(__import__(__name__))
    runner=unittest.TextTestRunner(verbosity=2)
    result=runner.run(suite)
    total=result.testsRun; failed=len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v17: {total-failed}/{total} passed")
    if failed:
        for t,tb in result.failures+result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
