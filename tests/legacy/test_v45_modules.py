"""OMNI AGENT v45: Consensus, QueryPlanner, Telemetry, ResourcePool"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# CONSENSUS
# ════════════════════════════════════════════════════════
class TestConsensus(unittest.TestCase):
    def _cluster(self, n=3):
        td = tempfile.mkdtemp()
        from agent.consensus import RaftCluster
        return RaftCluster.create(n, base_path=os.path.join(td,"raft"))

    def test_elect_leader(self):
        cl = self._cluster()
        leader = cl.elect_leader()
        self.assertIsNotNone(leader)

    def test_leader_role(self):
        from agent.consensus import Role
        cl = self._cluster()
        cl.elect_leader()
        self.assertEqual(cl.leader.role, Role.LEADER)

    def test_followers_role(self):
        from agent.consensus import Role
        cl = self._cluster()
        cl.elect_leader()
        followers = [n for n in cl.nodes if n.role != Role.LEADER]
        self.assertEqual(len(followers), 2)

    def test_term_increments(self):
        cl = self._cluster()
        cl.elect_leader()
        self.assertGreaterEqual(cl.leader.current_term, 1)

    def test_only_leader_appends(self):
        cl = self._cluster()
        cl.elect_leader()
        entry = cl.leader.append_command({"op":"set","key":"x","value":1})
        self.assertIsNotNone(entry)

    def test_non_leader_rejects_append(self):
        from agent.consensus import Role
        cl = self._cluster()
        cl.elect_leader()
        follower = next(n for n in cl.nodes if n.role != Role.LEADER)
        result = follower.append_command({"op":"set","key":"x","value":1})
        self.assertIsNone(result)

    def test_replicate_to_followers(self):
        cl = self._cluster()
        cl.elect_leader()
        cl.leader.append_command({"op":"set","key":"y","value":2})
        acks = cl.replicate()
        self.assertGreaterEqual(acks, 1)

    def test_commit_after_quorum(self):
        cl = self._cluster(3)
        cl.elect_leader()
        cl.leader.append_command({"op":"set","key":"z","value":3})
        cl.replicate()
        self.assertGreater(cl.leader.commit_index, 0)

    def test_state_machine_apply(self):
        cl = self._cluster()
        cl.elect_leader()
        cl.leader.append_command({"op":"set","key":"k","value":42})
        cl.replicate()
        val = cl.leader.read_state("k")
        self.assertEqual(val, 42)

    def test_state_machine_delete(self):
        cl = self._cluster()
        cl.elect_leader()
        cl.leader.append_command({"op":"set","key":"d","value":1})
        cl.replicate()
        cl.leader.append_command({"op":"delete","key":"d"})
        cl.replicate()
        val = cl.leader.read_state("d")
        self.assertIsNone(val)

    def test_state_machine_increment(self):
        cl = self._cluster()
        cl.elect_leader()
        cl.leader.append_command({"op":"set","key":"c","value":10})
        cl.leader.append_command({"op":"increment","key":"c","by":5})
        cl.replicate()
        val = cl.leader.read_state("c")
        self.assertEqual(val, 15)

    def test_non_leader_read_returns_none(self):
        from agent.consensus import Role
        cl = self._cluster()
        cl.elect_leader()
        follower = next(n for n in cl.nodes if n.role != Role.LEADER)
        self.assertIsNone(follower.read_state())

    def test_read_all_state(self):
        cl = self._cluster()
        cl.elect_leader()
        cl.leader.append_command({"op":"set","key":"a","value":1})
        cl.leader.append_command({"op":"set","key":"b","value":2})
        cl.replicate()
        state = cl.leader.read_state()
        self.assertIsInstance(state, dict)
        self.assertIn("a", state)

    def test_vote_once_per_term(self):
        cl = self._cluster()
        leader = cl.elect_leader()
        term = leader.current_term
        from agent.consensus import VoteRequest
        follower = [n for n in cl.nodes if n.node_id != leader.node_id][0]
        # Already voted — second vote request for same term rejected
        req = VoteRequest(term=term, candidate_id="other",
                           last_log_index=0, last_log_term=0)
        resp = follower.request_vote(req)
        self.assertFalse(resp.granted)

    def test_take_snapshot(self):
        cl = self._cluster()
        cl.elect_leader()
        cl.leader.append_command({"op":"set","key":"s","value":99})
        cl.replicate()
        snap = cl.leader.take_snapshot()
        self.assertIn("state", snap)
        self.assertIn("last_index", snap)

    def test_log_entry_has_fields(self):
        cl = self._cluster()
        cl.elect_leader()
        e = cl.leader.append_command({"op":"set","key":"f","value":7})
        self.assertGreater(e.index, 0)
        self.assertGreater(e.term, 0)

    def test_status_fields(self):
        cl = self._cluster()
        cl.elect_leader()
        s = cl.leader.status()
        for k in ["node_id","role","term","commit_index"]: self.assertIn(k,s)

    def test_cluster_stats(self):
        cl = self._cluster()
        s = cl.stats()
        self.assertEqual(len(s), 3)

# ════════════════════════════════════════════════════════
# QUERY PLANNER
# ════════════════════════════════════════════════════════
class TestQueryPlanner(unittest.TestCase):
    def setUp(self):
        from agent.query_planner import QueryPlanner
        self.qp = QueryPlanner()
        self.qp.create_table("users", [
            {"name":"Alice","age":30,"dept":"eng","salary":90000},
            {"name":"Bob",  "age":25,"dept":"hr", "salary":70000},
            {"name":"Carol","age":35,"dept":"eng","salary":95000},
            {"name":"Dave", "age":28,"dept":"hr", "salary":72000},
        ])

    def test_select_all(self):
        r = self.qp.execute("SELECT * FROM users")
        self.assertEqual(len(r), 4)

    def test_select_columns(self):
        r = self.qp.execute("SELECT name, age FROM users")
        self.assertIn("name", r[0]); self.assertIn("age", r[0])
        self.assertNotIn("dept", r[0])

    def test_where_eq(self):
        r = self.qp.execute("SELECT * FROM users WHERE name = 'Alice'")
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["name"], "Alice")

    def test_where_gt(self):
        r = self.qp.execute("SELECT * FROM users WHERE age > 28")
        self.assertTrue(all(row["age"] > 28 for row in r))

    def test_where_and(self):
        r = self.qp.execute(
            "SELECT * FROM users WHERE dept = 'eng' AND age > 28")
        self.assertTrue(all(row["dept"]=="eng" and row["age"]>28 for row in r))

    def test_where_or(self):
        r = self.qp.execute(
            "SELECT * FROM users WHERE age < 26 OR age > 34")
        self.assertEqual(len(r), 2)

    def test_where_like(self):
        r = self.qp.execute("SELECT * FROM users WHERE name LIKE 'A%'")
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["name"], "Alice")

    def test_where_in(self):
        r = self.qp.execute("SELECT * FROM users WHERE dept IN ('eng')")
        self.assertTrue(all(row["dept"]=="eng" for row in r))

    def test_order_by_asc(self):
        r = self.qp.execute("SELECT * FROM users ORDER BY age ASC")
        ages = [row["age"] for row in r]
        self.assertEqual(ages, sorted(ages))

    def test_order_by_desc(self):
        r = self.qp.execute("SELECT * FROM users ORDER BY age DESC")
        ages = [row["age"] for row in r]
        self.assertEqual(ages, sorted(ages, reverse=True))

    def test_limit(self):
        r = self.qp.execute("SELECT * FROM users LIMIT 2")
        self.assertEqual(len(r), 2)

    def test_offset(self):
        r1 = self.qp.execute("SELECT * FROM users ORDER BY name ASC LIMIT 1")
        r2 = self.qp.execute("SELECT * FROM users ORDER BY name ASC LIMIT 1 OFFSET 1")
        self.assertNotEqual(r1[0]["name"], r2[0]["name"])

    def test_group_by_count(self):
        r = self.qp.execute(
            "SELECT dept, COUNT(*) as cnt FROM users GROUP BY dept")
        depts = {row["dept"]: row["cnt"] for row in r}
        self.assertEqual(depts.get("eng"), 2)
        self.assertEqual(depts.get("hr"),  2)

    def test_group_by_avg(self):
        r = self.qp.execute(
            "SELECT dept, AVG(salary) as avg_sal FROM users GROUP BY dept")
        eng = next(row for row in r if row["dept"]=="eng")
        self.assertAlmostEqual(eng["avg_sal"], 92500)

    def test_group_by_sum(self):
        r = self.qp.execute(
            "SELECT dept, SUM(salary) as total FROM users GROUP BY dept")
        eng = next(row for row in r if row["dept"]=="eng")
        self.assertEqual(eng["total"], 185000)

    def test_having(self):
        r = self.qp.execute(
            "SELECT dept, COUNT(*) as cnt FROM users "
            "GROUP BY dept HAVING cnt > 1")
        self.assertTrue(all(row["cnt"] > 1 for row in r))

    def test_distinct(self):
        r = self.qp.execute("SELECT DISTINCT dept FROM users")
        depts = [row["dept"] for row in r]
        self.assertEqual(len(depts), len(set(depts)))

    def test_alias(self):
        r = self.qp.execute("SELECT name AS employee FROM users LIMIT 1")
        self.assertIn("employee", r[0])

    def test_insert_api(self):
        self.qp.insert("users", {"name":"Eve","age":22,"dept":"eng","salary":60000})
        r = self.qp.execute("SELECT * FROM users WHERE name = 'Eve'")
        self.assertEqual(len(r), 1)

    def test_update(self):
        self.qp.execute("UPDATE users SET age = 31 WHERE name = 'Alice'")
        r = self.qp.execute("SELECT * FROM users WHERE name = 'Alice'")
        self.assertEqual(r[0]["age"], 31)

    def test_delete(self):
        self.qp.execute("DELETE FROM users WHERE name = 'Bob'")
        r = self.qp.execute("SELECT * FROM users WHERE name = 'Bob'")
        self.assertEqual(len(r), 0)

    def test_explain(self):
        plan = self.qp.explain("SELECT * FROM users WHERE age > 25")
        self.assertIsInstance(plan, list)
        self.assertTrue(any("SCAN" in s for s in plan))

    def test_table_info(self):
        info = self.qp.table_info("users")
        self.assertEqual(info["rows"], 4)

    def test_stats(self):
        self.qp.execute("SELECT * FROM users")
        s = self.qp.stats()
        self.assertGreaterEqual(s["queries"], 1)

# ════════════════════════════════════════════════════════
# TELEMETRY
# ════════════════════════════════════════════════════════
class TestTelemetry(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.telemetry import Tracer
        self.tracer = Tracer(db_path=os.path.join(td,"tel.db"),
                              sample_rate=1.0, service_name="test-svc")

    def test_span_created(self):
        with self.tracer.span("op") as s:
            self.assertIsNotNone(s.span_id)
            self.assertIsNotNone(s.trace_id)

    def test_span_has_end_time(self):
        with self.tracer.span("op") as s: pass
        self.assertIsNotNone(s.end_time)

    def test_span_duration(self):
        with self.tracer.span("op") as s:
            time.sleep(0.01)
        self.assertGreater(s.duration_ms, 5)

    def test_nested_span_parent(self):
        with self.tracer.span("parent") as parent:
            with self.tracer.span("child") as child:
                self.assertEqual(child.parent_id, parent.span_id)
                self.assertEqual(child.trace_id, parent.trace_id)

    def test_set_attribute(self):
        with self.tracer.span("op") as s:
            s.set_attribute("http.method", "GET")
        self.assertEqual(s.attributes["http.method"], "GET")

    def test_add_event(self):
        with self.tracer.span("op") as s:
            s.add_event("cache.hit", {"key": "x"})
        self.assertEqual(len(s.events), 1)
        self.assertEqual(s.events[0].name, "cache.hit")

    def test_error_span(self):
        from agent.telemetry import SpanStatus
        with self.assertRaises(ValueError):
            with self.tracer.span("bad") as s:
                raise ValueError("oops")
        self.assertEqual(s.status, SpanStatus.ERROR)
        self.assertIn("error.type", s.attributes)

    def test_set_status(self):
        from agent.telemetry import SpanStatus
        with self.tracer.span("op") as s:
            s.set_status(SpanStatus.OK)
        self.assertEqual(s.status, SpanStatus.OK)

    def test_traceparent_format(self):
        with self.tracer.span("op") as s:
            tp = s.to_traceparent()
        self.assertTrue(tp.startswith("00-"))
        self.assertEqual(len(tp.split("-")), 4)

    def test_inject_extract_context(self):
        with self.tracer.span("op") as s:
            headers = {}
            self.tracer.inject_context(s, headers)
        ctx = self.tracer.extract_context(headers)
        self.assertEqual(ctx["trace_id"], s.trace_id)

    def test_baggage_propagates(self):
        with self.tracer.span("op", baggage={"user_id":"42"}) as parent:
            with self.tracer.span("child") as child:
                self.assertEqual(child.baggage.get("user_id"), "42")

    def test_get_trace(self):
        with self.tracer.span("op") as s: pass
        spans = self.tracer.get_trace(s.trace_id)
        self.assertGreater(len(spans), 0)

    def test_async_span(self):
        async def go():
            async with self.tracer.async_span("async_op") as s:
                await asyncio.sleep(0.01)
            return s
        s = _run(go())
        self.assertIsNotNone(s.end_time)

    def test_counter_metric(self):
        c = self.tracer.counter("requests")
        c.inc(); c.inc(5)
        self.assertEqual(c.value, 6)

    def test_gauge_metric(self):
        g = self.tracer.gauge("cpu")
        g.set(0.72)
        self.assertAlmostEqual(g.value, 0.72)
        g.inc(0.1); g.dec(0.05)
        self.assertAlmostEqual(g.value, 0.77, places=5)

    def test_histogram_metric(self):
        h = self.tracer.histogram("latency_ms")
        for v in [10, 20, 50, 100, 200]:
            h.observe(v)
        s = h.stats()
        self.assertEqual(s["count"], 5)
        self.assertIsNotNone(s["p50"])

    def test_histogram_percentiles(self):
        h = self.tracer.histogram("req_ms")
        for i in range(100): h.observe(float(i))
        self.assertAlmostEqual(h.percentile(50), 49.0, delta=5)
        self.assertAlmostEqual(h.percentile(99), 98.0, delta=5)

    def test_metrics_snapshot(self):
        self.tracer.counter("c").inc()
        snap = self.tracer.metrics_snapshot()
        self.assertTrue(any("counter" in k for k in snap))

    def test_on_end_span_hook(self):
        ended = []
        self.tracer.on_end_span(lambda s: ended.append(s.operation))
        with self.tracer.span("tracked"): pass
        self.assertIn("tracked", ended)

    def test_sampling_zero(self):
        td = tempfile.mkdtemp()
        from agent.telemetry import Tracer
        t0 = Tracer(db_path=os.path.join(td,"t0.db"), sample_rate=0.0)
        with t0.span("unsampled") as s: pass
        self.assertFalse(s._sampled)

    def test_stats(self):
        s = self.tracer.stats()
        for k in ["service","sample_rate","completed_spans"]:
            self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# RESOURCE POOL
# ════════════════════════════════════════════════════════
class TestResourcePool(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.resource_pool import ResourcePool
        self.RP = ResourcePool
        self._td = td

    def _pool(self, **kwargs):
        counter = [0]
        def factory():
            counter[0] += 1
            return {"conn": counter[0]}
        from agent.resource_pool import ResourcePool
        return ResourcePool(factory=factory,
                             db_path=os.path.join(self._td,"pool.db"),
                             **kwargs)

    def test_borrow_returns_resource(self):
        pool = self._pool(max_size=2)
        async def go():
            pr = await pool.borrow()
            self.assertIsNotNone(pr.resource)
            await pool.release(pr)
        _run(go())

    def test_borrow_context_manager(self):
        pool = self._pool(max_size=2)
        async def go():
            async with pool.borrow_ctx() as res:
                self.assertIsNotNone(res)
        _run(go())

    def test_borrow_count_increments(self):
        pool = self._pool(max_size=2)
        async def go():
            pr = await pool.borrow()
            await pool.release(pr)
            pr2 = await pool.borrow()
            self.assertGreaterEqual(pr2.borrow_count, 1)
            await pool.release(pr2)
        _run(go())

    def test_max_size_respected(self):
        pool = self._pool(max_size=2)
        async def go():
            p1 = await pool.borrow()
            p2 = await pool.borrow()
            with self.assertRaises(TimeoutError):
                await pool.borrow(timeout_s=0.05)
            await pool.release(p1)
            await pool.release(p2)
        _run(go())

    def test_release_returns_to_idle(self):
        pool = self._pool(max_size=2)
        async def go():
            pr = await pool.borrow()
            await pool.release(pr)
            self.assertGreater(len(pool._idle), 0)
        _run(go())

    def test_resource_reused(self):
        pool = self._pool(max_size=2)
        async def go():
            pr1 = await pool.borrow()
            rid = pr1.id
            await pool.release(pr1)
            pr2 = await pool.borrow()
            self.assertEqual(pr2.id, rid)
            await pool.release(pr2)
        _run(go())

    def test_error_increments_error_count(self):
        pool = self._pool(max_size=2)
        async def go():
            pr = await pool.borrow()
            await pool.release(pr, error=True)
            return pr
        pr = _run(go())
        self.assertEqual(pr.error_count, 1)

    def test_max_errors_discards(self):
        pool = self._pool(max_size=2, max_errors=2)
        async def go():
            pr = await pool.borrow()
            pr.error_count = 1   # one error already
            await pool.release(pr, error=True)  # this makes 2 → discard
        _run(go())
        self.assertEqual(len(pool._idle), 0)

    def test_discard_flag(self):
        pool = self._pool(max_size=2)
        async def go():
            pr = await pool.borrow()
            await pool.release(pr, discard=True)
        _run(go())
        self.assertEqual(len(pool._idle), 0)

    def test_on_create_hook(self):
        created = []
        pool = self._pool(max_size=2)
        pool.on_create(lambda pr: created.append(pr.id))
        async def go():
            pr = await pool.borrow()
            await pool.release(pr)
        _run(go())
        self.assertGreater(len(created), 0)

    def test_on_borrow_hook(self):
        borrowed = []
        pool = self._pool(max_size=2)
        pool.on_borrow(lambda pr: borrowed.append(pr.id))
        async def go():
            pr = await pool.borrow()
            await pool.release(pr)
        _run(go())
        self.assertGreater(len(borrowed), 0)

    def test_resize_reduces_idle(self):
        pool = self._pool(max_size=5)
        async def go():
            # Fill idle with 3 resources
            prs = [await pool.borrow() for _ in range(3)]
            for pr in prs: await pool.release(pr)
            self.assertEqual(len(pool._idle), 3)
            await pool.resize(new_max=1)
            self.assertLessEqual(len(pool._idle), 1)
        _run(go())

    def test_stats(self):
        pool = self._pool(max_size=2)
        async def go():
            pr = await pool.borrow()
            s = pool.stats()
            self.assertIn("idle", s); self.assertIn("borrowed", s)
            await pool.release(pr)
        _run(go())

    def test_start_warms_min_size(self):
        pool = self._pool(min_size=2, max_size=5)
        _run(pool.start())
        pool.stop_sweeper = lambda: None  # prevent background task issues
        self.assertEqual(len(pool._idle), 2)
        if pool._health_task: pool._health_task.cancel()

    def test_drain(self):
        pool = self._pool(max_size=3)
        async def go():
            pr = await pool.borrow()
            await pool.release(pr)
            await pool.drain()
            self.assertTrue(pool._drained)
        _run(go())

    def test_drain_blocks_new_borrow(self):
        pool = self._pool(max_size=2)
        async def go():
            await pool.drain()
            with self.assertRaises(RuntimeError):
                await pool.borrow()
        _run(go())

    def test_hits_misses_tracked(self):
        pool = self._pool(max_size=2)
        async def go():
            pr = await pool.borrow()
            await pool.release(pr)
            pr2 = await pool.borrow()  # reuse → hit
            await pool.release(pr2)
        _run(go())
        self.assertEqual(pool._misses, 1)
        self.assertEqual(pool._hits,   1)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v45: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
