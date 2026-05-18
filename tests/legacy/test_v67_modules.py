"""OMNI AGENT v67: QueryOptimizerV2, TaskDependencyV2, MultiLevelCacheV2, PromptChainV2"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# QUERY OPTIMIZER V2
# ════════════════════════════════════════════════════════
class TestQueryOptimizerV2(unittest.TestCase):
    def setUp(self):
        from agent.query_optimizer_v2 import QueryOptimizerV2
        self.qo = QueryOptimizerV2(db_path=":memory:", slow_query_ms=1.0)
        self.qo.register_table("users", row_count=10000)
        self.qo.register_table("orders", row_count=50000)

    def test_plan_full_scan(self):
        from agent.query_optimizer_v2 import PlanType
        p = self.qo.plan("SELECT * FROM users")
        self.assertEqual(p.plan_type, PlanType.FULL_SCAN)

    def test_plan_index_scan(self):
        from agent.query_optimizer_v2 import PlanType
        self.qo.register_index("users", ["email"], is_unique=True)
        p = self.qo.plan("SELECT * FROM users WHERE email = 'test@test.com'")
        self.assertEqual(p.plan_type, PlanType.INDEX_SCAN)

    def test_cost_lower_with_index(self):
        p1 = self.qo.plan("SELECT * FROM orders WHERE status = 'done'")
        self.qo.register_index("orders", ["status"])
        self.qo.invalidate_cache()
        p2 = self.qo.plan("SELECT * FROM orders WHERE status = 'done'")
        self.assertLess(p2.estimated_cost, p1.estimated_cost)

    def test_join_plan(self):
        from agent.query_optimizer_v2 import PlanType
        p = self.qo.plan("SELECT * FROM users JOIN orders ON users.id = orders.user_id")
        self.assertEqual(p.plan_type, PlanType.HASH_JOIN)

    def test_plan_cache_hit(self):
        self.qo.plan("SELECT * FROM users")
        p = self.qo.plan("SELECT * FROM users")
        self.assertTrue(p.from_cache)

    def test_cache_invalidate_by_table(self):
        self.qo.plan("SELECT * FROM users")
        self.qo.invalidate_cache("users")
        p = self.qo.plan("SELECT * FROM users")
        self.assertFalse(p.from_cache)

    def test_fingerprint_normalizes(self):
        fp1 = self.qo.fingerprint("SELECT * FROM users WHERE id = 42")
        fp2 = self.qo.fingerprint("SELECT * FROM users WHERE id = 99")
        self.assertEqual(fp1, fp2)

    def test_force_index_hint(self):
        from agent.query_optimizer_v2 import PlanType
        ix = self.qo.register_index("users", ["name"])
        p  = self.qo.plan("SELECT * FROM users WHERE name = 'Alice'",
                            force_index=ix.index_id)
        self.assertEqual(p.plan_type, PlanType.INDEX_SCAN)
        self.assertIn(ix.index_id, p.used_indexes)

    def test_rewrite_rule(self):
        self.qo.add_rewrite_rule(
            lambda q: q.replace("SELECT *", "SELECT id,name"))
        p = self.qo.plan("SELECT * FROM users")
        # After rewrite it should still plan fine
        self.assertIsNotNone(p)

    def test_slow_query_logged(self):
        p = self.qo.plan("SELECT * FROM orders")
        self.qo.record_execution(p.plan_id, actual_rows=100,
                                  duration_ms=500.0)
        sq = self.qo.slow_queries()
        self.assertGreater(len(sq), 0)

    def test_explain(self):
        result = self.qo.explain("SELECT * FROM users")
        self.assertIn("plan_type", result)
        self.assertIn("estimated_cost", result)

    def test_update_stats(self):
        self.qo.update_stats("users", row_count=20000)
        ts = self.qo._tables["users"]
        self.assertEqual(ts.row_count, 20000)

    def test_stats(self):
        self.qo.plan("SELECT * FROM users")
        s = self.qo.stats()
        self.assertGreater(s["total_planned"], 0)
        self.assertGreater(s["tables"], 0)


# ════════════════════════════════════════════════════════
# TASK DEPENDENCY V2
# ════════════════════════════════════════════════════════
class TestTaskDependencyV2(unittest.TestCase):
    def setUp(self):
        from agent.task_dependency_v2 import TaskDependencyV2
        self.td = TaskDependencyV2(db_path=":memory:")

    def test_add_task(self):
        t = self.td.add_task("t1", lambda d, c: 1)
        self.assertIsNotNone(t.task_id)

    def test_sequential_exec(self):
        from agent.task_dependency_v2 import TaskState
        t = self.td.add_task("t1", lambda d, c: 42)
        run = self.td.run()
        self.assertEqual(run.status, "done")
        self.assertEqual(self.td._tasks[t.task_id].state, TaskState.DONE)

    def test_dep_result_passed(self):
        t1 = self.td.add_task("t1", lambda d, c: 10)
        t2 = self.td.add_task("t2",
                               lambda d, c: d[t1.task_id] * 2,
                               deps=[t1.task_id])
        run = self.td.run()
        self.assertEqual(run.results[t2.task_id], 20)

    def test_dep_order_respected(self):
        order = []
        t1 = self.td.add_task("t1", lambda d, c: order.append("t1") or 1)
        t2 = self.td.add_task("t2", lambda d, c: order.append("t2") or 2,
                               deps=[t1.task_id])
        self.td.run()
        self.assertEqual(order.index("t1"), 0)

    def test_failed_task_blocks_downstream(self):
        from agent.task_dependency_v2 import TaskState
        t1 = self.td.add_task("t1",
                lambda d, c: (_ for _ in ()).throw(RuntimeError("fail")))
        t2 = self.td.add_task("t2", lambda d, c: 99, deps=[t1.task_id])
        self.td.run()
        self.assertEqual(self.td._tasks[t2.task_id].state, TaskState.BLOCKED)

    def test_skip_on_dep_fail(self):
        from agent.task_dependency_v2 import TaskState
        t1 = self.td.add_task("t1",
                lambda d, c: (_ for _ in ()).throw(RuntimeError("fail")))
        t2 = self.td.add_task("t2", lambda d, c: 99,
                               deps=[t1.task_id], skip_on_dep_fail=True)
        self.td.run()
        self.assertEqual(self.td._tasks[t2.task_id].state, TaskState.SKIPPED)

    def test_retry(self):
        calls = [0]
        def flaky(d, c):
            calls[0] += 1
            if calls[0] < 2: raise RuntimeError("retry")
            return "ok"
        t = self.td.add_task("retry_t", flaky, max_retries=2)
        run = self.td.run()
        self.assertEqual(run.results[t.task_id], "ok")

    def test_parallel_exec(self):
        from agent.task_dependency_v2 import ExecStrategy, TaskState
        for i in range(4):
            self.td.add_task(f"pt{i}", lambda d, c, i=i: i * 2)
        run = self.td.run(strategy=ExecStrategy.PARALLEL)
        self.assertEqual(run.status, "done")

    def test_wave_exec(self):
        from agent.task_dependency_v2 import ExecStrategy
        t1 = self.td.add_task("w1", lambda d, c: 1)
        t2 = self.td.add_task("w2", lambda d, c: 2, deps=[t1.task_id])
        run = self.td.run(strategy=ExecStrategy.WAVE)
        self.assertEqual(run.status, "done")

    def test_cycle_detected(self):
        t1 = self.td.add_task("c1", lambda d, c: 1)
        t2 = self.td.add_task("c2", lambda d, c: 2, deps=[t1.task_id])
        t1.deps.append(t2.task_id)   # create cycle manually
        self.assertTrue(self.td.detect_cycle())

    def test_critical_path(self):
        t1 = self.td.add_task("cp1", lambda d, c: 1, weight=1.0)
        t2 = self.td.add_task("cp2", lambda d, c: 2,
                               deps=[t1.task_id], weight=2.0)
        t3 = self.td.add_task("cp3", lambda d, c: 3,
                               deps=[t2.task_id], weight=1.0)
        path = self.td.critical_path()
        self.assertEqual(path[0], t1.task_id)
        self.assertEqual(path[-1], t3.task_id)

    def test_hooks_called(self):
        pre = []; post = []
        self.td.on_task_start(lambda t: pre.append(t.name))
        self.td.on_task_done(lambda t: post.append(t.name))
        self.td.add_task("hooked", lambda d, c: 1)
        self.td.run()
        self.assertGreater(len(pre), 0)

    def test_context_passed(self):
        t = self.td.add_task("ctx_t",
                              lambda d, ctx: ctx.get("val", 0) * 2)
        run = self.td.run(context={"val": 7})
        self.assertEqual(run.results[t.task_id], 14)

    def test_stats(self):
        self.td.add_task("s", lambda d, c: 1)
        self.td.run()
        s = self.td.stats()
        self.assertGreater(s["tasks"], 0)
        self.assertGreater(s["runs"], 0)


# ════════════════════════════════════════════════════════
# MULTI-LEVEL CACHE V2
# ════════════════════════════════════════════════════════
class TestMultiLevelCacheV2(unittest.TestCase):
    def setUp(self):
        from agent.multi_level_cache_v2 import MultiLevelCacheV2, LevelConfig
        self.mlc = MultiLevelCacheV2(db_path=":memory:")

    def test_put_get_l1(self):
        from agent.multi_level_cache_v2 import CacheLevel
        self.mlc.put("k1", "value1", level=CacheLevel.L1)
        val, lvl = self.mlc.get("k1")
        self.assertEqual(val, "value1")
        self.assertEqual(lvl, 1)

    def test_put_get_l2(self):
        from agent.multi_level_cache_v2 import CacheLevel
        self.mlc.put("k2", "v2", level=CacheLevel.L2)
        val, lvl = self.mlc.get("k2")
        self.assertEqual(val, "v2")

    def test_put_get_l3(self):
        from agent.multi_level_cache_v2 import CacheLevel
        self.mlc.put("k3", {"x": 1}, level=CacheLevel.L3)
        val, lvl = self.mlc.get("k3")
        self.assertEqual(val, {"x": 1})
        self.assertEqual(lvl, 3)

    def test_miss(self):
        val, lvl = self.mlc.get("nonexistent")
        self.assertIsNone(val)
        self.assertEqual(lvl, 0)

    def test_ttl_expiry(self):
        from agent.multi_level_cache_v2 import CacheLevel
        self.mlc.put("ttl_k", "expires", ttl_s=0.01, level=CacheLevel.L1)
        time.sleep(0.02)
        val, _ = self.mlc.get("ttl_k")
        self.assertIsNone(val)

    def test_promotion_l2_to_l1(self):
        from agent.multi_level_cache_v2 import CacheLevel
        self.mlc.put("promo", "data", level=CacheLevel.L2)
        self.mlc.get("promo")   # triggers promotion
        # Second get should hit L1
        val, lvl = self.mlc.get("promo")
        self.assertEqual(lvl, 1)

    def test_delete(self):
        from agent.multi_level_cache_v2 import CacheLevel
        self.mlc.put("del_k", "del_v", level=CacheLevel.L1)
        self.mlc.delete("del_k")
        val, _ = self.mlc.get("del_k")
        self.assertIsNone(val)

    def test_invalidate_by_tag(self):
        from agent.multi_level_cache_v2 import CacheLevel
        self.mlc.put("tag_k1", "v1", tags=["ml"], level=CacheLevel.L1)
        self.mlc.put("tag_k2", "v2", tags=["db"], level=CacheLevel.L1)
        self.mlc.invalidate_by_tag("ml")
        val, _ = self.mlc.get("tag_k1")
        self.assertIsNone(val)
        val2, _ = self.mlc.get("tag_k2")
        self.assertEqual(val2, "v2")

    def test_clear_l1(self):
        from agent.multi_level_cache_v2 import CacheLevel
        self.mlc.put("cl1", "v", level=CacheLevel.L1)
        self.mlc.clear(level=CacheLevel.L1)
        self.assertEqual(self.mlc._l1.size, 0)

    def test_lru_eviction(self):
        from agent.multi_level_cache_v2 import MultiLevelCacheV2, LevelConfig, EvictionPolicy, CacheLevel
        cfg = LevelConfig(max_size=3, eviction_policy=EvictionPolicy.LRU)
        mlc = MultiLevelCacheV2(l1=cfg, db_path=":memory:")
        for i in range(4):
            mlc.put(f"key{i}", f"val{i}", level=CacheLevel.L1)
        self.assertLessEqual(mlc._l1.size, 3)

    def test_lfu_eviction(self):
        from agent.multi_level_cache_v2 import MultiLevelCacheV2, LevelConfig, EvictionPolicy, CacheLevel
        cfg = LevelConfig(max_size=3, eviction_policy=EvictionPolicy.LFU)
        mlc = MultiLevelCacheV2(l1=cfg, db_path=":memory:")
        for i in range(5):
            mlc.put(f"lfu{i}", i, level=CacheLevel.L1)
        self.assertLessEqual(mlc._l1.size, 3)

    def test_hit_rate_tracking(self):
        from agent.multi_level_cache_v2 import CacheLevel
        self.mlc.put("hr", "v", level=CacheLevel.L1)
        self.mlc.get("hr")   # hit
        self.mlc.get("missing")  # miss
        hr = self.mlc.hit_rate()
        self.assertGreater(hr, 0.0)
        self.assertLess(hr, 1.0)

    def test_level_stats(self):
        from agent.multi_level_cache_v2 import CacheLevel
        self.mlc.put("s1", "v", level=CacheLevel.L1)
        s = self.mlc.level_stats()
        self.assertIn("L1", s)
        self.assertIn("L2", s)
        self.assertIn("L3", s)


# ════════════════════════════════════════════════════════
# PROMPT CHAIN V2
# ════════════════════════════════════════════════════════
class TestPromptChainV2(unittest.TestCase):
    def setUp(self):
        from agent.prompt_chain_v2 import PromptChainV2
        self.pc = PromptChainV2(
            llm_fn=lambda prompt, cfg: f"response to: {prompt[:30]}",
            db_path=":memory:")

    def test_create_chain(self):
        c = self.pc.create_chain("test_chain")
        self.assertIsNotNone(c.chain_id)

    def test_add_node(self):
        c = self.pc.create_chain("c1")
        n = self.pc.add_node(c.chain_id, "n1", prompt_template="Hello {name}")
        self.assertIsNotNone(n.node_id)

    def test_single_node_run(self):
        c = self.pc.create_chain("single")
        self.pc.add_node(c.chain_id, "greet",
                          prompt_template="Hello {name}")
        run = self.pc.run(c.chain_id, variables={"name": "Alice"})
        self.assertEqual(run.status, "done")
        self.assertGreater(len(run.nodes_visited), 0)

    def test_multi_node_chain(self):
        c  = self.pc.create_chain("multi")
        n1 = self.pc.add_node(c.chain_id, "step1",
                               prompt_template="First: {topic}")
        n2 = self.pc.add_node(c.chain_id, "step2",
                               prompt_template="Second step",
                               node_id="n2")
        n1.next_node_id = n2.node_id
        run = self.pc.run(c.chain_id, variables={"topic": "AI"})
        self.assertEqual(len(run.nodes_visited), 2)

    def test_variable_substitution(self):
        c = self.pc.create_chain("vars")
        n = self.pc.add_node(c.chain_id, "v_node",
                              prompt_template="Name={name}, Age={age}")
        run = self.pc.run(c.chain_id, variables={"name": "Bob", "age": 30})
        # The LLM was called with the rendered prompt
        self.assertIn(n.node_id, run.outputs)

    def test_memory_store_and_inject(self):
        c  = self.pc.create_chain("mem")
        n1 = self.pc.add_node(c.chain_id, "store_n",
                               prompt_template="Compute {x}",
                               memory_key="result1")
        n2 = self.pc.add_node(c.chain_id, "use_n",
                               prompt_template="Use {result1}",
                               memory_inject=["result1"])
        n1.next_node_id = n2.node_id
        run = self.pc.run(c.chain_id, variables={"x": 42})
        self.assertIn("result1", run.memory)

    def test_branch_node(self):
        from agent.prompt_chain_v2 import ChainNodeType
        c  = self.pc.create_chain("branch")
        n1 = self.pc.add_node(c.chain_id, "router",
                               node_type=ChainNodeType.BRANCH,
                               prompt_template="Route this",
                               node_id="router_n")
        n2 = self.pc.add_node(c.chain_id, "path_a",
                               prompt_template="Path A", node_id="path_a_n")
        n3 = self.pc.add_node(c.chain_id, "path_b",
                               prompt_template="Path B", node_id="path_b_n")
        n1.branch_fn = lambda out: "path_a_n"  # always go to A
        run = self.pc.run(c.chain_id)
        self.assertIn("path_a_n", run.nodes_visited)

    def test_transform_node(self):
        from agent.prompt_chain_v2 import ChainNodeType
        c  = self.pc.create_chain("transform")
        n1 = self.pc.add_node(c.chain_id, "prompt_n",
                               prompt_template="Generate {x}")
        n2 = self.pc.add_node(c.chain_id, "upper_n",
                               node_type=ChainNodeType.TRANSFORM,
                               transform_fn=lambda out, ctx: str(out).upper())
        n1.next_node_id = n2.node_id
        run = self.pc.run(c.chain_id, variables={"x": "test"})
        self.assertEqual(run.outputs[n2.node_id],
                          run.outputs[n1.node_id].upper())  # type: ignore

    def test_output_format_bool(self):
        from agent.prompt_chain_v2 import OutputFormat, PromptChainV2
        pc = PromptChainV2(llm_fn=lambda p, c: "yes", db_path=":memory:")
        ch = pc.create_chain("bool_ch")
        pc.add_node(ch.chain_id, "bn",
                    prompt_template="Q?",
                    output_format=OutputFormat.BOOL)
        run = pc.run(ch.chain_id)
        self.assertTrue(run.final_output)

    def test_output_format_number(self):
        from agent.prompt_chain_v2 import OutputFormat, PromptChainV2
        pc = PromptChainV2(llm_fn=lambda p, c: "The answer is 42.", db_path=":memory:")
        ch = pc.create_chain("num_ch")
        pc.add_node(ch.chain_id, "nn",
                    prompt_template="?",
                    output_format=OutputFormat.NUMBER)
        run = pc.run(ch.chain_id)
        self.assertAlmostEqual(run.final_output, 42.0)

    def test_output_format_list(self):
        from agent.prompt_chain_v2 import OutputFormat, PromptChainV2
        pc = PromptChainV2(llm_fn=lambda p, c: "- item1\n- item2\n- item3",
                            db_path=":memory:")
        ch = pc.create_chain("list_ch")
        pc.add_node(ch.chain_id, "ln",
                    prompt_template="List",
                    output_format=OutputFormat.LIST)
        run = pc.run(ch.chain_id)
        self.assertIsInstance(run.final_output, list)
        self.assertGreater(len(run.final_output), 0)

    def test_no_llm_echo_fallback(self):
        from agent.prompt_chain_v2 import PromptChainV2
        pc  = PromptChainV2(db_path=":memory:")  # no LLM
        ch  = pc.create_chain("echo_ch")
        pc.add_node(ch.chain_id, "en", prompt_template="test prompt")
        run = pc.run(ch.chain_id)
        self.assertEqual(run.status, "done")
        self.assertIn("[echo]", run.final_output)

    def test_run_count(self):
        c = self.pc.create_chain("cnt")
        self.pc.add_node(c.chain_id, "nn", prompt_template="P")
        self.pc.run(c.chain_id)
        self.pc.run(c.chain_id)
        self.assertEqual(c.run_count, 2)

    def test_token_usage_tracked(self):
        c = self.pc.create_chain("tok")
        self.pc.add_node(c.chain_id, "tn", prompt_template="Hello world {x}")
        run = self.pc.run(c.chain_id, variables={"x": "test"})
        self.assertGreater(run.token_usage, 0)

    def test_stats(self):
        c = self.pc.create_chain("st")
        self.pc.add_node(c.chain_id, "sn", prompt_template="P")
        self.pc.run(c.chain_id)
        s = self.pc.stats()
        self.assertGreater(s["chains"], 0)
        self.assertGreater(s["runs"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v67: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
