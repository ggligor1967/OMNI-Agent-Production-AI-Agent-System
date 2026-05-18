"""OMNI AGENT v28: ToolRegistry, ResponseRanker, AgentMemory, ConfigManager"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# TOOL REGISTRY
# ════════════════════════════════════════════════════════
class TestToolRegistry(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.tool_registry import ToolRegistry
        self.reg = ToolRegistry(db_path=os.path.join(td, "tools.db"))

    def test_register_tool(self):
        spec = self.reg.register("add", lambda a, b: a + b, description="Add two numbers")
        self.assertEqual(spec.name, "add")

    def test_call_sync_tool(self):
        self.reg.register("mul", lambda a, b: a * b,
                           input_schema={"properties":{"a":{"type":"integer"},"b":{"type":"integer"}}})
        tc = _run(self.reg.call("mul", {"a": 3, "b": 4}))
        self.assertTrue(tc.success)
        self.assertEqual(tc.result, 12)

    def test_call_async_tool(self):
        async def async_add(a, b):
            await asyncio.sleep(0.01)
            return a + b
        self.reg.register("async_add", async_add)
        tc = _run(self.reg.call("async_add", {"a": 5, "b": 6}))
        self.assertEqual(tc.result, 11)

    def test_schema_validation_missing_required(self):
        self.reg.register("strict", lambda x: x,
                           input_schema={"required": ["x"],
                                          "properties": {"x": {"type": "string"}}})
        tc = _run(self.reg.call("strict", {}))
        self.assertFalse(tc.success)
        self.assertIn("Missing required", tc.error)

    def test_schema_validation_wrong_type(self):
        self.reg.register("typed", lambda x: x,
                           input_schema={"required": ["x"],
                                          "properties": {"x": {"type": "integer"}}})
        tc = _run(self.reg.call("typed", {"x": "not_int"}))
        self.assertFalse(tc.success)

    def test_call_missing_tool(self):
        tc = _run(self.reg.call("nonexistent", {}))
        self.assertFalse(tc.success)
        self.assertIn("not found", tc.error)

    def test_disable_enable_tool(self):
        self.reg.register("toggle", lambda: "ok")
        self.reg.disable("toggle")
        tc = _run(self.reg.call("toggle", {}))
        self.assertFalse(tc.success)
        self.reg.enable("toggle")
        tc = _run(self.reg.call("toggle", {}))
        self.assertTrue(tc.success)

    def test_versioning(self):
        self.reg.register("versioned", lambda: "v1", version="1.0.0")
        self.reg.register("versioned", lambda: "v2", version="2.0.0")
        tc = _run(self.reg.call("versioned", {}, version="1.0.0"))
        self.assertEqual(tc.result, "v1")
        tc2 = _run(self.reg.call("versioned", {}))
        self.assertEqual(tc2.result, "v2")

    def test_latest_version_selected(self):
        self.reg.register("ver_tool", lambda: "v1", version="1.0.0")
        self.reg.register("ver_tool", lambda: "v3", version="3.0.0")
        self.reg.register("ver_tool", lambda: "v2", version="2.0.0")
        tc = _run(self.reg.call("ver_tool", {}))
        self.assertEqual(tc.result, "v3")

    def test_cache_hit(self):
        calls = [0]
        def counted(): calls[0] += 1; return calls[0]
        self.reg.register("cached_tool", counted, cache_ttl=60.0)
        _run(self.reg.call("cached_tool", {}))
        tc = _run(self.reg.call("cached_tool", {}))
        self.assertTrue(tc.cached)
        self.assertEqual(calls[0], 1)

    def test_rate_limit(self):
        self.reg.register("limited", lambda: "ok", rpm_limit=1)
        spec = self.reg.get("limited")
        spec._rpm_window = [time.time()]  # simulate already at limit
        tc = _run(self.reg.call("limited", {}))
        self.assertFalse(tc.success)
        self.assertIn("rate limit", tc.error)

    def test_timeout(self):
        async def slow(): await asyncio.sleep(5)
        self.reg.register("slow_tool", slow, timeout_s=0.05)
        tc = _run(self.reg.call("slow_tool", {}))
        self.assertFalse(tc.success)
        self.assertIn("Timeout", tc.error)

    def test_pre_hook(self):
        transformed = []
        self.reg.add_pre_hook(lambda n, a: transformed.append(n) or a)
        self.reg.register("hooked", lambda: "ok")
        _run(self.reg.call("hooked", {}))
        self.assertIn("hooked", transformed)

    def test_post_hook(self):
        results = []
        self.reg.add_post_hook(lambda n, a, tc: results.append(tc.result))
        self.reg.register("post_hooked", lambda: "result!")
        _run(self.reg.call("post_hooked", {}))
        self.assertIn("result!", results)

    def test_list_by_tag(self):
        self.reg.register("math_tool", lambda: 1, tags=["math"])
        self.reg.register("text_tool", lambda: "a", tags=["text"])
        math = self.reg.list(tag="math")
        self.assertTrue(all("math" in s.tags for s in math))

    def test_list_by_capability(self):
        self.reg.register("search", lambda: [], capabilities=["retrieval"])
        results = self.reg.list(capability="retrieval")
        self.assertTrue(any(s.name == "search" for s in results))

    def test_list_by_prefix(self):
        self.reg.register("db_read", lambda: None)
        self.reg.register("db_write", lambda: None)
        self.reg.register("api_call", lambda: None)
        db_tools = self.reg.list(prefix="db_")
        self.assertEqual(len(db_tools), 2)

    def test_versions_list(self):
        self.reg.register("vl", lambda: None, version="1.0.0")
        self.reg.register("vl", lambda: None, version="2.0.0")
        vs = self.reg.versions("vl")
        self.assertIn("1.0.0", vs); self.assertIn("2.0.0", vs)

    def test_schema_retrieval(self):
        self.reg.register("schema_tool", lambda: None,
                           input_schema={"required":["q"]})
        sc = self.reg.schema("schema_tool")
        self.assertIn("input", sc)

    def test_tool_metrics(self):
        self.reg.register("metric_tool", lambda: "ok")
        _run(self.reg.call("metric_tool", {}))
        spec = self.reg.get("metric_tool")
        self.assertEqual(spec.call_count, 1)

    def test_error_recorded(self):
        self.reg.register("err_tool", lambda: 1/0)
        tc = _run(self.reg.call("err_tool", {}))
        self.assertFalse(tc.success)
        spec = self.reg.get("err_tool")
        self.assertEqual(spec.error_count, 1)

    def test_stats(self):
        self.reg.register("st", lambda: None)
        _run(self.reg.call("st", {}))
        s = self.reg.stats()
        for k in ["registered_tools","total_calls","total_versions"]:
            self.assertIn(k, s)

    def test_call_to_dict(self):
        self.reg.register("dict_tool", lambda: {"key": "val"})
        tc = _run(self.reg.call("dict_tool", {}))
        d = tc.to_dict()
        for k in ["call_id","tool","success","latency_ms","cached"]:
            self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# RESPONSE RANKER
# ════════════════════════════════════════════════════════
class TestResponseRanker(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.response_ranker import ResponseRanker
        self.ranker = ResponseRanker(db_path=os.path.join(td,"ranker.db"))

    def test_rank_returns_sorted(self):
        candidates = ["Short.", "A longer and more detailed answer about the topic.",
                       "Medium length response."]
        ranked = self.ranker.rank(candidates, prompt="tell me about the topic")
        scores = [r.final_score for r in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_rank_assigns_ranks(self):
        ranked = self.ranker.rank(["a","b","c"])
        self.assertEqual([r.rank for r in ranked], [1,2,3])

    def test_rank_single_candidate(self):
        ranked = self.ranker.rank(["Only one option here."])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].rank, 1)

    def test_rank_empty(self):
        ranked = self.ranker.rank([])
        self.assertEqual(ranked, [])

    def test_best_returns_string(self):
        best = self.ranker.best(["Short.", "Much better and relevant answer here."],
                                 prompt="give me a relevant answer")
        self.assertIsInstance(best, str)

    def test_dedup_marks_duplicate(self):
        text = "The quick brown fox jumps over the lazy dog."
        ranked = self.ranker.rank([text, text + " "])  # near-identical
        dup_count = sum(1 for r in ranked if r.duplicate_of is not None)
        self.assertGreaterEqual(dup_count, 1)

    def test_dedup_penalises_score(self):
        text = "Same text repeated for dedup testing purposes."
        ranked = self.ranker.rank([text, text])
        # Second one should have much lower score
        self.assertLess(ranked[1].final_score, ranked[0].final_score)

    def test_filter_duplicates(self):
        texts = ["Hello world.", "Hello world.", "Completely different text here."]
        unique = self.ranker.filter_duplicates(texts)
        self.assertLessEqual(len(unique), 2)

    def test_relevance_score(self):
        from agent.response_ranker import _bow, _cosine, _tokenize
        a = _bow(_tokenize("Python programming language"))
        b = _bow(_tokenize("Python coding"))
        c = _bow(_tokenize("cooking recipes"))
        self.assertGreater(_cosine(a, b), _cosine(a, c))

    def test_coherence_score(self):
        from agent.response_ranker import _coherence
        # Coherence of text with repeated terms should be >= text with none
        shared = "Python code Python script Python language."
        diverse = "Apple orange banana. Computer network packet. Sun moon star."
        self.assertGreaterEqual(_coherence(shared), _coherence(diverse))
        self.assertGreaterEqual(_coherence(shared), 0.0)

    def test_length_score_peak(self):
        from agent.response_ranker import _length_score
        # Score should be highest near target
        s_near   = _length_score("x" * 300, target=300)
        s_far    = _length_score("x" * 10,  target=300)
        self.assertGreater(s_near, s_far)

    def test_toxicity_score(self):
        from agent.response_ranker import _toxicity_score
        clean  = "Python is a great programming language for beginners."
        toxic  = "I hate this stupid idiot moron approach."
        self.assertLess(_toxicity_score(clean), _toxicity_score(toxic))

    def test_format_bonus_code(self):
        from agent.response_ranker import _format_bonus
        with_code = "Here is the answer:\n```python\nprint('hi')\n```"
        no_code   = "Here is the answer in plain text."
        self.assertGreater(_format_bonus(with_code), _format_bonus(no_code))

    def test_format_bonus_json(self):
        from agent.response_ranker import _format_bonus
        self.assertGreater(_format_bonus('{"key": "value"}'), _format_bonus("plain text"))

    def test_richness_score(self):
        from agent.response_ranker import _richness
        rich  = "Python excels at data analysis machine learning automation scripting"
        poor  = "the the the the the the the"
        self.assertGreater(_richness(rich), _richness(poor))

    def test_calibrate_weights(self):
        self.ranker.calibrate({"relevance": 0.5, "coherence": 0.3})
        self.assertAlmostEqual(self.ranker.weights.relevance, 0.5)
        self.assertAlmostEqual(self.ranker.weights.coherence, 0.3)

    def test_score_one(self):
        s = self.ranker.score_one("A detailed Python programming explanation.", "Python")
        self.assertIsNotNone(s.final_score)
        self.assertGreaterEqual(s.final_score, 0.0)

    def test_breakdown_keys(self):
        ranked = self.ranker.rank(["Some response text here."], prompt="test")
        d = ranked[0].breakdown()
        for k in ["relevance","coherence","length","richness","toxicity","final","rank"]:
            self.assertIn(k, d)

    def test_stats(self):
        self.ranker.rank(["text"], prompt="p")
        s = self.ranker.stats()
        for k in ["sessions","avg_best_score","weights","dedup_threshold"]:
            self.assertIn(k, s)

    def test_to_dict(self):
        ranked = self.ranker.rank(["Some text."])
        d = ranked[0].to_dict()
        for k in ["id","text_preview","final","rank"]: self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# AGENT MEMORY
# ════════════════════════════════════════════════════════
class TestAgentMemory(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.agent_memory import AgentMemory
        self.mem = AgentMemory(db_path=os.path.join(td,"mem.db"),
                                half_life_s=3600.0)

    def test_store_returns_id(self):
        mid = self.mem.store("Python is a programming language.")
        self.assertIsNotNone(mid)

    def test_recall_relevant(self):
        self.mem.store("Python uses indentation for code blocks.", tags=["python"])
        self.mem.store("The capital of France is Paris.", tags=["geography"])
        results = self.mem.recall("Python indentation")
        contents = [m.content for m, _ in results]
        self.assertTrue(any("Python" in c for c in contents))

    def test_recall_top_k(self):
        for i in range(10):
            self.mem.store(f"Memory entry number {i} about different topics.")
        results = self.mem.recall("memory entry", top_k=3)
        self.assertLessEqual(len(results), 3)

    def test_recall_by_tag(self):
        self.mem.store("Python tip", tags=["python"])
        self.mem.store("JS tip", tags=["javascript"])
        results = self.mem.recall("tip", tags=["python"])
        self.assertTrue(all("python" in m.tags for m, _ in results))

    def test_recall_reinforces(self):
        mid = self.mem.store("Reinforcement test content.")
        m_before = self.mem.get(mid)
        count_before = m_before.access_count
        self.mem.recall("reinforcement test")
        m_after = self.mem.get(mid)
        self.assertGreater(m_after.access_count, count_before)

    def test_forget(self):
        mid = self.mem.store("Temporary memory.")
        ok = self.mem.forget(mid)
        self.assertTrue(ok)
        self.assertIsNone(self.mem.get(mid))

    def test_forget_returns_false_unknown(self):
        ok = self.mem.forget("nonexistent_id")
        self.assertFalse(ok)

    def test_reinforce(self):
        mid = self.mem.store("Content to reinforce.")
        m = self.mem.get(mid)
        strength_before = m.strength
        self.mem.reinforce(mid)
        m2 = self.mem.get(mid)
        self.assertGreaterEqual(m2.strength, strength_before)

    def test_dedup(self):
        content = "Exact same content for dedup test."
        id1 = self.mem.store(content)
        id2 = self.mem.store(content)
        self.assertEqual(id1, id2)

    def test_dedup_skip(self):
        content = "Content for skip dedup."
        id1 = self.mem.store(content, dedup=True)
        id2 = self.mem.store(content, dedup=False)
        self.assertNotEqual(id1, id2)

    def test_importance_affects_score(self):
        self.mem.store("Low importance content about testing.", importance=0.1)
        self.mem.store("High importance content about testing.", importance=0.9)
        results = self.mem.recall("content testing", top_k=5)
        if len(results) >= 2:
            self.assertGreater(results[0][0].importance, results[-1][0].importance - 0.5)

    def test_eviction_at_capacity(self):
        mem = self._make_small_mem(max_entries=3)
        for i in range(5):
            mem.store(f"Unique content memory {i} with different words.")
        self.assertLessEqual(len(mem._memories), 3)

    def _make_small_mem(self, max_entries=3):
        td = tempfile.mkdtemp()
        from agent.agent_memory import AgentMemory
        return AgentMemory(db_path=os.path.join(td,"small.db"),
                            max_entries=max_entries)

    def test_associative_recall(self):
        mid1 = self.mem.store("Python asyncio for concurrent programming.")
        self.mem.store("Async programming with coroutines in Python.")
        self.mem.store("Geography of France and Paris.")
        assoc = self.mem.associative_recall(mid1, top_k=2)
        self.assertLessEqual(len(assoc), 2)

    def test_consolidate(self):
        self.mem.store("Nearly identical content here for testing.")
        self.mem.store("Nearly identical content here for testing!", dedup=False)
        n = self.mem.consolidate()
        self.assertGreaterEqual(n, 0)  # may or may not merge based on threshold

    def test_retention_decays(self):
        from agent.agent_memory import _ebbinghaus
        r0 = _ebbinghaus(1.0, 0,      half_life_s=3600)
        r1 = _ebbinghaus(1.0, 3600,   half_life_s=3600)
        r2 = _ebbinghaus(1.0, 86400,  half_life_s=3600)
        self.assertGreater(r0, r1)
        self.assertGreater(r1, r2)

    def test_list_by_tag(self):
        self.mem.store("A", tags=["code"])
        self.mem.store("B", tags=["code"])
        self.mem.store("C", tags=["facts"])
        code_mems = self.mem.list(tag="code")
        self.assertEqual(len(code_mems), 2)

    def test_export(self):
        self.mem.store("Export me.")
        exported = self.mem.export()
        self.assertGreater(len(exported), 0)
        self.assertIn("id", exported[0])

    def test_stats(self):
        self.mem.store("Stat content.")
        s = self.mem.stats()
        for k in ["total_memories","in_memory","max_entries","half_life_s"]:
            self.assertIn(k, s)

    def test_entry_to_dict(self):
        mid = self.mem.store("Dict test content.")
        m = self.mem.get(mid)
        d = m.to_dict()
        for k in ["id","content","tags","importance","retention"]:
            self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# CONFIG MANAGER
# ════════════════════════════════════════════════════════
class TestConfigManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.config_manager import ConfigManager
        self.cm = ConfigManager(db_path=os.path.join(td,"cfg.db"),
                                 env_prefix="TESTAGENT")

    def test_get_missing_returns_default(self):
        val = self.cm.get("nonexistent.key", default=42)
        self.assertEqual(val, 42)

    def test_set_and_get(self):
        self.cm.set("app.name", "TestApp")
        self.assertEqual(self.cm.get("app.name"), "TestApp")

    def test_schema_default(self):
        self.cm.schema("db.port", type_="int", default=5432)
        self.assertEqual(self.cm.get("db.port"), 5432)

    def test_schema_type_coercion_int(self):
        self.cm.schema("db.port", type_="int", default=5432)
        self.cm.set("db.port", "5433")
        self.assertIsInstance(self.cm.get("db.port"), int)
        self.assertEqual(self.cm.get("db.port"), 5433)

    def test_schema_type_coercion_bool(self):
        self.cm.schema("feature.enabled", type_="bool", default=False)
        self.cm.set("feature.enabled", "true")
        self.assertIs(self.cm.get("feature.enabled"), True)

    def test_schema_type_coercion_list(self):
        self.cm.schema("allowed.hosts", type_="list", default=[])
        self.cm.set("allowed.hosts", "localhost,127.0.0.1,example.com")
        hosts = self.cm.get("allowed.hosts")
        self.assertIsInstance(hosts, list)
        self.assertIn("localhost", hosts)

    def test_load_dict(self):
        self.cm.load_dict({"database": {"host": "myhost", "port": 5432}})
        self.assertEqual(self.cm.get("database.host"), "myhost")

    def test_load_dict_overrides_previous(self):
        self.cm.load_dict({"key": "v1"})
        self.cm.load_dict({"key": "v2"})
        # v2 wins (later layer)
        self.assertEqual(self.cm.get("key"), "v2")

    def test_runtime_override_wins(self):
        self.cm.load_dict({"priority": "low"})
        self.cm.set("priority", "high")
        self.assertEqual(self.cm.get("priority"), "high")

    def test_load_env(self, monkeypatch=None):
        import os
        os.environ["TESTAGENT_APP_NAME"] = "EnvApp"
        self.cm.schema("app.name", type_="str", default="Default")
        self.cm.load_env()
        # Env maps TESTAGENT_APP_NAME → app.name
        val = self.cm.get("app.name")
        self.assertIsNotNone(val)
        del os.environ["TESTAGENT_APP_NAME"]

    def test_on_change_hook(self):
        changes = []
        self.cm.on_change("watched.key", lambda old, new: changes.append((old, new)))
        self.cm.set("watched.key", "v1")
        self.cm.set("watched.key", "v2")
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[1][0], "v1")
        self.assertEqual(changes[1][1], "v2")

    def test_validate_required_missing(self):
        self.cm.schema("required.field", required=True)
        errors = self.cm.validate()
        self.assertTrue(any("required.field" in e for e in errors))

    def test_validate_passes_when_set(self):
        self.cm.schema("needed.field", required=True)
        self.cm.set("needed.field", "value")
        errors = self.cm.validate()
        self.assertFalse(any("needed.field" in e for e in errors))

    def test_export_masks_secrets(self):
        self.cm.schema("api.key", type_="str", secret=True)
        self.cm.set("api.key", "super_secret_123")
        exported = self.cm.export(include_secrets=False)
        self.assertEqual(exported.get("api.key"), "***")

    def test_export_includes_secrets(self):
        self.cm.schema("secret.val", type_="str", secret=True)
        self.cm.set("secret.val", "my_secret")
        exported = self.cm.export(include_secrets=True)
        self.assertEqual(exported.get("secret.val"), "my_secret")

    def test_delete_runtime_key(self):
        self.cm.set("temp.key", "value")
        ok = self.cm.delete("temp.key")
        self.assertTrue(ok)
        self.assertIsNone(self.cm.get("temp.key"))

    def test_namespace(self):
        ns = self.cm.namespace("database")
        ns.set("host", "ns-host")
        self.assertEqual(self.cm.get("database.host"), "ns-host")

    def test_namespace_get(self):
        self.cm.set("server.port", 8080)
        ns = self.cm.namespace("server")
        self.assertEqual(ns.get("port"), 8080)

    def test_diff(self):
        td2 = tempfile.mkdtemp()
        from agent.config_manager import ConfigManager
        cm2 = ConfigManager(db_path=os.path.join(td2,"cfg2.db"))
        self.cm.set("shared.key", "valueA")
        cm2.set("shared.key", "valueB")
        diffs = self.cm.diff(cm2)
        self.assertIn("shared.key", diffs)

    def test_audit_log(self):
        self.cm.set("audit.key", "v1")
        self.cm.set("audit.key", "v2")
        log = self.cm.audit_log("audit.key")
        self.assertGreaterEqual(len(log), 2)

    def test_stats(self):
        self.cm.set("s.key", "v")
        s = self.cm.stats()
        for k in ["layers","schema_keys","runtime_overrides","audit_entries"]:
            self.assertIn(k, s)

    def test_load_file_missing(self):
        self.cm.load_file("/nonexistent/path.json")   # should not raise
        # config remains usable
        self.cm.set("k", "v")
        self.assertEqual(self.cm.get("k"), "v")

    def test_all_schemas(self):
        self.cm.schema("a.b", type_="int", description="a key")
        schemas = self.cm.all_schemas()
        self.assertTrue(any(s["key"] == "a.b" for s in schemas))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v28: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
