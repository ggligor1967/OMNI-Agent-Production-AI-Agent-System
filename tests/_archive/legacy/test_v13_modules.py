"""
OMNI AGENT v13 — Test Suite
Tests: VectorStore, OutputValidator, ConversationRouter, EventStore
Run: python3 tests/test_v13_modules.py
"""
import asyncio, json, os, sys, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ══════════════════════════════════════════════════════════════════════════════
# VECTOR STORE — math helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestVectorMath(unittest.TestCase):
    def test_l2_norm(self):
        from agent.vector_store import _l2_norm
        self.assertAlmostEqual(_l2_norm([3.0, 4.0]), 5.0)

    def test_normalize_unit(self):
        from agent.vector_store import _normalize
        v = _normalize([3.0, 4.0])
        norm = sum(x*x for x in v) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=5)

    def test_normalize_zero(self):
        from agent.vector_store import _normalize
        v = _normalize([0.0, 0.0])
        self.assertEqual(v, [0.0, 0.0])

    def test_cosine_identical(self):
        from agent.vector_store import _cosine, _normalize
        v = _normalize([1.0, 2.0, 3.0])
        self.assertAlmostEqual(_cosine(v, v), 1.0, places=5)

    def test_cosine_orthogonal(self):
        from agent.vector_store import _cosine
        self.assertAlmostEqual(_cosine([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_cosine_mismatched_dims(self):
        from agent.vector_store import _cosine
        self.assertEqual(_cosine([1.0, 2.0], [1.0]), 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# VECTOR STORE — NamespaceIndex
# ══════════════════════════════════════════════════════════════════════════════

class TestNamespaceIndex(unittest.TestCase):
    def setUp(self):
        from agent.vector_store import NamespaceIndex
        self.idx = NamespaceIndex()

    def test_add_and_len(self):
        self.idx.add("d1", [1.0, 0.0])
        self.assertEqual(len(self.idx), 1)

    def test_search_returns_results(self):
        self.idx.add("d1", [1.0, 0.0])
        self.idx.add("d2", [0.0, 1.0])
        results = self.idx.search([1.0, 0.0], top_k=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][1], "d1")

    def test_search_top_k(self):
        for i in range(10):
            self.idx.add(f"d{i}", [float(i), 0.0])
        results = self.idx.search([9.0, 0.0], top_k=3)
        self.assertEqual(len(results), 3)

    def test_remove(self):
        self.idx.add("d1", [1.0, 0.0])
        self.idx.remove("d1")
        self.assertEqual(len(self.idx), 0)

    def test_remove_nonexistent(self):
        self.idx.remove("ghost")   # should not raise
        self.assertEqual(len(self.idx), 0)

    def test_threshold_filters(self):
        self.idx.add("high", [1.0, 0.0])
        self.idx.add("low",  [0.0, 1.0])
        results = self.idx.search([1.0, 0.0], threshold=0.9)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], "high")

    def test_upsert_replaces(self):
        self.idx.add("d1", [1.0, 0.0])
        self.idx.add("d1", [0.0, 1.0])   # should replace
        self.assertEqual(len(self.idx), 1)


# ══════════════════════════════════════════════════════════════════════════════
# VECTOR STORE — VectorStore
# ══════════════════════════════════════════════════════════════════════════════

class TestVectorStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.vector_store import VectorStore
        self.store = VectorStore(
            db_path=os.path.join(self.tmpdir, "vecs.db")
        )

    def test_upsert_returns_document(self):
        doc = self.store.upsert("Hello world", namespace="test")
        self.assertIsNotNone(doc)
        self.assertEqual(doc.text, "Hello world")
        self.assertEqual(doc.namespace, "test")

    def test_upsert_custom_id(self):
        doc = self.store.upsert("Text", doc_id="my_doc")
        self.assertEqual(doc.id, "my_doc")

    def test_upsert_with_metadata(self):
        doc = self.store.upsert("Text", metadata={"source": "wiki"})
        self.assertEqual(doc.metadata["source"], "wiki")

    def test_upsert_with_tags(self):
        doc = self.store.upsert("Text", tags=["news", "tech"])
        self.assertIn("news", doc.tags)

    def test_get_existing(self):
        doc = self.store.upsert("Find me", doc_id="findable")
        got = self.store.get("findable")
        self.assertIsNotNone(got)
        self.assertEqual(got.text, "Find me")

    def test_get_nonexistent(self):
        self.assertIsNone(self.store.get("ghost_id"))

    def test_upsert_replaces_existing(self):
        self.store.upsert("Version 1", doc_id="doc1")
        self.store.upsert("Version 2", doc_id="doc1")
        doc = self.store.get("doc1")
        self.assertEqual(doc.text, "Version 2")

    def test_search_returns_results(self):
        self.store.upsert("Cats are mammals", namespace="facts")
        self.store.upsert("Dogs are loyal", namespace="facts")
        results = self.store.search("Tell me about cats", namespace="facts")
        self.assertGreater(len(results), 0)

    def test_search_returns_scored_results(self):
        self.store.upsert("Python programming language", namespace="tech")
        results = self.store.search("Python code", namespace="tech")
        for r in results:
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.01)

    def test_search_top_k(self):
        for i in range(10):
            self.store.upsert(f"Document {i}", namespace="ns")
        results = self.store.search("document", namespace="ns", top_k=3)
        self.assertLessEqual(len(results), 3)

    def test_search_threshold(self):
        self.store.upsert("Totally unrelated quantum physics", namespace="ns2")
        results = self.store.search("cooking recipes", namespace="ns2",
                                     threshold=0.99)
        self.assertEqual(len(results), 0)

    def test_search_filter_metadata(self):
        self.store.upsert("Article A", namespace="filtered",
                           metadata={"type": "news"})
        self.store.upsert("Article B", namespace="filtered",
                           metadata={"type": "blog"})
        results = self.store.search("Article", namespace="filtered",
                                     filter={"type": "news"})
        for r in results:
            self.assertEqual(r.document.metadata["type"], "news")

    def test_upsert_many(self):
        items = [{"text": f"Item {i}", "metadata": {"idx": i}}
                 for i in range(5)]
        docs = self.store.upsert_many(items, namespace="batch")
        self.assertEqual(len(docs), 5)

    def test_upsert_many_searchable(self):
        items = [{"text": "Apple fruit"}, {"text": "Orange citrus"}]
        self.store.upsert_many(items, namespace="fruit")
        results = self.store.search("Apple", namespace="fruit")
        self.assertGreater(len(results), 0)

    def test_search_many(self):
        self.store.upsert("Python is great", namespace="lang")
        results = self.store.search_many(["Python", "Java"], namespace="lang")
        self.assertIn("Python", results)
        self.assertIn("Java", results)

    def test_delete_document(self):
        self.store.upsert("Delete me", doc_id="del_me")
        ok = self.store.delete("del_me")
        self.assertTrue(ok)
        self.assertIsNone(self.store.get("del_me"))

    def test_delete_nonexistent(self):
        ok = self.store.delete("not_there")
        self.assertFalse(ok)

    def test_delete_namespace(self):
        self.store.upsert("A", namespace="ns_del")
        self.store.upsert("B", namespace="ns_del")
        count = self.store.delete_namespace("ns_del")
        self.assertGreaterEqual(count, 2)
        self.assertEqual(self.store.count("ns_del"), 0)

    def test_count_total(self):
        self.store.upsert("X", namespace="cnt")
        self.store.upsert("Y", namespace="cnt")
        self.assertGreaterEqual(self.store.count(), 2)

    def test_count_by_namespace(self):
        self.store.upsert("X", namespace="cnt2")
        self.store.upsert("Y", namespace="cnt2")
        self.assertEqual(self.store.count("cnt2"), 2)

    def test_list_namespaces(self):
        self.store.upsert("A", namespace="ns_a")
        self.store.upsert("B", namespace="ns_b")
        namespaces = self.store.list_namespaces()
        self.assertIn("ns_a", namespaces)
        self.assertIn("ns_b", namespaces)

    def test_stats(self):
        self.store.upsert("A", namespace="s1")
        stats = self.store.stats()
        self.assertIn("total_documents", stats)
        self.assertIn("namespaces", stats)
        self.assertGreaterEqual(stats["total_documents"], 1)

    def test_snapshot(self):
        self.store.upsert("A", namespace="snap_ns")
        snap = self.store.snapshot("snap_ns")
        self.assertIsInstance(snap, list)
        self.assertGreater(len(snap), 0)

    def test_persistence_survives_reload(self):
        from agent.vector_store import VectorStore
        self.store.upsert("Persistent text", doc_id="persist_doc",
                           namespace="persist_ns")
        store2 = VectorStore(db_path=os.path.join(self.tmpdir, "vecs.db"))
        doc = store2.get("persist_doc")
        self.assertIsNotNone(doc)
        self.assertEqual(doc.text, "Persistent text")

    def test_search_result_to_dict(self):
        self.store.upsert("Test doc", namespace="dict_ns")
        results = self.store.search("Test", namespace="dict_ns")
        if results:
            d = results[0].to_dict()
            self.assertIn("score", d)
            self.assertIn("document", d)

    def test_custom_embedding(self):
        def custom_embed(text):
            return [1.0, 0.0, 0.0]  # always same vector
        from agent.vector_store import VectorStore
        store = VectorStore(
            embedder=custom_embed,
            db_path=os.path.join(self.tmpdir, "custom_emb.db")
        )
        store.upsert("Any text", namespace="custom")
        results = store.search("anything", namespace="custom",
                                query_embedding=[1.0, 0.0, 0.0])
        self.assertGreater(len(results), 0)


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT VALIDATOR — JSON extraction & repair
# ══════════════════════════════════════════════════════════════════════════════

class TestJsonExtraction(unittest.TestCase):
    def test_extract_clean_json(self):
        from agent.output_validator import _extract_json
        j = _extract_json('{"name": "Alice"}')
        self.assertIsNotNone(j)

    def test_extract_from_markdown_block(self):
        from agent.output_validator import _extract_json
        text = '```json\n{"key": "value"}\n```'
        j = _extract_json(text)
        self.assertIsNotNone(j)
        self.assertIn('"key"', j)

    def test_extract_from_prose(self):
        from agent.output_validator import _extract_json
        text = 'Here is the result: {"score": 42} — done.'
        j = _extract_json(text)
        self.assertIsNotNone(j)

    def test_extract_array(self):
        from agent.output_validator import _extract_json
        j = _extract_json('Result: [1, 2, 3]')
        self.assertIsNotNone(j)

    def test_extract_missing_returns_none(self):
        from agent.output_validator import _extract_json
        j = _extract_json("No JSON here at all.")
        self.assertIsNone(j)


class TestJsonRepair(unittest.TestCase):
    def test_trailing_comma(self):
        from agent.output_validator import _repair_json
        repaired, repairs = _repair_json('{"a": 1,}')
        self.assertIsNotNone(repaired)
        self.assertIn("removed_trailing_commas", repairs)

    def test_python_true_false(self):
        from agent.output_validator import _repair_json
        repaired, repairs = _repair_json('{"ok": True, "bad": False}')
        self.assertIsNotNone(repaired)
        data = json.loads(repaired)
        self.assertEqual(data["ok"], True)

    def test_python_none(self):
        from agent.output_validator import _repair_json
        repaired, repairs = _repair_json('{"val": None}')
        self.assertIsNotNone(repaired)
        data = json.loads(repaired)
        self.assertIsNone(data["val"])

    def test_no_repair_needed(self):
        from agent.output_validator import _repair_json
        repaired, repairs = _repair_json('{"a": 1}')
        self.assertIsNotNone(repaired)

    def test_unrepairable_returns_none(self):
        from agent.output_validator import _repair_json
        repaired, _ = _repair_json("This is not JSON at all @@##")
        self.assertIsNone(repaired)


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT VALIDATOR — type coercion
# ══════════════════════════════════════════════════════════════════════════════

class TestTypeCoercion(unittest.TestCase):
    def test_str_coercion(self):
        from agent.output_validator import _coerce
        val, ok = _coerce(42, "str")
        self.assertTrue(ok)
        self.assertEqual(val, "42")

    def test_int_coercion(self):
        from agent.output_validator import _coerce
        val, ok = _coerce("42", "int")
        self.assertTrue(ok)
        self.assertEqual(val, 42)

    def test_float_from_string(self):
        from agent.output_validator import _coerce
        val, ok = _coerce("3.14", "float")
        self.assertTrue(ok)
        self.assertAlmostEqual(val, 3.14)

    def test_bool_true_variants(self):
        from agent.output_validator import _coerce
        for s in ["true", "1", "yes", "on"]:
            val, ok = _coerce(s, "bool")
            self.assertTrue(ok and val, f"Failed for '{s}'")

    def test_bool_false_variants(self):
        from agent.output_validator import _coerce
        for s in ["false", "0", "no", "off"]:
            val, ok = _coerce(s, "bool")
            self.assertTrue(ok and not val, f"Failed for '{s}'")

    def test_list_from_csv(self):
        from agent.output_validator import _coerce
        val, ok = _coerce("a, b, c", "list")
        self.assertTrue(ok)
        self.assertEqual(len(val), 3)

    def test_list_passthrough(self):
        from agent.output_validator import _coerce
        val, ok = _coerce([1, 2, 3], "list")
        self.assertTrue(ok)
        self.assertEqual(val, [1, 2, 3])

    def test_invalid_int(self):
        from agent.output_validator import _coerce
        val, ok = _coerce("not_a_number", "int")
        self.assertFalse(ok)


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT VALIDATOR — OutputValidator
# ══════════════════════════════════════════════════════════════════════════════

class TestOutputValidator(unittest.TestCase):
    def setUp(self):
        from agent.output_validator import OutputValidator
        self.v = OutputValidator()

    def test_extract_clean_json(self):
        from agent.output_validator import ValidationStatus
        result = self.v.extract_json('{"name": "Alice"}')
        self.assertTrue(result.ok)
        self.assertEqual(result.data["name"], "Alice")

    def test_extract_from_prose(self):
        result = self.v.extract_json(
            'Here is the result: {"score": 99} — end.'
        )
        self.assertTrue(result.ok)

    def test_extract_repaired(self):
        from agent.output_validator import ValidationStatus
        result = self.v.extract_json('{"name": "Bob", "ok": True,}')
        self.assertTrue(result.ok)
        self.assertEqual(result.status, ValidationStatus.REPAIRED)

    def test_extract_fail(self):
        from agent.output_validator import ValidationStatus
        result = self.v.extract_json("No JSON at all.")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, ValidationStatus.FAIL)

    def test_validate_schema_pass(self):
        schema = {
            "name": {"type": "str", "required": True},
            "age":  {"type": "int", "required": True},
        }
        result = self.v.validate('{"name": "Alice", "age": 30}', schema=schema)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["name"], "Alice")

    def test_validate_schema_type_coercion(self):
        schema = {"age": {"type": "int", "required": True}}
        result = self.v.validate('{"age": "25"}', schema=schema)
        self.assertTrue(result.ok)
        self.assertIsInstance(result.data["age"], int)

    def test_validate_schema_required_missing(self):
        from agent.output_validator import ValidationStatus
        schema = {"name": {"type": "str", "required": True}}
        result = self.v.validate('{"other": "value"}', schema=schema)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, ValidationStatus.FAIL)

    def test_validate_schema_default_applied(self):
        schema = {"score": {"type": "float", "default": 0.0}}
        result = self.v.validate('{}', schema=schema)
        self.assertTrue(result.ok)
        self.assertIn("score", result.repairs_applied[0])

    def test_validate_schema_choices(self):
        schema = {"role": {"type": "str", "choices": ["admin", "user"]}}
        result = self.v.validate('{"role": "hacker"}', schema=schema)
        self.assertFalse(result.ok)

    def test_validate_schema_min_max(self):
        schema = {"score": {"type": "int", "min": 0, "max": 100}}
        result = self.v.validate('{"score": 150}', schema=schema)
        self.assertFalse(result.ok)

    def test_validate_schema_min_length(self):
        schema = {"name": {"type": "str", "min_length": 3}}
        result = self.v.validate('{"name": "Al"}', schema=schema)
        self.assertFalse(result.ok)

    def test_validate_result_to_dict(self):
        result = self.v.extract_json('{"x": 1}')
        d = result.to_dict()
        self.assertIn("status", d)
        self.assertIn("ok", d)
        self.assertIn("data", d)

    def test_extract_fields_regex(self):
        patterns = {
            "name": r"Name:\s*(.+)",
            "score": r"Score:\s*(\d+)",
        }
        text = "Name: Alice\nScore: 95\nOther info"
        result = self.v.extract_fields(text, patterns)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["name"], "Alice")
        self.assertEqual(result.data["score"], "95")

    def test_extract_fields_missing(self):
        from agent.output_validator import ValidationStatus
        result = self.v.extract_fields("No patterns here",
                                        {"name": r"Name:\s*(.+)"})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, ValidationStatus.FAIL)

    def test_validate_with_retry_success(self):
        async def good_llm(prompt):
            return '{"result": "ok"}'
        result = _run(self.v.validate_with_retry(
            llm_fn=good_llm, prompt="Extract JSON", max_attempts=3
        ))
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 1)

    def test_validate_with_retry_eventual_success(self):
        calls = [0]
        async def flaky_llm(prompt):
            calls[0] += 1
            if calls[0] < 2:
                return "not json"
            return '{"result": "ok"}'
        result = _run(self.v.validate_with_retry(
            llm_fn=flaky_llm, prompt="Get JSON", max_attempts=3
        ))
        self.assertTrue(result.ok)
        self.assertEqual(calls[0], 2)

    def test_validate_with_retry_all_fail(self):
        async def bad_llm(prompt):
            return "I am sorry, I cannot provide JSON"
        result = _run(self.v.validate_with_retry(
            llm_fn=bad_llm, prompt="Get JSON", max_attempts=2
        ))
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 2)

    def test_register_custom_validator(self):
        self.v.register_validator("is_upper", lambda v: str(v).isupper())
        schema = {"code": {"type": "str", "validator": self.v._custom_validators["is_upper"]}}
        result = self.v.validate('{"code": "ABC"}', schema=schema)
        self.assertTrue(result.ok)
        result2 = self.v.validate('{"code": "abc"}', schema=schema)
        self.assertFalse(result2.ok)


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATION ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestRoutingTarget(unittest.TestCase):
    def test_to_dict_excludes_empty(self):
        from agent.conversation_router import RoutingTarget
        t = RoutingTarget(model="gpt-4")
        d = t.to_dict()
        self.assertIn("model", d)
        self.assertNotIn("persona", d)

    def test_to_dict_temperature_excluded_when_minus1(self):
        from agent.conversation_router import RoutingTarget
        t = RoutingTarget(model="x", temperature=-1)
        d = t.to_dict()
        self.assertNotIn("temperature", d)


class TestConversationRouter(unittest.TestCase):
    def setUp(self):
        from agent.conversation_router import ConversationRouter, RoutingTarget
        self.router = ConversationRouter(default_model="gpt-4o")
        self.RT = RoutingTarget

    def _register_code(self):
        self.router.register_intent(
            name="code_help",
            keywords=["code", "debug", "error", "function", "python"],
            patterns=[r"(write|fix|debug)\s+\w+"],
            target=self.RT(model="claude-3-5-sonnet"),
            priority=10,
        )

    def _register_creative(self):
        self.router.register_intent(
            name="creative_writing",
            keywords=["write", "story", "poem", "creative", "imagine"],
            target=self.RT(persona="writer"),
            priority=5,
        )

    def test_register_intent(self):
        self._register_code()
        intent = self.router.get_intent("code_help")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.name, "code_help")

    def test_register_duplicate_raises(self):
        self._register_code()
        with self.assertRaises(ValueError):
            self._register_code()

    def test_route_keyword_match(self):
        self._register_code()
        decision = self.router.route("Can you debug this Python code?")
        self.assertEqual(decision.intent_name, "code_help")
        self.assertGreater(decision.confidence, 0.0)

    def test_route_returns_decision(self):
        self._register_code()
        decision = self.router.route("help with code")
        self.assertIsNotNone(decision)
        self.assertIn("intent", decision.to_dict())

    def test_route_no_match_uses_fallback(self):
        self._register_code()
        decision = self.router.route("I love sunny weather today!")
        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.intent_name, "_fallback")

    def test_route_correct_target(self):
        self._register_code()
        decision = self.router.route("debug my python code please")
        self.assertEqual(decision.target.model, "claude-3-5-sonnet")

    def test_priority_ordering(self):
        self.router.register_intent(
            name="low_prio", keywords=["write"],
            target=self.RT(model="gpt-4o"), priority=1
        )
        self.router.register_intent(
            name="high_prio", keywords=["write", "story"],
            target=self.RT(model="claude-3-5-sonnet"), priority=20
        )
        decision = self.router.route("write a story for me")
        self.assertEqual(decision.intent_name, "high_prio")

    def test_disable_enable_intent(self):
        self._register_code()
        self.router.disable_intent("code_help")
        decision = self.router.route("debug this python code")
        self.assertNotEqual(decision.intent_name, "code_help")
        self.router.enable_intent("code_help")
        decision2 = self.router.route("debug this python code")
        self.assertEqual(decision2.intent_name, "code_help")

    def test_remove_intent(self):
        self._register_code()
        ok = self.router.remove_intent("code_help")
        self.assertTrue(ok)
        self.assertIsNone(self.router.get_intent("code_help"))

    def test_remove_nonexistent(self):
        self.assertFalse(self.router.remove_intent("ghost"))

    def test_fallback_chain(self):
        from agent.conversation_router import ConversationRouter, RoutingTarget
        router = ConversationRouter(
            default_model="gpt-4o-mini",
            fallback_chain=["support"]
        )
        router.register_intent(
            name="support",
            keywords=["support"],
            target=RoutingTarget(model="support-model"),
            priority=1,
        )
        decision = router.route("totally random query xyz")
        self.assertTrue(decision.fallback_used)

    def test_route_batch(self):
        self._register_code()
        messages = ["debug python code", "hello there"]
        decisions = self.router.route_batch(messages)
        self.assertEqual(len(decisions), 2)

    def test_list_intents(self):
        self._register_code()
        self._register_creative()
        intents = self.router.list_intents()
        names = [i.name for i in intents]
        self.assertIn("code_help", names)
        self.assertIn("creative_writing", names)

    def test_list_by_tag(self):
        self.router.register_intent(
            name="tagged_intent", keywords=["test"],
            target=self.RT(model="x"), tags=["beta"]
        )
        intents = self.router.list_intents(tag="beta")
        self.assertTrue(all("beta" in i.tags for i in intents))

    def test_routing_log_populated(self):
        self._register_code()
        self.router.route("debug code")
        log = self.router.routing_log()
        self.assertGreater(len(log), 0)

    def test_routing_log_limit(self):
        self._register_code()
        for _ in range(5):
            self.router.route("debug code")
        log = self.router.routing_log(limit=3)
        self.assertLessEqual(len(log), 3)

    def test_stats(self):
        self._register_code()
        self.router.route("debug python code")
        stats = self.router.stats()
        self.assertIn("total_routed", stats)
        self.assertIn("intent_hit_counts", stats)
        self.assertGreater(stats["total_routed"], 0)

    def test_stats_hit_rate_sums_to_one(self):
        self._register_code()
        self._register_creative()
        self.router.route("debug code")
        self.router.route("write a poem")
        stats = self.router.stats()
        total_rate = sum(stats["intent_hit_rates"].values())
        self.assertAlmostEqual(total_rate, 1.0, places=3)

    def test_intent_to_dict(self):
        self._register_code()
        intent = self.router.get_intent("code_help")
        d = intent.to_dict()
        self.assertIn("name", d)
        self.assertIn("keywords", d)
        self.assertIn("target", d)
        self.assertIn("priority", d)

    def test_decision_to_dict(self):
        self._register_code()
        decision = self.router.route("debug my code")
        d = decision.to_dict()
        for key in ["intent", "confidence", "target", "latency_ms"]:
            self.assertIn(key, d)

    def test_semantic_scoring_with_examples(self):
        from agent.conversation_router import MatchStrategy
        self.router.register_intent(
            name="math_help",
            keywords=[],
            examples=["solve this equation", "calculate the integral",
                       "what is 2+2"],
            strategy=MatchStrategy.SEMANTIC,
            target=self.RT(model="math-model"),
        )
        decision = self.router.route("compute the derivative of x squared")
        # Should get some semantic score
        self.assertGreaterEqual(decision.confidence, 0.0)

    def test_dispatch_calls_handler(self):
        from agent.conversation_router import RoutingTarget
        called = []
        def my_handler(message, decision):
            called.append(message)
            return "handled"

        self.router.register_intent(
            name="greeter",
            keywords=["hello", "hi", "hey"],
            target=RoutingTarget(handler="greet"),
        )
        self.router.register_handler("greet", my_handler)
        result = _run(self.router.dispatch("hello there"))
        self.assertEqual(result, "handled")
        self.assertIn("hello there", called)


# ══════════════════════════════════════════════════════════════════════════════
# EVENT STORE
# ══════════════════════════════════════════════════════════════════════════════

class TestDomainEvent(unittest.TestCase):
    def test_to_dict(self):
        from agent.event_store import DomainEvent
        e = DomainEvent(
            id="e1", event_type="UserCreated",
            aggregate_id="user_1", aggregate_type="User",
            payload={"name": "Alice"}, version=1,
        )
        d = e.to_dict()
        self.assertEqual(d["event_type"], "UserCreated")
        self.assertEqual(d["payload"]["name"], "Alice")
        self.assertEqual(d["version"], 1)


class TestEventStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.event_store import EventStore
        self.store = EventStore(
            db_path=os.path.join(self.tmpdir, "events.db")
        )

    def test_append_event(self):
        event = self.store.append(
            event_type="UserCreated",
            aggregate_id="user_1",
            aggregate_type="User",
            payload={"name": "Alice"},
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, "UserCreated")
        self.assertEqual(event.version, 1)

    def test_version_increments(self):
        self.store.append("EvA", aggregate_id="agg1", payload={})
        e2 = self.store.append("EvB", aggregate_id="agg1", payload={})
        self.assertEqual(e2.version, 2)

    def test_different_aggregates_independent_versions(self):
        e1 = self.store.append("EvA", aggregate_id="agg1", payload={})
        e2 = self.store.append("EvA", aggregate_id="agg2", payload={})
        self.assertEqual(e1.version, 1)
        self.assertEqual(e2.version, 1)

    def test_load_stream(self):
        self.store.append("EvA", aggregate_id="stream1", payload={"x": 1})
        self.store.append("EvB", aggregate_id="stream1", payload={"x": 2})
        events = self.store.load_stream("stream1")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, "EvA")
        self.assertEqual(events[1].event_type, "EvB")

    def test_load_stream_from_version(self):
        for i in range(5):
            self.store.append("Ev", aggregate_id="paged", payload={"i": i})
        events = self.store.load_stream("paged", from_version=3)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].version, 3)

    def test_load_stream_to_version(self):
        for i in range(5):
            self.store.append("Ev", aggregate_id="limited", payload={})
        events = self.store.load_stream("limited", to_version=2)
        self.assertEqual(len(events), 2)

    def test_current_version(self):
        self.store.append("Ev", aggregate_id="cv1", payload={})
        self.store.append("Ev", aggregate_id="cv1", payload={})
        self.assertEqual(self.store.current_version("cv1"), 2)

    def test_current_version_nonexistent(self):
        self.assertEqual(self.store.current_version("ghost"), 0)

    def test_optimistic_concurrency_pass(self):
        self.store.append("EvA", aggregate_id="occ1", payload={})
        e2 = self.store.append("EvB", aggregate_id="occ1",
                                expected_version=1, payload={})
        self.assertEqual(e2.version, 2)

    def test_optimistic_concurrency_fail(self):
        from agent.event_store import ConcurrencyError
        self.store.append("EvA", aggregate_id="occ2", payload={})
        self.store.append("EvB", aggregate_id="occ2", payload={})
        with self.assertRaises(ConcurrencyError):
            self.store.append("EvC", aggregate_id="occ2",
                               expected_version=1, payload={})

    def test_query_by_type(self):
        self.store.append("TypeA", aggregate_id="q1", payload={})
        self.store.append("TypeB", aggregate_id="q2", payload={})
        results = self.store.query(event_type="TypeA")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].event_type, "TypeA")

    def test_query_by_aggregate(self):
        self.store.append("Ev", aggregate_id="qa1", payload={"x": 1})
        self.store.append("Ev", aggregate_id="qa2", payload={"x": 2})
        results = self.store.query(aggregate_id="qa1")
        self.assertEqual(len(results), 1)

    def test_query_by_correlation(self):
        corr = "corr_123"
        self.store.append("Ev", aggregate_id="c1", correlation_id=corr, payload={})
        self.store.append("Ev", aggregate_id="c2", correlation_id=corr, payload={})
        results = self.store.get_by_correlation(corr)
        self.assertEqual(len(results), 2)

    def test_append_many(self):
        events = [
            {"event_type": "EvA", "aggregate_id": "batch1", "payload": {"i": 0}},
            {"event_type": "EvB", "aggregate_id": "batch1", "payload": {"i": 1}},
        ]
        result = self.store.append_many(events)
        self.assertEqual(len(result), 2)
        # Same correlation_id for all
        corr_ids = {e.correlation_id for e in result}
        self.assertEqual(len(corr_ids), 1)

    def test_replay_with_reducer(self):
        self.store.append("Deposited", aggregate_id="acc1",
                           payload={"amount": 100})
        self.store.append("Deposited", aggregate_id="acc1",
                           payload={"amount": 50})
        self.store.append("Withdrawn", aggregate_id="acc1",
                           payload={"amount": 30})

        def reducer(state, event):
            if event.event_type == "Deposited":
                return {**state, "balance": state.get("balance", 0) + event.payload["amount"]}
            elif event.event_type == "Withdrawn":
                return {**state, "balance": state.get("balance", 0) - event.payload["amount"]}
            return state

        state = self.store.replay("acc1", reducer)
        self.assertEqual(state["balance"], 120)

    def test_save_and_load_snapshot(self):
        self.store.append("Ev", aggregate_id="snap1", payload={})
        snap = self.store.save_snapshot("snap1", {"balance": 500}, version=1)
        loaded = self.store.load_snapshot("snap1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.state["balance"], 500)

    def test_replay_uses_snapshot(self):
        """Replay starting from snapshot should only process events after snapshot version."""
        for i in range(5):
            self.store.append("EvA", aggregate_id="snap_acc",
                               payload={"amount": 10})
        self.store.save_snapshot("snap_acc", {"balance": 50}, version=5)
        # Add more events after snapshot
        self.store.append("EvA", aggregate_id="snap_acc",
                           payload={"amount": 10})

        def reducer(state, event):
            return {**state, "balance": state.get("balance", 0) + 10}

        state = self.store.replay("snap_acc", reducer)
        self.assertEqual(state["balance"], 60)

    def test_projection_registered(self):
        proj = self.store.register_projection(
            "user_count",
            handler=lambda e, s: {**s, "count": s.get("count", 0) + 1},
            event_types=["UserCreated"],
        )
        self.assertIsNotNone(proj)
        self.assertEqual(proj.name, "user_count")

    def test_projection_updated_on_append(self):
        self.store.register_projection(
            "event_count",
            handler=lambda e, s: {**s, "n": s.get("n", 0) + 1},
            event_types=["Tracked"],
        )
        self.store.append("Tracked", aggregate_id="p1", payload={})
        self.store.append("Tracked", aggregate_id="p2", payload={})
        self.store.append("OtherEvent", aggregate_id="p3", payload={})  # not tracked

        proj = self.store.get_projection("event_count")
        self.assertEqual(proj.state.get("n", 0), 2)

    def test_projection_filters_event_types(self):
        self.store.register_projection(
            "only_a",
            handler=lambda e, s: {**s, "n": s.get("n", 0) + 1},
            event_types=["TypeA"],
        )
        self.store.append("TypeA", aggregate_id="fa", payload={})
        self.store.append("TypeB", aggregate_id="fb", payload={})
        proj = self.store.get_projection("only_a")
        self.assertEqual(proj.state.get("n", 0), 1)

    def test_subscription_fires(self):
        received = []
        async def handler(event):
            received.append(event.event_type)

        self.store.subscribe(handler)
        self.store.append("SubEvent", aggregate_id="sub1", payload={})
        # Give the event loop a tick
        _run(asyncio.sleep(0.01))
        self.assertIn("SubEvent", received)

    def test_stats(self):
        self.store.append("EvX", aggregate_id="stat1", payload={})
        stats = self.store.stats()
        self.assertIn("total_events", stats)
        self.assertIn("by_event_type", stats)
        self.assertGreaterEqual(stats["total_events"], 1)

    def test_event_persistence(self):
        from agent.event_store import EventStore
        self.store.append("PersistEvent", aggregate_id="persist1",
                           payload={"data": "important"})
        store2 = EventStore(db_path=os.path.join(self.tmpdir, "events.db"))
        events = store2.load_stream("persist1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["data"], "important")

    def test_snapshot_to_dict(self):
        self.store.append("Ev", aggregate_id="sd1", payload={})
        snap = self.store.save_snapshot("sd1", {"key": "val"})
        d = snap.to_dict()
        self.assertIn("aggregate_id", d)
        self.assertIn("state", d)
        self.assertIn("version", d)


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
    print(f"  v13 Test Results: {passed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  ✗ {t}")
            print(f"    {tb.strip().splitlines()[-1]}")
    else:
        print(f"  ✅ ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
