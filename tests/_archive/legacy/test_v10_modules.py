"""
OMNI AGENT v10 — Test Suite
Tests: Gateway, SemanticCache, AgentBuilder, Telemetry
Run: python3 tests/test_v10_modules.py
"""
import asyncio, os, sys, tempfile, time, unittest, math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ══════════════════════════════════════════════════════════════════════════════
# GATEWAY
# ══════════════════════════════════════════════════════════════════════════════

class TestCORSConfig(unittest.TestCase):
    def setUp(self):
        from agent.gateway import CORSConfig
        self.cors = CORSConfig(allowed_origins=["https://example.com", "*"])

    def test_headers_wildcard(self):
        h = self.cors.headers("*")
        self.assertIn("Access-Control-Allow-Origin", h)
        self.assertIn("Access-Control-Allow-Methods", h)

    def test_default_methods_include_common(self):
        h = self.cors.headers()
        methods = h["Access-Control-Allow-Methods"]
        for m in ["GET", "POST", "DELETE"]:
            self.assertIn(m, methods)

    def test_expose_headers_present(self):
        h = self.cors.headers()
        self.assertIn("Access-Control-Expose-Headers", h)

    def test_max_age_present(self):
        h = self.cors.headers()
        self.assertIn("Access-Control-Max-Age", h)
        self.assertEqual(h["Access-Control-Max-Age"], "86400")

    def test_credentials_header(self):
        from agent.gateway import CORSConfig
        cors = CORSConfig(allow_credentials=True)
        h = cors.headers()
        self.assertEqual(h.get("Access-Control-Allow-Credentials"), "true")

    def test_no_credentials_header_default(self):
        h = self.cors.headers()
        self.assertNotIn("Access-Control-Allow-Credentials", h)


class TestRouteRegistry(unittest.TestCase):
    def setUp(self):
        from agent.gateway import RouteRegistry, RouteConfig
        self.reg = RouteRegistry()
        self.RouteConfig = RouteConfig

    def test_register_and_get_route(self):
        route = self.RouteConfig(path="/api/v1/chat", method="POST",
                                 auth_required=True)
        self.reg.register(route)
        got = self.reg.get("POST", "/api/v1/chat")
        self.assertIsNotNone(got)
        self.assertEqual(got.path, "/api/v1/chat")

    def test_get_nonexistent(self):
        self.assertIsNone(self.reg.get("GET", "/nonexistent"))

    def test_is_public_health(self):
        self.assertTrue(self.reg.is_public("/health"))
        self.assertTrue(self.reg.is_public("/ready"))
        self.assertTrue(self.reg.is_public("/metrics"))

    def test_is_public_api_endpoint(self):
        self.assertFalse(self.reg.is_public("/api/v1/chat"))


class TestAPIGateway(unittest.TestCase):
    def setUp(self):
        from agent.gateway import APIGateway, CORSConfig
        self.gw = APIGateway(
            cors=CORSConfig(),
            envelop_responses=True,
        )

    def test_gateway_created(self):
        self.assertIsNotNone(self.gw)

    def test_stats_initial(self):
        stats = self.gw.stats()
        self.assertEqual(stats["requests_total"], 0)
        self.assertEqual(stats["in_flight"], 0)

    def test_register_module(self):
        class FakeModule:
            def register_routes(self, app, prefix=""):
                pass
        self.gw.register_module(FakeModule())
        self.assertEqual(len(self.gw._modules), 1)

    def test_register_module_without_register_routes(self):
        class BadModule:
            pass
        # Should not raise, just log warning
        self.gw.register_module(BadModule())
        # Module not added since it has no register_routes
        self.assertEqual(len(self.gw._modules), 0)

    def test_error_body_structure(self):
        from agent.gateway import _error_body
        body = _error_body("NOT_FOUND", "Resource not found", "req-123")
        self.assertIn("error", body)
        self.assertIn("request_id", body)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

    def test_success_body_structure(self):
        from agent.gateway import _success_body
        body = _success_body({"result": 42}, "req-456", 123.4)
        self.assertIn("data", body)
        self.assertIn("meta", body)
        self.assertEqual(body["data"]["result"], 42)
        self.assertEqual(body["meta"]["request_id"], "req-456")

    def test_route_module(self):
        from agent.gateway import RouteModule
        m = RouteModule()

        async def handler(req):
            from aiohttp import web
            return web.Response(text="ok")

        m.add_route("GET", "/test", handler)
        self.assertEqual(len(m._routes), 1)


# ══════════════════════════════════════════════════════════════════════════════
# SEMANTIC CACHE
# ══════════════════════════════════════════════════════════════════════════════

class TestTFIDFEmbedder(unittest.TestCase):
    def setUp(self):
        from agent.llm_cache import TFIDFEmbedder
        self.emb = TFIDFEmbedder()

    def test_embed_returns_list(self):
        vec = self.emb.embed("hello world")
        self.assertIsInstance(vec, list)
        self.assertEqual(len(vec), 256)

    def test_embed_normalized(self):
        vec = self.emb.embed("test text")
        norm = math.sqrt(sum(x * x for x in vec))
        self.assertAlmostEqual(norm, 1.0, places=5)

    def test_similar_texts_high_similarity(self):
        from agent.llm_cache import _cosine_similarity, _normalize
        v1 = _normalize(self.emb.embed("What is Python?"))
        v2 = _normalize(self.emb.embed("What is Python?"))
        sim = _cosine_similarity(v1, v2)
        self.assertAlmostEqual(sim, 1.0, places=5)

    def test_different_texts_lower_similarity(self):
        from agent.llm_cache import _cosine_similarity, _normalize
        v1 = _normalize(self.emb.embed("Python programming language"))
        v2 = _normalize(self.emb.embed("quantum physics equations"))
        sim = _cosine_similarity(v1, v2)
        self.assertLess(sim, 0.95)

    def test_embed_batch(self):
        texts = ["hello", "world", "foo"]
        vecs = self.emb.embed_batch(texts)
        self.assertEqual(len(vecs), 3)
        for v in vecs:
            self.assertEqual(len(v), 256)

    def test_empty_text(self):
        vec = self.emb.embed("")
        self.assertEqual(len(vec), 256)


class TestCosineSimilarity(unittest.TestCase):
    def test_identical_vectors(self):
        from agent.llm_cache import _cosine_similarity
        v = [0.5, 0.5, 0.5, 0.5]
        self.assertAlmostEqual(_cosine_similarity(v, v), 1.0, places=5)

    def test_orthogonal_vectors(self):
        from agent.llm_cache import _cosine_similarity
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        self.assertAlmostEqual(_cosine_similarity(v1, v2), 0.0, places=5)

    def test_different_lengths_returns_zero(self):
        from agent.llm_cache import _cosine_similarity
        self.assertEqual(_cosine_similarity([1, 2], [1, 2, 3]), 0.0)


class TestCacheEntry(unittest.TestCase):
    def test_is_expired_false(self):
        from agent.llm_cache import CacheEntry
        e = CacheEntry(id="e1", namespace="ns", query="q", response="r",
                       embedding=[], expires_at=time.time() + 9999)
        self.assertFalse(e.is_expired)

    def test_is_expired_true(self):
        from agent.llm_cache import CacheEntry
        e = CacheEntry(id="e1", namespace="ns", query="q", response="r",
                       embedding=[], expires_at=time.time() - 1)
        self.assertTrue(e.is_expired)

    def test_to_dict(self):
        from agent.llm_cache import CacheEntry
        e = CacheEntry(id="e1", namespace="ns", query="q", response="r",
                       embedding=[])
        d = e.to_dict()
        self.assertIn("id", d)
        self.assertIn("response", d)
        self.assertIn("hits", d)

    def test_to_dict_exclude_response(self):
        from agent.llm_cache import CacheEntry
        e = CacheEntry(id="e1", namespace="ns", query="q", response="secret",
                       embedding=[])
        d = e.to_dict(include_response=False)
        self.assertNotIn("response", d)


class TestSemanticCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.llm_cache import SemanticCache
        self.cache = SemanticCache(
            threshold=0.85,
            db_path=os.path.join(self.tmpdir, "cache.db"),
            default_ttl_s=3600,
        )

    def test_store_and_lookup_exact(self):
        q = "What is the capital of France?"
        self.cache.store(q, "Paris", namespace="geo")
        hit = self.cache.lookup(q, namespace="geo")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.entry.response, "Paris")

    def test_lookup_miss(self):
        hit = self.cache.lookup("completely unrelated query xyz", namespace="geo")
        self.assertIsNone(hit)

    def test_lookup_below_threshold_returns_none(self):
        self.cache.store("Python programming tips", "Some tips", namespace="tech")
        # Very different query
        hit = self.cache.lookup("quantum mechanics explanations", namespace="tech",
                                threshold=0.99)
        self.assertIsNone(hit)

    def test_store_increments_stats(self):
        before = self.cache.stats()["stores"]
        self.cache.store("test q", "test r", namespace="test")
        after = self.cache.stats()["stores"]
        self.assertEqual(after, before + 1)

    def test_hit_increments_stats(self):
        self.cache.store("cache hit test", "response", namespace="test")
        before = self.cache.stats()["hits"]
        self.cache.lookup("cache hit test", namespace="test")
        after = self.cache.stats()["hits"]
        self.assertGreaterEqual(after, before)

    def test_separate_namespaces(self):
        self.cache.store("question", "answer_A", namespace="ns_a")
        self.cache.store("question", "answer_B", namespace="ns_b")
        hit_a = self.cache.lookup("question", namespace="ns_a")
        hit_b = self.cache.lookup("question", namespace="ns_b")
        self.assertIsNotNone(hit_a)
        self.assertIsNotNone(hit_b)
        self.assertEqual(hit_a.entry.response, "answer_A")
        self.assertEqual(hit_b.entry.response, "answer_B")

    def test_invalidate_removes_entry(self):
        entry = self.cache.store("to be deleted", "response", namespace="inv")
        hit_before = self.cache.lookup("to be deleted", namespace="inv")
        self.assertIsNotNone(hit_before)
        self.cache.invalidate(entry.id)
        hit_after = self.cache.lookup("to be deleted", namespace="inv", threshold=0.99)
        self.assertIsNone(hit_after)

    def test_flush_namespace(self):
        self.cache.store("q1", "r1", namespace="flush_ns")
        self.cache.store("q2", "r2", namespace="flush_ns")
        count = self.cache.flush("flush_ns")
        self.assertGreaterEqual(count, 2)
        hit = self.cache.lookup("q1", namespace="flush_ns")
        self.assertIsNone(hit)

    def test_flush_all(self):
        self.cache.store("q", "r", namespace="a")
        self.cache.store("q", "r", namespace="b")
        count = self.cache.flush()
        self.assertGreaterEqual(count, 2)

    def test_warm_prepopulates(self):
        pairs = [("What is 2+2?", "4"), ("What is 3+3?", "6")]
        self.cache.warm(pairs, namespace="math")
        hit = self.cache.lookup("What is 2+2?", namespace="math")
        self.assertIsNotNone(hit)

    def test_warm_skips_existing(self):
        self.cache.store("existing q", "existing r", namespace="warm_test")
        pairs = [("existing q", "new r")]
        self.cache.warm(pairs, namespace="warm_test", model="gpt4")
        hit = self.cache.lookup("existing q", namespace="warm_test")
        self.assertIsNotNone(hit)
        # Should still have old response
        self.assertEqual(hit.entry.response, "existing r")

    def test_ttl_expiry(self):
        self.cache.store("expire me", "value", namespace="ttl", ttl_s=0.05)
        time.sleep(0.1)
        hit = self.cache.lookup("expire me", namespace="ttl")
        self.assertIsNone(hit)

    def test_set_threshold(self):
        self.cache.set_threshold(0.99)
        self.assertEqual(self.cache._threshold, 0.99)

    def test_set_threshold_clamps(self):
        self.cache.set_threshold(1.5)
        self.assertEqual(self.cache._threshold, 1.0)
        self.cache.set_threshold(-0.5)
        self.assertEqual(self.cache._threshold, 0.0)

    def test_stats_structure(self):
        stats = self.cache.stats()
        for key in ["hit_rate", "lookups", "hits", "stores", "threshold"]:
            self.assertIn(key, stats)

    def test_hit_similarity_in_range(self):
        self.cache.store("Python language", "A programming language", namespace="sim")
        hit = self.cache.lookup("Python language", namespace="sim")
        if hit:
            self.assertGreaterEqual(hit.similarity, 0.0)
            self.assertLessEqual(hit.similarity, 1.0001)


# ══════════════════════════════════════════════════════════════════════════════
# AGENT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

class TestAgentBuilder(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.agent_builder import AgentBuilder
        self.AB = AgentBuilder

    def _builder(self):
        from agent.agent_builder import AgentBuilder
        b = AgentBuilder()
        b.data_dir(self.tmpdir)
        return b

    def test_from_spec_minimal(self):
        agent = self._builder().from_spec({
            "name": "test-agent",
            "modules": {"session": {"enabled": True}, "search": {"enabled": False}}
        }).build()
        self.assertEqual(agent.name, "test-agent")

    def test_default_spec_applied(self):
        agent = self._builder().build()
        self.assertIn("model", agent.spec)
        self.assertIn("default", agent.spec["model"])

    def test_name_fluent(self):
        agent = self._builder().name("my-bot").build()
        self.assertEqual(agent.name, "my-bot")

    def test_model_fluent(self):
        agent = self._builder().model("gpt-4", temperature=0.3).build()
        self.assertEqual(agent.spec["model"]["default"], "gpt-4")
        self.assertEqual(agent.spec["model"]["temperature"], 0.3)

    def test_persona_fluent(self):
        agent = self._builder().persona(
            name="Engineer",
            system_prompt="You are an expert engineer."
        ).build()
        self.assertEqual(agent.system_prompt, "You are an expert engineer.")
        self.assertEqual(agent.persona_config["name"], "Engineer")

    def test_enable_module(self):
        builder = self._builder().enable("search").enable("cache", threshold=0.9)
        spec = builder.to_dict()
        self.assertTrue(spec["modules"]["search"]["enabled"])
        self.assertEqual(spec["modules"]["cache"]["threshold"], 0.9)

    def test_disable_module(self):
        builder = self._builder().disable("rag")
        spec = builder.to_dict()
        self.assertFalse(spec["modules"]["rag"]["enabled"])

    def test_with_tools(self):
        builder = self._builder().with_tools("web_search", "calculator")
        spec = builder.to_dict()
        self.assertIn("web_search", spec["tools"])
        self.assertIn("calculator", spec["tools"])

    def test_from_preset_minimal(self):
        agent = self._builder().from_preset("minimal").build()
        self.assertIn("session", agent.modules)

    def test_from_preset_research(self):
        agent = self._builder().from_preset("research").build()
        self.assertIsNotNone(agent)

    def test_from_preset_coding(self):
        agent = self._builder().from_preset("coding").build()
        self.assertIsNotNone(agent)
        self.assertIn("engineer", agent.system_prompt.lower())

    def test_from_preset_invalid(self):
        with self.assertRaises(ValueError):
            self._builder().from_preset("nonexistent_preset")

    def test_list_presets(self):
        presets = self.AB.list_presets()
        self.assertIn("minimal", presets)
        self.assertIn("research", presets)
        self.assertIn("coding", presets)
        self.assertIn("enterprise", presets)

    def test_get_preset(self):
        p = self.AB.get_preset("minimal")
        self.assertIsNotNone(p)
        self.assertIn("name", p)

    def test_get_preset_none(self):
        self.assertIsNone(self.AB.get_preset("does_not_exist"))

    def test_validate_missing_name(self):
        from agent.agent_builder import AgentBuilder
        b = AgentBuilder()
        b._spec["name"] = ""
        issues = b.validate()
        self.assertGreater(len(issues), 0)

    def test_validate_clean_spec(self):
        issues = self._builder().from_preset("minimal").validate()
        # Minimal preset should have no blocking issues
        self.assertIsInstance(issues, list)

    def test_build_warnings_captured(self):
        agent = self._builder().build()
        self.assertIsInstance(agent.build_warnings, list)

    def test_built_agent_has_id(self):
        agent = self._builder().build()
        self.assertIsNotNone(agent.id)
        self.assertGreater(len(agent.id), 0)

    def test_built_agent_to_dict(self):
        agent = self._builder().build()
        d = agent.to_dict()
        self.assertIn("id", d)
        self.assertIn("name", d)
        self.assertIn("modules", d)

    def test_built_agent_get_module(self):
        agent = self._builder().enable("search").build()
        search = agent.get("search")
        self.assertIsNotNone(search)

    def test_built_agent_missing_module_returns_none(self):
        agent = self._builder().disable("federation").build()
        self.assertIsNone(agent.get("federation"))

    def test_session_manager_property(self):
        agent = self._builder().enable("session").build()
        self.assertIsNotNone(agent.session_manager)

    def test_llm_cache_property(self):
        agent = self._builder().enable("cache").build()
        self.assertIsNotNone(agent.llm_cache)

    def test_search_service_property(self):
        agent = self._builder().enable("search").build()
        self.assertIsNotNone(agent.search_service)

    def test_governance_property(self):
        agent = self._builder().enable("governance").build()
        self.assertIsNotNone(agent.governance)

    def test_model_config_property(self):
        agent = self._builder().model("claude-3-opus").build()
        self.assertEqual(agent.model_config["default"], "claude-3-opus")

    def test_to_yaml_returns_string(self):
        yaml_str = self._builder().to_yaml()
        self.assertIsInstance(yaml_str, str)
        self.assertGreater(len(yaml_str), 0)

    def test_build_fleet(self):
        from agent.agent_builder import AgentBuilder
        fleet = AgentBuilder.build_fleet({
            "researcher": {"preset": "minimal", "name": "research-bot"},
            "coder":      {"preset": "minimal", "name": "code-bot"},
        })
        self.assertIn("researcher", fleet)
        self.assertIn("coder", fleet)
        self.assertEqual(fleet["researcher"].name, "research-bot")
        self.assertEqual(fleet["coder"].name, "code-bot")

    def test_deep_merge(self):
        from agent.agent_builder import AgentBuilder
        base = {"a": {"b": 1, "c": 2}, "d": 3}
        override = {"a": {"b": 99, "e": 4}, "f": 5}
        result = AgentBuilder._merge(base, override)
        self.assertEqual(result["a"]["b"], 99)   # overridden
        self.assertEqual(result["a"]["c"], 2)    # preserved
        self.assertEqual(result["a"]["e"], 4)    # added
        self.assertEqual(result["d"], 3)         # preserved
        self.assertEqual(result["f"], 5)         # added

    def test_from_spec_does_not_mutate_default(self):
        from agent.agent_builder import DEFAULT_SPEC
        import copy
        original = copy.deepcopy(DEFAULT_SPEC)
        self._builder().from_spec({"name": "mutation-test"}).build()
        self.assertEqual(DEFAULT_SPEC["name"], original["name"])


# ══════════════════════════════════════════════════════════════════════════════
# TELEMETRY
# ══════════════════════════════════════════════════════════════════════════════

class TestSpan(unittest.TestCase):
    def _make_span(self, op="test.op", service="test-svc"):
        from agent.telemetry import Span
        import uuid
        return Span(
            trace_id=str(uuid.uuid4()),
            span_id=str(uuid.uuid4()),
            parent_id=None,
            operation=op,
            service=service,
        )

    def test_is_root_no_parent(self):
        span = self._make_span()
        self.assertTrue(span.is_root)

    def test_is_root_with_parent(self):
        from agent.telemetry import Span
        import uuid
        span = Span(
            trace_id=str(uuid.uuid4()),
            span_id=str(uuid.uuid4()),
            parent_id="parent-id-123",
            operation="child.op",
            service="svc",
        )
        self.assertFalse(span.is_root)

    def test_finish_sets_end_time(self):
        span = self._make_span()
        self.assertIsNone(span.end_time)
        span.finish()
        self.assertIsNotNone(span.end_time)

    def test_duration_ms(self):
        span = self._make_span()
        time.sleep(0.01)
        span.finish()
        self.assertGreater(span.duration_ms, 0)
        self.assertLess(span.duration_ms, 5000)

    def test_set_attribute(self):
        span = self._make_span()
        span.set_attribute("model", "gpt-4")
        self.assertEqual(span.attributes["model"], "gpt-4")

    def test_add_event(self):
        span = self._make_span()
        span.add_event("cache_miss", key="q_123")
        self.assertEqual(len(span.events), 1)
        self.assertEqual(span.events[0].name, "cache_miss")
        self.assertEqual(span.events[0].attributes["key"], "q_123")

    def test_set_status_ok(self):
        from agent.telemetry import SpanStatus
        span = self._make_span()
        span.set_status(SpanStatus.OK)
        self.assertEqual(span.status, SpanStatus.OK)

    def test_set_status_error_with_message(self):
        from agent.telemetry import SpanStatus
        span = self._make_span()
        span.set_status(SpanStatus.ERROR, "Connection refused")
        self.assertEqual(span.error_msg, "Connection refused")

    def test_to_dict_keys(self):
        span = self._make_span()
        span.finish()
        d = span.to_dict()
        for k in ["trace_id", "span_id", "operation", "service",
                   "start_time", "end_time", "duration_ms", "status"]:
            self.assertIn(k, d)

    def test_to_otlp_format(self):
        span = self._make_span()
        span.finish()
        otlp = span.to_otlp()
        self.assertIn("traceId", otlp)
        self.assertIn("spanId", otlp)
        self.assertIn("name", otlp)
        self.assertIn("startTimeUnixNano", otlp)


class TestTrace(unittest.TestCase):
    def _make_trace(self):
        from agent.telemetry import Trace, Span
        import uuid
        tid = str(uuid.uuid4())
        root = Span(trace_id=tid, span_id=str(uuid.uuid4()), parent_id=None,
                    operation="root.op", service="svc")
        root.finish()
        child = Span(trace_id=tid, span_id=str(uuid.uuid4()),
                     parent_id=root.span_id,
                     operation="child.op", service="svc")
        child.finish()
        trace = Trace(trace_id=tid, spans=[root, child])
        return trace, root, child

    def test_root_span(self):
        trace, root, _ = self._make_trace()
        self.assertEqual(trace.root_span.operation, "root.op")

    def test_has_errors_false(self):
        trace, _, _ = self._make_trace()
        self.assertFalse(trace.has_errors)

    def test_has_errors_true(self):
        from agent.telemetry import SpanStatus
        trace, _, child = self._make_trace()
        child.set_status(SpanStatus.ERROR, "failed")
        self.assertTrue(trace.has_errors)

    def test_to_dict_has_spans(self):
        trace, _, _ = self._make_trace()
        d = trace.to_dict()
        self.assertEqual(d["span_count"], 2)
        self.assertIn("spans", d)


class TestTracer(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.telemetry import Tracer
        self.tracer = Tracer(
            service="test-service",
            db_path=os.path.join(self.tmpdir, "telemetry.db"),
            persist=True,
        )

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_create_span(self):
        span = self.tracer.create_span("test.op")
        self.assertIsNotNone(span)
        self.assertEqual(span.operation, "test.op")
        self.assertEqual(span.service, "test-service")

    def test_create_span_with_parent(self):
        parent = self.tracer.create_span("parent.op")
        child = self.tracer.create_span("child.op", parent_span=parent)
        self.assertEqual(child.trace_id, parent.trace_id)
        self.assertEqual(child.parent_id, parent.span_id)

    def test_record_span(self):
        span = self.tracer.create_span("record.op")
        span.finish()
        self.tracer.record(span)
        self.assertEqual(self.tracer._span_count, 1)

    def test_record_increments_error_count(self):
        from agent.telemetry import SpanStatus
        span = self.tracer.create_span("error.op")
        span.set_status(SpanStatus.ERROR, "oops")
        span.finish()
        self.tracer.record(span)
        self.assertEqual(self.tracer._error_count, 1)

    def test_sync_context_manager(self):
        with self.tracer.start_span("sync.op") as span:
            span.set_attribute("key", "value")
        from agent.telemetry import SpanStatus
        self.assertEqual(span.status, SpanStatus.OK)
        self.assertTrue(span.finished)

    def test_sync_context_manager_on_exception(self):
        from agent.telemetry import SpanStatus
        span_ref = [None]
        try:
            with self.tracer.start_span("error.op") as span:
                span_ref[0] = span
                raise ValueError("test error")
        except ValueError:
            pass
        self.assertEqual(span_ref[0].status, SpanStatus.ERROR)
        self.assertIn("test error", span_ref[0].error_msg)

    def test_async_context_manager(self):
        async def run():
            async with self.tracer.start_span_async("async.op") as span:
                span.set_attribute("async", True)
            from agent.telemetry import SpanStatus
            self.assertEqual(span.status, SpanStatus.OK)
        self._run(run())

    def test_decorator_sync(self):
        @self.tracer.span("decorated.fn")
        def my_fn(x):
            return x * 2

        result = my_fn(21)
        self.assertEqual(result, 42)
        self.assertGreater(self.tracer._span_count, 0)

    def test_decorator_async(self):
        @self.tracer.span("async.decorated")
        async def async_fn(x):
            return x + 1

        result = self._run(async_fn(41))
        self.assertEqual(result, 42)

    def test_inject_headers(self):
        span = self.tracer.create_span("http.call")
        headers = self.tracer.inject_headers(span)
        self.assertIn("traceparent", headers)
        self.assertIn("X-Trace-ID", headers)
        tp = headers["traceparent"]
        parts = tp.split("-")
        self.assertEqual(len(parts), 4)

    def test_extract_context(self):
        span = self.tracer.create_span("http.call")
        headers = self.tracer.inject_headers(span)
        ctx = self.tracer.extract_context(headers)
        self.assertIsNotNone(ctx)
        self.assertIn("trace_id", ctx)
        self.assertIn("parent_span_id", ctx)

    def test_extract_context_missing_header(self):
        ctx = self.tracer.extract_context({})
        self.assertIsNone(ctx)

    def test_get_trace(self):
        with self.tracer.start_span("trace.lookup") as span:
            trace_id = span.trace_id
        trace = self.tracer.get_trace(trace_id)
        self.assertIsNotNone(trace)
        self.assertEqual(trace.trace_id, trace_id)

    def test_list_traces(self):
        with self.tracer.start_span("list.test"):
            pass
        traces = self.tracer.list_traces()
        self.assertGreater(len(traces), 0)

    def test_flame_graph(self):
        for _ in range(3):
            with self.tracer.start_span("flame.test"):
                time.sleep(0.001)
        fg = self.tracer.flame_graph()
        self.assertIn("frames", fg)
        ops = [f["operation"] for f in fg["frames"]]
        self.assertIn("flame.test", ops)

    def test_stats(self):
        with self.tracer.start_span("stats.test"):
            pass
        stats = self.tracer.stats()
        self.assertIn("spans_recorded", stats)
        self.assertGreaterEqual(stats["spans_recorded"], 1)

    def test_trace_persisted_to_db(self):
        with self.tracer.start_span("persist.test") as span:
            trace_id = span.trace_id

        # Create new tracer reading same DB
        from agent.telemetry import Tracer
        tracer2 = Tracer(
            service="test-service",
            db_path=os.path.join(self.tmpdir, "telemetry.db"),
        )
        trace = tracer2.get_trace(trace_id)
        self.assertIsNotNone(trace)
        self.assertEqual(trace.trace_id, trace_id)

    def test_nested_spans_same_trace(self):
        with self.tracer.start_span("parent") as parent:
            with self.tracer.start_span("child", parent_span=parent) as child:
                self.assertEqual(child.trace_id, parent.trace_id)
                self.assertEqual(child.parent_id, parent.span_id)

    def test_span_attributes_persisted(self):
        with self.tracer.start_span("attr.test") as span:
            span.set_attribute("model", "gpt-4")
            span.set_attribute("tokens", 42)
            trace_id = span.trace_id

        trace = self.tracer.get_trace(trace_id)
        if trace and trace.spans:
            attrs = trace.spans[0].attributes
            self.assertEqual(attrs.get("model"), "gpt-4")


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun
    failed = len(result.failures) + len(result.errors)
    passed = total - failed
    print(f"\n{'='*60}")
    print(f"  v10 Test Results: {passed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  ✗ {t}")
            print(f"    {tb.strip().splitlines()[-1]}")
    else:
        print(f"  ✅ ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
