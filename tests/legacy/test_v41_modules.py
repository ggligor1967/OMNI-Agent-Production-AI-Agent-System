"""OMNI AGENT v41: ConnectionPool, MessageQueue, RuleEngine, TimeSeries"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# CONNECTION POOL
# ════════════════════════════════════════════════════════
class TestConnectionPool(unittest.TestCase):
    def setUp(self):
        from agent.connection_pool import ConnectionPool, PooledConn
        self.CP = ConnectionPool; self.PC = PooledConn
        self._id = 0
        def factory():
            self._id += 1
            return {"id": self._id, "alive": True}
        td = tempfile.mkdtemp()
        self.pool = ConnectionPool(
            factory=factory, max_size=5, min_size=0, timeout_s=1.0,
            db_path=os.path.join(td,"cp.db"))

    def test_acquire_returns_pooled_conn(self):
        pc = _run(self.pool.acquire())
        self.assertIsNotNone(pc)
        self.assertIsNotNone(pc.conn)

    def test_acquire_increments_stats(self):
        _run(self.pool.acquire())
        self.assertGreaterEqual(self.pool.stats()["acquired"], 1)

    def test_release_returns_to_pool(self):
        pc = _run(self.pool.acquire())
        _run(self.pool.release(pc))
        self.assertGreaterEqual(self.pool.size, 1)

    def test_reuse_after_release(self):
        pc1 = _run(self.pool.acquire())
        _run(self.pool.release(pc1))
        pc2 = _run(self.pool.acquire())
        self.assertEqual(pc1.conn["id"], pc2.conn["id"])

    def test_max_size_enforced(self):
        pcs = [_run(self.pool.acquire()) for _ in range(5)]
        with self.assertRaises(asyncio.TimeoutError):
            _run(self.pool.acquire(timeout_s=0.1))
        for pc in pcs: _run(self.pool.release(pc))

    def test_create_new_on_empty_pool(self):
        pc1 = _run(self.pool.acquire())
        pc2 = _run(self.pool.acquire())
        self.assertNotEqual(pc1.conn["id"], pc2.conn["id"])
        _run(self.pool.release(pc1)); _run(self.pool.release(pc2))

    def test_discard_on_error(self):
        pc = _run(self.pool.acquire())
        size_before = self.pool._total
        _run(self.pool.release(pc, discard=True))
        self.assertLess(self.pool._total, size_before)

    def test_context_manager(self):
        received = []
        async def use():
            async with self.pool.acquire_ctx() as conn:
                received.append(conn)
        _run(use())
        self.assertEqual(len(received), 1)

    def test_on_create_hook(self):
        created = []
        self.pool.on_create(lambda c: created.append(c))
        pc = _run(self.pool.acquire())
        self.assertGreaterEqual(len(created), 1)
        _run(self.pool.release(pc))

    def test_on_acquire_hook(self):
        acquired = []
        self.pool.on_acquire(lambda c: acquired.append(c))
        pc = _run(self.pool.acquire())
        self.assertEqual(len(acquired), 1)
        _run(self.pool.release(pc))

    def test_on_release_hook(self):
        released = []
        self.pool.on_release(lambda c: released.append(c))
        pc = _run(self.pool.acquire())
        _run(self.pool.release(pc))
        self.assertEqual(len(released), 1)

    def test_ping_all_with_validate(self):
        def validate(c): return c.get("alive", False)
        self.pool._validate_fn = validate
        pc = _run(self.pool.acquire())
        _run(self.pool.release(pc))
        result = _run(self.pool.ping_all())
        self.assertIn("alive", result)

    def test_ping_all_removes_dead(self):
        def validate(c): return False  # always dead
        self.pool._validate_fn = validate
        pc = _run(self.pool.acquire())
        _run(self.pool.release(pc))
        result = _run(self.pool.ping_all())
        self.assertGreaterEqual(result["dead"], 1)

    def test_resize_smaller(self):
        pc1 = _run(self.pool.acquire())
        pc2 = _run(self.pool.acquire())
        _run(self.pool.release(pc1)); _run(self.pool.release(pc2))
        _run(self.pool.resize(2))
        self.assertLessEqual(self.pool._max_size, 2)

    def test_drain_clears_pool(self):
        pc = _run(self.pool.acquire())
        _run(self.pool.release(pc))
        _run(self.pool.drain())
        self.assertEqual(self.pool.size, 0)

    def test_stats_fields(self):
        s = self.pool.stats()
        for k in ["created","acquired","released","pool_idle","max_size"]:
            self.assertIn(k, s)

    def test_borrow_count_increments(self):
        pc = _run(self.pool.acquire())
        _run(self.pool.release(pc))
        pc2 = _run(self.pool.acquire())
        self.assertGreaterEqual(pc2.borrow_count, 1)
        _run(self.pool.release(pc2))

    def test_destroy_fn_called(self):
        destroyed = []
        self.pool._destroy_fn = lambda c: destroyed.append(c)
        pc = _run(self.pool.acquire())
        _run(self.pool.release(pc, discard=True))
        self.assertEqual(len(destroyed), 1)

# ════════════════════════════════════════════════════════
# MESSAGE QUEUE
# ════════════════════════════════════════════════════════
class TestMessageQueue(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.message_queue import MessageQueue, QueueMode
        self.mq = MessageQueue(db_path=os.path.join(td,"mq.db"))
        self.QM = QueueMode
        self.mq.create_queue("test", max_receive_count=3)

    def test_send_returns_message(self):
        msg = self.mq.send("test", {"x": 1})
        self.assertIsNotNone(msg.id)

    def test_receive_returns_message(self):
        self.mq.send("test", {"a": 1})
        msgs = self.mq.receive("test")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].body["a"], 1)

    def test_ack_removes_message(self):
        self.mq.send("test", {"b": 2})
        msgs = self.mq.receive("test")
        ok = self.mq.ack(msgs[0]._receipt_id)
        self.assertTrue(ok)
        msgs2 = self.mq.receive("test")
        self.assertEqual(len(msgs2), 0)

    def test_nack_requeues_message(self):
        self.mq.send("test", {"c": 3})
        msgs = self.mq.receive("test")
        self.mq.nack(msgs[0]._receipt_id)
        msgs2 = self.mq.receive("test")
        self.assertEqual(len(msgs2), 1)

    def test_visibility_timeout_hides_message(self):
        self.mq.send("test", {"d": 4})
        msgs = self.mq.receive("test", visibility_timeout_s=100)
        msgs2 = self.mq.receive("test")
        self.assertEqual(len(msgs2), 0)

    def test_visibility_expire_makes_visible(self):
        self.mq.send("test", {"e": 5})
        msgs = self.mq.receive("test", visibility_timeout_s=0.01)
        time.sleep(0.02)
        msgs2 = self.mq.receive("test")
        self.assertEqual(len(msgs2), 1)

    def test_dlq_after_max_receives(self):
        self.mq.create_queue("dlq_q", max_receive_count=2)
        self.mq.send("dlq_q", {"f": 6})
        for _ in range(2):
            msgs = self.mq.receive("dlq_q")
            if msgs: self.mq.nack(msgs[0]._receipt_id)
        dlq = self.mq.dlq("dlq_q")
        self.assertGreater(len(dlq), 0)

    def test_priority_queue_ordering(self):
        self.mq.create_queue("pq", mode=self.QM.PRIORITY)
        self.mq.send("pq", {"label":"low"},  priority=9)
        self.mq.send("pq", {"label":"high"}, priority=1)
        msgs = self.mq.receive("pq", count=1)
        self.assertEqual(msgs[0].body["label"], "high")

    def test_delay_hides_message(self):
        self.mq.send("test", {"g": 7}, delay_s=100)
        msgs = self.mq.receive("test")
        self.assertEqual(len(msgs), 0)

    def test_send_batch(self):
        msgs = self.mq.send_batch("test", [{"n": i} for i in range(5)])
        self.assertEqual(len(msgs), 5)
        received = self.mq.receive("test", count=5)
        self.assertEqual(len(received), 5)

    def test_peek_no_receipt(self):
        self.mq.send("test", {"peek": True})
        msgs = self.mq.peek("test")
        self.assertGreater(len(msgs), 0)
        msgs2 = self.mq.receive("test")
        self.assertGreater(len(msgs2), 0)  # still receivable

    def test_purge(self):
        for i in range(3): self.mq.send("test", {"i": i})
        n = self.mq.purge("test")
        self.assertGreaterEqual(n, 3)

    def test_on_send_hook(self):
        sent = []
        self.mq.on_send(lambda m: sent.append(m.body))
        self.mq.send("test", {"hook": True})
        self.assertEqual(len(sent), 1)

    def test_on_ack_hook(self):
        acked = []
        self.mq.on_ack(lambda mid: acked.append(mid))
        self.mq.send("test", {"h": 1})
        msgs = self.mq.receive("test")
        self.mq.ack(msgs[0]._receipt_id)
        self.assertEqual(len(acked), 1)

    def test_on_dlq_hook(self):
        dlq_fired = []
        self.mq.on_dlq(lambda m: dlq_fired.append(m))
        self.mq.create_queue("dlq2", max_receive_count=1)
        self.mq.send("dlq2", {"dlq": True})
        msgs = self.mq.receive("dlq2")
        if msgs: self.mq.nack(msgs[0]._receipt_id)
        self.mq.receive("dlq2")   # triggers DLQ

    def test_receive_count_increments(self):
        self.mq.send("test", {"cnt": 1})
        msgs = self.mq.receive("test")
        self.mq.nack(msgs[0]._receipt_id)
        msgs2 = self.mq.receive("test")
        self.assertEqual(msgs2[0].receive_count, 2)

    def test_stats_per_queue(self):
        self.mq.send("test", {"x": 1})
        s = self.mq.stats("test")
        for k in ["pending","done","dlq","in_flight"]: self.assertIn(k, s)

    def test_auto_create_queue(self):
        self.mq.send("auto_q", {"auto": True})
        self.assertIn("auto_q", self.mq._queues)

# ════════════════════════════════════════════════════════
# RULE ENGINE
# ════════════════════════════════════════════════════════
class TestRuleEngine(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.rule_engine import (RuleEngine, ConflictStrategy,
                                        ConditionLogic)
        self.re = RuleEngine(db_path=os.path.join(td,"re.db"))
        self.CS = ConflictStrategy; self.CL = ConditionLogic

    def test_basic_rule_fires(self):
        self.re.add_rule("r1",
            conditions=[{"field":"x","op":"==","value":1}],
            actions=[{"type":"set_fact","field":"y","value":"set"}])
        r = self.re.evaluate({"x": 1})
        self.assertIn("r1", r.fired)
        self.assertEqual(r.facts["y"], "set")

    def test_rule_not_fire_no_match(self):
        self.re.add_rule("r2",
            conditions=[{"field":"x","op":"==","value":99}],
            actions=[{"type":"set_fact","field":"z","value":"bad"}])
        r = self.re.evaluate({"x": 1})
        self.assertNotIn("r2", r.fired)
        self.assertNotIn("z", r.facts)

    def test_op_gt(self):
        self.re.add_rule("gt_r",
            conditions=[{"field":"score","op":">","value":80}],
            actions=[{"type":"set_fact","field":"grade","value":"A"}])
        r = self.re.evaluate({"score": 90})
        self.assertEqual(r.facts.get("grade"), "A")

    def test_op_lte(self):
        self.re.add_rule("lte_r",
            conditions=[{"field":"age","op":"<=","value":18}],
            actions=[{"type":"set_fact","field":"minor","value":True}])
        r = self.re.evaluate({"age": 16})
        self.assertTrue(r.facts.get("minor"))

    def test_op_in(self):
        self.re.add_rule("in_r",
            conditions=[{"field":"plan","op":"in","value":["pro","enterprise"]}],
            actions=[{"type":"set_fact","field":"premium","value":True}])
        r = self.re.evaluate({"plan": "pro"})
        self.assertTrue(r.facts.get("premium"))

    def test_op_regex(self):
        self.re.add_rule("rx_r",
            conditions=[{"field":"email","op":"regex","value":"@corp\\.com$"}],
            actions=[{"type":"set_fact","field":"internal","value":True}])
        r = self.re.evaluate({"email": "alice@corp.com"})
        self.assertTrue(r.facts.get("internal"))

    def test_op_exists(self):
        self.re.add_rule("ex_r",
            conditions=[{"field":"token","op":"exists","value":True}],
            actions=[{"type":"set_fact","field":"auth","value":True}])
        r = self.re.evaluate({"token": "abc"})
        self.assertTrue(r.facts.get("auth"))

    def test_condition_logic_any(self):
        from agent.rule_engine import ConditionLogic
        self.re.add_rule("any_r",
            conditions=[{"field":"a","op":"==","value":1},
                        {"field":"b","op":"==","value":2}],
            actions=[{"type":"set_fact","field":"ok","value":True}],
            logic=ConditionLogic.ANY)
        r = self.re.evaluate({"a": 1, "b": 99})
        self.assertTrue(r.facts.get("ok"))

    def test_priority_ordering(self):
        fired_order = []
        def make_action(tag):
            return {"type":"call_fn","fn":f"record_{tag}"}
        for tag, pri in [("first",1),("second",2)]:
            self.re.register_function(f"record_{tag}",
                lambda f, _tag=tag: fired_order.append(_tag))
            self.re.add_rule(f"p_{tag}",
                conditions=[{"field":"x","op":"==","value":1}],
                actions=[{"type":"call_fn","fn":f"record_{tag}"}],
                priority=pri)
        self.re.evaluate({"x":1})
        self.assertEqual(fired_order[0], "first")

    def test_forward_chaining(self):
        self.re.add_rule("chain1",priority=1,
            conditions=[{"field":"score","op":">=","value":90}],
            actions=[{"type":"set_fact","field":"grade","value":"A"}])
        self.re.add_rule("chain2",priority=2,
            conditions=[{"field":"grade","op":"==","value":"A"}],
            actions=[{"type":"set_fact","field":"honor_roll","value":True}])
        r = self.re.evaluate({"score": 95})
        self.assertTrue(r.facts.get("honor_roll"))

    def test_action_delete_fact(self):
        self.re.add_rule("del_r",
            conditions=[{"field":"x","op":"==","value":1}],
            actions=[{"type":"delete_fact","field":"x"}])
        r = self.re.evaluate({"x":1,"y":2})
        self.assertIsNone(r.facts.get("x"))
        self.assertEqual(r.facts.get("y"), 2)

    def test_action_emit_event(self):
        self.re.add_rule("ev_r",
            conditions=[{"field":"alert","op":"==","value":True}],
            actions=[{"type":"emit_event","event":"ALERT","data":{"msg":"hi"}}])
        r = self.re.evaluate({"alert":True})
        self.assertEqual(len(r.events), 1)
        self.assertEqual(r.events[0]["event"], "ALERT")

    def test_action_raise_error(self):
        self.re.add_rule("err_r",
            conditions=[{"field":"danger","op":"==","value":True}],
            actions=[{"type":"raise_error","message":"Danger!"}])
        r = self.re.evaluate({"danger":True})
        self.assertIsNotNone(r.error)
        self.assertIn("Danger!", r.error)

    def test_call_fn_action(self):
        results = []
        self.re.register_function("capture",
            lambda facts, **kw: results.append(facts.get("v")))
        self.re.add_rule("fn_r",
            conditions=[{"field":"v","op":"exists","value":True}],
            actions=[{"type":"call_fn","fn":"capture"}])
        self.re.evaluate({"v": 42})
        self.assertIn(42, results)

    def test_salience_limits_fires(self):
        count = [0]
        self.re.register_function("inc", lambda f: count.__setitem__(0, count[0]+1))
        self.re.add_rule("sal_r", salience=2,
            conditions=[{"field":"on","op":"==","value":True}],
            actions=[{"type":"call_fn","fn":"inc"}])
        self.re.evaluate({"on":True})
        self.assertLessEqual(count[0], 2)

    def test_disable_rule(self):
        self.re.add_rule("dis_r",
            conditions=[{"field":"x","op":"==","value":1}],
            actions=[{"type":"set_fact","field":"y","value":1}])
        self.re.disable_rule("dis_r")
        r = self.re.evaluate({"x":1})
        self.assertNotIn("dis_r", r.fired)

    def test_disable_group(self):
        self.re.add_rule("grp_r",group="blocked",
            conditions=[{"field":"x","op":"==","value":1}],
            actions=[{"type":"set_fact","field":"y","value":1}])
        self.re.disable_group("blocked")
        r = self.re.evaluate({"x":1})
        self.assertNotIn("grp_r", r.fired)

    def test_first_strategy(self):
        from agent.rule_engine import ConflictStrategy
        re2 = __import__("agent.rule_engine",fromlist=["RuleEngine"]).RuleEngine(
            db_path=tempfile.mktemp(suffix=".db"),
            strategy=ConflictStrategy.FIRST)
        re2.add_rule("f1",priority=1,
            conditions=[{"field":"x","op":"==","value":1}],
            actions=[{"type":"set_fact","field":"a","value":1}])
        re2.add_rule("f2",priority=2,
            conditions=[{"field":"x","op":"==","value":1}],
            actions=[{"type":"set_fact","field":"b","value":2}])
        r = re2.evaluate({"x":1})
        self.assertIn("f1", r.fired)

    def test_explain(self):
        self.re.add_rule("exp_r",
            conditions=[{"field":"v","op":">","value":5}],
            actions=[])
        expl = self.re.explain({"v":10})
        self.assertGreater(len(expl), 0)
        self.assertTrue(expl[0]["would_fire"])

    def test_dot_path_access(self):
        self.re.add_rule("dot_r",
            conditions=[{"field":"user.age","op":">=","value":18}],
            actions=[{"type":"set_fact","field":"adult","value":True}])
        r = self.re.evaluate({"user":{"age":25}})
        self.assertTrue(r.facts.get("adult"))

    def test_stats(self):
        self.re.add_rule("st_r",conditions=[],actions=[])
        s = self.re.stats()
        for k in ["rules","in_memory"]: self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# TIME SERIES
# ════════════════════════════════════════════════════════
class TestTimeSeries(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.time_series import TimeSeries
        self.ts = TimeSeries(db_path=os.path.join(td,"ts.db"))

    def _now(self): return time.time()

    def test_write_and_query(self):
        t0 = self._now()
        self.ts.write("cpu", t0, 50.0)
        r = self.ts.query("cpu", t0-1, t0+1)
        self.assertEqual(len(r.points), 1)
        self.assertAlmostEqual(r.points[0][1], 50.0)

    def test_auto_create_series(self):
        self.ts.write("new_metric", time.time(), 1.0)
        self.assertIn("new_metric", [s["name"] for s in self.ts.list_series()])

    def test_write_batch(self):
        t0 = self._now()
        pts = [(t0+i, float(i*10)) for i in range(5)]
        self.ts.write_batch("batch", pts)
        r = self.ts.query("batch", t0-1, t0+10)
        self.assertEqual(len(r.points), 5)

    def test_range_filter(self):
        t0 = self._now()
        for i in range(10):
            self.ts.write("range_s", t0+i, float(i))
        r = self.ts.query("range_s", t0+2, t0+5)
        self.assertEqual(len(r.points), 4)

    def test_downsampling_mean(self):
        t0 = self._now()
        pts = [(t0+i, float(i)) for i in range(100)]
        self.ts.write_batch("ds", pts)
        r = self.ts.query("ds", t0, t0+99, step=10, aggregation="mean")
        self.assertGreater(len(r.points), 0)
        self.assertIn("mean", r.aggregation)

    def test_downsampling_sum(self):
        t0 = self._now()
        pts = [(t0+i, 1.0) for i in range(10)]
        self.ts.write_batch("sum_s", pts)
        r = self.ts.query("sum_s", t0, t0+9, step=5, aggregation="sum")
        total = sum(v for _, v in r.points if v is not None)
        self.assertAlmostEqual(total, 10.0)

    def test_aggregation_min_max(self):
        t0 = self._now()
        for v in [1.0, 5.0, 3.0]:
            self.ts.write("mm", t0, v); t0 += 0.001
        t0_orig = time.time() - 1
        r_min = self.ts.query("mm", t0_orig, time.time()+1,
                               step=100, aggregation="min")
        r_max = self.ts.query("mm", t0_orig, time.time()+1,
                               step=100, aggregation="max")
        min_v = [v for _, v in r_min.points if v is not None]
        max_v = [v for _, v in r_max.points if v is not None]
        self.assertAlmostEqual(min(min_v), 1.0)
        self.assertAlmostEqual(max(max_v), 5.0)

    def test_aggregation_count(self):
        t0 = self._now()
        for i in range(6):
            self.ts.write("cnt_s", t0+i, 1.0)
        r = self.ts.query("cnt_s", t0, t0+5, step=10, aggregation="count")
        counts = [v for _, v in r.points if v is not None]
        self.assertAlmostEqual(sum(counts), 6.0)

    def test_aggregation_stddev(self):
        t0 = self._now()
        for v in [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]:
            self.ts.write("std_s", t0, v); t0 += 0.001
        r = self.ts.query("std_s", time.time()-2, time.time()+1,
                           step=100, aggregation="stddev")
        vals = [v for _, v in r.points if v is not None]
        self.assertGreater(len(vals), 0)

    def test_percentile_p95(self):
        t0 = self._now()
        for i in range(100):
            self.ts.write("pct", t0+i, float(i))
        r = self.ts.query("pct", t0, t0+99, step=200, aggregation="p95")
        vals = [v for _, v in r.points if v is not None]
        self.assertGreater(len(vals), 0)

    def test_rate_of_change(self):
        t0 = self._now()
        self.ts.write("rate_s", t0,   0.0)
        self.ts.write("rate_s", t0+1, 10.0)
        self.ts.write("rate_s", t0+2, 20.0)
        rates = self.ts.rate("rate_s", t0-1, t0+3)
        self.assertEqual(len(rates), 2)
        self.assertAlmostEqual(rates[0][1], 10.0)

    def test_moving_average(self):
        t0 = self._now()
        for i in range(10):
            self.ts.write("ma_s", t0+i, float(i))
        ma = self.ts.moving_average("ma_s", window=3, start=t0-1, end=t0+10)
        self.assertEqual(len(ma), 10)

    def test_anomaly_detection(self):
        t0 = self._now()
        for i in range(50):
            self.ts.write("anom", t0+i, 10.0)
        self.ts.write("anom", t0+50, 1000.0)  # spike
        anomalies = self.ts.anomalies("anom", window=20, threshold=3.0,
                                       start=t0-1, end=t0+51)
        self.assertGreater(len(anomalies), 0)

    def test_retention_sweep(self):
        t0 = self._now()
        self.ts.create_series("ret_s", retention_s=1)
        self.ts.write("ret_s", t0-2, 1.0)  # old
        self.ts.write("ret_s", t0,   2.0)  # current
        n = self.ts.sweep_retention()
        self.assertGreaterEqual(n, 1)

    def test_fill_locf(self):
        t0 = self._now()
        self.ts.write("locf_s", t0, 5.0)
        r = self.ts.query("locf_s", t0-5, t0+5,
                           step=2, aggregation="mean", fill="locf")
        vals = [v for _, v in r.points]
        non_none = [v for v in vals if v is not None]
        self.assertGreater(len(non_none), 0)

    def test_multi_query(self):
        t0 = self._now()
        self.ts.write("m1", t0, 1.0); self.ts.write("m2", t0, 2.0)
        results = self.ts.multi_query(["m1","m2"], t0-1, t0+1)
        self.assertEqual(len(results), 2)
        names = [r.series for r in results]
        self.assertIn("m1", names); self.assertIn("m2", names)

    def test_export_csv(self):
        t0 = self._now()
        self.ts.write("csv_s", t0, 42.0)
        csv = self.ts.export_csv("csv_s", t0-1, t0+1)
        self.assertIn("ts,value", csv)
        self.assertIn("42.0", csv)

    def test_create_series_with_tags(self):
        self.ts.create_series("tagged", tags={"host":"srv1","env":"prod"})
        s = next(s for s in self.ts.list_series() if s["name"]=="tagged")
        self.assertEqual(s["tags"]["host"], "srv1")

    def test_stats(self):
        self.ts.write("stat_s", time.time(), 1.0)
        s = self.ts.stats()
        for k in ["series_count","total_points"]: self.assertIn(k, s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v41: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
