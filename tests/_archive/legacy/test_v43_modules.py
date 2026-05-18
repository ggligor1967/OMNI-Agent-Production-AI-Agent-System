"""OMNI AGENT v43: ServiceMesh, DistributedLock, LogAggregator, DAGScheduler"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# SERVICE MESH
# ════════════════════════════════════════════════════════
class TestServiceMesh(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.service_mesh import ServiceMesh, LBStrategy, HealthStatus
        self.SM = ServiceMesh; self.LB = LBStrategy; self.HS = HealthStatus
        self.mesh = ServiceMesh(db_path=os.path.join(td,"mesh.db"))
        self.mesh.register_service("api", lb_strategy=LBStrategy.ROUND_ROBIN)

    def test_add_instance(self):
        inst = self.mesh.add_instance("api","10.0.0.1",8080)
        self.assertIsNotNone(inst.id)
        self.assertEqual(inst.host,"10.0.0.1")

    def test_resolve_returns_instance(self):
        self.mesh.add_instance("api","127.0.0.1",8080)
        inst = self.mesh.resolve("api")
        self.assertIsNotNone(inst)

    def test_resolve_no_instances(self):
        self.assertIsNone(self.mesh.resolve("api"))

    def test_round_robin(self):
        i1 = self.mesh.add_instance("api","h1",80)
        i2 = self.mesh.add_instance("api","h2",80)
        hits = [self.mesh.resolve("api").id for _ in range(4)]
        self.assertIn(i1.id, hits); self.assertIn(i2.id, hits)

    def test_random_lb(self):
        td = tempfile.mkdtemp()
        from agent.service_mesh import ServiceMesh, LBStrategy
        mesh2 = ServiceMesh(db_path=os.path.join(td,"m2.db"))
        mesh2.register_service("s",lb_strategy=LBStrategy.RANDOM)
        mesh2.add_instance("s","h1",80); mesh2.add_instance("s","h2",80)
        ids = {mesh2.resolve("s").id for _ in range(20)}
        self.assertGreater(len(ids),1)

    def test_least_conn(self):
        td = tempfile.mkdtemp()
        from agent.service_mesh import ServiceMesh, LBStrategy
        mesh3 = ServiceMesh(db_path=os.path.join(td,"m3.db"))
        mesh3.register_service("lc",lb_strategy=LBStrategy.LEAST_CONN)
        i1 = mesh3.add_instance("lc","h1",80)
        i2 = mesh3.add_instance("lc","h2",80)
        mesh3.acquire(i1.id)  # i1 has 1 in-flight
        inst = mesh3.resolve("lc")
        self.assertEqual(inst.id, i2.id)  # picks i2 (0 in-flight)
        mesh3.release(i1.id)

    def test_weighted_lb(self):
        td = tempfile.mkdtemp()
        from agent.service_mesh import ServiceMesh, LBStrategy
        mesh4 = ServiceMesh(db_path=os.path.join(td,"m4.db"))
        mesh4.register_service("w",lb_strategy=LBStrategy.WEIGHTED)
        mesh4.add_instance("w","heavy",80,weight=9.0)
        mesh4.add_instance("w","light",80,weight=1.0)
        hits = [mesh4.resolve("w").host for _ in range(100)]
        self.assertGreater(hits.count("heavy"), hits.count("light"))

    def test_unhealthy_excluded(self):
        i1 = self.mesh.add_instance("api","good",80)
        i2 = self.mesh.add_instance("api","bad",80)
        self.mesh.set_health(i2.id, self.HS.UNHEALTHY)
        for _ in range(10):
            self.assertNotEqual(self.mesh.resolve("api").id, i2.id)

    def test_circuit_breaker_opens(self):
        self.mesh.register_service("cb",cb_threshold=3)
        inst = self.mesh.add_instance("cb","h",80)
        for _ in range(3):
            self.mesh.record_call("cb", inst.id, 10, error=True)
        self.assertTrue(inst.cb_open)
        resolved = self.mesh.resolve("cb")
        self.assertIsNone(resolved)

    def test_circuit_breaker_recovers(self):
        self.mesh.register_service("cbr",cb_threshold=2,cb_recovery_s=0.01)
        inst = self.mesh.add_instance("cbr","h",80)
        for _ in range(2):
            self.mesh.record_call("cbr", inst.id, 10, error=True)
        time.sleep(0.02)
        resolved = self.mesh.resolve("cbr")
        self.assertIsNotNone(resolved)

    def test_record_call_updates_stats(self):
        self.mesh.add_instance("api","h",80)
        inst = self.mesh.resolve("api")
        self.mesh.record_call("api", inst.id, 50.0, error=False)
        self.assertEqual(inst.requests, 1)
        self.assertAlmostEqual(inst.avg_latency_ms, 50.0)

    def test_tag_filter(self):
        i1 = self.mesh.add_instance("api","h1",80,tags=["v2"])
        i2 = self.mesh.add_instance("api","h2",80,tags=["v1"])
        resolved = self.mesh.resolve("api",tags=["v2"])
        self.assertEqual(resolved.id, i1.id)

    def test_deregister(self):
        inst = self.mesh.add_instance("api","h",80)
        ok = self.mesh.remove_instance(inst.id)
        self.assertTrue(ok)
        self.assertIsNone(self.mesh.resolve("api"))

    def test_heartbeat_marks_healthy(self):
        inst = self.mesh.add_instance("api","h",80)
        self.mesh.heartbeat(inst.id)
        self.assertEqual(inst.health, self.HS.HEALTHY)

    def test_expire_stale(self):
        self.mesh.register_service("stale",heartbeat_ttl_s=0.01)
        inst = self.mesh.add_instance("stale","h",80)
        self.mesh.heartbeat(inst.id)
        time.sleep(0.02)
        self.mesh.expire_stale("stale")
        self.assertEqual(inst.health, self.HS.UNHEALTHY)

    def test_on_register_hook(self):
        registered = []
        self.mesh.on_register(lambda i: registered.append(i.host))
        self.mesh.add_instance("api","hook-host",80)
        self.assertIn("hook-host", registered)

    def test_service_stats(self):
        self.mesh.add_instance("api","h",80)
        s = self.mesh.service_stats("api")
        for k in ["service","instance_count","total_requests"]:
            self.assertIn(k, s)

    def test_canary_routing(self):
        self.mesh.register_service("canary_svc", canary_pct=100,
                                    canary_tag="canary")
        i1 = self.mesh.add_instance("canary_svc","main",80)
        i2 = self.mesh.add_instance("canary_svc","canary_inst",80,
                                      tags=["canary"])
        results = {self.mesh.resolve("canary_svc").id for _ in range(10)}
        self.assertIn(i2.id, results)

    def test_stats(self):
        s = self.mesh.stats()
        for k in ["total_instances","services"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# DISTRIBUTED LOCK
# ════════════════════════════════════════════════════════
class TestDistributedLock(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.distributed_lock import LockManager, LockMode
        self.lm = LockManager(db_path=os.path.join(td,"locks.db"))
        self.LM = LockMode

    def test_acquire_returns_token(self):
        token = _run(self.lm.acquire("r","w1"))
        self.assertIsNotNone(token)
        self.assertIsInstance(token, int)

    def test_token_monotonic(self):
        t1 = _run(self.lm.acquire("r1","w1"))
        _run(self.lm.release("r1","w1"))
        t2 = _run(self.lm.acquire("r2","w2"))
        self.assertGreater(t2, t1)

    def test_exclusive_blocks_second(self):
        _run(self.lm.acquire("res","w1"))
        token = _run(self.lm.acquire("res","w2",timeout_s=0.05))
        self.assertIsNone(token)

    def test_release_allows_next(self):
        _run(self.lm.acquire("res2","w1"))
        _run(self.lm.release("res2","w1"))
        token = _run(self.lm.acquire("res2","w2"))
        self.assertIsNotNone(token)

    def test_reentrant_same_owner(self):
        t1 = _run(self.lm.acquire("re","w1"))
        t2 = _run(self.lm.acquire("re","w1"))  # reentrant
        self.assertEqual(t1, t2)
        entry = self.lm._locks.get("re")
        self.assertEqual(entry.reentrant_count, 2)

    def test_release_wrong_owner(self):
        _run(self.lm.acquire("ro","w1"))
        ok = _run(self.lm.release("ro","w2"))
        self.assertFalse(ok)

    def test_force_release(self):
        _run(self.lm.acquire("fr","w1"))
        ok = self.lm.force_release("fr")
        self.assertTrue(ok)
        self.assertIsNone(self.lm._locks.get("fr"))

    def test_ttl_expiry(self):
        _run(self.lm.acquire("ttl","w1",ttl_s=0.01))
        time.sleep(0.02)
        token = _run(self.lm.acquire("ttl","w2"))
        self.assertIsNotNone(token)

    def test_sweep_expired(self):
        _run(self.lm.acquire("exp","w1",ttl_s=0.01))
        time.sleep(0.02)
        n = _run(self.lm.sweep_expired())
        self.assertGreaterEqual(n, 1)

    def test_renew(self):
        _run(self.lm.acquire("ren","w1",ttl_s=0.05))
        ok = _run(self.lm.renew("ren","w1",ttl_s=60))
        self.assertTrue(ok)
        entry = self.lm._locks.get("ren")
        self.assertGreater(entry.ttl_remaining, 50)

    def test_renew_wrong_owner(self):
        _run(self.lm.acquire("rw","w1"))
        ok = _run(self.lm.renew("rw","w2",ttl_s=10))
        self.assertFalse(ok)

    def test_shared_lock_multiple_readers(self):
        from agent.distributed_lock import LockMode
        t1 = _run(self.lm.acquire("shared","r1",LockMode.SHARED))
        t2 = _run(self.lm.acquire("shared","r2",LockMode.SHARED))
        self.assertIsNotNone(t1); self.assertIsNotNone(t2)

    def test_context_manager(self):
        async def use():
            async with self.lm.lock("ctx","w1") as token:
                return token
        token = _run(use())
        self.assertIsNotNone(token)
        self.assertIsNone(self.lm._locks.get("ctx"))  # released

    def test_on_acquire_hook(self):
        acquired = []
        self.lm.on_acquire(lambda e, o: acquired.append(e.key))
        _run(self.lm.acquire("h1","w1"))
        self.assertIn("h1", acquired)

    def test_on_release_hook(self):
        released = []
        self.lm.on_release(lambda e, o: released.append(e.key))
        _run(self.lm.acquire("h2","w1"))
        _run(self.lm.release("h2","w1"))
        self.assertIn("h2", released)

    def test_on_expire_hook(self):
        expired = []
        self.lm.on_expire(lambda e: expired.append(e.key))
        _run(self.lm.acquire("ex","w1",ttl_s=0.01))
        time.sleep(0.02)
        _run(self.lm.sweep_expired())
        self.assertIn("ex", expired)

    def test_deadlock_detected(self):
        # w1 holds res_a, w2 holds res_b
        # w1 tries to acquire res_b, w2 tries res_a → deadlock
        _run(self.lm.acquire("res_a","w1"))
        _run(self.lm.acquire("res_b","w2"))
        # Manually set waits-for to simulate
        self.lm._waits_for["w1"] = "res_b"
        self.lm._waits_for["w2"] = "res_a"
        # Now w1 requests res_b — deadlock detection fires
        token = _run(self.lm.acquire("res_b","w1",timeout_s=0.01))
        self.assertIsNone(token)

    def test_info(self):
        _run(self.lm.acquire("inf","w1"))
        info = self.lm.info("inf")
        self.assertIsNotNone(info)
        self.assertEqual(info["owner"],"w1")

    def test_list_locks(self):
        _run(self.lm.acquire("l1","w1"))
        _run(self.lm.acquire("l2","w2"))
        locks = self.lm.list_locks()
        keys = [l["key"] for l in locks]
        self.assertIn("l1", keys); self.assertIn("l2", keys)

    def test_stats(self):
        _run(self.lm.acquire("s1","w1"))
        s = self.lm.stats()
        self.assertGreaterEqual(s["active_locks"], 1)

# ════════════════════════════════════════════════════════
# LOG AGGREGATOR
# ════════════════════════════════════════════════════════
class TestLogAggregator(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.log_aggregator import LogAggregator, Level
        self.agg = LogAggregator(db_path=os.path.join(td,"logs.db"))
        self.Level = Level

    def test_log_returns_entry(self):
        e = self.agg.log("svc","info","Hello")
        self.assertIsNotNone(e)
        self.assertEqual(e.message,"Hello")

    def test_log_with_level_enum(self):
        e = self.agg.log("svc", self.Level.ERROR, "err msg")
        self.assertEqual(e.level, self.Level.ERROR)

    def test_log_with_level_string(self):
        from agent.log_aggregator import Level
        e = self.agg.log("svc", Level.from_str("warn"), "warn msg")
        self.assertEqual(e.level, Level.WARN)

    def test_min_level_filters(self):
        self.agg.set_min_level(self.Level.ERROR)
        e = self.agg.log("svc", self.Level.DEBUG, "debug msg")
        self.assertIsNone(e)

    def test_fields_stored(self):
        e = self.agg.log("svc", self.Level.INFO, "req",
                          fields={"method":"GET","status":200})
        self.assertEqual(e.fields["method"],"GET")

    def test_tags_stored(self):
        e = self.agg.log("svc", self.Level.INFO, "tagged",
                          tags=["api","v2"])
        self.assertIn("api", e.tags)

    def test_tail_returns_recent(self):
        for i in range(5):
            self.agg.log("svc", self.Level.INFO, f"msg {i}")
        entries = self.agg.tail(3)
        self.assertEqual(len(entries), 3)

    def test_tail_by_source(self):
        self.agg.log("svc_a", self.Level.INFO, "a msg")
        self.agg.log("svc_b", self.Level.INFO, "b msg")
        entries = self.agg.tail(10, source="svc_a")
        self.assertTrue(all(e.source=="svc_a" for e in entries))

    def test_search_finds_message(self):
        self.agg.log("svc", self.Level.ERROR, "database timeout error")
        results = self.agg.search("timeout")
        self.assertGreater(len(results), 0)

    def test_search_min_level(self):
        self.agg.log("svc", self.Level.DEBUG, "debug timeout")
        self.agg.log("svc", self.Level.ERROR, "error timeout")
        results = self.agg.search("timeout", min_level=self.Level.ERROR)
        self.assertTrue(all(e.level >= self.Level.ERROR for e in results))

    def test_alert_fires(self):
        alerts = []
        self.agg.add_alert("err_alert", pattern=r"CRITICAL",
                             min_level=self.Level.ERROR)
        self.agg.on_alert(lambda rule, e: alerts.append(rule))
        self.agg.log("svc", self.Level.ERROR, "CRITICAL failure")
        self.assertIn("err_alert", alerts)

    def test_alert_cooldown(self):
        alerts = []
        self.agg.add_alert("cd_alert", pattern=r"boom",
                             min_level=self.Level.WARN, cooldown_s=60)
        self.agg.on_alert(lambda r, e: alerts.append(r))
        self.agg.log("svc", self.Level.ERROR, "boom 1")
        self.agg.log("svc", self.Level.ERROR, "boom 2")
        self.assertEqual(len(alerts), 1)  # second suppressed by cooldown

    def test_alert_below_min_level_ignored(self):
        alerts = []
        self.agg.add_alert("lvl_alert", pattern=r"test",
                             min_level=self.Level.ERROR)
        self.agg.on_alert(lambda r, e: alerts.append(r))
        self.agg.log("svc", self.Level.INFO, "test message")
        self.assertEqual(len(alerts), 0)

    def test_remove_alert(self):
        self.agg.add_alert("rm","pattern")
        ok = self.agg.remove_alert("rm")
        self.assertTrue(ok)
        self.assertNotIn("rm", self.agg._alert_rules)

    def test_dedup(self):
        td = tempfile.mkdtemp()
        from agent.log_aggregator import LogAggregator, Level
        agg2 = LogAggregator(db_path=os.path.join(td,"d.db"),
                               dedup_window_s=5)
        agg2.log("s", Level.INFO, "dup msg")
        e2 = agg2.log("s", Level.INFO, "dup msg")
        self.assertIsNone(e2)

    def test_on_log_hook(self):
        seen = []
        self.agg.on_log(lambda e: seen.append(e.message))
        self.agg.log("svc", self.Level.INFO, "hooked")
        self.assertIn("hooked", seen)

    def test_batch_log(self):
        entries = [{"source":"s","level":"INFO","message":f"m{i}"}
                    for i in range(5)]
        n = self.agg.log_batch(entries)
        self.assertEqual(n, 5)

    def test_export_jsonl(self):
        self.agg.log("svc", self.Level.INFO, "exported")
        out = self.agg.export("jsonl")
        self.assertIn("exported", out)

    def test_export_csv(self):
        self.agg.log("svc", self.Level.INFO, "csv row")
        out = self.agg.export("csv")
        self.assertIn("ts,level,source,message", out)

    def test_stats(self):
        self.agg.log("svc", self.Level.INFO, "stat")
        s = self.agg.stats()
        for k in ["total","by_level","alert_rules"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# DAG SCHEDULER
# ════════════════════════════════════════════════════════
class TestDAGScheduler(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.task_scheduler_v2 import DAGScheduler, TaskSpec
        self.sched = DAGScheduler(db_path=os.path.join(td,"dag.db"))
        self.TS = TaskSpec

    def _simple_dag(self, name="dag"):
        async def step(ctx): return "ok"
        self.sched.register_dag(name,[
            self.TS("a", fn=step),
            self.TS("b", fn=step, deps=["a"]),
            self.TS("c", fn=step, deps=["a"]),
            self.TS("d", fn=step, deps=["b","c"]),
        ])
        return name

    def test_run_simple_dag(self):
        dag = self._simple_dag()
        run = _run(self.sched.run(dag))
        self.assertEqual(run.status, "done")

    def test_all_tasks_done(self):
        dag = self._simple_dag()
        run = _run(self.sched.run(dag))
        from agent.task_scheduler_v2 import TaskStatus
        for t in run.tasks.values():
            self.assertEqual(t.status, TaskStatus.DONE)

    def test_dependency_ordering(self):
        order = []
        async def step(ctx, name):
            order.append(name)
        self.sched.register_dag("order",[
            self.TS("first",  fn=lambda ctx: order.append("first")),
            self.TS("second", fn=lambda ctx: order.append("second"), deps=["first"]),
        ])
        _run(self.sched.run("order"))
        self.assertEqual(order.index("first") < order.index("second"), True)

    def test_parallel_wave(self):
        results = []
        import asyncio as _aio
        async def parallel_task(ctx):
            await _aio.sleep(0.01)
            results.append(time.time())
        self.sched.register_dag("parallel",[
            self.TS("p1", fn=parallel_task),
            self.TS("p2", fn=parallel_task),
        ])
        t0 = time.time()
        _run(self.sched.run("parallel"))
        elapsed = time.time() - t0
        self.assertLess(elapsed, 0.1)  # ran in parallel

    def test_context_shared(self):
        async def producer(ctx): ctx["x"] = 42
        async def consumer(ctx): return ctx.get("x")
        self.sched.register_dag("ctx_dag",[
            self.TS("produce", fn=producer),
            self.TS("consume", fn=consumer, deps=["produce"]),
        ])
        run = _run(self.sched.run("ctx_dag"))
        self.assertEqual(run.tasks["consume"].result, 42)

    def test_task_result_stored(self):
        async def compute(ctx): return 99
        self.sched.register_dag("result_dag",[
            self.TS("calc", fn=compute),
        ])
        run = _run(self.sched.run("result_dag"))
        self.assertEqual(run.tasks["calc"].result, 99)

    def test_fail_propagates(self):
        async def bad(ctx): raise RuntimeError("boom")
        async def after(ctx): return "after"
        self.sched.register_dag("fail_dag",[
            self.TS("bad_task", fn=bad),
            self.TS("after",    fn=after, deps=["bad_task"]),
        ])
        run = _run(self.sched.run("fail_dag", fail_fast=True))
        self.assertEqual(run.status, "failed")

    def test_retry_on_failure(self):
        attempts = [0]
        async def flaky(ctx):
            attempts[0] += 1
            if attempts[0] < 3: raise RuntimeError("not yet")
            return "ok"
        self.sched.register_dag("retry_dag",[
            self.TS("flaky", fn=flaky, max_retries=3, backoff_s=0.01),
        ])
        run = _run(self.sched.run("retry_dag"))
        self.assertEqual(run.status, "done")
        self.assertGreaterEqual(run.tasks["flaky"].attempt, 3)

    def test_timeout(self):
        import asyncio as _aio
        async def slow(ctx): await _aio.sleep(10)
        self.sched.register_dag("to_dag",[
            self.TS("slow", fn=slow, timeout_s=0.05, max_retries=0),
        ])
        run = _run(self.sched.run("to_dag"))
        from agent.task_scheduler_v2 import TaskStatus
        self.assertEqual(run.tasks["slow"].status, TaskStatus.TIMEOUT)

    def test_skip_if(self):
        from agent.task_scheduler_v2 import TaskStatus
        async def noop(ctx): return "done"
        self.sched.register_dag("skip_dag",[
            self.TS("skipped", fn=noop,
                     skip_if=lambda ctx: True),
        ])
        run = _run(self.sched.run("skip_dag"))
        self.assertEqual(run.tasks["skipped"].status, TaskStatus.SKIPPED)

    def test_cycle_detection(self):
        with self.assertRaises(ValueError):
            self.sched.register_dag("cycle",[
                self.TS("a", fn=lambda c: None, deps=["b"]),
                self.TS("b", fn=lambda c: None, deps=["a"]),
            ])

    def test_progress(self):
        async def step(ctx): return "x"
        self.sched.register_dag("prog",[
            self.TS("t1",fn=step),self.TS("t2",fn=step),
        ])
        run = _run(self.sched.run("prog"))
        self.assertAlmostEqual(run.progress, 1.0)

    def test_on_done_hook(self):
        done = []
        self.sched.on_done(lambda spec, r: done.append(spec.name))
        async def step(ctx): return 1
        self.sched.register_dag("hook_dag",[self.TS("h",fn=step)])
        _run(self.sched.run("hook_dag"))
        self.assertIn("h", done)

    def test_on_fail_hook(self):
        failed = []
        self.sched.on_fail(lambda spec, e: failed.append(spec.name))
        async def bad(ctx): raise RuntimeError("x")
        self.sched.register_dag("fail_hook",[self.TS("bad",fn=bad)])
        _run(self.sched.run("fail_hook"))
        self.assertIn("bad", failed)

    def test_to_dot(self):
        self._simple_dag("dot_dag")
        dot = self.sched.to_dot("dot_dag")
        self.assertIn("digraph", dot)
        self.assertIn("->", dot)

    def test_status_lookup(self):
        async def step(ctx): return 1
        self.sched.register_dag("st_dag",[self.TS("s",fn=step)])
        run = _run(self.sched.run("st_dag"))
        found = self.sched.status(run.id)
        self.assertIsNotNone(found)

    def test_unknown_dag_raises(self):
        with self.assertRaises(KeyError):
            _run(self.sched.run("no_such_dag"))

    def test_stats(self):
        s = self.sched.stats()
        for k in ["runs","registered_dags"]: self.assertIn(k,s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v43: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
