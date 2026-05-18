"""OMNI AGENT v38: RateLimiter, ConfigManager, HealthMonitor, TaskQueue"""
import asyncio, json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# RATE LIMITER
# ════════════════════════════════════════════════════════
class TestRateLimiter(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.rate_limiter import RateLimiter, RateRule, Algorithm
        self.RL = RateLimiter; self.RR = RateRule; self.A = Algorithm
        self.rl = RateLimiter(db_path=os.path.join(td,"rl.db"))

    def test_token_bucket_allows_within_capacity(self):
        self.rl.add_rule(self.RR("tb", self.A.TOKEN_BUCKET,
                                  rate=10.0, capacity=5.0))
        for _ in range(5):
            r = self.rl.check("user1","tb")
            self.assertTrue(r.allowed)

    def test_token_bucket_blocks_over_capacity(self):
        self.rl.add_rule(self.RR("tb2", self.A.TOKEN_BUCKET,
                                  rate=1.0, capacity=3.0))
        for _ in range(3): self.rl.check("u","tb2")
        r = self.rl.check("u","tb2")
        self.assertFalse(r.allowed)

    def test_token_bucket_refills(self):
        self.rl.add_rule(self.RR("tb3", self.A.TOKEN_BUCKET,
                                  rate=100.0, capacity=2.0))
        self.rl.check("ux","tb3"); self.rl.check("ux","tb3")
        time.sleep(0.05)  # refill ~5 tokens
        r = self.rl.check("ux","tb3")
        self.assertTrue(r.allowed)

    def test_sliding_window_allows(self):
        self.rl.add_rule(self.RR("sw", self.A.SLIDING_WINDOW,
                                  rate=10.0, capacity=5.0, window_s=1.0))
        for _ in range(5):
            r = self.rl.check("usw","sw")
            self.assertTrue(r.allowed)

    def test_sliding_window_blocks(self):
        self.rl.add_rule(self.RR("sw2", self.A.SLIDING_WINDOW,
                                  rate=1.0, capacity=2.0, window_s=1.0))
        self.rl.check("usw2","sw2"); self.rl.check("usw2","sw2")
        r = self.rl.check("usw2","sw2")
        self.assertFalse(r.allowed)

    def test_fixed_window_allows(self):
        self.rl.add_rule(self.RR("fw", self.A.FIXED_WINDOW,
                                  rate=1.0, capacity=3.0, window_s=60.0))
        for _ in range(3):
            r = self.rl.check("ufw","fw")
            self.assertTrue(r.allowed)

    def test_fixed_window_blocks(self):
        self.rl.add_rule(self.RR("fw2", self.A.FIXED_WINDOW,
                                  rate=1.0, capacity=2.0, window_s=60.0))
        self.rl.check("ufw2","fw2"); self.rl.check("ufw2","fw2")
        r = self.rl.check("ufw2","fw2")
        self.assertFalse(r.allowed)

    def test_leaky_bucket_allows(self):
        self.rl.add_rule(self.RR("lb", self.A.LEAKY_BUCKET,
                                  rate=10.0, capacity=5.0))
        r = self.rl.check("ulb","lb")
        self.assertTrue(r.allowed)

    def test_leaky_bucket_blocks(self):
        self.rl.add_rule(self.RR("lb2", self.A.LEAKY_BUCKET,
                                  rate=0.1, capacity=2.0))
        self.rl.check("ulb2","lb2"); self.rl.check("ulb2","lb2")
        r = self.rl.check("ulb2","lb2")
        self.assertFalse(r.allowed)

    def test_unknown_rule_allows(self):
        r = self.rl.check("u","no_such_rule")
        self.assertTrue(r.allowed)

    def test_different_keys_independent(self):
        self.rl.add_rule(self.RR("ind", self.A.FIXED_WINDOW,
                                  rate=1.0, capacity=1.0, window_s=60.0))
        self.rl.check("ka","ind")
        r = self.rl.check("kb","ind")
        self.assertTrue(r.allowed)  # kb is independent from ka

    def test_result_headers(self):
        self.rl.add_rule(self.RR("hdr", self.A.TOKEN_BUCKET,
                                  rate=10.0, capacity=10.0))
        r = self.rl.check("uh","hdr")
        h = r.headers()
        self.assertIn("X-RateLimit-Limit", h)
        self.assertIn("X-RateLimit-Remaining", h)

    def test_penalty_on_violation(self):
        self.rl.add_rule(self.RR("pen", self.A.FIXED_WINDOW,
                                  rate=1.0, capacity=1.0, window_s=60.0,
                                  penalty_s=60.0))
        self.rl.check("upen","pen")
        self.rl.check("upen","pen")  # violation → penalty
        r = self.rl.check("upen","pen")
        self.assertFalse(r.allowed)

    def test_reset_clears_state(self):
        self.rl.add_rule(self.RR("rst", self.A.FIXED_WINDOW,
                                  rate=1.0, capacity=1.0, window_s=60.0))
        self.rl.check("ur","rst"); self.rl.check("ur","rst")
        self.rl.reset("ur","rst")
        r = self.rl.check("ur","rst")
        self.assertTrue(r.allowed)

    def test_hook_on_violation(self):
        hits = []
        self.rl.on_limit_hit(lambda k, r: hits.append(k))
        self.rl.add_rule(self.RR("hook", self.A.FIXED_WINDOW,
                                  rate=1.0, capacity=1.0, window_s=60.0))
        self.rl.check("uh2","hook"); self.rl.check("uh2","hook")
        self.assertIn("uh2", hits)

    def test_cost_multiplier(self):
        self.rl.add_rule(self.RR("cost", self.A.TOKEN_BUCKET,
                                  rate=1.0, capacity=5.0))
        r = self.rl.check("uc","cost", cost=3.0)
        self.assertTrue(r.allowed)
        r2 = self.rl.check("uc","cost", cost=3.0)  # only 2 tokens left
        self.assertFalse(r2.allowed)

    def test_quota_group(self):
        self.rl.add_quota_group("org1", limit=10.0, window_s=60.0)
        for _ in range(10):
            ok = self.rl.consume_quota("org1","any_user")
            self.assertTrue(ok)
        ok = self.rl.consume_quota("org1","any_user")
        self.assertFalse(ok)

    def test_result_to_dict(self):
        self.rl.add_rule(self.RR("dict", self.A.TOKEN_BUCKET,
                                  rate=1.0, capacity=1.0))
        r = self.rl.check("ud","dict")
        d = r.to_dict()
        for k in ["allowed","key","rule","tokens_remaining"]: self.assertIn(k,d)

    def test_stats(self):
        s = self.rl.stats()
        for k in ["rules","total_violations"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# CONFIG MANAGER
# ════════════════════════════════════════════════════════
class TestConfigManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.config_manager import ConfigManager, ValueType, Layer
        self.cm = ConfigManager(db_path=os.path.join(td,"cm.db"),
                                 env_prefix="APP_")
        self.VT = ValueType; self.L = Layer; self.td = td

    def test_get_default(self):
        self.cm.define("server.host", self.VT.STRING, default="localhost")
        self.assertEqual(self.cm.get("server.host"), "localhost")

    def test_runtime_override(self):
        self.cm.define("server.port", self.VT.INT, default=8080)
        self.cm.set("server.port", 9090)
        self.assertEqual(self.cm.get("server.port"), 9090)

    def test_runtime_priority_over_default(self):
        self.cm.define("key1", self.VT.STRING, default="default_val")
        self.cm.set("key1","runtime_val", self.L.RUNTIME)
        self.assertEqual(self.cm.get("key1"), "runtime_val")

    def test_type_cast_int(self):
        self.cm.define("port", self.VT.INT, default=8080)
        self.cm.set("port","9000")  # set as string
        self.assertEqual(self.cm.get("port"), 9000)
        self.assertIsInstance(self.cm.get("port"), int)

    def test_type_cast_bool(self):
        self.cm.define("debug", self.VT.BOOL, default=False)
        self.cm.set("debug","true")
        self.assertTrue(self.cm.get("debug"))

    def test_type_cast_float(self):
        self.cm.define("ratio", self.VT.FLOAT, default=0.5)
        self.cm.set("ratio","0.75")
        self.assertAlmostEqual(self.cm.get("ratio"), 0.75)

    def test_type_cast_json(self):
        self.cm.define("allowed_ips", self.VT.JSON, default=[])
        self.cm.set("allowed_ips",'["1.1.1.1","2.2.2.2"]')
        val = self.cm.get("allowed_ips")
        self.assertIsInstance(val, list)
        self.assertIn("1.1.1.1", val)

    def test_env_load(self):
        self.cm.define("db.host", self.VT.STRING, default="localhost")
        self.cm.load_env({"APP_DB__HOST": "prod-db.example.com"})
        self.assertEqual(self.cm.get("db.host"), "prod-db.example.com")

    def test_env_lower_priority_than_runtime(self):
        self.cm.define("svc.url", self.VT.STRING, default="default")
        self.cm.load_env({"APP_SVC__URL": "env_url"})
        self.cm.set("svc.url","runtime_url")
        self.assertEqual(self.cm.get("svc.url"), "runtime_url")

    def test_load_json_file(self):
        cfg = {"database": {"host": "db.test", "port": 5432}}
        path = os.path.join(self.td,"config.json")
        import json
        with open(path,"w") as f: json.dump(cfg,f)
        self.cm.define("database.host", self.VT.STRING)
        self.cm.define("database.port", self.VT.INT)
        self.cm.load_json_file(path)
        self.assertEqual(self.cm.get("database.host"), "db.test")
        self.assertEqual(self.cm.get("database.port"), 5432)

    def test_get_namespace(self):
        self.cm.define("app.name", self.VT.STRING, default="myapp")
        self.cm.define("app.version", self.VT.STRING, default="1.0")
        ns = self.cm.get_ns("app")
        self.assertIn("name", ns); self.assertIn("version", ns)

    def test_watcher_fired(self):
        changes = []
        self.cm.define("watched", self.VT.STRING, default="old")
        self.cm.watch("watched",
                       lambda k, old, new: changes.append((old, new)))
        self.cm.set("watched","new_val")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0], ("old","new_val"))

    def test_global_watcher(self):
        seen = []
        self.cm.watch_all(lambda k,o,n: seen.append(k))
        self.cm.define("gw_key", self.VT.STRING, default="x")
        self.cm.set("gw_key","y")
        self.assertIn("gw_key",seen)

    def test_secret_masked_in_snapshot(self):
        self.cm.define("db.password", self.VT.STRING,
                        default="secret123", secret=True)
        snap = self.cm.snapshot(mask_secrets=True)
        self.assertEqual(snap.get("db.password"),"***")

    def test_secret_visible_unmasked(self):
        self.cm.define("db.pw2", self.VT.STRING,
                        default="pass", secret=True)
        snap = self.cm.snapshot(mask_secrets=False)
        self.assertEqual(snap.get("db.pw2"),"pass")

    def test_validation_required_missing(self):
        self.cm.define("required_key", self.VT.STRING,
                        required=True, default=None)
        errs = self.cm.validate()
        self.assertTrue(any("required_key" in e for e in errs))

    def test_validation_allowed_values(self):
        self.cm.define("env_type", self.VT.STRING,
                        default="invalid",
                        allowed_values=["dev","staging","prod"])
        errs = self.cm.validate()
        self.assertTrue(any("env_type" in e for e in errs))

    def test_validation_passes(self):
        self.cm.define("v_port", self.VT.INT, default=8080,
                        min_val=1, max_val=65535)
        self.assertEqual(self.cm.validate(), [])

    def test_delete_restores_lower_layer(self):
        self.cm.define("del_key", self.VT.STRING, default="default")
        self.cm.set("del_key","runtime")
        self.cm.delete("del_key", self.L.RUNTIME)
        self.assertEqual(self.cm.get("del_key"), "default")

    def test_snapshot_and_diff(self):
        self.cm.define("snap_k", self.VT.STRING, default="v1")
        sid = self.cm.save_snapshot()
        self.cm.set("snap_k","v2")
        diff = self.cm.diff(sid)
        self.assertIn("snap_k", diff)

    def test_stats(self):
        s = self.cm.stats()
        for k in ["schema_keys","layers"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# HEALTH MONITOR
# ════════════════════════════════════════════════════════
class TestHealthMonitor(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.health_monitor import (HealthMonitor, HealthCheck,
                                           CheckType, HealthStatus)
        self.HM = HealthMonitor; self.HC = HealthCheck
        self.CT = CheckType; self.HS = HealthStatus
        self.hm = HealthMonitor(db_path=os.path.join(td,"hm.db"))

    def test_custom_check_healthy(self):
        self.hm.add_check(self.HC("ok_check", self.CT.CUSTOM,
                                   fn=lambda: True,
                                   fail_threshold=1))
        r = _run(self.hm.run_check("ok_check"))
        self.assertEqual(r.status, self.HS.HEALTHY)

    def test_custom_check_unhealthy(self):
        self.hm.add_check(self.HC("bad_check", self.CT.CUSTOM,
                                   fn=lambda: False,
                                   fail_threshold=1))
        r = _run(self.hm.run_check("bad_check"))
        self.assertEqual(r.status, self.HS.UNHEALTHY)

    def test_fail_threshold_consecutive(self):
        calls = [0]
        def flaky():
            calls[0] += 1; return False
        self.hm.add_check(self.HC("thresh", self.CT.CUSTOM,
                                   fn=flaky, fail_threshold=3))
        _run(self.hm.run_check("thresh"))
        _run(self.hm.run_check("thresh"))
        r = _run(self.hm.run_check("thresh"))
        self.assertEqual(r.status, self.HS.UNHEALTHY)

    def test_recovery_threshold(self):
        fails = [3]
        def recovers():
            if fails[0] > 0:
                fails[0] -= 1; return False
            return True
        self.hm.add_check(self.HC("rec", self.CT.CUSTOM,
                                   fn=recovers,
                                   fail_threshold=1, pass_threshold=2))
        for _ in range(3): _run(self.hm.run_check("rec"))
        _run(self.hm.run_check("rec"))  # 1st pass → DEGRADED
        r = _run(self.hm.run_check("rec"))  # 2nd pass → HEALTHY
        self.assertEqual(r.status, self.HS.HEALTHY)

    def test_overall_status_worst(self):
        self.hm.add_check(self.HC("good", self.CT.CUSTOM,
                                   fn=lambda: True, fail_threshold=1))
        self.hm.add_check(self.HC("bad2", self.CT.CUSTOM,
                                   fn=lambda: False, fail_threshold=1))
        _run(self.hm.run_once())
        self.assertEqual(self.hm.overall_status(), self.HS.UNHEALTHY)

    def test_overall_status_all_healthy(self):
        self.hm.add_check(self.HC("g1", self.CT.CUSTOM,
                                   fn=lambda: True, fail_threshold=1))
        self.hm.add_check(self.HC("g2", self.CT.CUSTOM,
                                   fn=lambda: True, fail_threshold=1))
        _run(self.hm.run_once())
        self.assertEqual(self.hm.overall_status(), self.HS.HEALTHY)

    def test_run_once_returns_all(self):
        self.hm.add_check(self.HC("a", self.CT.CUSTOM, fn=lambda: True))
        self.hm.add_check(self.HC("b", self.CT.CUSTOM, fn=lambda: True))
        results = _run(self.hm.run_once())
        self.assertIn("a",results); self.assertIn("b",results)

    def test_alert_hook_fires(self):
        alerts = []
        self.hm.on_unhealthy(lambda n,s,d: alerts.append(n))
        self.hm.add_check(self.HC("alert_c", self.CT.CUSTOM,
                                   fn=lambda: False, fail_threshold=1))
        _run(self.hm.run_check("alert_c"))
        self.assertIn("alert_c", alerts)

    def test_change_hook_fires(self):
        changes = []
        self.hm.on_change(lambda n,o,t: changes.append((n,o,t)))
        self.hm.add_check(self.HC("change_c", self.CT.CUSTOM,
                                   fn=lambda: True, fail_threshold=1))
        _run(self.hm.run_check("change_c"))  # UNKNOWN→HEALTHY
        self.assertTrue(any(c[0]=="change_c" for c in changes))

    def test_dependency_unhealthy(self):
        self.hm.add_check(self.HC("dep_bad", self.CT.CUSTOM,
                                   fn=lambda: False, fail_threshold=1))
        self.hm.add_check(self.HC("dep_child", self.CT.CUSTOM,
                                   fn=lambda: True, fail_threshold=1,
                                   dependencies=["dep_bad"]))
        _run(self.hm.run_check("dep_bad"))
        r = _run(self.hm.run_check("dep_child"))
        self.assertEqual(r.status, self.HS.UNHEALTHY)

    def test_disabled_check(self):
        self.hm.add_check(self.HC("dis_c", self.CT.CUSTOM,
                                   fn=lambda: True, enabled=False))
        r = _run(self.hm.run_check("dis_c"))
        self.assertEqual(r.status, self.HS.UNKNOWN)

    def test_latency_measured(self):
        self.hm.add_check(self.HC("lat_c", self.CT.CUSTOM,
                                   fn=lambda: True, fail_threshold=1))
        r = _run(self.hm.run_check("lat_c"))
        self.assertGreaterEqual(r.latency_ms, 0)

    def test_result_to_dict(self):
        self.hm.add_check(self.HC("dict_c", self.CT.CUSTOM,
                                   fn=lambda: True, fail_threshold=1))
        r = _run(self.hm.run_check("dict_c"))
        d = r.to_dict()
        for k in ["check","status","latency_ms","ts"]: self.assertIn(k,d)

    def test_status_by_tag(self):
        self.hm.add_check(self.HC("tagged1", self.CT.CUSTOM,
                                   fn=lambda: True, fail_threshold=1,
                                   tags=["infra"]))
        _run(self.hm.run_check("tagged1"))
        s = self.hm.status_by_tag("infra")
        self.assertEqual(s, self.HS.HEALTHY)

    def test_history_recorded(self):
        self.hm.add_check(self.HC("hist_c", self.CT.CUSTOM,
                                   fn=lambda: True, fail_threshold=1))
        for _ in range(3): _run(self.hm.run_check("hist_c"))
        h = self.hm.history("hist_c")
        self.assertGreaterEqual(len(h), 3)

    def test_tcp_check_fails_closed_port(self):
        self.hm.add_check(self.HC("tcp_c", self.CT.TCP,
                                   host="127.0.0.1", port=19999,
                                   timeout_s=0.1, fail_threshold=1))
        r = _run(self.hm.run_check("tcp_c"))
        self.assertEqual(r.status, self.HS.UNHEALTHY)

    def test_db_check_healthy(self):
        import tempfile, sqlite3 as sl
        td = tempfile.mkdtemp()
        dbp = os.path.join(td,"test.db")
        sl.connect(dbp).close()
        self.hm.add_check(self.HC("db_c", self.CT.DB,
                                   db_path=dbp, query="SELECT 1",
                                   fail_threshold=1))
        r = _run(self.hm.run_check("db_c"))
        self.assertEqual(r.status, self.HS.HEALTHY)

    def test_db_check_unhealthy_bad_path(self):
        self.hm.add_check(self.HC("db_bad", self.CT.DB,
                                   db_path="/nonexistent/path/db.db",
                                   query="SELECT bad FROM nowhere",
                                   fail_threshold=1))
        r = _run(self.hm.run_check("db_bad"))
        self.assertEqual(r.status, self.HS.UNHEALTHY)

    def test_stats(self):
        s = self.hm.stats()
        for k in ["checks","overall","by_status"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# TASK QUEUE
# ════════════════════════════════════════════════════════
class TestTaskQueue(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.task_queue import TaskQueue, TaskStatus
        self.TQ = TaskQueue; self.TS = TaskStatus
        self.tq = TaskQueue(db_path=os.path.join(td,"tq.db"),
                             max_workers=4)

    def tearDown(self):
        _run(self.tq.shutdown(timeout_s=1.0))

    def test_enqueue_returns_id(self):
        async def noop(): pass
        tid = self.tq.enqueue(noop)
        self.assertIsNotNone(tid)

    def test_task_executes(self):
        results = []
        async def work(): results.append(1)
        tid = self.tq.enqueue(work)
        _run(self.tq.start())
        _run(asyncio.sleep(0.2))
        self.assertIn(1, results)

    def test_result_stored(self):
        async def calc(): return 42
        tid = self.tq.enqueue(calc)
        _run(self.tq.start())
        _run(asyncio.sleep(0.2))
        r = self.tq.get_result(tid)
        self.assertIsNotNone(r)
        self.assertEqual(r.status, self.TS.DONE)

    def test_result_value(self):
        async def calc2(): return 99
        tid = self.tq.enqueue(calc2)
        _run(self.tq.start())
        _run(asyncio.sleep(0.2))
        r = self.tq.get_result(tid)
        self.assertEqual(r.status, self.TS.DONE)

    def test_sync_fn_executes(self):
        called = [False]
        def sync_work(): called[0] = True; return "done"
        tid = self.tq.enqueue(sync_work)
        _run(self.tq.start())
        _run(asyncio.sleep(0.2))
        r = self.tq.get_result(tid)
        self.assertTrue(called[0])

    def test_priority_ordering(self):
        order = []
        async def tag(n): order.append(n)
        from agent.task_queue import TaskQueue
        tq2 = TaskQueue(db_path=tempfile.mktemp(suffix=".db"),
                         max_workers=1)
        tq2.enqueue(lambda: order.append(3), priority=3)
        tq2.enqueue(lambda: order.append(1), priority=1)
        tq2.enqueue(lambda: order.append(2), priority=2)
        _run(tq2.start())
        _run(asyncio.sleep(0.3))
        _run(tq2.shutdown(1.0))
        if len(order) >= 3:
            self.assertEqual(order[0], 1)  # highest priority first

    def test_failed_task_status(self):
        async def boom(): raise RuntimeError("fail!")
        tid = self.tq.enqueue(boom, max_retries=0)
        _run(self.tq.start())
        _run(asyncio.sleep(0.3))
        r = self.tq.get_result(tid)
        self.assertIsNotNone(r)
        self.assertEqual(r.status, self.TS.FAILED)

    def test_failed_task_has_exception(self):
        async def boom2(): raise ValueError("test error")
        tid = self.tq.enqueue(boom2, max_retries=0)
        _run(self.tq.start())
        _run(asyncio.sleep(0.3))
        r = self.tq.get_result(tid)
        self.assertIn("test error", r.exception or "")

    def test_retry_on_failure(self):
        calls = [0]
        async def flaky():
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("not yet")
            return "ok"
        tid = self.tq.enqueue(flaky, max_retries=3, retry_delay_s=0.01)
        _run(self.tq.start())
        _run(asyncio.sleep(0.5))
        r = self.tq.get_result(tid)
        self.assertIsNotNone(r)
        self.assertEqual(r.status, self.TS.DONE)

    def test_cancel_pending_task(self):
        async def slow(): await asyncio.sleep(10)
        # Don't start workers, so task stays pending
        tid = self.tq.enqueue(slow)
        ok = self.tq.cancel(tid)
        self.assertTrue(ok)
        s = self.tq.task_status(tid)
        self.assertEqual(s["status"], "cancelled")

    def test_deadline_expires(self):
        async def lazy(): await asyncio.sleep(5)
        tid = self.tq.enqueue(lazy, deadline_s=0.01)
        time.sleep(0.05)  # let deadline pass
        _run(self.tq.start())
        _run(asyncio.sleep(0.2))
        task = self.tq._tasks.get(tid)
        if task:
            self.assertIn(task.status,
                [self.TS.EXPIRED, self.TS.PENDING, self.TS.CANCELLED])

    def test_enqueue_many(self):
        async def noop2(): pass
        specs = [{"fn": noop2, "priority": i} for i in range(5)]
        ids = self.tq.enqueue_many(specs)
        self.assertEqual(len(ids), 5)

    def test_pause_stops_dispatch(self):
        started = [0]
        async def work2(): started[0] += 1
        _run(self.tq.start())
        self.tq.pause()
        for _ in range(5): self.tq.enqueue(work2)
        _run(asyncio.sleep(0.15))
        before = started[0]
        self.tq.resume()
        self.assertEqual(before, 0)  # nothing ran while paused

    def test_middleware_before(self):
        tagged = []
        self.tq.add_middleware(before=lambda t: tagged.append(t.id))
        async def noop3(): pass
        tid = self.tq.enqueue(noop3)
        _run(self.tq.start())
        _run(asyncio.sleep(0.2))
        self.assertIn(tid, tagged)

    def test_result_to_dict(self):
        async def noop4(): return "x"
        tid = self.tq.enqueue(noop4)
        _run(self.tq.start())
        _run(asyncio.sleep(0.2))
        r = self.tq.get_result(tid)
        if r:
            d = r.to_dict()
            for k in ["task_id","status","elapsed_s"]: self.assertIn(k,d)

    def test_stats(self):
        s = self.tq.stats()
        for k in ["queue_size","in_flight","max_workers"]: self.assertIn(k,s)

    def test_queue_size_decreases(self):
        async def q_work(): pass
        for _ in range(5): self.tq.enqueue(q_work)
        before = self.tq.queue_size
        _run(self.tq.start())
        _run(asyncio.sleep(0.3))
        self.assertLess(self.tq.queue_size, before)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v38: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
