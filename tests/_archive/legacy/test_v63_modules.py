"""OMNI AGENT v63: GraphExecutor, ModelRegistryV2, ContextComposerV2, WebhookManagerV2"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# GRAPH EXECUTOR
# ════════════════════════════════════════════════════════
class TestGraphExecutor(unittest.TestCase):
    def setUp(self):
        from agent.graph_executor import GraphExecutor
        self.ge = GraphExecutor()

    def test_add_node(self):
        n = self.ge.add_node("a", lambda ins, ctx: 1)
        self.assertIsNotNone(n.node_id)

    def test_simple_run(self):
        from agent.graph_executor import NodeStatus
        n = self.ge.add_node("a", lambda ins, ctx: 42)
        run = self.ge.run()
        self.assertEqual(run.status, "done")
        self.assertEqual(run.node_results[n.node_id], 42)

    def test_dependent_nodes(self):
        from agent.graph_executor import NodeStatus
        a = self.ge.add_node("a", lambda ins, ctx: 10)
        b = self.ge.add_node("b", lambda ins, ctx: ins[a.node_id] * 2,
                              inputs=[a.node_id])
        run = self.ge.run()
        self.assertEqual(run.node_results[b.node_id], 20)

    def test_context_passed(self):
        n = self.ge.add_node("ctx_node", lambda ins, ctx: ctx["val"])
        run = self.ge.run(context={"val": 99})
        self.assertEqual(run.node_results[n.node_id], 99)

    def test_cycle_detection(self):
        self.assertTrue(not self.ge.detect_cycles())
        a = self.ge.add_node("a", lambda ins, ctx: 1)
        b = self.ge.add_node("b", lambda ins, ctx: 1, inputs=[a.node_id])
        # Manually create cycle
        a.inputs.append(b.node_id)
        b.outputs.append(a.node_id)
        self.assertTrue(self.ge.detect_cycles())

    def test_topo_sort_order(self):
        a = self.ge.add_node("a", lambda ins, ctx: 1)
        b = self.ge.add_node("b", lambda ins, ctx: 2, inputs=[a.node_id])
        c = self.ge.add_node("c", lambda ins, ctx: 3, inputs=[b.node_id])
        order = self.ge._topo_sort()
        self.assertLess(order.index(a.node_id), order.index(b.node_id))
        self.assertLess(order.index(b.node_id), order.index(c.node_id))

    def test_failed_node_skips_downstream(self):
        from agent.graph_executor import NodeStatus
        a = self.ge.add_node("a", lambda ins, ctx: (_ for _ in ()).throw(RuntimeError("err")))
        b = self.ge.add_node("b", lambda ins, ctx: 99, inputs=[a.node_id])
        run = self.ge.run()
        self.assertEqual(self.ge._nodes[b.node_id].status, NodeStatus.SKIPPED)

    def test_skip_on_fail(self):
        from agent.graph_executor import NodeStatus
        a = self.ge.add_node("a", lambda ins, ctx: (_ for _ in ()).throw(RuntimeError("e")))
        b = self.ge.add_node("b", lambda ins, ctx: "ok",
                              inputs=[a.node_id], skip_on_fail=True)
        run = self.ge.run()
        self.assertEqual(run.node_results[b.node_id], "ok")

    def test_node_retry(self):
        calls = [0]
        def flaky(ins, ctx):
            calls[0] += 1
            if calls[0] < 2: raise RuntimeError("retry")
            return "done"
        n = self.ge.add_node("retry", flaky, max_retries=2)
        run = self.ge.run()
        self.assertEqual(run.node_results[n.node_id], "done")

    def test_caching(self):
        calls = [0]
        def counter(ins, ctx):
            calls[0] += 1
            return calls[0]
        n = self.ge.add_node("cached", counter)
        self.ge.run()
        c1 = calls[0]
        self.ge.run()  # second run should use cache
        self.assertEqual(calls[0], c1)  # not called again
        self.assertGreater(self.ge.cache_size(), 0)

    def test_clear_cache(self):
        n = self.ge.add_node("c", lambda ins, ctx: 1)
        self.ge.run()
        self.ge.clear_cache()
        self.assertEqual(self.ge.cache_size(), 0)

    def test_parallel_mode(self):
        from agent.graph_executor import ExecMode
        a = self.ge.add_node("a", lambda ins, ctx: 1)
        b = self.ge.add_node("b", lambda ins, ctx: 2)
        run = self.ge.run(mode=ExecMode.PARALLEL)
        self.assertIn(a.node_id, run.node_results)
        self.assertIn(b.node_id, run.node_results)

    def test_lazy_mode_only_needed(self):
        from agent.graph_executor import ExecMode, NodeStatus
        a = self.ge.add_node("a", lambda ins, ctx: 1)
        b = self.ge.add_node("b", lambda ins, ctx: 2, inputs=[a.node_id])
        c = self.ge.add_node("c", lambda ins, ctx: 3)  # not needed
        run = self.ge.run(mode=ExecMode.LAZY, output_ids=[b.node_id])
        self.assertIn(b.node_id, run.node_results)
        # c should not be run (it's not in ancestors of b)
        self.assertEqual(self.ge._nodes[c.node_id].status, NodeStatus.PENDING)

    def test_critical_path(self):
        a = self.ge.add_node("a", lambda ins, ctx: 1)
        b = self.ge.add_node("b", lambda ins, ctx: 2, inputs=[a.node_id])
        c = self.ge.add_node("c", lambda ins, ctx: 3, inputs=[b.node_id])
        path = self.ge.critical_path()
        self.assertEqual(path[0], a.node_id)
        self.assertEqual(path[-1], c.node_id)

    def test_hooks_called(self):
        pre, post = [], []
        self.ge.on_node_start(lambda n: pre.append(n.name))
        self.ge.on_node_done(lambda n: post.append(n.status.value))
        self.ge.add_node("h", lambda ins, ctx: 1)
        self.ge.run()
        self.assertGreater(len(pre), 0)
        self.assertGreater(len(post), 0)

    def test_run_history(self):
        self.ge.add_node("x", lambda ins, ctx: 1)
        self.ge.run()
        self.ge.run()
        h = self.ge.run_history()
        self.assertGreaterEqual(len(h), 2)

    def test_stats(self):
        self.ge.add_node("s", lambda ins, ctx: 1)
        self.ge.run()
        s = self.ge.stats()
        self.assertGreater(s["nodes"], 0)
        self.assertGreater(s["runs"], 0)

# ════════════════════════════════════════════════════════
# MODEL REGISTRY V2
# ════════════════════════════════════════════════════════
class TestModelRegistryV2(unittest.TestCase):
    def setUp(self):
        from agent.model_registry_v2 import ModelRegistryV2
        self.mr = ModelRegistryV2(db_path=":memory:")

    def test_register_model(self):
        m = self.mr.register_model("classifier", task_type="classification")
        self.assertIsNotNone(m.model_id)

    def test_find_model(self):
        self.mr.register_model("mymodel")
        m = self.mr.find_model("mymodel")
        self.assertIsNotNone(m)

    def test_log_version(self):
        from agent.model_registry_v2 import ModelFramework
        m = self.mr.register_model("m1")
        v = self.mr.log_version(m.model_id, "1.0.0",
                                 framework=ModelFramework.SKLEARN,
                                 metrics={"accuracy": 0.95})
        self.assertEqual(v.version, "1.0.0")
        self.assertAlmostEqual(v.metrics["accuracy"], 0.95)

    def test_get_latest(self):
        m = self.mr.register_model("m2")
        self.mr.log_version(m.model_id, "1.0")
        v2 = self.mr.log_version(m.model_id, "2.0")
        latest = self.mr.get_latest(m.model_id)
        self.assertEqual(latest.version_id, v2.version_id)

    def test_stage_transition(self):
        from agent.model_registry_v2 import ModelStage
        m = self.mr.register_model("m3")
        v = self.mr.log_version(m.model_id, "1.0")
        ok = self.mr.transition(v.version_id, ModelStage.PRODUCTION)
        self.assertTrue(ok)
        self.assertEqual(v.stage, ModelStage.PRODUCTION)

    def test_promote_to_production(self):
        from agent.model_registry_v2 import ModelStage
        m = self.mr.register_model("m4")
        v = self.mr.log_version(m.model_id, "1.0")
        self.mr.promote_to_production(v.version_id)
        self.assertEqual(v.stage, ModelStage.PRODUCTION)

    def test_archive(self):
        from agent.model_registry_v2 import ModelStage
        m = self.mr.register_model("m5")
        v = self.mr.log_version(m.model_id, "1.0")
        self.mr.archive(v.version_id)
        self.assertEqual(v.stage, ModelStage.ARCHIVED)

    def test_champion(self):
        m = self.mr.register_model("m6")
        v1 = self.mr.log_version(m.model_id, "1.0")
        v2 = self.mr.log_version(m.model_id, "2.0")
        self.mr.set_champion(v2.version_id)
        champ = self.mr.get_champion(m.model_id)
        self.assertEqual(champ.version_id, v2.version_id)
        self.assertFalse(v1.is_champion)

    def test_challenger(self):
        m = self.mr.register_model("m7")
        v = self.mr.log_version(m.model_id, "1.0")
        self.mr.set_challenger(v.version_id)
        self.assertTrue(v.is_challenger)

    def test_log_metric(self):
        m = self.mr.register_model("m8")
        v = self.mr.log_version(m.model_id, "1.0")
        self.mr.log_metric(v.version_id, "f1", 0.88)
        self.assertAlmostEqual(v.metrics["f1"], 0.88)

    def test_compare_versions(self):
        m = self.mr.register_model("m9")
        v1 = self.mr.log_version(m.model_id, "1.0", metrics={"acc": 0.80})
        v2 = self.mr.log_version(m.model_id, "2.0", metrics={"acc": 0.90})
        ranked = self.mr.compare_versions([v1.version_id, v2.version_id], "acc")
        self.assertEqual(ranked[0][0], v2.version_id)

    def test_best_version(self):
        m = self.mr.register_model("m10")
        self.mr.log_version(m.model_id, "1.0", metrics={"acc": 0.80})
        v2 = self.mr.log_version(m.model_id, "2.0", metrics={"acc": 0.95})
        best = self.mr.best_version(m.model_id, "acc")
        self.assertEqual(best.version_id, v2.version_id)

    def test_lineage(self):
        m  = self.mr.register_model("m11")
        v1 = self.mr.log_version(m.model_id, "1.0")
        v2 = self.mr.log_version(m.model_id, "2.0",
                                  parent_version_id=v1.version_id)
        chain = self.mr.lineage_chain(v2.version_id)
        self.assertEqual(chain[0], v1.version_id)
        self.assertEqual(chain[1], v2.version_id)

    def test_approval_hook_blocks(self):
        from agent.model_registry_v2 import ModelStage
        self.mr.add_approval_hook(lambda v, f, t: False)
        m = self.mr.register_model("m12")
        v = self.mr.log_version(m.model_id, "1.0")
        ok = self.mr.transition(v.version_id, ModelStage.PRODUCTION)
        self.assertFalse(ok)

    def test_transition_history(self):
        from agent.model_registry_v2 import ModelStage
        m = self.mr.register_model("m13")
        v = self.mr.log_version(m.model_id, "1.0")
        self.mr.transition(v.version_id, ModelStage.STAGING)
        h = self.mr.transition_history(v.version_id)
        self.assertGreater(len(h), 0)

    def test_list_models_by_task(self):
        self.mr.register_model("clf", task_type="classification")
        self.mr.register_model("reg", task_type="regression")
        results = self.mr.list_models(task_type="classification")
        self.assertEqual(len(results), 1)

    def test_stats(self):
        m = self.mr.register_model("m14")
        self.mr.log_version(m.model_id, "1.0")
        s = self.mr.stats()
        self.assertGreater(s["models"], 0)
        self.assertGreater(s["versions"], 0)

# ════════════════════════════════════════════════════════
# CONTEXT COMPOSER V2
# ════════════════════════════════════════════════════════
class TestContextComposerV2(unittest.TestCase):
    def setUp(self):
        from agent.context_composer_v2 import ContextComposerV2
        self.cc = ContextComposerV2(token_budget=200)

    def test_add_block(self):
        from agent.context_composer_v2 import ContextRole
        b = self.cc.add("Hello world", ContextRole.USER)
        self.assertIsNotNone(b)
        self.assertEqual(self.cc.block_count, 1)

    def test_add_system(self):
        from agent.context_composer_v2 import ContextRole
        b = self.cc.add_system("System prompt here")
        self.assertTrue(b.pinned)
        self.assertEqual(b.role, ContextRole.SYSTEM)

    def test_compose_basic(self):
        self.cc.add("Hello", __import__('agent.context_composer_v2', fromlist=['ContextRole']).ContextRole.USER)
        ctx = self.cc.compose()
        self.assertGreater(len(ctx.blocks), 0)

    def test_budget_enforcement(self):
        from agent.context_composer_v2 import ContextComposerV2, ContextRole, ContextPriority
        cc = ContextComposerV2(token_budget=20)
        cc.add("x" * 100, ContextRole.USER)  # ~25 tokens
        cc.add("y" * 100, ContextRole.USER)
        ctx = cc.compose()
        self.assertLessEqual(ctx.total_tokens, 20 + 5)  # small allowance

    def test_pinned_always_included(self):
        from agent.context_composer_v2 import ContextComposerV2, ContextRole
        cc = ContextComposerV2(token_budget=5)
        cc.add_system("Very important system prompt that must always be here")
        ctx = cc.compose()
        self.assertGreater(len(ctx.blocks), 0)

    def test_drop_low_priority(self):
        from agent.context_composer_v2 import (ContextComposerV2, ContextRole,
                                                ContextPriority, TruncationStrategy)
        cc = ContextComposerV2(token_budget=50,
                                truncation_strategy=TruncationStrategy.DROP_LOW_PRIORITY)
        cc.add("important " * 5, ContextRole.USER, ContextPriority.HIGH)
        cc.add("optional " * 20, ContextRole.USER, ContextPriority.OPTIONAL)
        ctx = cc.compose()
        self.assertGreater(ctx.dropped_count, 0)

    def test_trim_strategy(self):
        from agent.context_composer_v2 import (ContextComposerV2, ContextRole,
                                                TruncationStrategy)
        cc = ContextComposerV2(token_budget=10,
                                truncation_strategy=TruncationStrategy.TRIM_CONTENT)
        cc.add("a" * 200, ContextRole.USER)
        ctx = cc.compose()
        self.assertLessEqual(ctx.total_tokens, 15)

    def test_sliding_window(self):
        from agent.context_composer_v2 import (ContextComposerV2, ContextRole,
                                                TruncationStrategy)
        cc = ContextComposerV2(token_budget=20,
                                truncation_strategy=TruncationStrategy.SLIDING_WINDOW)
        for i in range(5):
            cc.add(f"message {i} here", ContextRole.USER)
        ctx = cc.compose()
        self.assertLessEqual(ctx.total_tokens, 25)

    def test_remove_block(self):
        b = self.cc.add("remove me", __import__('agent.context_composer_v2', fromlist=['ContextRole']).ContextRole.USER)
        self.cc.remove(b.block_id)
        self.assertEqual(self.cc.block_count, 0)

    def test_update_block(self):
        b = self.cc.add("old", __import__('agent.context_composer_v2', fromlist=['ContextRole']).ContextRole.USER)
        self.cc.update(b.block_id, "new content")
        self.assertEqual(b.content, "new content")

    def test_ttl_expiry(self):
        self.cc.add("expires", __import__('agent.context_composer_v2', fromlist=['ContextRole']).ContextRole.USER,
                    ttl_s=0.01)
        time.sleep(0.02)
        ctx = self.cc.compose()
        self.assertEqual(len([b for b in ctx.blocks if b.content == "expires"]), 0)

    def test_deduplication(self):
        CR = __import__('agent.context_composer_v2', fromlist=['ContextRole']).ContextRole
        self.cc.add("same content", CR.USER, deduplicate=True)
        b2 = self.cc.add("same content", CR.USER, deduplicate=True)
        self.assertIsNone(b2)
        self.assertEqual(self.cc.block_count, 1)

    def test_template_render(self):
        CR = __import__('agent.context_composer_v2', fromlist=['ContextRole']).ContextRole
        b = self.cc.add("Hello {name}, you are {age}.", CR.SYSTEM)
        rendered = self.cc.render_template(b.block_id, name="Alice", age=30)
        self.assertEqual(rendered, "Hello Alice, you are 30.")

    def test_snapshot_restore(self):
        CR = __import__('agent.context_composer_v2', fromlist=['ContextRole']).ContextRole
        self.cc.add("block1", CR.USER)
        self.cc.snapshot("s1")
        self.cc.add("block2", CR.USER)
        self.cc.restore("s1")
        # After restore, order should match snapshot
        self.assertEqual(len(self.cc._order), 1)

    def test_to_openai_messages(self):
        CR = __import__('agent.context_composer_v2', fromlist=['ContextRole']).ContextRole
        self.cc.add_system("sys")
        self.cc.add("hello", CR.USER)
        ctx  = self.cc.compose()
        msgs = ctx.to_openai_messages()
        self.assertGreater(len(msgs), 0)
        self.assertTrue(all("role" in m and "content" in m for m in msgs))

    def test_merge(self):
        from agent.context_composer_v2 import ContextComposerV2, ContextRole
        cc2 = ContextComposerV2(token_budget=200)
        cc2.add("from cc2", ContextRole.USER)
        self.cc.merge(cc2)
        self.assertGreater(self.cc.block_count, 0)

    def test_stats(self):
        CR = __import__('agent.context_composer_v2', fromlist=['ContextRole']).ContextRole
        self.cc.add("hello", CR.USER)
        s = self.cc.stats()
        self.assertGreater(s["blocks"], 0)
        self.assertIn("by_role", s)

# ════════════════════════════════════════════════════════
# WEBHOOK MANAGER V2
# ════════════════════════════════════════════════════════
class TestWebhookManagerV2(unittest.TestCase):
    def setUp(self):
        from agent.webhook_manager_v2 import WebhookManagerV2
        self.wm = WebhookManagerV2(db_path=":memory:")

    def test_register_endpoint(self):
        ep = self.wm.register("https://example.com/hook")
        self.assertIsNotNone(ep.endpoint_id)

    def test_dispatch_delivers(self):
        from agent.webhook_manager_v2 import DeliveryStatus
        ep = self.wm.register("https://example.com/hook")
        deliveries = self.wm.dispatch("user.created", {"id": 1})
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].status, DeliveryStatus.SENT)

    def test_event_filter(self):
        ep1 = self.wm.register("https://a.com", events=["user.created"])
        ep2 = self.wm.register("https://b.com", events=["order.placed"])
        deliveries = self.wm.dispatch("user.created", {})
        endpoint_ids = [d.endpoint_id for d in deliveries]
        self.assertIn(ep1.endpoint_id, endpoint_ids)
        self.assertNotIn(ep2.endpoint_id, endpoint_ids)

    def test_wildcard_event(self):
        ep = self.wm.register("https://all.com", events=["*"])
        deliveries = self.wm.dispatch("anything", {})
        self.assertGreater(len(deliveries), 0)

    def test_no_events_matches_all(self):
        ep = self.wm.register("https://all2.com", events=[])
        deliveries = self.wm.dispatch("any_event", {})
        ids = [d.endpoint_id for d in deliveries]
        self.assertIn(ep.endpoint_id, ids)

    def test_signature_signing(self):
        sig = self.wm.sign_payload(b"payload", "secret123")
        self.assertTrue(self.wm.verify_signature(b"payload", sig, "secret123"))
        self.assertFalse(self.wm.verify_signature(b"payload", "badsig", "secret123"))

    def test_failed_delivery_increments_failures(self):
        from agent.webhook_manager_v2 import WebhookManagerV2, DeliveryStatus
        def fail_sender(url, payload, headers):
            return (500, "error")
        wm = WebhookManagerV2(db_path=":memory:", http_sender=fail_sender)
        ep = wm.register("https://fail.com", max_retries=0)
        wm.dispatch("ev", {})
        self.assertGreater(ep.consecutive_failures, 0)

    def test_auto_failing_status(self):
        from agent.webhook_manager_v2 import WebhookManagerV2, WebhookStatus
        def fail_sender(url, payload, headers): return (503, "err")
        wm = WebhookManagerV2(db_path=":memory:", http_sender=fail_sender)
        ep = wm.register("https://x.com", max_retries=0, failure_threshold=2)
        wm.dispatch("e", {})
        wm.dispatch("e", {})
        self.assertEqual(ep.status, WebhookStatus.FAILING)

    def test_enable_disable(self):
        from agent.webhook_manager_v2 import WebhookStatus
        ep = self.wm.register("https://x.com")
        self.wm.disable(ep.endpoint_id)
        self.assertEqual(ep.status, WebhookStatus.DISABLED)
        self.wm.enable(ep.endpoint_id)
        self.assertEqual(ep.status, WebhookStatus.ACTIVE)

    def test_disabled_not_dispatched(self):
        ep = self.wm.register("https://disabled.com")
        self.wm.disable(ep.endpoint_id)
        deliveries = self.wm.dispatch("ev", {})
        ids = [d.endpoint_id for d in deliveries]
        self.assertNotIn(ep.endpoint_id, ids)

    def test_subscribe_unsubscribe(self):
        ep = self.wm.register("https://sub.com", events=["ev1"])
        self.wm.subscribe(ep.endpoint_id, "ev2")
        self.assertIn("ev2", ep.events)
        self.wm.unsubscribe(ep.endpoint_id, "ev2")
        self.assertNotIn("ev2", ep.events)

    def test_unregister(self):
        ep = self.wm.register("https://del.com")
        self.wm.unregister(ep.endpoint_id)
        self.assertIsNone(self.wm.get_endpoint(ep.endpoint_id))

    def test_delivery_log(self):
        ep = self.wm.register("https://log.com")
        self.wm.dispatch("logged_event", {"data": 1})
        log = self.wm.delivery_log(ep.endpoint_id)
        self.assertGreater(len(log), 0)

    def test_list_endpoints_by_event(self):
        self.wm.register("https://a.com", events=["payment"])
        self.wm.register("https://b.com", events=["shipment"])
        eps = self.wm.list_endpoints(event="payment")
        self.assertEqual(len(eps), 1)

    def test_health(self):
        self.wm.register("https://h.com")
        h = self.wm.health()
        self.assertGreater(h["active"], 0)

    def test_stats(self):
        ep = self.wm.register("https://s.com")
        self.wm.dispatch("ev", {})
        s = self.wm.stats()
        self.assertGreater(s["sent"], 0)
        self.assertGreater(s["deliveries"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v63: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
