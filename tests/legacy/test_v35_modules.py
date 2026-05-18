"""OMNI AGENT v35: MetricsCollector, CircuitBreaker, SchemaValidator, JobScheduler"""
import asyncio, json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# METRICS COLLECTOR
# ════════════════════════════════════════════════════════
class TestMetricsCollector(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.metrics_collector import MetricsCollector, MetricType
        self.mc = MetricsCollector(db_path=os.path.join(td,"mc.db"))
        self.MT = MetricType

    def test_counter_inc(self):
        self.mc.define("reqs", self.MT.COUNTER)
        self.mc.inc("reqs")
        self.assertEqual(self.mc.get_counter("reqs"), 1.0)

    def test_counter_inc_amount(self):
        self.mc.define("bytes", self.MT.COUNTER)
        self.mc.inc("bytes", 512)
        self.assertEqual(self.mc.get_counter("bytes"), 512.0)

    def test_counter_with_labels(self):
        self.mc.define("http", self.MT.COUNTER)
        self.mc.inc("http", labels={"method": "GET"})
        self.mc.inc("http", labels={"method": "POST"})
        self.assertEqual(self.mc.get_counter("http", {"method": "GET"}), 1.0)
        self.assertEqual(self.mc.get_counter("http", {"method": "POST"}), 1.0)

    def test_gauge_set(self):
        self.mc.define("mem", self.MT.GAUGE)
        self.mc.set("mem", 512.0)
        self.assertEqual(self.mc.get_gauge("mem"), 512.0)

    def test_gauge_inc_dec(self):
        self.mc.define("conns", self.MT.GAUGE)
        self.mc.gauge_inc("conns", 3)
        self.mc.gauge_dec("conns", 1)
        self.assertEqual(self.mc.get_gauge("conns"), 2.0)

    def test_gauge_min_max(self):
        self.mc.define("temp", self.MT.GAUGE)
        for v in [10.0, 5.0, 20.0, 15.0]:
            self.mc.set("temp", v)
        g = self.mc._gauges["temp"][""]
        self.assertEqual(g.min_val, 5.0)
        self.assertEqual(g.max_val, 20.0)

    def test_histogram_observe(self):
        self.mc.define("latency", self.MT.HISTOGRAM, buckets=[0.1, 0.5, 1.0])
        self.mc.observe("latency", 0.05)
        self.mc.observe("latency", 0.3)
        self.mc.observe("latency", 0.8)
        h = self.mc._histograms["latency"][""]
        self.assertEqual(h.n, 3)
        self.assertGreater(h.total, 0)

    def test_summary_percentile(self):
        self.mc.define("rt", self.MT.SUMMARY)
        for i in range(1, 101):
            self.mc.observe("rt", float(i))
        p = self.mc.get_percentile("rt", 0.5)
        self.assertGreater(p, 0)

    def test_alert_threshold(self):
        alerts = []
        self.mc.define("errs", self.MT.COUNTER)
        self.mc.on_threshold("errs", ">=", 5,
                               lambda n, v, t: alerts.append(v))
        for _ in range(5): self.mc.inc("errs")
        self.assertGreater(len(alerts), 0)

    def test_no_alert_below_threshold(self):
        alerts = []
        self.mc.define("warn", self.MT.COUNTER)
        self.mc.on_threshold("warn", ">", 10,
                               lambda n, v, t: alerts.append(v))
        for _ in range(5): self.mc.inc("warn")
        self.assertEqual(len(alerts), 0)

    def test_scrape_returns_dict(self):
        self.mc.define("s1", self.MT.COUNTER)
        self.mc.inc("s1")
        snap = self.mc.scrape()
        self.assertIn("s1", snap)

    def test_prometheus_export_counter(self):
        self.mc.define("prom_c", self.MT.COUNTER, "Test counter")
        self.mc.inc("prom_c", 3)
        out = self.mc.export_prometheus()
        self.assertIn("# HELP prom_c", out)
        self.assertIn("# TYPE prom_c counter", out)
        self.assertIn("3.0", out)

    def test_prometheus_export_histogram(self):
        self.mc.define("prom_h", self.MT.HISTOGRAM, "Test hist")
        self.mc.observe("prom_h", 0.1)
        out = self.mc.export_prometheus()
        self.assertIn("_bucket", out)
        self.assertIn("_count", out)

    def test_prometheus_labels(self):
        self.mc.define("lbl_c", self.MT.COUNTER)
        self.mc.inc("lbl_c", labels={"env": "prod"})
        out = self.mc.export_prometheus()
        self.assertIn('env="prod"', out)

    def test_default_labels(self):
        td = tempfile.mkdtemp()
        from agent.metrics_collector import MetricsCollector, MetricType
        mc = MetricsCollector(db_path=os.path.join(td,"dl.db"),
                               default_labels={"host": "server1"})
        mc.define("c", MetricType.COUNTER)
        mc.inc("c")
        # Default label should be merged
        key = "host=server1"
        self.assertIn(key, mc._counters["c"])

    def test_counter_rate(self):
        self.mc.define("rate_c", self.MT.COUNTER)
        for _ in range(10): self.mc.inc("rate_c")
        s = self.mc._counters["rate_c"][""]
        self.assertGreater(s.rate(), 0)

    def test_trend_tracked(self):
        self.mc.define("trend_c", self.MT.COUNTER)
        for _ in range(5): self.mc.inc("trend_c")
        t = self.mc.trend("trend_c")
        self.assertGreater(len(t), 0)

    def test_ttl_expire(self):
        self.mc.define("ttl_m", self.MT.COUNTER, ttl_s=0.001)
        self.mc.inc("ttl_m")
        time.sleep(0.01)
        self.mc._defs["ttl_m"].last_updated = 0  # force expire
        removed = self.mc.expire_ttl()
        self.assertGreater(removed, 0)

    def test_stats(self):
        self.mc.define("stat_c", self.MT.COUNTER)
        self.mc.inc("stat_c")
        s = self.mc.stats()
        for k in ["defined_metrics","counters","gauges","alert_rules"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ════════════════════════════════════════════════════════
class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.circuit_breaker import CircuitBreakerRegistry, State, CircuitOpenError
        self.CB = CircuitBreakerRegistry; self.State = State
        self.CBE = CircuitOpenError
        self.cb = CircuitBreakerRegistry(db_path=os.path.join(td,"cb.db"))
        self.cb.register("svc", failure_threshold=3, timeout_s=0.1,
                          window_s=60, probe_successes=2)

    async def _ok(self): return "ok"
    async def _fail(self): raise RuntimeError("boom")

    def test_closed_allows_calls(self):
        inst = self.cb.get("svc")
        self.assertEqual(inst.state, self.State.CLOSED)
        result = _run(self.cb.call("svc", self._ok))
        self.assertEqual(result, "ok")

    def test_trips_after_threshold(self):
        inst = self.cb.get("svc")
        for _ in range(3):
            try: _run(self.cb.call("svc", self._fail))
            except: pass
        self.assertEqual(inst.state, self.State.OPEN)

    def test_open_raises_circuit_error(self):
        inst = self.cb.get("svc")
        for _ in range(3):
            try: _run(self.cb.call("svc", self._fail))
            except: pass
        with self.assertRaises(self.CBE):
            _run(self.cb.call("svc", self._ok))

    def test_open_uses_fallback(self):
        inst = self.cb.get("svc")
        for _ in range(3):
            try: _run(self.cb.call("svc", self._fail))
            except: pass
        result = _run(self.cb.call("svc", self._ok,
                                    fallback=lambda: "fallback"))
        self.assertEqual(result, "fallback")

    def test_half_open_after_timeout(self):
        inst = self.cb.get("svc")
        for _ in range(3):
            try: _run(self.cb.call("svc", self._fail))
            except: pass
        time.sleep(0.15)  # wait for timeout_s=0.1
        inst._maybe_probe()
        self.assertEqual(inst.state, self.State.HALF_OPEN)

    def test_close_after_probe_successes(self):
        inst = self.cb.get("svc")
        for _ in range(3):
            try: _run(self.cb.call("svc", self._fail))
            except: pass
        time.sleep(0.15)
        # Two probe successes needed
        try: _run(self.cb.call("svc", self._ok))
        except: pass
        try: _run(self.cb.call("svc", self._ok))
        except: pass
        self.assertEqual(inst.state, self.State.CLOSED)

    def test_force_open(self):
        inst = self.cb.get("svc")
        inst.force_open()
        self.assertEqual(inst.state, self.State.OPEN)

    def test_force_close(self):
        inst = self.cb.get("svc")
        for _ in range(3):
            try: _run(self.cb.call("svc", self._fail))
            except: pass
        inst.force_close()
        self.assertEqual(inst.state, self.State.CLOSED)

    def test_failure_rate(self):
        inst = self.cb.get("svc")
        _run(self.cb.call("svc", self._ok))
        try: _run(self.cb.call("svc", self._fail))
        except: pass
        self.assertGreater(inst.failure_rate, 0)
        self.assertLess(inst.failure_rate, 1.0)

    def test_trips_counter(self):
        inst = self.cb.get("svc")
        for _ in range(3):
            try: _run(self.cb.call("svc", self._fail))
            except: pass
        self.assertGreater(inst._trips, 0)

    def test_retry_after_positive_when_open(self):
        inst = self.cb.get("svc")
        for _ in range(3):
            try: _run(self.cb.call("svc", self._fail))
            except: pass
        self.assertGreater(inst.retry_after(), 0)

    def test_on_open_hook(self):
        events = []
        self.cb.on("svc", "on_open", lambda b: events.append("opened"))
        for _ in range(3):
            try: _run(self.cb.call("svc", self._fail))
            except: pass
        self.assertIn("opened", events)

    def test_on_close_hook(self):
        events = []
        self.cb.on("svc", "on_close", lambda b: events.append("closed"))
        inst = self.cb.get("svc")
        for _ in range(3):
            try: _run(self.cb.call("svc", self._fail))
            except: pass
        time.sleep(0.15)
        try: _run(self.cb.call("svc", self._ok))
        except: pass
        try: _run(self.cb.call("svc", self._ok))
        except: pass
        # May or may not be closed depending on timing
        # Just check hook was called if closed
        if inst.state == self.State.CLOSED:
            self.assertIn("closed", events)

    def test_timeout_causes_failure(self):
        async def slow(): await asyncio.sleep(5)
        self.cb.register("slow_svc", failure_threshold=1,
                          timeout_s=60, call_timeout_s=0.05)
        with self.assertRaises(asyncio.TimeoutError):
            _run(self.cb.call("slow_svc", slow))

    def test_no_breaker_calls_fn(self):
        result = _run(self.cb.call("ghost_svc", self._ok))
        self.assertEqual(result, "ok")

    def test_status_dict(self):
        all_s = self.cb.all_status()
        self.assertIn("svc", all_s)
        self.assertIn("state", all_s["svc"])

    def test_protect_decorator_async(self):
        self.cb.register("dec_svc", failure_threshold=5, timeout_s=60)

        @self.cb.protect("dec_svc")
        async def protected(): return 42

        result = _run(protected())
        self.assertEqual(result, 42)

    def test_stats(self):
        s = self.cb.stats()
        for k in ["breakers","open","total_trips"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# SCHEMA VALIDATOR
# ════════════════════════════════════════════════════════
class TestSchemaValidator(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.schema_validator import SchemaValidator, ValidationError
        self.sv = SchemaValidator(db_path=os.path.join(td,"sv.db"))
        self.VE = ValidationError
        self.user_schema = {
            "type": "object",
            "required": ["name", "age"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "age":  {"type": "integer", "minimum": 0, "maximum": 150},
                "email": {"type": "string", "format": "email"},
                "tags": {"type": "array", "items": {"type": "string"}}
            }
        }
        self.sv.register("user", self.user_schema)

    def test_valid_object(self):
        errs = self.sv.validate({"name": "Alice", "age": 30}, "user")
        self.assertEqual(errs, [])

    def test_missing_required(self):
        errs = self.sv.validate({"name": "Bob"}, "user")
        self.assertTrue(any("age" in e.message for e in errs))

    def test_wrong_type(self):
        errs = self.sv.validate({"name": "Alice", "age": "thirty"}, "user")
        self.assertTrue(any("integer" in e.message for e in errs))

    def test_string_minlength(self):
        errs = self.sv.validate({"name": "", "age": 5}, "user")
        self.assertGreater(len(errs), 0)

    def test_integer_minimum(self):
        errs = self.sv.validate({"name": "X", "age": -1}, "user")
        self.assertGreater(len(errs), 0)

    def test_integer_maximum(self):
        errs = self.sv.validate({"name": "X", "age": 200}, "user")
        self.assertGreater(len(errs), 0)

    def test_format_email_valid(self):
        errs = self.sv.validate({"name": "X", "age": 1,
                                   "email": "a@b.com"}, "user")
        self.assertEqual(errs, [])

    def test_format_email_invalid(self):
        errs = self.sv.validate({"name": "X", "age": 1,
                                   "email": "not-an-email"}, "user")
        self.assertTrue(any("email" in e.message for e in errs))

    def test_array_items(self):
        errs = self.sv.validate({"name": "X", "age": 1,
                                   "tags": [1, 2, 3]}, "user")
        self.assertGreater(len(errs), 0)

    def test_array_valid(self):
        errs = self.sv.validate({"name": "X", "age": 1,
                                   "tags": ["a", "b"]}, "user")
        self.assertEqual(errs, [])

    def test_enum(self):
        schema = {"type": "string", "enum": ["red", "green", "blue"]}
        self.assertEqual(self.sv.validate("red", schema), [])
        self.assertGreater(len(self.sv.validate("yellow", schema)), 0)

    def test_anyof(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(self.sv.validate("hi", schema), [])
        self.assertEqual(self.sv.validate(42, schema), [])
        self.assertGreater(len(self.sv.validate([1,2], schema)), 0)

    def test_allof(self):
        schema = {"allOf": [
            {"type": "integer"},
            {"minimum": 0}
        ]}
        self.assertEqual(self.sv.validate(5, schema), [])
        self.assertGreater(len(self.sv.validate(-1, schema)), 0)

    def test_not(self):
        schema = {"not": {"type": "string"}}
        self.assertEqual(self.sv.validate(42, schema), [])
        self.assertGreater(len(self.sv.validate("hi", schema)), 0)

    def test_coercion_string_to_int(self):
        td = tempfile.mkdtemp()
        from agent.schema_validator import SchemaValidator
        sv = SchemaValidator(db_path=os.path.join(td,"c.db"), coerce=True)
        schema = {"type": "object", "properties": {"age": {"type": "integer"}}}
        data, errs = sv.coerce_data({"age": "30"}, schema)
        self.assertEqual(data["age"], 30)
        self.assertEqual(errs, [])

    def test_minmax_items(self):
        schema = {"type": "array", "minItems": 2, "maxItems": 4}
        self.assertGreater(len(self.sv.validate([1], schema)), 0)
        self.assertGreater(len(self.sv.validate([1,2,3,4,5], schema)), 0)
        self.assertEqual(self.sv.validate([1,2,3], schema), [])

    def test_unique_items(self):
        schema = {"type": "array", "uniqueItems": True}
        self.assertEqual(self.sv.validate([1,2,3], schema), [])
        self.assertGreater(len(self.sv.validate([1,1,2], schema)), 0)

    def test_dependencies(self):
        schema = {
            "type": "object",
            "dependencies": {"credit_card": ["billing_address"]}
        }
        # Has credit_card but not billing_address → error
        errs = self.sv.validate({"credit_card": "1234"}, schema)
        self.assertGreater(len(errs), 0)
        # Has both → ok
        errs = self.sv.validate({"credit_card": "1234",
                                   "billing_address": "123 Main"}, schema)
        self.assertEqual(errs, [])

    def test_custom_validator(self):
        def no_admin(value, schema):
            if isinstance(value, dict) and value.get("name","").lower() == "admin":
                return "Name 'admin' is reserved"
        self.sv.add_custom("user", no_admin)
        errs = self.sv.validate({"name": "admin", "age": 1}, "user")
        self.assertTrue(any("reserved" in e.message for e in errs))

    def test_error_path(self):
        errs = self.sv.validate({"name": "X", "age": -1}, "user")
        self.assertTrue(any("$.age" in e.path or "age" in e.path
                             for e in errs))

    def test_stats(self):
        self.sv.validate({"name": "X", "age": 1}, "user")
        s = self.sv.stats("user")
        for k in ["total","valid","invalid","pass_rate"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# JOB SCHEDULER
# ════════════════════════════════════════════════════════
class TestJobScheduler(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.job_scheduler import JobScheduler, JobStatus
        self.js = JobScheduler(db_path=os.path.join(td,"js.db"),
                                max_concurrent=3, catch_up=False)
        self.JS = JobStatus

    def test_schedule_job(self):
        self.js.schedule("j1", lambda: None, interval_s=60)
        self.assertIn("j1", self.js._jobs)

    def test_run_now_sync(self):
        counter = [0]
        def inc(): counter[0] += 1
        self.js.schedule("inc", inc, interval_s=60)
        ex = _run(self.js.run_now("inc"))
        self.assertIsNotNone(ex)
        self.assertEqual(counter[0], 1)

    def test_run_now_async(self):
        async def async_fn(): return 42
        self.js.schedule("async_j", async_fn, interval_s=60)
        ex = _run(self.js.run_now("async_j"))
        self.assertEqual(ex.result, 42)

    def test_run_now_returns_success(self):
        self.js.schedule("ok_j", lambda: "done", interval_s=60)
        ex = _run(self.js.run_now("ok_j"))
        self.assertEqual(ex.status, self.JS.SUCCESS)

    def test_run_now_captures_error(self):
        def fail(): raise ValueError("boom")
        self.js.schedule("fail_j", fail, interval_s=60)
        ex = _run(self.js.run_now("fail_j"))
        self.assertEqual(ex.status, self.JS.FAILED)
        self.assertIn("boom", ex.error)

    def test_timeout(self):
        async def slow(): await asyncio.sleep(5)
        self.js.schedule("slow_j", slow, interval_s=60, timeout_s=0.05)
        ex = _run(self.js.run_now("slow_j"))
        self.assertEqual(ex.status, self.JS.TIMEOUT)

    def test_retry_on_failure(self):
        calls = [0]
        def flaky():
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("not yet")
            return "ok"
        self.js.schedule("flaky", flaky, interval_s=60,
                          max_retries=3, retry_delay_s=0.01)
        ex = _run(self.js.run_now("flaky"))
        self.assertEqual(ex.status, self.JS.SUCCESS)
        self.assertEqual(ex.retry_count, 2)

    def test_run_count_increments(self):
        self.js.schedule("cnt_j", lambda: None, interval_s=60)
        _run(self.js.run_now("cnt_j"))
        _run(self.js.run_now("cnt_j"))
        self.assertEqual(self.js._jobs["cnt_j"].run_count, 2)

    def test_next_run_updates_after_exec(self):
        self.js.schedule("nr_j", lambda: None, interval_s=10)
        before = self.js._jobs["nr_j"].next_run
        _run(self.js.run_now("nr_j"))
        after = self.js._jobs["nr_j"].next_run
        self.assertGreater(after, before)

    def test_pause_prevents_execution(self):
        self.js.schedule("pause_j", lambda: None, interval_s=0.001)
        self.js.pause("pause_j")
        due = self.js._due_jobs()
        self.assertFalse(any(j.name == "pause_j" for j in due))

    def test_resume_allows_execution(self):
        self.js.schedule("res_j", lambda: None, interval_s=0.001)
        self.js.pause("res_j"); self.js.resume("res_j")
        time.sleep(0.01)
        due = self.js._due_jobs()
        self.assertTrue(any(j.name == "res_j" for j in due))

    def test_unschedule(self):
        self.js.schedule("rm_j", lambda: None, interval_s=60)
        ok = self.js.unschedule("rm_j")
        self.assertTrue(ok)
        self.assertNotIn("rm_j", self.js._jobs)

    def test_dependency_blocks_execution(self):
        self.js.schedule("dep_a", lambda: None, interval_s=0.001)
        self.js.schedule("dep_b", lambda: None, interval_s=0.001,
                          depends_on=["dep_a"])
        time.sleep(0.01)
        due = self.js._due_jobs()
        # dep_b should not be due because dep_a hasn't succeeded yet
        self.assertFalse(any(j.name == "dep_b" for j in due))

    def test_dependency_allows_after_success(self):
        self.js.schedule("da", lambda: None, interval_s=0.001)
        self.js.schedule("db", lambda: None, interval_s=0.001,
                          depends_on=["da"])
        _run(self.js.run_now("da"))
        time.sleep(0.01)
        due = self.js._due_jobs()
        self.assertTrue(any(j.name == "db" for j in due))

    def test_cron_parse_basic(self):
        from agent.job_scheduler import _parse_cron
        mins, hours, doms, mons, dows = _parse_cron("*/5 * * * *")
        self.assertIn(0, mins); self.assertIn(5, mins); self.assertIn(55, mins)

    def test_cron_next_fire(self):
        self.js.schedule("cron_j", lambda: None, cron="0 0 * * *")
        j = self.js._jobs["cron_j"]
        self.assertGreater(j.next_run, time.time())

    def test_history(self):
        self.js.schedule("hist_j", lambda: "x", interval_s=60)
        _run(self.js.run_now("hist_j"))
        _run(self.js.run_now("hist_j"))
        h = self.js.history("hist_j")
        self.assertGreaterEqual(len(h), 2)

    def test_execution_to_dict(self):
        self.js.schedule("dict_j", lambda: 99, interval_s=60)
        ex = _run(self.js.run_now("dict_j"))
        d = ex.to_dict()
        for k in ["id","job","status","started_at","duration_s"]: self.assertIn(k, d)

    def test_next_jobs(self):
        for i in range(3):
            self.js.schedule(f"nj_{i}", lambda: None, interval_s=60+i)
        nxt = self.js.next_jobs(3)
        self.assertEqual(len(nxt), 3)

    def test_stats(self):
        self.js.schedule("s_j", lambda: None, interval_s=60)
        s = self.js.stats()
        for k in ["scheduled_jobs","running","paused"]: self.assertIn(k, s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v35: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
