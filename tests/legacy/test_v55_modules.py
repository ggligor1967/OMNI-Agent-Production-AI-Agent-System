"""OMNI AGENT v55: SchemaRegistry, TaskSchedulerV3, VectorIndexV2, RateLimiterV3"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# SCHEMA REGISTRY
# ════════════════════════════════════════════════════════
class TestSchemaRegistry(unittest.TestCase):
    def setUp(self):
        from agent.schema_registry import SchemaRegistry, CompatMode
        self.sr = SchemaRegistry(compat_mode=CompatMode.NONE, db_path=":memory:")

    def test_register_schema(self):
        sv = self.sr.register("user", {"type": "object", "properties": {"name": {"type": "string"}}})
        self.assertEqual(sv.version, 1)

    def test_register_new_version(self):
        self.sr.register("item", {"type": "object"})
        sv2 = self.sr.register("item", {"type": "object", "required": ["id"]})
        self.assertEqual(sv2.version, 2)

    def test_validate_valid_object(self):
        self.sr.register("person", {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}
        })
        result = self.sr.validate("person", {"name": "Alice", "age": 30})
        self.assertTrue(result.valid)

    def test_validate_missing_required(self):
        self.sr.register("req", {"type": "object", "required": ["id"],
                                  "properties": {"id": {"type": "string"}}})
        result = self.sr.validate("req", {})
        self.assertFalse(result.valid)
        self.assertTrue(any("id" in e for e in result.errors))

    def test_validate_wrong_type(self):
        self.sr.register("typed", {"type": "object",
                                    "properties": {"count": {"type": "integer"}}})
        result = self.sr.validate("typed", {"count": "not_an_int"})
        self.assertFalse(result.valid)

    def test_validate_min_length(self):
        self.sr.register("str_schema", {"type": "string", "minLength": 5})
        self.assertFalse(self.sr.validate("str_schema", "hi").valid)
        self.assertTrue(self.sr.validate("str_schema", "hello world").valid)

    def test_validate_max_length(self):
        self.sr.register("short", {"type": "string", "maxLength": 3})
        self.assertFalse(self.sr.validate("short", "toolong").valid)

    def test_validate_pattern(self):
        self.sr.register("pat", {"type": "string", "pattern": r"^\d{3}$"})
        self.assertTrue(self.sr.validate("pat", "123").valid)
        self.assertFalse(self.sr.validate("pat", "abc").valid)

    def test_validate_enum(self):
        self.sr.register("color", {"type": "string", "enum": ["red", "green", "blue"]})
        self.assertTrue(self.sr.validate("color", "red").valid)
        self.assertFalse(self.sr.validate("color", "yellow").valid)

    def test_validate_number_range(self):
        self.sr.register("score", {"type": "number", "minimum": 0, "maximum": 100})
        self.assertTrue(self.sr.validate("score", 50.0).valid)
        self.assertFalse(self.sr.validate("score", 150.0).valid)

    def test_validate_array(self):
        self.sr.register("arr", {"type": "array", "minItems": 1, "maxItems": 3,
                                  "items": {"type": "integer"}})
        self.assertTrue(self.sr.validate("arr", [1, 2]).valid)
        self.assertFalse(self.sr.validate("arr", []).valid)
        self.assertFalse(self.sr.validate("arr", [1, 2, 3, 4]).valid)

    def test_validate_additional_properties_false(self):
        self.sr.register("strict", {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "additionalProperties": False
        })
        self.assertFalse(self.sr.validate("strict", {"a": 1, "b": 2}).valid)
        self.assertTrue(self.sr.validate("strict", {"a": 1}).valid)

    def test_validate_strict_raises(self):
        from agent.schema_registry import SchemaValidationError
        self.sr.register("s", {"type": "string"})
        with self.assertRaises(SchemaValidationError):
            self.sr.validate_strict("s", 123)

    def test_compat_backward_blocks_new_required(self):
        from agent.schema_registry import SchemaRegistry, CompatMode, SchemaValidationError
        sr = SchemaRegistry(compat_mode=CompatMode.BACKWARD, db_path=":memory:")
        sr.register("x", {"type": "object", "required": ["a"],
                           "properties": {"a": {"type": "string"}}})
        with self.assertRaises(SchemaValidationError):
            sr.register("x", {"type": "object",
                                "required": ["a", "b"],
                                "properties": {"a": {"type": "string"},
                                               "b": {"type": "string"}}})

    def test_compat_none_allows_anything(self):
        from agent.schema_registry import SchemaRegistry, CompatMode
        sr = SchemaRegistry(compat_mode=CompatMode.NONE, db_path=":memory:")
        sr.register("y", {"type": "object", "required": ["a"]})
        sv = sr.register("y", {"type": "object", "required": ["a", "b", "c"]})
        self.assertEqual(sv.version, 2)

    def test_migration(self):
        self.sr.register("v1", {"type": "object"})
        self.sr.register("v1", {"type": "object"})
        self.sr.register_migration("v1", 1, 2, lambda d: {**d, "migrated": True})
        result = self.sr.migrate("v1", {"x": 1}, 1, 2)
        self.assertTrue(result.get("migrated"))

    def test_deactivate_version(self):
        sv = self.sr.register("deact", {"type": "object"})
        self.sr.deactivate("deact", sv.version)
        self.assertFalse(self.sr.get_schema("deact", sv.version) is None)

    def test_list_schemas(self):
        self.sr.register("a", {"type": "string"})
        self.sr.register("b", {"type": "integer"})
        names = self.sr.list_schemas()
        self.assertIn("a", names); self.assertIn("b", names)

    def test_schema_versions(self):
        self.sr.register("ver", {"type": "string"})
        self.sr.register("ver", {"type": "string", "minLength": 1})
        versions = self.sr.schema_versions("ver")
        self.assertEqual(len(versions), 2)

    def test_latest_version(self):
        self.sr.register("lv", {"type": "string"})
        self.sr.register("lv", {"type": "string"})
        self.assertEqual(self.sr.latest_version("lv"), 2)

    def test_stats(self):
        self.sr.register("s1", {"type": "string"})
        self.sr.validate("s1", "hello")
        s = self.sr.stats()
        self.assertEqual(s["schemas"], 1)
        self.assertEqual(s["validations"], 1)

# ════════════════════════════════════════════════════════
# TASK SCHEDULER V3
# ════════════════════════════════════════════════════════
class TestTaskSchedulerV3(unittest.TestCase):
    def setUp(self):
        from agent.task_scheduler_v3 import TaskSchedulerV3
        self.ts = TaskSchedulerV3(db_path=":memory:", tick_s=0.01)

    def test_schedule_and_tick(self):
        from agent.task_scheduler_v3 import ScheduleType, TaskStatus
        ran = []
        self.ts.schedule("t1", lambda: ran.append(1),
                         schedule_type=ScheduleType.IMMEDIATE)
        runs = self.ts.tick()
        self.assertGreater(len(runs), 0)
        self.assertIn(1, ran)

    def test_interval_task_reschedules(self):
        from agent.task_scheduler_v3 import ScheduleType
        calls = []
        task = self.ts.schedule("interval", lambda: calls.append(1),
                                schedule_type=ScheduleType.INTERVAL,
                                schedule=0.0)
        self.ts.tick()
        self.ts.tick()
        self.assertGreater(len(calls), 1)

    def test_once_task_runs_once(self):
        from agent.task_scheduler_v3 import ScheduleType
        calls = []
        self.ts.schedule("once", lambda: calls.append(1),
                         schedule_type=ScheduleType.ONCE,
                         schedule=time.time() - 1)
        self.ts.tick()
        self.ts.tick()
        self.assertEqual(len(calls), 1)

    def test_failed_task_logged(self):
        from agent.task_scheduler_v3 import ScheduleType, TaskStatus
        self.ts.schedule("fail", lambda: (_ for _ in ()).throw(RuntimeError("oops")),
                         schedule_type=ScheduleType.IMMEDIATE)
        runs = self.ts.tick()
        self.assertEqual(runs[0].status, TaskStatus.FAILED)

    def test_retry_on_failure(self):
        attempts = [0]
        def flaky():
            attempts[0] += 1
            if attempts[0] < 3: raise RuntimeError("retry")
            return "ok"
        from agent.task_scheduler_v3 import ScheduleType
        self.ts.schedule("retry", flaky, schedule_type=ScheduleType.IMMEDIATE,
                         max_retries=3, retry_delay_s=0.0)
        runs = self.ts.tick()
        self.assertEqual(runs[0].result, "ok")
        self.assertEqual(runs[0].attempt, 3)

    def test_pause_and_resume(self):
        from agent.task_scheduler_v3 import ScheduleType
        calls = []
        task = self.ts.schedule("pr", lambda: calls.append(1),
                                schedule_type=ScheduleType.INTERVAL, schedule=0.0)
        self.ts.pause(task.task_id)
        self.ts.tick()
        self.assertEqual(len(calls), 0)
        self.ts.resume(task.task_id)
        self.ts.tick()
        self.assertGreater(len(calls), 0)

    def test_cancel_task(self):
        from agent.task_scheduler_v3 import ScheduleType
        calls = []
        task = self.ts.schedule("cancel", lambda: calls.append(1),
                                schedule_type=ScheduleType.INTERVAL, schedule=0.0)
        self.ts.cancel(task.task_id)
        self.ts.tick()
        self.assertEqual(len(calls), 0)

    def test_remove_task(self):
        from agent.task_scheduler_v3 import ScheduleType
        task = self.ts.schedule("rm", lambda: None,
                                schedule_type=ScheduleType.INTERVAL, schedule=60)
        self.ts.remove(task.task_id)
        self.assertIsNone(self.ts.get_task(task.task_id))

    def test_priority_ordering(self):
        from agent.task_scheduler_v3 import ScheduleType, TaskPriority
        order = []
        self.ts.schedule("low",  lambda: order.append("low"),
                         schedule_type=ScheduleType.IMMEDIATE,
                         priority=TaskPriority.LOW)
        self.ts.schedule("high", lambda: order.append("high"),
                         schedule_type=ScheduleType.IMMEDIATE,
                         priority=TaskPriority.HIGH)
        self.ts.tick()
        self.assertLess(order.index("high"), order.index("low"))

    def test_before_after_hooks(self):
        from agent.task_scheduler_v3 import ScheduleType
        before, after = [], []
        self.ts.on_before(lambda s: before.append(s.task_id))
        self.ts.on_after(lambda s, r: after.append(s.task_id))
        task = self.ts.schedule("hook", lambda: 1,
                                schedule_type=ScheduleType.IMMEDIATE)
        self.ts.tick()
        self.assertIn(task.task_id, before)
        self.assertIn(task.task_id, after)

    def test_cron_expression(self):
        from agent.task_scheduler_v3 import ScheduleType, _cron_matches
        import datetime
        # Test * * * * * (every minute)
        self.assertTrue(_cron_matches("* * * * *", time.time()))

    def test_list_tasks(self):
        from agent.task_scheduler_v3 import ScheduleType
        self.ts.schedule("lt", lambda: None, schedule_type=ScheduleType.INTERVAL,
                         schedule=60, tags=["test"])
        tasks = self.ts.list_tasks(tag="test")
        self.assertEqual(len(tasks), 1)

    def test_run_history(self):
        from agent.task_scheduler_v3 import ScheduleType
        task = self.ts.schedule("hist", lambda: 42,
                                schedule_type=ScheduleType.IMMEDIATE)
        self.ts.tick()
        hist = self.ts.run_history(task.task_id)
        self.assertEqual(len(hist), 1)

    def test_stats(self):
        from agent.task_scheduler_v3 import ScheduleType
        self.ts.schedule("s", lambda: 1, schedule_type=ScheduleType.IMMEDIATE)
        self.ts.tick()
        s = self.ts.stats()
        self.assertGreater(s["runs"], 0)

# ════════════════════════════════════════════════════════
# VECTOR INDEX V2
# ════════════════════════════════════════════════════════
class TestVectorIndexV2(unittest.TestCase):
    def _vec(self, seed=1, dim=8):
        import math
        v = [math.sin(seed * i + 0.1) for i in range(dim)]
        n = math.sqrt(sum(x*x for x in v))
        return [x/n for x in v]

    def setUp(self):
        from agent.vector_index_v2 import VectorIndexV2
        self.idx = VectorIndexV2(dim=8, db_path=":memory:")

    def test_insert_and_get(self):
        rec = self.idx.insert(self._vec(1), payload={"label": "a"})
        got = self.idx.get(rec.record_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.payload["label"], "a")

    def test_insert_wrong_dim_raises(self):
        with self.assertRaises(ValueError):
            self.idx.insert([1.0, 2.0])  # dim=2 but index expects 8

    def test_search_returns_results(self):
        self.idx.insert(self._vec(1))
        self.idx.insert(self._vec(2))
        hits = self.idx.search(self._vec(1), top_k=2)
        self.assertGreater(len(hits), 0)

    def test_nearest_is_self(self):
        rec = self.idx.insert(self._vec(1))
        for _ in range(5):
            self.idx.insert(self._vec(_ + 10))
        hits = self.idx.search(self._vec(1), top_k=1)
        self.assertEqual(hits[0].record.record_id, rec.record_id)

    def test_search_by_id(self):
        r1 = self.idx.insert(self._vec(1))
        self.idx.insert(self._vec(2))
        self.idx.insert(self._vec(3))
        hits = self.idx.search_by_id(r1.record_id, top_k=2)
        self.assertFalse(any(h.record.record_id == r1.record_id for h in hits))

    def test_namespace_isolation(self):
        self.idx.insert(self._vec(1), namespace="ns1")
        self.idx.insert(self._vec(2), namespace="ns2")
        hits = self.idx.search(self._vec(1), top_k=5, namespace="ns1")
        self.assertTrue(all(h.record.namespace == "ns1" for h in hits))

    def test_filter_fn(self):
        self.idx.insert(self._vec(1), payload={"keep": True})
        self.idx.insert(self._vec(2), payload={"keep": False})
        hits = self.idx.search(self._vec(1), top_k=5,
                               filter_fn=lambda r: r.payload.get("keep"))
        self.assertTrue(all(h.record.payload.get("keep") for h in hits))

    def test_delete(self):
        rec = self.idx.insert(self._vec(1))
        self.assertTrue(self.idx.delete(rec.record_id))
        self.assertIsNone(self.idx.get(rec.record_id))

    def test_upsert_replaces(self):
        rec = self.idx.insert(self._vec(1), payload={"v": 1})
        self.idx.upsert(rec.record_id, self._vec(2), payload={"v": 2})
        got = self.idx.get(rec.record_id)
        self.assertEqual(got.payload["v"], 2)

    def test_update_payload(self):
        rec = self.idx.insert(self._vec(1), payload={"a": 1})
        self.idx.update_payload(rec.record_id, {"b": 2})
        self.assertEqual(self.idx.get(rec.record_id).payload["b"], 2)

    def test_approximate_search(self):
        for i in range(20):
            self.idx.insert(self._vec(i))
        hits = self.idx.search(self._vec(0), top_k=3, approximate=True)
        self.assertGreater(len(hits), 0)

    def test_euclidean_metric(self):
        from agent.vector_index_v2 import VectorIndexV2, DistanceMetric
        idx = VectorIndexV2(dim=8, metric=DistanceMetric.EUCLIDEAN, db_path=":memory:")
        idx.insert(self._vec(1))
        idx.insert(self._vec(2))
        hits = idx.search(self._vec(1), top_k=1)
        self.assertGreater(len(hits), 0)

    def test_list_namespaces(self):
        self.idx.insert(self._vec(1), namespace="alpha")
        self.idx.insert(self._vec(2), namespace="beta")
        ns = self.idx.list_namespaces()
        self.assertIn("alpha", ns); self.assertIn("beta", ns)

    def test_delete_namespace(self):
        self.idx.insert(self._vec(1), namespace="del_ns")
        self.idx.insert(self._vec(2), namespace="del_ns")
        removed = self.idx.delete_namespace("del_ns")
        self.assertEqual(removed, 2)
        self.assertEqual(self.idx.namespace_size("del_ns"), 0)

    def test_len(self):
        self.idx.insert(self._vec(1))
        self.idx.insert(self._vec(2))
        self.assertEqual(len(self.idx), 2)

    def test_stats(self):
        self.idx.insert(self._vec(1))
        self.idx.search(self._vec(1), top_k=1)
        s = self.idx.stats()
        self.assertEqual(s["records"], 1)
        self.assertEqual(s["inserts"], 1)
        self.assertEqual(s["searches"], 1)

# ════════════════════════════════════════════════════════
# RATE LIMITER V3
# ════════════════════════════════════════════════════════
class TestRateLimiterV3(unittest.TestCase):
    def setUp(self):
        from agent.rate_limiter_v3 import RateLimiterV3
        self.rl = RateLimiterV3(db_path=":memory:")

    def test_token_bucket_allows_within_limit(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("tb", Algorithm.TOKEN_BUCKET,
                                limit=10, burst=10, refill_rate=1.0)
        d = self.rl.check(p.policy_id, "user1")
        self.assertTrue(d.allowed)

    def test_token_bucket_denies_when_exhausted(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("tb2", Algorithm.TOKEN_BUCKET,
                                limit=2, burst=2, refill_rate=0.001)
        self.rl.check(p.policy_id, "u")
        self.rl.check(p.policy_id, "u")
        d = self.rl.check(p.policy_id, "u")
        self.assertFalse(d.allowed)

    def test_sliding_window_allows(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("sw", Algorithm.SLIDING_WINDOW,
                                limit=5, window_s=60)
        for _ in range(5):
            d = self.rl.check(p.policy_id, "u")
            self.assertTrue(d.allowed)

    def test_sliding_window_denies(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("sw2", Algorithm.SLIDING_WINDOW,
                                limit=3, window_s=60)
        for _ in range(3): self.rl.check(p.policy_id, "u")
        d = self.rl.check(p.policy_id, "u")
        self.assertFalse(d.allowed)

    def test_sliding_window_retry_after(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("sw3", Algorithm.SLIDING_WINDOW,
                                limit=1, window_s=60)
        self.rl.check(p.policy_id, "u")
        d = self.rl.check(p.policy_id, "u")
        self.assertGreater(d.retry_after_s, 0)

    def test_fixed_window_allows(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("fw", Algorithm.FIXED_WINDOW,
                                limit=3, window_s=60)
        for _ in range(3):
            self.assertTrue(self.rl.check(p.policy_id, "u").allowed)

    def test_fixed_window_denies(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("fw2", Algorithm.FIXED_WINDOW,
                                limit=2, window_s=60)
        self.rl.check(p.policy_id, "u"); self.rl.check(p.policy_id, "u")
        self.assertFalse(self.rl.check(p.policy_id, "u").allowed)

    def test_leaky_bucket_allows(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("lb", Algorithm.LEAKY_BUCKET,
                                limit=10, leak_rate=1.0)
        d = self.rl.check(p.policy_id, "u")
        self.assertTrue(d.allowed)

    def test_leaky_bucket_denies_when_full(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("lb2", Algorithm.LEAKY_BUCKET,
                                limit=2, leak_rate=0.001)
        self.rl.check(p.policy_id, "u", cost=2)
        d = self.rl.check(p.policy_id, "u")
        self.assertFalse(d.allowed)

    def test_concurrency_allows(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("cc", Algorithm.CONCURRENCY, limit=2)
        d = self.rl.check(p.policy_id, "u")
        self.assertTrue(d.allowed)

    def test_concurrency_denies_at_limit(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("cc2", Algorithm.CONCURRENCY, limit=1)
        self.rl.check(p.policy_id, "u")
        d = self.rl.check(p.policy_id, "u")
        self.assertFalse(d.allowed)

    def test_concurrency_release(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("cc3", Algorithm.CONCURRENCY, limit=1)
        self.rl.check(p.policy_id, "u")
        self.rl.release(p.policy_id, "u")
        d = self.rl.check(p.policy_id, "u")
        self.assertTrue(d.allowed)

    def test_different_keys_independent(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("ind", Algorithm.SLIDING_WINDOW,
                                limit=1, window_s=60)
        self.rl.check(p.policy_id, "user1")
        d2 = self.rl.check(p.policy_id, "user2")
        self.assertTrue(d2.allowed)

    def test_disabled_policy_always_allows(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("dis", Algorithm.FIXED_WINDOW, limit=0, window_s=1)
        self.rl.disable_policy(p.policy_id)
        d = self.rl.check(p.policy_id, "u")
        self.assertTrue(d.allowed)

    def test_enable_policy(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("en", Algorithm.FIXED_WINDOW, limit=0, window_s=60)
        self.rl.disable_policy(p.policy_id)
        self.rl.enable_policy(p.policy_id)
        d = self.rl.check(p.policy_id, "u")
        self.assertFalse(d.allowed)

    def test_reset_key(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("rst", Algorithm.SLIDING_WINDOW,
                                limit=1, window_s=60)
        self.rl.check(p.policy_id, "u")
        self.rl.reset_key(p.policy_id, "u")
        d = self.rl.check(p.policy_id, "u")
        self.assertTrue(d.allowed)

    def test_on_limit_hook_fires(self):
        from agent.rate_limiter_v3 import Algorithm
        fired = []
        self.rl.on_limit(lambda d: fired.append(d.key))
        p = self.rl.add_policy("hook", Algorithm.FIXED_WINDOW, limit=1, window_s=60)
        self.rl.check(p.policy_id, "u")
        self.rl.check(p.policy_id, "u")
        self.assertIn("u", fired)

    def test_event_log(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("log", Algorithm.FIXED_WINDOW, limit=5, window_s=60)
        self.rl.check(p.policy_id, "u")
        log = self.rl.event_log(p.policy_id)
        self.assertGreater(len(log), 0)

    def test_weighted_cost(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("wt", Algorithm.SLIDING_WINDOW, limit=5, window_s=60)
        self.rl.check(p.policy_id, "u", cost=3)
        d = self.rl.check(p.policy_id, "u", cost=3)
        self.assertFalse(d.allowed)

    def test_stats(self):
        from agent.rate_limiter_v3 import Algorithm
        p = self.rl.add_policy("stat", Algorithm.FIXED_WINDOW, limit=1, window_s=60)
        self.rl.check(p.policy_id, "u")
        self.rl.check(p.policy_id, "u")
        s = self.rl.stats()
        self.assertEqual(s["allowed"], 1)
        self.assertEqual(s["denied"], 1)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v55: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
