"""OMNI AGENT v48: CircuitBreakerV2, KnowledgeDistillerV2, TaskDependencyGraph, OutputValidatorV2"""
import asyncio, os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# CIRCUIT BREAKER V2
# ════════════════════════════════════════════════════════
class TestCircuitBreakerV2(unittest.TestCase):
    def _breaker(self, **kw):
        from agent.circuit_breaker_v2 import CircuitBreaker, BreakerConfig
        cfg = BreakerConfig(**kw)
        return CircuitBreaker("test", cfg)

    def test_initial_state_closed(self):
        from agent.circuit_breaker_v2 import BreakerState
        b = self._breaker()
        self.assertEqual(b.state, BreakerState.CLOSED)

    def test_success_stays_closed(self):
        from agent.circuit_breaker_v2 import BreakerState
        b = self._breaker()
        b.call(lambda: "ok")
        self.assertEqual(b.state, BreakerState.CLOSED)

    def test_failures_open_breaker(self):
        from agent.circuit_breaker_v2 import BreakerState
        b = self._breaker(failure_threshold=3)
        for _ in range(3):
            try: b.call(lambda: (_ for _ in ()).throw(RuntimeError()))
            except Exception: pass
        self.assertEqual(b.state, BreakerState.OPEN)

    def test_open_rejects_calls(self):
        from agent.circuit_breaker_v2 import BreakerOpenError
        b = self._breaker(failure_threshold=1)
        try: b.call(lambda: (_ for _ in ()).throw(RuntimeError()))
        except Exception: pass
        with self.assertRaises(BreakerOpenError):
            b.call(lambda: "ok")

    def test_open_to_half_open_after_timeout(self):
        from agent.circuit_breaker_v2 import BreakerState
        b = self._breaker(failure_threshold=1, timeout_s=0.05)
        try: b.call(lambda: (_ for _ in ()).throw(RuntimeError()))
        except Exception: pass
        time.sleep(0.06)
        self.assertEqual(b.state, BreakerState.HALF_OPEN)

    def test_half_open_success_closes(self):
        from agent.circuit_breaker_v2 import BreakerState
        b = self._breaker(failure_threshold=1, timeout_s=0.05, success_threshold=1)
        try: b.call(lambda: (_ for _ in ()).throw(RuntimeError()))
        except Exception: pass
        time.sleep(0.06)
        b.call(lambda: "ok")
        self.assertEqual(b.state, BreakerState.CLOSED)

    def test_half_open_failure_reopens(self):
        from agent.circuit_breaker_v2 import BreakerState
        b = self._breaker(failure_threshold=1, timeout_s=0.05)
        try: b.call(lambda: (_ for _ in ()).throw(RuntimeError()))
        except Exception: pass
        time.sleep(0.06)
        try: b.call(lambda: (_ for _ in ()).throw(RuntimeError()))
        except Exception: pass
        self.assertEqual(b.state, BreakerState.OPEN)

    def test_excluded_exception_not_counted(self):
        from agent.circuit_breaker_v2 import BreakerState
        b = self._breaker(failure_threshold=2, excluded_exceptions=[ValueError])
        for _ in range(5):
            try: b.call(lambda: (_ for _ in ()).throw(ValueError("excluded")))
            except Exception: pass
        self.assertEqual(b.state, BreakerState.CLOSED)

    def test_reset_closes_breaker(self):
        from agent.circuit_breaker_v2 import BreakerState
        b = self._breaker(failure_threshold=1)
        try: b.call(lambda: (_ for _ in ()).throw(RuntimeError()))
        except Exception: pass
        b.reset()
        self.assertEqual(b.state, BreakerState.CLOSED)

    def test_stats_tracked(self):
        b = self._breaker()
        b.call(lambda: "ok")
        self.assertEqual(b.stats.successes, 1)
        self.assertEqual(b.stats.calls, 1)

    def test_failure_rate(self):
        b = self._breaker(failure_threshold=10)
        b.call(lambda: "ok")
        try: b.call(lambda: (_ for _ in ()).throw(RuntimeError()))
        except Exception: pass
        self.assertAlmostEqual(b.stats.failure_rate, 0.5)

    def test_on_state_change_hook(self):
        changes = []
        from agent.circuit_breaker_v2 import CircuitBreaker, BreakerConfig
        b = CircuitBreaker("cb", BreakerConfig(failure_threshold=1))
        b.on_state_change(lambda name, state: changes.append(state.value))
        try: b.call(lambda: (_ for _ in ()).throw(RuntimeError()))
        except Exception: pass
        self.assertIn("open", changes)

    def test_async_call_success(self):
        b = self._breaker()
        async def go(): return await b.call_async(asyncio.coroutine(lambda: "ok")() if False else _async_ok())
        async def _async_ok(): return "ok"
        result = _run(b.call_async(_async_ok))
        self.assertEqual(result, "ok")

    def test_registry(self):
        from agent.circuit_breaker_v2 import CircuitBreakerRegistry
        reg = CircuitBreakerRegistry()
        b1 = reg.get_or_create("svc1")
        b2 = reg.get_or_create("svc2")
        self.assertIn("svc1", reg.list_breakers())
        self.assertIn("svc2", reg.list_breakers())

    def test_registry_stats_all(self):
        from agent.circuit_breaker_v2 import CircuitBreakerRegistry
        reg = CircuitBreakerRegistry()
        reg.get_or_create("svc1")
        s = reg.stats_all()
        self.assertIn("svc1", s)

    def test_registry_open_count(self):
        from agent.circuit_breaker_v2 import CircuitBreakerRegistry, BreakerConfig
        reg = CircuitBreakerRegistry()
        b = reg.get_or_create("svc", BreakerConfig(failure_threshold=1))
        try: b.call(lambda: (_ for _ in ()).throw(RuntimeError()))
        except Exception: pass
        self.assertEqual(reg.open_count(), 1)

    def test_bulkhead_allows_up_to_max(self):
        from agent.circuit_breaker_v2 import CircuitBreakerRegistry
        reg = CircuitBreakerRegistry()
        bh = reg.get_bulkhead("db", max_concurrent=3)
        async def task(ctx=None): return "ok"
        result = _run(bh.call(task))
        self.assertEqual(result, "ok")

    def test_bulkhead_rejects_over_max(self):
        from agent.circuit_breaker_v2 import Bulkhead, BulkheadFullError
        bh = Bulkhead("test", max_concurrent=0)
        async def task(): return "ok"
        with self.assertRaises(BulkheadFullError):
            _run(bh.call(task))

    def test_to_dict(self):
        b = self._breaker()
        d = b.to_dict()
        for k in ["name", "state", "calls", "successes", "failures"]:
            self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# KNOWLEDGE DISTILLER V2
# ════════════════════════════════════════════════════════
class TestKnowledgeDistillerV2(unittest.TestCase):
    def setUp(self):
        from agent.knowledge_distiller_v2 import KnowledgeDistillerV2
        self.kd = KnowledgeDistillerV2(db_path=":memory:")

    def test_ingest_extracts_facts(self):
        facts = self.kd.ingest("The sky is blue. Water is wet. Fire is hot.")
        self.assertGreater(len(facts), 0)

    def test_add_fact_manual(self):
        from agent.knowledge_distiller_v2 import FactType
        f = self.kd.add_fact("Python is a programming language.", FactType.DEFINITION)
        self.assertIsNotNone(f)

    def test_deduplication(self):
        self.kd.add_fact("same content")
        f2 = self.kd.add_fact("same content")
        self.assertIsNone(f2)  # duplicate → None

    def test_get_fact(self):
        from agent.knowledge_distiller_v2 import FactType
        f = self.kd.add_fact("test fact", FactType.FACT)
        got = self.kd.get(f.fact_id)
        self.assertEqual(got.content, "test fact")

    def test_get_increments_access(self):
        from agent.knowledge_distiller_v2 import FactType
        f = self.kd.add_fact("access me", FactType.FACT)
        self.kd.get(f.fact_id)
        self.kd.get(f.fact_id)
        self.assertEqual(self.kd.get(f.fact_id).access_count, 3)

    def test_search_finds_keyword(self):
        from agent.knowledge_distiller_v2 import FactType
        self.kd.add_fact("Machine learning is powerful", FactType.FACT)
        self.kd.add_fact("Cooking is an art form", FactType.FACT)
        results = self.kd.search("machine learning")
        self.assertEqual(len(results), 1)

    def test_search_by_type(self):
        from agent.knowledge_distiller_v2 import FactType
        self.kd.add_fact("A cat is a feline", FactType.DEFINITION)
        self.kd.add_fact("A cat is nice", FactType.FACT)
        results = self.kd.search("cat", fact_type=FactType.DEFINITION)
        self.assertEqual(len(results), 1)

    def test_search_by_min_confidence(self):
        from agent.knowledge_distiller_v2 import FactType
        self.kd.add_fact("high confidence", confidence=0.9)
        self.kd.add_fact("low confidence", confidence=0.2)
        results = self.kd.search("confidence", min_confidence=0.5)
        self.assertEqual(len(results), 1)

    def test_search_by_tag(self):
        from agent.knowledge_distiller_v2 import FactType
        self.kd.add_fact("tagged fact", tags=["science"])
        self.kd.add_fact("untagged", tags=[])
        results = self.kd.search("fact", tag="science")
        self.assertEqual(len(results), 1)

    def test_top_facts_sorted(self):
        from agent.knowledge_distiller_v2 import FactType
        self.kd.add_fact("high imp", importance=0.9, confidence=0.9)
        self.kd.add_fact("low imp", importance=0.1, confidence=0.1)
        top = self.kd.top_facts(n=1)
        self.assertIn("high imp", top[0].content)

    def test_update_confidence(self):
        from agent.knowledge_distiller_v2 import FactType
        f = self.kd.add_fact("update me", confidence=0.5)
        self.kd.update_fact(f.fact_id, confidence=0.9)
        self.assertAlmostEqual(self.kd.get(f.fact_id).confidence, 0.9)

    def test_verify_fact(self):
        from agent.knowledge_distiller_v2 import FactType
        f = self.kd.add_fact("verify me")
        self.kd.update_fact(f.fact_id, verified=True)
        self.assertTrue(self.kd.get(f.fact_id).verified)

    def test_delete_fact(self):
        from agent.knowledge_distiller_v2 import FactType
        f = self.kd.add_fact("delete me")
        self.kd.delete_fact(f.fact_id)
        self.assertIsNone(self.kd.get(f.fact_id))

    def test_link_facts(self):
        f1 = self.kd.add_fact("fact A")
        f2 = self.kd.add_fact("fact B")
        self.assertTrue(self.kd.link(f1.fact_id, f2.fact_id))
        self.assertIn(f2.fact_id, f1.related)

    def test_prune_low_confidence(self):
        self.kd.add_fact("keep me", confidence=0.9)
        self.kd.add_fact("prune me", confidence=0.1)
        removed = self.kd.prune_low_confidence(threshold=0.5)
        self.assertEqual(removed, 1)

    def test_get_by_tag(self):
        self.kd.add_fact("tagged fact A", tags=["ml"])
        self.kd.add_fact("tagged fact B", tags=["ml"])
        self.kd.add_fact("other fact", tags=["other"])
        results = self.kd.get_by_tag("ml")
        self.assertEqual(len(results), 2)

    def test_custom_extractor(self):
        def extractor(text):
            return [{"content": "custom: " + text[:20], "fact_type": "fact"}]
        self.kd.add_extractor(extractor)
        facts = self.kd.ingest("Some raw input text here.")
        self.assertTrue(any("custom:" in f.content for f in facts))

    def test_score_property(self):
        f = self.kd.add_fact("scored", confidence=1.0, importance=1.0)
        self.assertGreater(f.score, 0.7)

    def test_stats(self):
        self.kd.add_fact("f1", confidence=0.8)
        self.kd.add_fact("f2", confidence=0.9)
        s = self.kd.stats()
        self.assertEqual(s["total_facts"], 2)
        self.assertIn("by_type", s)

# ════════════════════════════════════════════════════════
# TASK DEPENDENCY GRAPH
# ════════════════════════════════════════════════════════
class TestTaskDependencyGraph(unittest.TestCase):
    def setUp(self):
        from agent.task_dependency_graph import TaskDependencyGraph
        self.dag = TaskDependencyGraph()

    def _fn(self, val):
        async def fn(ctx): return val
        return fn

    def test_add_node(self):
        n = self.dag.add("task1", self._fn(1))
        self.assertIsNotNone(n.node_id)

    def test_run_single(self):
        n = self.dag.add("t", self._fn(42))
        results = _run(self.dag.run())
        self.assertEqual(results[n.node_id], 42)

    def test_dependency_ordering(self):
        order = []
        async def t1(ctx): order.append("t1"); return 1
        async def t2(ctx): order.append("t2"); return 2
        n1 = self.dag.add("t1", t1)
        n2 = self.dag.add("t2", t2, deps=[n1.node_id])
        _run(self.dag.run())
        self.assertEqual(order, ["t1", "t2"])

    def test_parallel_wave(self):
        n1 = self.dag.add("t1", self._fn(1))
        n2 = self.dag.add("t2", self._fn(2))  # no deps → same wave
        results = _run(self.dag.run())
        self.assertEqual(results[n1.node_id], 1)
        self.assertEqual(results[n2.node_id], 2)

    def test_validate_no_cycle(self):
        n1 = self.dag.add("t1", self._fn(1))
        n2 = self.dag.add("t2", self._fn(2), deps=[n1.node_id])
        self.assertTrue(self.dag.validate())

    def test_validate_cycle_detected(self):
        n1 = self.dag.add("t1", self._fn(1), node_id="n1")
        n2 = self.dag.add("t2", self._fn(2), deps=["n1"], node_id="n2")
        self.dag._nodes["n1"].deps.add("n2")
        self.assertFalse(self.dag.validate())

    def test_failed_node_state(self):
        from agent.task_dependency_graph import NodeState
        async def bad(ctx): raise ValueError("boom")
        n = self.dag.add("bad", bad, on_failure="fail")
        _run(self.dag.run())
        self.assertEqual(n.state, NodeState.FAILED)

    def test_skipped_on_failure(self):
        from agent.task_dependency_graph import NodeState
        async def bad(ctx): raise ValueError("boom")
        n = self.dag.add("bad", bad, on_failure="skip")
        _run(self.dag.run())
        self.assertEqual(n.state, NodeState.SKIPPED)

    def test_dependent_skipped_after_failure(self):
        from agent.task_dependency_graph import NodeState
        async def bad(ctx): raise ValueError("boom")
        n1 = self.dag.add("n1", bad, on_failure="fail")
        n2 = self.dag.add("n2", self._fn(99), deps=[n1.node_id])
        _run(self.dag.run())
        self.assertEqual(n2.state, NodeState.SKIPPED)

    def test_retry_succeeds(self):
        attempts = [0]
        async def flaky(ctx):
            attempts[0] += 1
            if attempts[0] < 3: raise RuntimeError("retry")
            return "ok"
        n = self.dag.add("flaky", flaky, max_retries=2)
        _run(self.dag.run())
        self.assertEqual(n.result, "ok")

    def test_timeout_fails(self):
        from agent.task_dependency_graph import NodeState
        async def slow(ctx): await asyncio.sleep(10)
        n = self.dag.add("slow", slow, timeout_s=0.05)
        _run(self.dag.run())
        self.assertEqual(n.state, NodeState.FAILED)

    def test_cancel_propagates(self):
        from agent.task_dependency_graph import NodeState, TaskDependencyGraph
        dag = TaskDependencyGraph()
        async def slow(ctx): await asyncio.sleep(5)
        n1 = dag.add("n1", slow)
        dag.cancel()
        _run(dag.run())
        self.assertEqual(n1.state, NodeState.CANCELLED)

    def test_checkpoint_saved(self):
        n = self.dag.add("cp", self._fn("checkpoint_val"), checkpoint=True)
        _run(self.dag.run())
        self.assertEqual(self.dag.get_checkpoint(n.node_id), "checkpoint_val")

    def test_checkpoint_restored(self):
        n = self.dag.add("cp", self._fn("fresh"), checkpoint=True, node_id="cp_node")
        _run(self.dag.run())
        self.assertEqual(self.dag.get_checkpoint("cp_node"), "fresh")
        # Simulate second run — fn now raises, but checkpoint should be used
        async def fail_fn(ctx): raise RuntimeError("should not run")
        self.dag._nodes["cp_node"].fn = fail_fn
        results = _run(self.dag.run())
        self.assertEqual(results["cp_node"], "fresh")

    def test_priority_ordering(self):
        order = []
        async def hi(ctx): order.append("hi"); return 1
        async def lo(ctx): order.append("lo"); return 2
        self.dag.add("lo", lo, priority=0)
        self.dag.add("hi", hi, priority=10)
        _run(self.dag.run())
        self.assertEqual(order[0], "hi")

    def test_critical_path(self):
        n1 = self.dag.add("start", self._fn(1))
        n2 = self.dag.add("mid",   self._fn(2), deps=[n1.node_id])
        n3 = self.dag.add("end",   self._fn(3), deps=[n2.node_id])
        path = self.dag.critical_path()
        self.assertEqual(path, ["start", "mid", "end"])

    def test_hooks_called(self):
        started, done = [], []
        self.dag.on_node_start(lambda n: started.append(n.name))
        self.dag.on_node_done(lambda n: done.append(n.name))
        self.dag.add("h", self._fn(1))
        _run(self.dag.run())
        self.assertIn("h", started)
        self.assertIn("h", done)

    def test_stats(self):
        self.dag.add("t1", self._fn(1))
        self.dag.add("t2", self._fn(2))
        _run(self.dag.run())
        s = self.dag.stats()
        self.assertEqual(s["total_nodes_run"], 2)
        self.assertEqual(s["run_count"], 1)

# ════════════════════════════════════════════════════════
# OUTPUT VALIDATOR V2
# ════════════════════════════════════════════════════════
class TestOutputValidatorV2(unittest.TestCase):
    def test_required_field_present(self):
        from agent.output_validator_v2 import SchemaValidator
        sv = SchemaValidator()
        sv.field("name").required()
        r = sv.validate({"name": "Alice"})
        self.assertTrue(r.valid)

    def test_required_field_missing(self):
        from agent.output_validator_v2 import SchemaValidator
        sv = SchemaValidator()
        sv.field("name").required()
        r = sv.validate({})
        self.assertFalse(r.valid)
        self.assertEqual(len(r.errors), 1)

    def test_type_check(self):
        from agent.output_validator_v2 import SchemaValidator
        sv = SchemaValidator()
        sv.field("age").type_is(int)
        self.assertTrue(sv.validate({"age": 25}).valid)
        self.assertFalse(sv.validate({"age": "old"}).valid)

    def test_min_length(self):
        from agent.output_validator_v2 import SchemaValidator
        sv = SchemaValidator()
        sv.field("bio").min_length(10)
        self.assertFalse(sv.validate({"bio": "short"}).valid)
        self.assertTrue(sv.validate({"bio": "long enough bio here"}).valid)

    def test_max_length(self):
        from agent.output_validator_v2 import SchemaValidator
        sv = SchemaValidator()
        sv.field("code").max_length(5)
        self.assertTrue(sv.validate({"code": "abc"}).valid)
        self.assertFalse(sv.validate({"code": "toolongcode"}).valid)

    def test_min_max_value(self):
        from agent.output_validator_v2 import SchemaValidator
        sv = SchemaValidator()
        sv.field("score").min_value(0).max_value(100)
        self.assertTrue(sv.validate({"score": 50}).valid)
        self.assertFalse(sv.validate({"score": -1}).valid)
        self.assertFalse(sv.validate({"score": 101}).valid)

    def test_regex_pattern(self):
        from agent.output_validator_v2 import SchemaValidator
        sv = SchemaValidator()
        sv.field("code").matches(r"[A-Z]{3}-\d{3}")
        self.assertTrue(sv.validate({"code": "ABC-123"}).valid)
        self.assertFalse(sv.validate({"code": "abc-123"}).valid)

    def test_one_of(self):
        from agent.output_validator_v2 import SchemaValidator
        sv = SchemaValidator()
        sv.field("status").one_of(["active", "inactive", "pending"])
        self.assertTrue(sv.validate({"status": "active"}).valid)
        self.assertFalse(sv.validate({"status": "deleted"}).valid)

    def test_no_extra_fields(self):
        from agent.output_validator_v2 import SchemaValidator
        sv = SchemaValidator()
        sv.field("name").required()
        sv.no_extra_fields()
        self.assertFalse(sv.validate({"name": "Alice", "extra": "field"}).valid)

    def test_warning_severity(self):
        from agent.output_validator_v2 import SchemaValidator, Severity
        sv = SchemaValidator()
        sv.field("note").custom(lambda v: len(str(v)) > 5, "Too short", Severity.WARNING)
        r = sv.validate({"note": "hi"})
        self.assertTrue(r.valid)  # warnings don't invalidate
        self.assertEqual(len(r.warnings), 1)

    def test_json_validator_valid(self):
        from agent.output_validator_v2 import JSONValidator
        jv = JSONValidator()
        r = jv.validate('{"key": "value"}')
        self.assertTrue(r.valid)
        self.assertEqual(r.value["key"], "value")

    def test_json_validator_invalid(self):
        from agent.output_validator_v2 import JSONValidator
        jv = JSONValidator()
        r = jv.validate("{not valid json}")
        self.assertFalse(r.valid)

    def test_json_with_schema(self):
        from agent.output_validator_v2 import JSONValidator, SchemaValidator
        sv = SchemaValidator()
        sv.field("name").required()
        jv = JSONValidator()
        r = jv.validate('{"name": "Alice"}', sv)
        self.assertTrue(r.valid)

    def test_semantic_min_words(self):
        from agent.output_validator_v2 import SemanticValidator
        sv = SemanticValidator().min_words(5)
        self.assertTrue(sv.validate("this has exactly five words here").valid)
        self.assertFalse(sv.validate("too short").valid)

    def test_semantic_max_words(self):
        from agent.output_validator_v2 import SemanticValidator
        sv = SemanticValidator().max_words(3)
        self.assertTrue(sv.validate("one two three").valid)
        self.assertFalse(sv.validate("one two three four five").valid)

    def test_semantic_banned_words(self):
        from agent.output_validator_v2 import SemanticValidator
        sv = SemanticValidator().no_banned_words(["spam", "hack"])
        self.assertTrue(sv.validate("clean text here").valid)
        self.assertFalse(sv.validate("this is spam content").valid)

    def test_semantic_starts_with(self):
        from agent.output_validator_v2 import SemanticValidator
        sv = SemanticValidator().starts_with("Answer:")
        self.assertTrue(sv.validate("Answer: yes").valid)
        self.assertFalse(sv.validate("Maybe: yes").valid)

    def test_pipeline_all_pass(self):
        from agent.output_validator_v2 import ValidationPipeline, SchemaValidator, SemanticValidator
        sv = SchemaValidator()
        sv.field("text").required()
        sem = SemanticValidator().min_words(2)
        pipeline = ValidationPipeline()
        pipeline.add_stage("schema", sv)
        pipeline.add_stage("semantic", lambda v: isinstance(v, dict))
        r = pipeline.run({"text": "hello world"})
        self.assertTrue(r.valid)

    def test_pipeline_fail_fast(self):
        from agent.output_validator_v2 import ValidationPipeline, SchemaValidator
        sv1 = SchemaValidator(); sv1.field("x").required()
        sv2 = SchemaValidator(); sv2.field("y").required()
        pipeline = ValidationPipeline(fail_fast=True)
        pipeline.add_stage("s1", sv1)
        pipeline.add_stage("s2", sv2)
        r = pipeline.run({})
        self.assertFalse(r.valid)

    def test_pipeline_stats(self):
        from agent.output_validator_v2 import ValidationPipeline
        pipeline = ValidationPipeline()
        pipeline.add_stage("fn", lambda v: True)
        pipeline.run("anything")
        s = pipeline.stats()
        self.assertEqual(s["runs"], 1)
        self.assertEqual(s["passed"], 1)

    def test_result_to_dict(self):
        from agent.output_validator_v2 import ValidationResult
        r = ValidationResult(valid=True, value="test")
        d = r.to_dict()
        for k in ["valid", "errors", "warnings", "duration_ms"]:
            self.assertIn(k, d)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v48: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
