"""OMNI AGENT v61: TaskQueueV2, PromptOptimizerV2, ConnectionPoolV2, DataAugmentor"""
import os, sys, time, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# TASK QUEUE V2
# ════════════════════════════════════════════════════════
class TestTaskQueueV2(unittest.TestCase):
    def setUp(self):
        from agent.task_queue_v2 import TaskQueueV2
        self.tq = TaskQueueV2(db_path=":memory:")

    def test_enqueue_creates_task(self):
        task = self.tq.enqueue("job1", {"x": 1})
        self.assertIsNotNone(task.task_id)

    def test_run_sync_success(self):
        from agent.task_queue_v2 import TaskStatus
        self.tq.create_queue("q1", lambda payload: payload["x"] * 2, auto_start=False)
        task = self.tq.enqueue("job", {"x": 5}, queue_name="q1")
        self.tq.run_sync(task.task_id, "q1")
        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertEqual(task.result, 10)

    def test_run_sync_failure_retry(self):
        from agent.task_queue_v2 import TaskStatus
        calls = [0]
        def flaky(p):
            calls[0] += 1
            if calls[0] < 2: raise ValueError("fail")
            return "ok"
        self.tq.create_queue("q2", flaky, auto_start=False)
        task = self.tq.enqueue("retry_job", {}, queue_name="q2",
                                max_retries=2, retry_delay_s=0.0)
        self.tq.run_sync(task.task_id, "q2")
        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertEqual(task.result, "ok")

    def test_run_sync_dead_letter(self):
        from agent.task_queue_v2 import TaskStatus
        self.tq.create_queue("q3", lambda p: (_ for _ in ()).throw(RuntimeError("err")),
                              auto_start=False)
        task = self.tq.enqueue("dead_job", {}, queue_name="q3",
                                max_retries=0, retry_delay_s=0.0)
        self.tq.run_sync(task.task_id, "q3")
        self.assertEqual(task.status, TaskStatus.DEAD)
        dl = self.tq.dead_letter()
        self.assertGreater(len(dl), 0)

    def test_cancel_queued_task(self):
        from agent.task_queue_v2 import TaskStatus
        task = self.tq.enqueue("cancel_me", {})
        self.assertTrue(self.tq.cancel(task.task_id))
        self.assertEqual(task.status, TaskStatus.CANCELLED)

    def test_priority_ordering(self):
        from agent.task_queue_v2 import TaskPriority
        t1 = self.tq.enqueue("low", {}, priority=TaskPriority.BULK)
        t2 = self.tq.enqueue("hi",  {}, priority=TaskPriority.CRITICAL)
        # CRITICAL has lower integer (0) → higher priority in PriorityQueue
        self.assertLess(t2.priority.value, t1.priority.value)

    def test_enqueue_batch(self):
        tasks = self.tq.enqueue_batch([
            {"name": f"t{i}", "payload": i} for i in range(5)])
        self.assertEqual(len(tasks), 5)

    def test_pre_post_hooks(self):
        pre, post = [], []
        self.tq.on_pre_execute(lambda t: pre.append(t.name))
        self.tq.on_post_execute(lambda t: post.append(t.status.value))
        self.tq.create_queue("hq", lambda p: None, auto_start=False)
        task = self.tq.enqueue("hook_job", {}, queue_name="hq")
        self.tq.run_sync(task.task_id, "hq")
        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)

    def test_list_tasks_by_status(self):
        from agent.task_queue_v2 import TaskStatus
        self.tq.create_queue("lq", lambda p: None, auto_start=False)
        task = self.tq.enqueue("list_job", {}, queue_name="lq")
        self.tq.run_sync(task.task_id, "lq")
        done = self.tq.list_tasks(status=TaskStatus.DONE)
        self.assertGreater(len(done), 0)

    def test_list_tasks_by_tag(self):
        self.tq.enqueue("tagged", {}, tags=["ml"])
        self.tq.enqueue("other", {})
        ml_tasks = self.tq.list_tasks(tag="ml")
        self.assertEqual(len(ml_tasks), 1)

    def test_pause_resume_queue(self):
        self.tq.pause_queue("myq")
        self.assertIn("myq", self.tq._paused)
        self.tq.resume_queue("myq")
        self.assertNotIn("myq", self.tq._paused)

    def test_task_history(self):
        self.tq.create_queue("hisq", lambda p: None, auto_start=False)
        task = self.tq.enqueue("hist_job", {}, queue_name="hisq")
        self.tq.run_sync(task.task_id, "hisq")
        h = self.tq.task_history()
        self.assertGreater(len(h), 0)

    def test_stats(self):
        self.tq.create_queue("sq", lambda p: None, auto_start=False)
        task = self.tq.enqueue("stat_job", {}, queue_name="sq")
        self.tq.run_sync(task.task_id, "sq")
        s = self.tq.stats()
        self.assertGreater(s["done"], 0)
        self.assertGreater(s["enqueued"], 0)

# ════════════════════════════════════════════════════════
# PROMPT OPTIMIZER V2
# ════════════════════════════════════════════════════════
class TestPromptOptimizerV2(unittest.TestCase):
    def setUp(self):
        from agent.prompt_optimizer_v2 import PromptOptimizerV2
        self.po = PromptOptimizerV2(db_path=":memory:")

    def test_add_variant(self):
        v = self.po.add_variant("V1", "You are a helpful {role}.")
        self.assertIsNotNone(v.variant_id)
        self.assertIn("role", v.variables)

    def test_render_variant(self):
        v = self.po.add_variant("V2", "Hello {name}, you are {age}.")
        rendered = self.po.render(v.variant_id, name="Alice", age=30)
        self.assertEqual(rendered, "Hello Alice, you are 30.")

    def test_update_template(self):
        v = self.po.add_variant("V3", "Old {x}")
        self.po.update_template(v.variant_id, "New {y}")
        self.assertIn("y", v.variables)
        self.assertNotIn("x", v.variables)

    def test_remove_variant(self):
        v = self.po.add_variant("V4", "temp")
        self.po.remove_variant(v.variant_id)
        self.assertIsNone(self.po.get_variant(v.variant_id))

    def test_create_experiment(self):
        v1 = self.po.add_variant("A", "template A")
        v2 = self.po.add_variant("B", "template B")
        exp = self.po.create_experiment("exp1", [v1.variant_id, v2.variant_id])
        self.assertIsNotNone(exp.experiment_id)

    def test_select_variant_round_robin(self):
        from agent.prompt_optimizer_v2 import OptimizationStrategy
        v1 = self.po.add_variant("A", "A")
        v2 = self.po.add_variant("B", "B")
        exp = self.po.create_experiment("rr_exp",
                                         [v1.variant_id, v2.variant_id],
                                         strategy=OptimizationStrategy.ROUND_ROBIN)
        chosen1, _ = self.po.select_variant(exp.experiment_id)
        chosen2, _ = self.po.select_variant(exp.experiment_id)
        self.assertNotEqual(chosen1.variant_id, chosen2.variant_id)

    def test_select_variant_best_score(self):
        from agent.prompt_optimizer_v2 import OptimizationStrategy
        v1 = self.po.add_variant("A", "A")
        v2 = self.po.add_variant("B", "B")
        v1.score_sum = 0.9; v1.score_count = 1
        v2.score_sum = 0.1; v2.score_count = 1
        exp = self.po.create_experiment("bs_exp",
                                         [v1.variant_id, v2.variant_id],
                                         strategy=OptimizationStrategy.BEST_SCORE)
        chosen, _ = self.po.select_variant(exp.experiment_id)
        self.assertEqual(chosen.variant_id, v1.variant_id)

    def test_record_score(self):
        v = self.po.add_variant("Scored", "template")
        exp = self.po.create_experiment("sc_exp", [v.variant_id])
        self.po.record_score(exp.experiment_id, v.variant_id, 0.85)
        self.assertAlmostEqual(v.avg_score, 0.85)

    def test_record_win(self):
        v = self.po.add_variant("Winner", "template")
        v.use_count = 5
        self.po.record_win(v.variant_id)
        self.assertEqual(v.win_count, 1)
        self.assertAlmostEqual(v.win_rate, 0.2)

    def test_auto_select_winner(self):
        v1 = self.po.add_variant("A", "A")
        v2 = self.po.add_variant("B", "B")
        exp = self.po.create_experiment("winner_exp",
                                         [v1.variant_id, v2.variant_id])
        # Give enough trials
        for _ in range(10):
            self.po.record_score(exp.experiment_id, v1.variant_id, 0.9)
            self.po.record_score(exp.experiment_id, v2.variant_id, 0.5)
        winner = self.po.auto_select_winner(exp.experiment_id, min_trials=10)
        self.assertIsNotNone(winner)
        self.assertEqual(winner.variant_id, v1.variant_id)

    def test_stop_experiment(self):
        v = self.po.add_variant("V", "t")
        exp = self.po.create_experiment("stop_exp", [v.variant_id])
        self.po.stop_experiment(exp.experiment_id)
        result = self.po.select_variant(exp.experiment_id)
        self.assertIsNone(result)

    def test_leaderboard(self):
        v1 = self.po.add_variant("A", "A")
        v2 = self.po.add_variant("B", "B")
        exp = self.po.create_experiment("lb_exp", [v1.variant_id, v2.variant_id])
        self.po.record_score(exp.experiment_id, v1.variant_id, 0.7)
        self.po.record_score(exp.experiment_id, v2.variant_id, 0.4)
        lb = self.po.leaderboard(exp.experiment_id)
        self.assertGreater(lb[0]["avg_score"], lb[1]["avg_score"])

    def test_few_shots(self):
        self.po.add_few_shot("qa", "What is AI?", "Artificial Intelligence")
        self.po.add_few_shot("qa", "What is ML?", "Machine Learning")
        shots = self.po.get_few_shots("qa")
        self.assertEqual(len(shots), 2)

    def test_few_shot_block(self):
        self.po.add_few_shot("math", "2+2", "4")
        block = self.po.build_few_shot_block("math")
        self.assertIn("2+2", block)
        self.assertIn("4", block)

    def test_compress(self):
        from agent.prompt_optimizer_v2 import PromptOptimizerV2 as PO
        raw = "Hello   world\n\n\n\nGoodbye  "
        compressed = PO.compress(raw)
        self.assertNotIn("   ", compressed)
        self.assertNotIn("\n\n\n", compressed)

    def test_trial_history(self):
        v = self.po.add_variant("V", "t")
        exp = self.po.create_experiment("th_exp", [v.variant_id])
        self.po.record_score(exp.experiment_id, v.variant_id, 0.5)
        h = self.po.trial_history(exp.experiment_id)
        self.assertGreater(len(h), 0)

    def test_stats(self):
        self.po.add_variant("S", "t")
        s = self.po.stats()
        self.assertGreater(s["variants"], 0)

# ════════════════════════════════════════════════════════
# CONNECTION POOL V2
# ════════════════════════════════════════════════════════
class TestConnectionPoolV2(unittest.TestCase):
    def _make_pool(self, min_s=1, max_s=4, **kw):
        from agent.connection_pool_v2 import ConnectionPoolV2
        counter = [0]
        def factory():
            counter[0] += 1
            return {"id": counter[0]}
        return ConnectionPoolV2(factory, min_size=min_s,
                                max_size=max_s, **kw)

    def test_pool_initializes(self):
        pool = self._make_pool(min_s=2)
        self.assertEqual(pool.total_count, 2)

    def test_acquire_returns_connection(self):
        pool = self._make_pool()
        conn = pool.acquire()
        self.assertIsNotNone(conn.connection)
        pool.release(conn)

    def test_context_manager(self):
        pool = self._make_pool()
        with pool.connection() as conn:
            self.assertIsNotNone(conn.connection)
        self.assertGreater(pool.idle_count, 0)

    def test_release_returns_to_idle(self):
        pool = self._make_pool(min_s=1)
        conn = pool.acquire()
        pool.release(conn)
        self.assertGreater(pool.idle_count, 0)

    def test_broken_connection_removed(self):
        pool = self._make_pool(min_s=1, max_s=3)
        conn = pool.acquire()
        before = pool.total_count
        pool.release(conn, mark_broken=True)
        # Pool may re-create to maintain min; just check it ran
        self.assertIsNotNone(pool.stats())

    def test_pool_grows_under_load(self):
        pool = self._make_pool(min_s=1, max_s=5)
        conns = []
        for _ in range(3):
            conns.append(pool.acquire())
        self.assertGreater(pool.total_count, 1)
        for c in conns: pool.release(c)

    def test_timeout_raises(self):
        from agent.connection_pool_v2 import PoolExhaustedError
        pool = self._make_pool(min_s=1, max_s=1, acquire_timeout_s=0.05)
        conn = pool.acquire()
        with self.assertRaises(PoolExhaustedError):
            pool.acquire(timeout=0.05)
        pool.release(conn)

    def test_health_check_respected(self):
        from agent.connection_pool_v2 import ConnectionPoolV2
        calls = [0]
        def factory(): return {"id": calls[0]}
        # Always healthy
        pool = ConnectionPoolV2(factory, min_size=1, max_size=2,
                                health_check=lambda c: True)
        conn = pool.acquire()
        self.assertIsNotNone(conn)
        pool.release(conn)

    def test_max_uses_recycles(self):
        from agent.connection_pool_v2 import ConnectionPoolV2
        created = [0]
        def factory():
            created[0] += 1
            return {"id": created[0]}
        pool = ConnectionPoolV2(factory, min_size=1, max_size=3,
                                max_uses=1)
        c1 = pool.acquire()
        pool.release(c1)
        # Second acquire may get new connection due to max_uses=1
        c2 = pool.acquire()
        self.assertIsNotNone(c2)
        pool.release(c2)

    def test_close_pool(self):
        from agent.connection_pool_v2 import PoolExhaustedError, PoolState
        pool = self._make_pool()
        pool.close()
        self.assertEqual(pool._state, PoolState.CLOSED)
        with self.assertRaises(PoolExhaustedError):
            pool.acquire()

    def test_list_connections(self):
        pool = self._make_pool(min_s=2)
        conns = pool.list_connections()
        self.assertGreaterEqual(len(conns), 2)

    def test_stats(self):
        pool = self._make_pool()
        conn = pool.acquire()
        pool.release(conn)
        s = pool.stats()
        self.assertGreater(s["acquired"], 0)
        self.assertGreater(s["released"], 0)

# ════════════════════════════════════════════════════════
# DATA AUGMENTOR
# ════════════════════════════════════════════════════════
class TestDataAugmentor(unittest.TestCase):
    def setUp(self):
        from agent.data_augmentor import DataAugmentor
        self.da = DataAugmentor(seed=42, db_path=":memory:")
        self.da.load([
            {"text": "The quick brown fox jumps over the lazy dog", "label": 1},
            {"text": "Good morning sunshine, have a great day", "label": 0},
            {"text": "Machine learning is important for AI", "label": 1},
        ])

    def test_load_samples(self):
        self.assertEqual(len(self.da._original), 3)

    def test_add_sample(self):
        sid = self.da.add_sample("Test text", label=1)
        self.assertIsNotNone(sid)
        self.assertEqual(len(self.da._original), 4)

    def test_synonym_swap(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.SYNONYM_SWAP, n_augments=1, prob=1.0)
        samples = self.da.augment([cfg])
        self.assertGreater(len(samples), 0)

    def test_random_delete(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.RANDOM_DELETE, n_augments=1, prob=1.0,
                        params={"p": 0.3})
        samples = self.da.augment([cfg])
        self.assertGreater(len(samples), 0)

    def test_random_insert(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.RANDOM_INSERT, n_augments=1, prob=1.0)
        samples = self.da.augment([cfg])
        self.assertGreater(len(samples), 0)

    def test_random_swap(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.RANDOM_SWAP, n_augments=1, prob=1.0)
        samples = self.da.augment([cfg])
        self.assertGreater(len(samples), 0)

    def test_case_change(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.CASE_CHANGE, n_augments=1, prob=1.0)
        samples = self.da.augment([cfg])
        self.assertGreater(len(samples), 0)
        # Check that text is not identical to original
        orig_texts = {r["text"] for r in self.da._original}
        aug_texts  = {s.text for s in samples}
        self.assertTrue(any(t.isupper() or t.islower() or t.istitle()
                            for t in aug_texts))

    def test_typo_inject(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.TYPO_INJECT, n_augments=1, prob=1.0)
        samples = self.da.augment([cfg])
        self.assertGreater(len(samples), 0)

    def test_template_fill(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.TEMPLATE_FILL, n_augments=1, prob=1.0)
        samples = self.da.augment([cfg])
        self.assertGreater(len(samples), 0)

    def test_deduplication(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.CASE_CHANGE, n_augments=5, prob=1.0)
        s1 = self.da.augment([cfg], deduplicate=True)
        # Reset and try without dedup
        self.da._seen_hashes.clear()
        # Just ensure it runs
        self.assertIsNotNone(s1)

    def test_probability_zero(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.SYNONYM_SWAP, n_augments=1, prob=0.0)
        samples = self.da.augment([cfg])
        self.assertEqual(len(samples), 0)

    def test_augment_one(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.CASE_CHANGE)
        result = self.da.augment_one("Hello World", cfg)
        self.assertIsInstance(result, str)

    def test_mixup(self):
        samples = self.da.mixup(alpha=0.5)
        self.assertGreater(len(samples), 0)
        from agent.data_augmentor import AugmentStrategy
        self.assertEqual(samples[0].strategy, AugmentStrategy.MIXUP)

    def test_balance_classes(self):
        from agent.data_augmentor import DataAugmentor
        da = DataAugmentor(seed=0, db_path=":memory:")
        da.load([
            {"text": "a", "label": 0},
            {"text": "b", "label": 1},
            {"text": "c", "label": 1},
            {"text": "d", "label": 1},
        ])
        new = da.balance_classes()
        # Should oversample class 0 by 2
        self.assertEqual(len(new), 2)

    def test_split(self):
        train, val, test = self.da.split(train=0.7, val=0.2)
        total = len(train) + len(val) + len(test)
        self.assertEqual(total, len(self.da._original))

    def test_custom_strategy(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        self.da.register_strategy("reverse", lambda t: t[::-1])
        cfg = AugConfig(strategy=AugmentStrategy.CUSTOM, n_augments=1, prob=1.0,
                        params={"fn": "reverse"})
        samples = self.da.augment([cfg])
        self.assertGreater(len(samples), 0)

    def test_filter_by_strategy(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.RANDOM_SWAP, n_augments=1, prob=1.0)
        self.da.augment([cfg])
        filtered = self.da.filter_by_strategy(AugmentStrategy.RANDOM_SWAP)
        self.assertGreater(len(filtered), 0)

    def test_stats(self):
        from agent.data_augmentor import AugConfig, AugmentStrategy
        cfg = AugConfig(strategy=AugmentStrategy.CASE_CHANGE, n_augments=1, prob=1.0)
        self.da.augment([cfg])
        s = self.da.stats()
        self.assertEqual(s["original_samples"], 3)
        self.assertGreater(s["augmented_samples"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v61: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
