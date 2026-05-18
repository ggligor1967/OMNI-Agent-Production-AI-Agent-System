"""OMNI AGENT v25: VectorStoreAdvanced, OutputParser, AgentSupervisor, CacheManager"""
import asyncio, os, sys, tempfile, time, math, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# VECTOR STORE ADVANCED
# ════════════════════════════════════════════════════════
class TestVectorStoreAdvanced(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.vector_store_advanced import VectorStoreAdvanced
        self.vs = VectorStoreAdvanced(db_path=os.path.join(td,"vs.db"), metric="cosine")

    def _vec(self, *vals): return list(vals)

    def test_upsert_returns_entry(self):
        e = self.vs.upsert("v1", self._vec(1.0, 0.0, 0.0), namespace="test")
        self.assertEqual(e.id, "v1")

    def test_upsert_with_metadata(self):
        self.vs.upsert("v2", self._vec(0.5, 0.5, 0.0), namespace="test",
                        text="hello", metadata={"topic": "greet"})
        e = self.vs.get("v2", "test")
        self.assertIsNotNone(e)
        self.assertEqual(e.metadata["topic"], "greet")

    def test_query_returns_results(self):
        for i in range(5):
            self.vs.upsert(f"doc{i}", self._vec(float(i)/5, 1-float(i)/5, 0.0), "ns")
        results = self.vs.query(self._vec(0.0, 1.0, 0.0), k=3, namespace="ns")
        self.assertGreater(len(results), 0)

    def test_query_top_result_most_similar(self):
        self.vs.upsert("a", self._vec(1.0, 0.0), "ns2")
        self.vs.upsert("b", self._vec(0.0, 1.0), "ns2")
        results = self.vs.query(self._vec(1.0, 0.0), k=2, namespace="ns2")
        self.assertEqual(results[0].entry.id, "a")

    def test_query_k_limit(self):
        for i in range(10):
            self.vs.upsert(f"x{i}", self._vec(float(i)/10, 1.0-float(i)/10), "lim")
        results = self.vs.query(self._vec(0.5, 0.5), k=3, namespace="lim")
        self.assertLessEqual(len(results), 3)

    def test_metadata_filter(self):
        self.vs.upsert("py1", self._vec(0.9, 0.1), "docs",
                        metadata={"lang": "python"})
        self.vs.upsert("js1", self._vec(0.8, 0.2), "docs",
                        metadata={"lang": "javascript"})
        results = self.vs.query(self._vec(0.85, 0.15), k=5, namespace="docs",
                                 filter={"lang": "python"})
        self.assertTrue(all(r.entry.metadata["lang"] == "python" for r in results))

    def test_delete(self):
        self.vs.upsert("del1", self._vec(1.0, 0.0), "del_ns")
        ok = self.vs.delete("del1", "del_ns")
        self.assertTrue(ok)
        self.assertIsNone(self.vs.get("del1", "del_ns"))

    def test_delete_missing(self):
        ok = self.vs.delete("nonexistent", "ns")
        self.assertFalse(ok)

    def test_batch_upsert(self):
        items = [{"id": f"b{i}", "vector": self._vec(float(i)/10, 1.0),
                   "text": f"item {i}"} for i in range(5)]
        entries = self.vs.batch_upsert(items, namespace="batch")
        self.assertEqual(len(entries), 5)

    def test_namespaces_isolated(self):
        self.vs.upsert("shared_id", self._vec(1.0, 0.0), "ns_a")
        self.vs.upsert("shared_id", self._vec(0.0, 1.0), "ns_b")
        a = self.vs.get("shared_id", "ns_a")
        b = self.vs.get("shared_id", "ns_b")
        self.assertEqual(a.vector, self._vec(1.0, 0.0))
        self.assertEqual(b.vector, self._vec(0.0, 1.0))

    def test_namespaces_list(self):
        self.vs.upsert("v1", self._vec(1.0, 0.0), "alpha")
        self.vs.upsert("v2", self._vec(0.0, 1.0), "beta")
        ns = self.vs.namespaces()
        self.assertIn("alpha", ns); self.assertIn("beta", ns)

    def test_query_result_has_rank(self):
        for i in range(3):
            self.vs.upsert(f"r{i}", self._vec(float(i)/3, 1.0-float(i)/3), "rank_ns")
        results = self.vs.query(self._vec(0.5, 0.5), k=3, namespace="rank_ns")
        ranks = [r.rank for r in results]
        self.assertEqual(sorted(ranks), list(range(1, len(results)+1)))

    def test_stats_namespace(self):
        self.vs.upsert("s1", self._vec(1.0, 0.0), "stat_ns")
        s = self.vs.stats("stat_ns")
        self.assertIn("active", s)

    def test_stats_global(self):
        s = self.vs.stats()
        self.assertIn("namespaces", s); self.assertIn("total_indexed", s)

    def test_entry_to_dict(self):
        self.vs.upsert("td1", self._vec(1.0, 0.0), "td_ns", text="hello")
        e = self.vs.get("td1", "td_ns")
        d = e.to_dict()
        for k in ["id","namespace","text","metadata"]: self.assertIn(k, d)

    def test_result_to_dict(self):
        self.vs.upsert("rd1", self._vec(1.0, 0.0), "rd_ns")
        results = self.vs.query(self._vec(1.0, 0.0), k=1, namespace="rd_ns")
        if results:
            d = results[0].to_dict()
            self.assertIn("score", d); self.assertIn("rank", d)

    def test_cosine_similarity_identical(self):
        from agent.vector_store_advanced import _cosine_sim
        v = [1.0, 2.0, 3.0]
        self.assertAlmostEqual(_cosine_sim(v, v), 1.0, places=5)

    def test_euclidean_sim(self):
        from agent.vector_store_advanced import _euclidean_sim
        a = [0.0, 0.0]; b = [0.0, 0.0]
        self.assertAlmostEqual(_euclidean_sim(a, b), 1.0, places=5)

    def test_dot_product(self):
        from agent.vector_store_advanced import _dot
        self.assertAlmostEqual(_dot([1.0,0.0],[0.0,1.0]), 0.0, places=5)
        self.assertAlmostEqual(_dot([1.0,0.0],[1.0,0.0]), 1.0, places=5)

    def test_upsert_update(self):
        self.vs.upsert("upd1", self._vec(1.0, 0.0), "upd_ns")
        self.vs.upsert("upd1", self._vec(0.0, 1.0), "upd_ns")
        e = self.vs.get("upd1", "upd_ns")
        self.assertEqual(e.vector, self._vec(0.0, 1.0))

# ════════════════════════════════════════════════════════
# OUTPUT PARSER
# ════════════════════════════════════════════════════════
class TestOutputParser(unittest.TestCase):
    def setUp(self):
        from agent.output_parser import OutputParser
        self.p = OutputParser()

    def test_parse_clean_json(self):
        r = self.p.parse_json('{"name": "Alice", "age": 30}')
        self.assertTrue(r.success); self.assertEqual(r.value["name"], "Alice")

    def test_parse_json_with_prose(self):
        r = self.p.parse_json('Sure! Here is the JSON: {"x": 1, "y": 2}')
        self.assertTrue(r.success); self.assertEqual(r.value["x"], 1)

    def test_parse_json_fenced(self):
        r = self.p.parse_json('```json\n{"key": "val"}\n```')
        self.assertTrue(r.success); self.assertEqual(r.value["key"], "val")

    def test_parse_json_trailing_comma(self):
        r = self.p.parse_json('{"a": 1, "b": 2,}')
        self.assertTrue(r.success)

    def test_parse_json_array(self):
        r = self.p.parse_json('[1, 2, 3]')
        self.assertTrue(r.success); self.assertEqual(r.value, [1, 2, 3])

    def test_parse_json_schema_valid(self):
        schema = {"name": {"type": "str", "required": True},
                   "age":  {"type": "int", "required": True}}
        r = self.p.parse_json('{"name": "Bob", "age": 25}', schema=schema)
        self.assertTrue(r.success); self.assertEqual(len(r.schema_errors), 0)

    def test_parse_json_schema_missing(self):
        schema = {"name": {"type": "str", "required": True}}
        r = self.p.parse_json('{"age": 25}', schema=schema)
        self.assertTrue(r.success); self.assertGreater(len(r.schema_errors), 0)

    def test_parse_json_schema_type_error(self):
        schema = {"count": {"type": "int"}}
        r = self.p.parse_json('{"count": "not_an_int"}', schema=schema)
        self.assertTrue(r.success); self.assertGreater(len(r.schema_errors), 0)

    def test_parse_xml_tag(self):
        r = self.p.parse_xml("<answer>42</answer>", tag="answer")
        self.assertTrue(r.success); self.assertEqual(r.value[0], "42")

    def test_parse_xml_missing(self):
        r = self.p.parse_xml("<other>val</other>", tag="answer")
        self.assertFalse(r.success)

    def test_parse_list_unordered(self):
        text = "- item one\n- item two\n- item three"
        r = self.p.parse_list(text)
        self.assertTrue(r.success); self.assertEqual(len(r.value), 3)

    def test_parse_list_ordered(self):
        text = "1. first\n2. second\n3. third"
        r = self.p.parse_list(text)
        self.assertTrue(r.success); self.assertIn("first", r.value)

    def test_parse_list_empty(self):
        r = self.p.parse_list("no lists here")
        self.assertFalse(r.success)

    def test_parse_kv_colon(self):
        text = "Name: Alice\nAge: 30\nCity: Paris"
        r = self.p.parse_kv(text)
        self.assertTrue(r.success)
        self.assertEqual(r.value["name"], "Alice")

    def test_parse_kv_type_hints(self):
        text = "count: 42\nactive: true"
        r = self.p.parse_kv(text, type_hints={"count": "int", "active": "bool"})
        self.assertTrue(r.success)
        self.assertEqual(r.value["count"], 42)
        self.assertTrue(r.value["active"])

    def test_parse_regex(self):
        r = self.p.parse_regex("Score: 95 out of 100", r'Score:\s*(?P<score>\d+)')
        self.assertTrue(r.success); self.assertEqual(r.value[0]["score"], "95")

    def test_extract_code_blocks(self):
        text = "Here is code:\n```python\nprint('hello')\n```"
        blocks = self.p.extract_code(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["language"], "python")
        self.assertIn("print", blocks[0]["code"])

    def test_extract_code_by_lang(self):
        text = "```python\npy code\n```\n```js\njs code\n```"
        blocks = self.p.extract_code(text, lang="python")
        self.assertEqual(len(blocks), 1)
        self.assertIn("py code", blocks[0]["code"])

    def test_extract_table(self):
        text = "| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |"
        rows = self.p.extract_table(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Name"], "Alice")

    def test_parse_multi_strategy_json_first(self):
        r = self.p.parse('{"x": 1}', strategies=["json","list"])
        self.assertTrue(r.success); self.assertEqual(r.strategy, "json")

    def test_parse_multi_strategy_fallback(self):
        r = self.p.parse("- item a\n- item b", strategies=["json","list"])
        self.assertTrue(r.success); self.assertEqual(r.strategy, "list")

    def test_parse_bool(self):
        self.assertTrue(self.p.parse_bool("yes"))
        self.assertTrue(self.p.parse_bool("True"))
        self.assertFalse(self.p.parse_bool("no"))

    def test_parse_number(self):
        self.assertAlmostEqual(self.p.parse_number("Score is 3.14"), 3.14)
        self.assertIsNone(self.p.parse_number("no numbers here"))

    def test_extract_emails(self):
        emails = self.p.extract_emails("Contact alice@example.com or bob@test.org")
        self.assertIn("alice@example.com", emails)
        self.assertIn("bob@test.org", emails)

    def test_extract_urls(self):
        urls = self.p.extract_urls("Visit https://example.com and http://test.org/page")
        self.assertIn("https://example.com", urls)

    def test_parse_result_to_dict(self):
        r = self.p.parse_json('{"a":1}')
        d = r.to_dict()
        for k in ["success","strategy","value","error"]: self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# AGENT SUPERVISOR
# ════════════════════════════════════════════════════════
class TestAgentSupervisor(unittest.TestCase):
    def setUp(self):
        from agent.agent_supervisor import AgentSupervisor
        self.sup = AgentSupervisor(max_total_concurrent=10, global_timeout_s=10.0)

    def _reg(self, name, fn, **kw):
        return self.sup.register(name, fn, **kw)

    def test_register_agent(self):
        spec = self._reg("adder", lambda input_data: input_data + 1)
        self.assertEqual(spec.name, "adder")

    def test_run_parallel_all(self):
        from agent.agent_supervisor import AggregationMode
        self._reg("double", lambda input_data: input_data * 2)
        self._reg("triple", lambda input_data: input_data * 3)
        run = _run(self.sup.run_parallel(["double","triple"],
                                          input_data=5, mode=AggregationMode.ALL))
        self.assertIn(10, run.final_output)
        self.assertIn(15, run.final_output)

    def test_run_parallel_first(self):
        from agent.agent_supervisor import AggregationMode
        self._reg("fast_a", lambda input_data: "A")
        self._reg("fast_b", lambda input_data: "B")
        run = _run(self.sup.run_parallel(["fast_a","fast_b"],
                                          input_data=None, mode=AggregationMode.FIRST))
        self.assertIn(run.final_output, ["A","B"])

    def test_run_sequential_pipes_output(self):
        self._reg("step1", lambda input_data: input_data + "_step1")
        self._reg("step2", lambda input_data: input_data + "_step2")
        run = _run(self.sup.run_sequential(["step1","step2"],
                                            input_data="start", pipe_output=True))
        self.assertEqual(run.final_output, "start_step1_step2")

    def test_async_agent(self):
        async def async_fn(input_data):
            await asyncio.sleep(0.01); return input_data * 2
        self._reg("async_agent", async_fn)
        run = _run(self.sup.run_parallel(["async_agent"], input_data=7))
        self.assertIn(14, run.final_output)

    def test_failed_agent(self):
        from agent.agent_supervisor import AgentState
        self._reg("boom", lambda input_data: 1/0, max_retries=0)
        run = _run(self.sup.run_parallel(["boom"], input_data=None))
        self.assertTrue(any(r.state == AgentState.FAILED for r in run.results))

    def test_unknown_agent_returns_failed(self):
        from agent.agent_supervisor import AgentState
        run = _run(self.sup.run_parallel(["nonexistent"], input_data=None))
        self.assertTrue(any(r.state == AgentState.FAILED for r in run.results))

    def test_circuit_breaker_disables(self):
        self._reg("flaky", lambda input_data: 1/0,
                   max_retries=0, circuit_threshold=0.5, circuit_window=4)
        for _ in range(5):
            _run(self.sup.run_parallel(["flaky"], input_data=None))
        spec = self.sup._agents["flaky"]
        self.assertTrue(spec.disabled)

    def test_enable_disabled_agent(self):
        self._reg("disabled_ag", lambda input_data: "ok")
        self.sup.disable("disabled_ag")
        self.sup.enable("disabled_ag")
        self.assertFalse(self.sup._agents["disabled_ag"].disabled)

    def test_retry_on_failure(self):
        calls = [0]
        def flaky2(input_data):
            calls[0] += 1
            if calls[0] < 3: raise RuntimeError("not yet")
            return "ok"
        self._reg("flaky2", flaky2, max_retries=3, retry_delay=0.01)
        run = _run(self.sup.run_parallel(["flaky2"], input_data=None))
        self.assertGreaterEqual(calls[0], 2)

    def test_get_run(self):
        self._reg("simple", lambda input_data: 42)
        run = _run(self.sup.run_parallel(["simple"], input_data=None))
        fetched = self.sup.get_run(run.id)
        self.assertIsNotNone(fetched)

    def test_history(self):
        self._reg("hist_ag", lambda input_data: "done")
        _run(self.sup.run_parallel(["hist_ag"], input_data=None))
        h = self.sup.history("hist_ag")
        self.assertGreater(len(h), 0)

    def test_stats(self):
        self._reg("stat_ag", lambda input_data: None)
        _run(self.sup.run_parallel(["stat_ag"], input_data=None))
        s = self.sup.stats()
        for k in ["total_invocations","success_rate","registered_agents"]: self.assertIn(k, s)

    def test_agents_list(self):
        self._reg("tagged_ag", lambda input_data: None, tags=["retrieval"])
        agents = self.sup.agents()
        self.assertTrue(any(a.name == "tagged_ag" for a in agents))

    def test_agents_filter_tag(self):
        self._reg("tagged2", lambda input_data: None, tags=["special"])
        self._reg("untagged2", lambda input_data: None, tags=[])
        tagged = self.sup.agents(tag="special")
        self.assertTrue(all("special" in a.tags for a in tagged))

    def test_run_to_dict(self):
        self._reg("dict_ag", lambda input_data: "result")
        run = _run(self.sup.run_parallel(["dict_ag"], input_data=None))
        d = run.to_dict()
        for k in ["id","state","duration_ms","results"]: self.assertIn(k, d)

    def test_result_to_dict(self):
        self._reg("res_ag", lambda input_data: "out")
        run = _run(self.sup.run_parallel(["res_ag"], input_data=None))
        d = run.results[0].to_dict()
        for k in ["agent","run_id","state","duration_ms"]: self.assertIn(k, d)

    def test_spec_to_dict(self):
        s = self._reg("spec_ag", lambda input_data: None)
        d = s.to_dict()
        for k in ["id","name","call_count","error_rate","disabled"]: self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# CACHE MANAGER
# ════════════════════════════════════════════════════════
class TestCacheManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.cache_manager import CacheManager
        self.cm = CacheManager(l1_max_items=100,
                                l2_db=os.path.join(td,"cache.db"),
                                default_ttl=3600.0)

    def test_set_and_get(self):
        _run(self.cm.set("key1", "value1"))
        val = _run(self.cm.get("key1"))
        self.assertEqual(val, "value1")

    def test_get_miss(self):
        val = _run(self.cm.get("nonexistent_key_xyz"))
        self.assertIsNone(val)

    def test_set_complex_value(self):
        data = {"name": "Alice", "scores": [1, 2, 3], "active": True}
        _run(self.cm.set("complex", data))
        val = _run(self.cm.get("complex"))
        self.assertEqual(val["name"], "Alice")
        self.assertEqual(val["scores"], [1, 2, 3])

    def test_ttl_expiry_l1(self):
        self.cm._l1.set("exp_key", "val", ttl=0.01)
        time.sleep(0.05)
        val = self.cm._l1.get("exp_key")
        self.assertIsNone(val)

    def test_ttl_expiry_l2(self):
        self.cm._l2.set("exp2", "val", ttl=0.01)
        time.sleep(0.05)
        val = self.cm._l2.get("exp2")
        self.assertIsNone(val)

    def test_delete(self):
        _run(self.cm.set("del_key", "to_delete"))
        ok = _run(self.cm.delete("del_key"))
        self.assertTrue(ok)
        val = _run(self.cm.get("del_key"))
        self.assertIsNone(val)

    def test_l1_promotes_from_l2(self):
        # Write directly to L2, bypass L1
        self.cm._l2.set("promo_key", "from_l2")
        val = _run(self.cm.get("promo_key"))
        self.assertEqual(val, "from_l2")
        # Now should be in L1 too
        l1_val = self.cm._l1.get("promo_key")
        self.assertEqual(l1_val, "from_l2")

    def test_get_or_set(self):
        calls = [0]
        def factory():
            calls[0] += 1; return "computed"
        val1 = _run(self.cm.get_or_set("gos_key", factory))
        val2 = _run(self.cm.get_or_set("gos_key", factory))
        self.assertEqual(val1, "computed")
        self.assertEqual(val2, "computed")
        self.assertEqual(calls[0], 1)  # factory called only once

    def test_async_factory(self):
        async def async_factory(): return "async_val"
        val = _run(self.cm.get_or_set("async_key", async_factory))
        self.assertEqual(val, "async_val")

    def test_warm(self):
        data = {"w1": "val1", "w2": "val2", "w3": "val3"}
        _run(self.cm.warm(data))
        for k, v in data.items():
            self.assertEqual(_run(self.cm.get(k)), v)

    def test_namespace(self):
        self.cm.register_namespace("users", "u")
        _run(self.cm.set("42", "alice", namespace="users"))
        val = _run(self.cm.get("42", namespace="users"))
        self.assertEqual(val, "alice")
        # Raw key should be "u:42"
        raw = _run(self.cm.get("u:42"))
        self.assertEqual(raw, "alice")

    def test_flush_l1(self):
        _run(self.cm.set("f1", "v1")); _run(self.cm.set("f2", "v2"))
        self.cm.flush("l1")
        self.assertIsNone(self.cm._l1.get("f1"))

    def test_flush_all(self):
        _run(self.cm.set("fa1", "v1"))
        self.cm.flush("all")
        self.assertIsNone(self.cm._l1.get("fa1"))

    def test_l1_lru_eviction(self):
        from agent.cache_manager import CacheManager
        td = tempfile.mkdtemp()
        cm = CacheManager(l1_max_items=3,
                           l2_db=os.path.join(td,"lru.db"), default_ttl=3600)
        for i in range(5):
            _run(cm.set(f"lru_{i}", f"val_{i}"))
        self.assertLessEqual(len(cm._l1._data), 3)

    def test_l2_sweep_expired(self):
        self.cm._l2.set("sw1", "val", ttl=0.01)
        time.sleep(0.05)
        n = self.cm._l2.sweep_expired()
        self.assertGreaterEqual(n, 1)

    def test_stats_structure(self):
        s = self.cm.stats()
        for level in ["l1","l2","l3"]: self.assertIn(level, s)
        self.assertIn("hit_rate", s["l1"])

    def test_l1_stats(self):
        self.cm._l1.set("s1", "v1")
        self.cm._l1.get("s1"); self.cm._l1.get("s1")
        self.cm._l1.get("miss_key")
        s = self.cm._l1.stats()
        self.assertGreaterEqual(s["hits"], 2)
        self.assertGreaterEqual(s["misses"], 1)

    def test_l2_compression(self):
        from agent.cache_manager import CacheManager
        td = tempfile.mkdtemp()
        cm = CacheManager(l2_db=os.path.join(td,"comp.db"),
                           l2_compress=True, default_ttl=3600)
        big_val = {"data": "x" * 1000}
        _run(cm.set("big", big_val))
        val = _run(cm.get("big"))
        self.assertEqual(val["data"], "x" * 1000)

    def test_write_through(self):
        # With write_through=True (default), set should populate L2
        _run(self.cm.set("wt_key", "wt_val"))
        # Get directly from L2 (bypass L1)
        l2_val = self.cm._l2.get("wt_key")
        self.assertEqual(l2_val, "wt_val")

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v25: {total-failed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
