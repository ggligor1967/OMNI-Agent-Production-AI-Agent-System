"""OMNI AGENT v66: ResourceMonitor, DocumentSummarizerV2, CacheWarmupManager, ToolComposerV2"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# RESOURCE MONITOR
# ════════════════════════════════════════════════════════
class TestResourceMonitor(unittest.TestCase):
    def setUp(self):
        from agent.resource_monitor import ResourceMonitor
        self.rm = ResourceMonitor(db_path=":memory:", poll_interval_s=999)

    def test_collect_now(self):
        samples = self.rm.collect_now()
        self.assertGreater(len(samples), 0)

    def test_cpu_metric_recorded(self):
        self.rm.collect_now()
        s = self.rm.latest("cpu_percent")
        self.assertIsNotNone(s)
        self.assertGreaterEqual(s.value, 0.0)

    def test_mem_metric_recorded(self):
        self.rm.collect_now()
        s = self.rm.latest("mem_percent")
        self.assertIsNotNone(s)

    def test_disk_metric_recorded(self):
        self.rm.collect_now()
        s = self.rm.latest("disk_percent")
        self.assertIsNotNone(s)

    def test_custom_collector(self):
        from agent.resource_monitor import ResourceSample, ResourceType
        self.rm.register_collector("custom",
            lambda: [ResourceSample(resource_type=ResourceType.CUSTOM,
                                    metric="my_metric", value=42.0)])
        self.rm.collect_now()
        s = self.rm.latest("my_metric")
        self.assertIsNotNone(s)
        self.assertAlmostEqual(s.value, 42.0)

    def test_alert_rule_fires(self):
        from agent.resource_monitor import ResourceSample, ResourceType, AlertSeverity
        fired = []
        self.rm.on_alert(lambda a: fired.append(a))
        self.rm.add_rule("my_metric", threshold=10.0, operator=">",
                          resource_type=ResourceType.CUSTOM, cooldown_s=0)
        self.rm.register_collector("cust2",
            lambda: [ResourceSample(resource_type=ResourceType.CUSTOM,
                                    metric="my_metric", value=99.0)])
        self.rm.collect_now()
        self.assertGreater(len(fired), 0)

    def test_alert_no_fire_below_threshold(self):
        from agent.resource_monitor import ResourceSample, ResourceType
        fired = []
        self.rm.on_alert(lambda a: fired.append(a))
        self.rm.add_rule("safe_metric", threshold=50.0, operator=">",
                          resource_type=ResourceType.CUSTOM, cooldown_s=0)
        self.rm.register_collector("safe",
            lambda: [ResourceSample(resource_type=ResourceType.CUSTOM,
                                    metric="safe_metric", value=10.0)])
        self.rm.collect_now()
        self.assertEqual(len(fired), 0)

    def test_alert_cooldown(self):
        from agent.resource_monitor import ResourceSample, ResourceType
        fired = []
        self.rm.on_alert(lambda a: fired.append(a))
        self.rm.add_rule("cd_metric", threshold=5.0, operator=">",
                          resource_type=ResourceType.CUSTOM, cooldown_s=999)
        self.rm.register_collector("cd",
            lambda: [ResourceSample(resource_type=ResourceType.CUSTOM,
                                    metric="cd_metric", value=10.0)])
        self.rm.collect_now()
        self.rm.collect_now()
        self.assertEqual(len(fired), 1)  # cooldown prevents 2nd

    def test_remove_rule(self):
        rule = self.rm.add_rule("r_metric", threshold=0.0)
        ok = self.rm.remove_rule(rule.rule_id)
        self.assertTrue(ok)
        self.assertNotIn(rule.rule_id, self.rm._rules)

    def test_disable_rule(self):
        from agent.resource_monitor import ResourceSample, ResourceType
        fired = []
        self.rm.on_alert(lambda a: fired.append(a))
        rule = self.rm.add_rule("dis_metric", threshold=5.0,
                                 operator=">",
                                 resource_type=ResourceType.CUSTOM,
                                 cooldown_s=0)
        self.rm.disable_rule(rule.rule_id)
        self.rm.register_collector("dis_c",
            lambda: [ResourceSample(resource_type=ResourceType.CUSTOM,
                                    metric="dis_metric", value=99.0)])
        self.rm.collect_now()
        self.assertEqual(len(fired), 0)

    def test_aggregate_avg(self):
        from agent.resource_monitor import ResourceSample, ResourceType
        for v in [10.0, 20.0, 30.0]:
            s = ResourceSample(resource_type=ResourceType.CUSTOM,
                               metric="agg_m", value=v)
            self.rm._record(s)
        avg = self.rm.aggregate("agg_m", fn="avg")
        self.assertAlmostEqual(avg, 20.0)

    def test_aggregate_p95(self):
        from agent.resource_monitor import ResourceSample, ResourceType
        for v in range(100):
            s = ResourceSample(resource_type=ResourceType.CUSTOM,
                               metric="p95_m", value=float(v))
            self.rm._record(s)
        p95 = self.rm.aggregate("p95_m", fn="p95")
        self.assertGreaterEqual(p95, 90.0)

    def test_acknowledge_alert(self):
        from agent.resource_monitor import ResourceSample, ResourceType
        self.rm.add_rule("ack_m", threshold=0.0, operator=">",
                          resource_type=ResourceType.CUSTOM, cooldown_s=0)
        self.rm.register_collector("ack",
            lambda: [ResourceSample(resource_type=ResourceType.CUSTOM,
                                    metric="ack_m", value=1.0)])
        self.rm.collect_now()
        alerts = self.rm.get_alerts()
        self.assertGreater(len(alerts), 0)
        ok = self.rm.acknowledge(alerts[0]["alert_id"])
        self.assertTrue(ok)

    def test_snapshot(self):
        self.rm.collect_now()
        snap = self.rm.snapshot()
        self.assertIsInstance(snap, dict)
        self.assertGreater(len(snap), 0)

    def test_stats(self):
        self.rm.collect_now()
        s = self.rm.stats()
        self.assertGreater(s["total_samples"], 0)
        self.assertGreater(s["collectors"], 0)

# ════════════════════════════════════════════════════════
# DOCUMENT SUMMARIZER V2
# ════════════════════════════════════════════════════════
TEXT = (
    "Python is a versatile programming language. "
    "It supports multiple paradigms including procedural, object-oriented, and functional. "
    "Python has a large standard library. "
    "It is widely used in data science, web development, and automation. "
    "Python's syntax is clean and readable. "
    "Many companies use Python for machine learning. "
    "Python was created by Guido van Rossum in 1991. "
    "It is open source and free to use."
)

class TestDocumentSummarizerV2(unittest.TestCase):
    def setUp(self):
        from agent.document_summarizer_v2 import DocumentSummarizerV2
        self.ds = DocumentSummarizerV2(db_path=":memory:")

    def test_extractive_summary(self):
        from agent.document_summarizer_v2 import SummaryConfig, SummaryStrategy
        cfg = SummaryConfig(strategy=SummaryStrategy.EXTRACTIVE, max_sentences=3)
        res = self.ds.summarize(TEXT, cfg)
        self.assertGreater(len(res.summary), 0)
        self.assertGreater(len(res.key_sentences), 0)

    def test_headline_summary(self):
        from agent.document_summarizer_v2 import SummaryConfig, SummaryStrategy
        cfg = SummaryConfig(strategy=SummaryStrategy.HEADLINE)
        res = self.ds.summarize(TEXT, cfg)
        # Headline = single sentence
        self.assertNotIn("\n", res.summary.strip())

    def test_bullet_summary(self):
        from agent.document_summarizer_v2 import SummaryConfig, SummaryStrategy
        cfg = SummaryConfig(strategy=SummaryStrategy.BULLET, max_sentences=3)
        res = self.ds.summarize(TEXT, cfg)
        self.assertTrue(res.summary.startswith("•"))
        self.assertGreater(len(res.bullet_points), 0)

    def test_hierarchical_summary(self):
        from agent.document_summarizer_v2 import SummaryConfig, SummaryStrategy, ChunkStrategy
        cfg = SummaryConfig(strategy=SummaryStrategy.HIERARCHICAL,
                             chunk_strategy=ChunkStrategy.SENTENCE,
                             chunk_size=2, max_sentences=2)
        res = self.ds.summarize(TEXT, cfg)
        self.assertGreater(res.chunk_count, 0)

    def test_abstractive_with_llm(self):
        from agent.document_summarizer_v2 import DocumentSummarizerV2, SummaryConfig, SummaryStrategy
        ds = DocumentSummarizerV2(llm_fn=lambda p: "LLM summary", db_path=":memory:")
        cfg = SummaryConfig(strategy=SummaryStrategy.ABSTRACTIVE)
        res = ds.summarize(TEXT, cfg)
        self.assertEqual(res.summary, "LLM summary")

    def test_abstractive_fallback(self):
        from agent.document_summarizer_v2 import SummaryConfig, SummaryStrategy
        cfg = SummaryConfig(strategy=SummaryStrategy.ABSTRACTIVE)
        res = self.ds.summarize(TEXT, cfg)  # no llm_fn → extractive fallback
        self.assertGreater(len(res.summary), 0)

    def test_compression_ratio(self):
        from agent.document_summarizer_v2 import SummaryConfig, SummaryStrategy
        cfg = SummaryConfig(strategy=SummaryStrategy.EXTRACTIVE, max_sentences=2)
        res = self.ds.summarize(TEXT, cfg)
        self.assertGreater(res.compression_ratio, 0.0)
        self.assertLess(res.compression_ratio, 1.0)

    def test_caching(self):
        from agent.document_summarizer_v2 import SummaryConfig
        cfg = SummaryConfig()
        self.ds.summarize(TEXT, cfg)
        self.ds.summarize(TEXT, cfg)  # second hit
        self.assertGreater(len(self.ds._cache), 0)

    def test_multi_document(self):
        texts = [TEXT, "Machine learning is a subset of AI. Neural networks are useful."]
        res   = self.ds.summarize_multi(texts)
        self.assertGreater(len(res.summary), 0)

    def test_template(self):
        from agent.document_summarizer_v2 import SummaryConfig, SummaryStrategy
        cfg = SummaryConfig(strategy=SummaryStrategy.BULLET, max_sentences=2)
        self.ds.register_template("brief", cfg)
        res = self.ds.summarize_with_template(TEXT, "brief")
        self.assertGreater(len(res.summary), 0)

    def test_history(self):
        self.ds.summarize(TEXT)
        h = self.ds.history()
        self.assertGreater(len(h), 0)

    def test_stats(self):
        self.ds.summarize(TEXT)
        s = self.ds.stats()
        self.assertGreater(s["runs"], 0)

# ════════════════════════════════════════════════════════
# CACHE WARMUP MANAGER
# ════════════════════════════════════════════════════════
class TestCacheWarmupManager(unittest.TestCase):
    def setUp(self):
        from agent.cache_warmup_manager import CacheWarmupManager
        self.cw = CacheWarmupManager(db_path=":memory:", max_workers=2)

    def test_register_and_warm(self):
        from agent.cache_warmup_manager import WarmupStatus
        e = self.cw.register("k1", lambda: 42)
        ok = self.cw.warm(e.entry_id)
        self.assertTrue(ok)
        self.assertEqual(e.status, WarmupStatus.DONE)
        self.assertEqual(e.value, 42)

    def test_get_value(self):
        self.cw.register("k2", lambda: "cached_value")
        self.cw.find("k2")
        # warm first
        e = self.cw.find("k2")
        self.cw.warm(e.entry_id)
        val = self.cw.get("k2")
        self.assertEqual(val, "cached_value")

    def test_get_or_load(self):
        self.cw.register("k3", lambda: 99)
        val = self.cw.get_or_load("k3")
        self.assertEqual(val, 99)

    def test_ttl_expiry(self):
        from agent.cache_warmup_manager import WarmupStatus
        e = self.cw.register("k_ttl", lambda: "exp", ttl_s=0.01)
        self.cw.warm(e.entry_id)
        time.sleep(0.02)
        self.assertTrue(e.is_expired)

    def test_warm_again_after_expiry(self):
        calls = [0]
        def loader():
            calls[0] += 1
            return calls[0]
        e = self.cw.register("k_re", loader, ttl_s=0.01)
        self.cw.warm(e.entry_id)
        time.sleep(0.02)
        self.cw.warm(e.entry_id, force=True)
        self.assertEqual(e.load_count, 2)

    def test_invalidate(self):
        from agent.cache_warmup_manager import WarmupStatus
        e = self.cw.register("k_inv", lambda: 1)
        self.cw.warm(e.entry_id)
        self.cw.invalidate("k_inv")
        self.assertEqual(e.status, WarmupStatus.PENDING)

    def test_evict_expired(self):
        e = self.cw.register("k_evict", lambda: 1, ttl_s=0.01)
        self.cw.warm(e.entry_id)
        time.sleep(0.02)
        evicted = self.cw.evict_expired()
        self.assertGreater(evicted, 0)

    def test_warm_all(self):
        for i in range(4):
            self.cw.register(f"bulk_{i}", lambda i=i: i * 10)
        run = self.cw.warm_all()
        self.assertEqual(run.loaded, 4)

    def test_warm_by_tag(self):
        self.cw.register("tagged1", lambda: 1, tags=["ml"])
        self.cw.register("tagged2", lambda: 2, tags=["ml"])
        self.cw.register("other",   lambda: 3, tags=["db"])
        run = self.cw.warm_all(tag="ml")
        self.assertEqual(run.loaded, 2)

    def test_priority_ordering(self):
        from agent.cache_warmup_manager import WarmupPriority
        order = []
        for p, name in [(WarmupPriority.LOW, "low"),
                        (WarmupPriority.CRITICAL, "crit"),
                        (WarmupPriority.NORMAL, "norm")]:
            self.cw.register(name, lambda n=name: order.append(n) or n, priority=p)
        self.cw.warm_all()
        self.assertEqual(order[0], "crit")

    def test_hit_miss_tracking(self):
        e = self.cw.register("hm", lambda: 1)
        self.cw.warm(e.entry_id)       # warm (miss)
        self.cw.warm(e.entry_id)       # already done (hit)
        self.assertGreater(e.hit_count + e.miss_count, 0)

    def test_failed_loader(self):
        from agent.cache_warmup_manager import WarmupStatus
        e = self.cw.register("fail_k",
                              lambda: (_ for _ in ()).throw(RuntimeError("oops")))
        ok = self.cw.warm(e.entry_id)
        self.assertFalse(ok)
        self.assertEqual(e.status, WarmupStatus.FAILED)

    def test_unregister(self):
        e = self.cw.register("del_k", lambda: 1)
        ok = self.cw.unregister(e.entry_id)
        self.assertTrue(ok)
        self.assertIsNone(self.cw.find("del_k"))

    def test_stats(self):
        self.cw.register("s1", lambda: 1)
        self.cw.warm_all()
        s = self.cw.stats()
        self.assertGreater(s["total"], 0)
        self.assertGreater(s["done"], 0)

# ════════════════════════════════════════════════════════
# TOOL COMPOSER V2
# ════════════════════════════════════════════════════════
class TestToolComposerV2(unittest.TestCase):
    def setUp(self):
        from agent.tool_composer_v2 import ToolComposerV2
        self.tc = ToolComposerV2(db_path=":memory:")

    def test_register_tool(self):
        t = self.tc.register_tool("adder", lambda x: x + 1)
        self.assertIsNotNone(t.tool_id)

    def test_invoke_tool(self):
        from agent.tool_composer_v2 import InvocationStatus
        t   = self.tc.register_tool("doubler", lambda x: x * 2)
        rec = self.tc.invoke(t.tool_id, 5)
        self.assertEqual(rec.status, InvocationStatus.SUCCESS)
        self.assertEqual(rec.output_data, 10)

    def test_tool_call_count(self):
        t = self.tc.register_tool("counter", lambda x: x)
        self.tc.invoke(t.tool_id, 1)
        self.tc.invoke(t.tool_id, 2)
        self.assertEqual(t.call_count, 2)

    def test_invoke_missing_tool(self):
        from agent.tool_composer_v2 import InvocationStatus
        rec = self.tc.invoke("nonexistent", "data")
        self.assertEqual(rec.status, InvocationStatus.FAILED)

    def test_disabled_tool(self):
        from agent.tool_composer_v2 import InvocationStatus
        t = self.tc.register_tool("disabled_t", lambda x: x)
        self.tc.disable_tool(t.tool_id)
        rec = self.tc.invoke(t.tool_id, 1)
        self.assertEqual(rec.status, InvocationStatus.FAILED)

    def test_chain_sequential(self):
        t1 = self.tc.register_tool("add1", lambda x: x + 1)
        t2 = self.tc.register_tool("mul2", lambda x: x * 2)
        c  = self.tc.create_chain("simple")
        self.tc.add_chain_step(c.chain_id, t1.tool_id)
        self.tc.add_chain_step(c.chain_id, t2.tool_id)
        result = self.tc.run_chain(c.chain_id, 3)
        self.assertEqual(result["final"], 8)   # (3+1)*2

    def test_chain_parallel_steps(self):
        t1 = self.tc.register_tool("p1", lambda x: x * 2)
        t2 = self.tc.register_tool("p2", lambda x: x * 3)
        c  = self.tc.create_chain("par")
        self.tc.add_chain_step(c.chain_id, t1.tool_id, parallel_group="g1")
        self.tc.add_chain_step(c.chain_id, t2.tool_id, parallel_group="g1")
        result = self.tc.run_chain(c.chain_id, 4)
        self.assertIn("final", result)

    def test_chain_input_map(self):
        t = self.tc.register_tool("echo", lambda x: x["value"] * 2)
        c = self.tc.create_chain("mapped")
        self.tc.add_chain_step(c.chain_id, t.tool_id,
                                input_map={"value": "$prev"})
        result = self.tc.run_chain(c.chain_id, 5)
        self.assertEqual(result["final"], 10)

    def test_chain_conditional_step(self):
        t = self.tc.register_tool("cond_t", lambda x: x * 99)
        c = self.tc.create_chain("cond_chain")
        self.tc.add_chain_step(c.chain_id, t.tool_id,
                                condition=lambda x, ctx: x > 100)
        result = self.tc.run_chain(c.chain_id, 5)
        self.assertEqual(result["final"], 5)  # condition false, skipped

    def test_chain_error_skip(self):
        t1 = self.tc.register_tool("err_t",
                lambda x: (_ for _ in ()).throw(RuntimeError("err")))
        t2 = self.tc.register_tool("ok_t2", lambda x: x + 10)
        c  = self.tc.create_chain("err_skip")
        self.tc.add_chain_step(c.chain_id, t1.tool_id, on_error="skip")
        self.tc.add_chain_step(c.chain_id, t2.tool_id)
        result = self.tc.run_chain(c.chain_id, 5)
        self.assertEqual(result["final"], 15)

    def test_chain_error_default(self):
        t1 = self.tc.register_tool("err_def",
                lambda x: (_ for _ in ()).throw(ValueError("bad")))
        t2 = self.tc.register_tool("uses_default", lambda x: x + 1)
        c  = self.tc.create_chain("err_def_chain")
        self.tc.add_chain_step(c.chain_id, t1.tool_id,
                                on_error="default", default_value=0)
        self.tc.add_chain_step(c.chain_id, t2.tool_id)
        result = self.tc.run_chain(c.chain_id, 99)
        self.assertEqual(result["final"], 1)

    def test_hooks_called(self):
        pre_c = []; post_c = []
        self.tc.on_before_invoke(lambda t, i: pre_c.append(1))
        self.tc.on_after_invoke(lambda t, r: post_c.append(1))
        t = self.tc.register_tool("hook_t", lambda x: x)
        self.tc.invoke(t.tool_id, 1)
        self.assertEqual(len(pre_c), 1)
        self.assertEqual(len(post_c), 1)

    def test_find_tool_by_name(self):
        self.tc.register_tool("findme", lambda x: x)
        t = self.tc.find_tool("findme")
        self.assertIsNotNone(t)

    def test_stats(self):
        t = self.tc.register_tool("stat_t", lambda x: x)
        self.tc.invoke(t.tool_id, 1)
        s = self.tc.stats()
        self.assertGreater(s["invocations"], 0)
        self.assertGreater(s["success"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v66: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
