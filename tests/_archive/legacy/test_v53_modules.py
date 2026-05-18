"""OMNI AGENT v53: QueryPlannerV2, DocumentChunker, TokenBudgetV2, AgentMemoryV2"""
import asyncio, os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# QUERY PLANNER V2
# ════════════════════════════════════════════════════════
class TestQueryPlannerV2(unittest.TestCase):
    def setUp(self):
        from agent.query_planner_v2 import QueryPlannerV2
        self.qp = QueryPlannerV2(db_path=":memory:")
        self.qp.register_table("users", row_count=10_000)
        self.qp.register_table("orders", row_count=50_000)
        self.qp.register_index("users", ["id"], unique=True, selectivity=0.0001)
        self.qp.register_index("users", ["email"], selectivity=0.001)

    def test_plan_single_table(self):
        plan = self.qp.plan(["users"])
        self.assertIsNotNone(plan.root)

    def test_plan_has_cost(self):
        plan = self.qp.plan(["users"])
        self.assertGreater(plan.estimated_cost, 0)

    def test_index_scan_chosen_with_filter(self):
        from agent.query_planner_v2 import PlanNodeType
        plan = self.qp.plan(["users"],
                            filters=[{"column": "id", "op": "eq", "value": 1}])
        # Walk root to find any index scan
        def has_index_scan(node):
            if node.node_type in (PlanNodeType.INDEX_SCAN, PlanNodeType.INDEX_SEEK):
                return True
            return any(has_index_scan(c) for c in node.children)
        self.assertTrue(has_index_scan(plan.root))

    def test_seq_scan_without_index(self):
        from agent.query_planner_v2 import PlanNodeType
        self.qp.register_table("raw", row_count=500)
        plan = self.qp.plan(["raw"],
                            filters=[{"column": "x", "op": "eq", "value": 1}])
        def has_seq_scan(node):
            if node.node_type == PlanNodeType.SEQ_SCAN:
                return True
            return any(has_seq_scan(c) for c in node.children)
        self.assertTrue(has_seq_scan(plan.root))

    def test_join_two_tables(self):
        from agent.query_planner_v2 import PlanNodeType
        plan = self.qp.plan(["users", "orders"])
        def has_join(node):
            if node.node_type == PlanNodeType.HASH_JOIN:
                return True
            return any(has_join(c) for c in node.children)
        self.assertTrue(has_join(plan.root))

    def test_limit_applied(self):
        from agent.query_planner_v2 import PlanNodeType
        plan = self.qp.plan(["users"], limit=10)
        def has_limit(node):
            if node.node_type == PlanNodeType.LIMIT:
                return True
            return any(has_limit(c) for c in node.children)
        self.assertTrue(has_limit(plan.root))

    def test_projection_applied(self):
        from agent.query_planner_v2 import PlanNodeType
        plan = self.qp.plan(["users"], projections=["id", "email"])
        def has_proj(node):
            if node.node_type == PlanNodeType.PROJECTION:
                return True
            return any(has_proj(c) for c in node.children)
        self.assertTrue(has_proj(plan.root))

    def test_sort_applied(self):
        from agent.query_planner_v2 import PlanNodeType
        plan = self.qp.plan(["orders"], order_by=["created_at"])
        def has_sort(node):
            if node.node_type == PlanNodeType.SORT:
                return True
            return any(has_sort(c) for c in node.children)
        self.assertTrue(has_sort(plan.root))

    def test_hints_for_missing_index(self):
        plan = self.qp.plan(["orders"],
                            filters=[{"column": "status", "op": "eq", "value": "new"}])
        self.assertTrue(any("index" in h.lower() for h in plan.hints))

    def test_explain_returns_string(self):
        plan = self.qp.plan(["users"])
        text = self.qp.explain(plan)
        self.assertIn("Query Plan", text)

    def test_execute_with_source(self):
        self.qp.register_source("users",
            lambda f, l: [{"id": i, "name": f"User{i}"} for i in range(5)])
        plan = self.qp.plan(["users"])
        result = self.qp.execute(plan)
        self.assertEqual(len(result.rows), 5)

    def test_execute_with_limit(self):
        self.qp.register_source("users",
            lambda f, l: [{"id": i} for i in range(20)])
        result = self.qp.plan_and_execute(["users"], limit=3)
        self.assertLessEqual(len(result.rows), 3)

    def test_execute_with_projection(self):
        self.qp.register_source("users",
            lambda f, l: [{"id": 1, "email": "a@b.com", "pwd": "secret"}])
        result = self.qp.plan_and_execute(["users"], projections=["id", "email"])
        self.assertNotIn("pwd", result.rows[0])
        self.assertIn("email", result.rows[0])

    def test_query_log_populated(self):
        self.qp.register_source("orders", lambda f, l: [])
        self.qp.plan_and_execute(["orders"])
        log = self.qp.query_log()
        self.assertGreater(len(log), 0)

    def test_stats(self):
        s = self.qp.stats()
        self.assertEqual(s["tables"], 2)
        self.assertGreater(s["indexes"], 0)

# ════════════════════════════════════════════════════════
# DOCUMENT CHUNKER
# ════════════════════════════════════════════════════════
class TestDocumentChunker(unittest.TestCase):
    def setUp(self):
        from agent.document_chunker import DocumentChunker
        self.dc = DocumentChunker(max_tokens=50, min_chunk_tokens=1)

    def test_chunk_plain_text(self):
        from agent.document_chunker import DocumentType
        chunks = self.dc.chunk("Hello world. This is a sentence. And another one.",
                               doc_type=DocumentType.TEXT)
        self.assertGreater(len(chunks), 0)

    def test_chunks_have_ids(self):
        from agent.document_chunker import DocumentType
        chunks = self.dc.chunk("Some text here.", doc_type=DocumentType.TEXT)
        for c in chunks:
            self.assertIsNotNone(c.chunk_id)

    def test_chunk_markdown_splits_sections(self):
        from agent.document_chunker import DocumentType, DocumentChunker
        dc = DocumentChunker(max_tokens=30, min_chunk_tokens=1)
        md = "# Section 1\n\nSome content here.\n\n# Section 2\n\nMore content."
        chunks = dc.chunk(md, doc_type=DocumentType.MARKDOWN)
        self.assertGreater(len(chunks), 0)

    def test_markdown_heading_extracted(self):
        from agent.document_chunker import DocumentType, DocumentChunker
        dc = DocumentChunker(max_tokens=200, min_chunk_tokens=1)
        md = "# Introduction\n\nThis is the intro text."
        chunks = dc.chunk(md, doc_type=DocumentType.MARKDOWN)
        headings = [c.heading for c in chunks if c.heading]
        self.assertGreater(len(headings), 0)

    def test_chunk_html_strips_tags(self):
        from agent.document_chunker import DocumentType
        html = "<h1>Title</h1><p>Some paragraph text here.</p>"
        chunks = self.dc.chunk(html, doc_type=DocumentType.HTML)
        self.assertGreater(len(chunks), 0)
        for c in chunks:
            self.assertNotIn("<h1>", c.content)

    def test_chunk_code_splits_functions(self):
        from agent.document_chunker import DocumentType
        code = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        chunks = self.dc.chunk(code, doc_type=DocumentType.CODE)
        self.assertGreater(len(chunks), 0)

    def test_chunk_csv_keeps_header(self):
        from agent.document_chunker import DocumentType
        csv = "id,name,value\n1,Alice,10\n2,Bob,20\n3,Carol,30\n"
        chunks = self.dc.chunk(csv, doc_type=DocumentType.CSV)
        for c in chunks:
            self.assertIn("id,name,value", c.content)

    def test_chunk_chat_splits_turns(self):
        from agent.document_chunker import DocumentType
        chat = "User: Hello\nAssistant: Hi there!\nUser: How are you?\nAssistant: Fine!"
        chunks = self.dc.chunk(chat, doc_type=DocumentType.CHAT)
        self.assertGreater(len(chunks), 0)

    def test_dedup_removes_duplicates(self):
        from agent.document_chunker import DocumentType, DocumentChunker
        dc = DocumentChunker(max_tokens=200, min_chunk_tokens=1, dedup=True)
        text = "Hello world."
        c1 = dc.chunk(text, doc_type=DocumentType.TEXT)
        c2 = dc.chunk(text, doc_type=DocumentType.TEXT)
        self.assertGreater(len(c1), 0)
        self.assertEqual(len(c2), 0)

    def test_dedup_reset(self):
        from agent.document_chunker import DocumentType, DocumentChunker
        dc = DocumentChunker(max_tokens=200, min_chunk_tokens=1, dedup=True)
        text = "Unique text here for test."
        c1 = dc.chunk(text, doc_type=DocumentType.TEXT)
        dc.reset_dedup()
        c2 = dc.chunk(text, doc_type=DocumentType.TEXT)
        self.assertGreater(len(c1), 0)
        self.assertGreater(len(c2), 0)

    def test_chunk_has_word_count(self):
        from agent.document_chunker import DocumentType
        chunks = self.dc.chunk("one two three four five", doc_type=DocumentType.TEXT)
        self.assertGreater(chunks[0].word_count, 0)

    def test_chunk_has_token_count(self):
        from agent.document_chunker import DocumentType
        chunks = self.dc.chunk("Token counting test text.", doc_type=DocumentType.TEXT)
        self.assertGreater(chunks[0].token_count, 0)

    def test_post_hook_applied(self):
        from agent.document_chunker import DocumentType
        seen = []
        self.dc.add_post_hook(lambda c: seen.append(c.chunk_id) or c)
        self.dc.reset_dedup()
        self.dc.chunk("Testing hooks.", doc_type=DocumentType.TEXT)
        self.assertGreater(len(seen), 0)

    def test_metadata_passed_through(self):
        from agent.document_chunker import DocumentType, DocumentChunker
        dc = DocumentChunker(max_tokens=200, min_chunk_tokens=1)
        chunks = dc.chunk("Meta text.", doc_type=DocumentType.TEXT,
                          metadata={"source": "test"})
        self.assertEqual(chunks[0].metadata["source"], "test")

    def test_stats(self):
        from agent.document_chunker import DocumentType, DocumentChunker
        dc = DocumentChunker(max_tokens=200, min_chunk_tokens=1)
        dc.chunk("Stats test.", doc_type=DocumentType.TEXT)
        s = dc.stats()
        self.assertEqual(s["docs_processed"], 1)
        self.assertGreater(s["chunks_produced"], 0)

# ════════════════════════════════════════════════════════
# TOKEN BUDGET V2
# ════════════════════════════════════════════════════════
class TestTokenBudgetV2(unittest.TestCase):
    def setUp(self):
        from agent.token_budget_v2 import TokenBudgetV2, BudgetPeriod
        self.tb = TokenBudgetV2(db_path=":memory:")
        self.tb.create_budget("gpt4-daily", "gpt-4", max_tokens=10_000,
                              period=BudgetPeriod.DAY, cost_per_1k=0.03)

    def test_request_granted(self):
        from agent.token_budget_v2 import Priority
        req = self.tb.request("gpt-4", 100)
        self.assertTrue(req.granted)

    def test_request_denied_when_exhausted(self):
        from agent.token_budget_v2 import Priority
        self.tb.request("gpt-4", 9_999)
        req = self.tb.request("gpt-4", 200)
        self.assertFalse(req.granted)

    def test_critical_priority_always_granted(self):
        from agent.token_budget_v2 import Priority
        self.tb.request("gpt-4", 9_999)
        req = self.tb.request("gpt-4", 200, priority=Priority.CRITICAL)
        self.assertTrue(req.granted)

    def test_raise_on_deny(self):
        from agent.token_budget_v2 import BudgetExceeded, Priority
        self.tb.request("gpt-4", 9_999)
        with self.assertRaises(BudgetExceeded):
            self.tb.request("gpt-4", 200, raise_on_deny=True)

    def test_no_budget_always_granted(self):
        req = self.tb.request("unknown-model", 99999)
        self.assertTrue(req.granted)

    def test_remaining_decreases(self):
        b = list(self.tb._budgets.values())[0]
        initial = b.remaining
        self.tb.request("gpt-4", 500)
        self.assertEqual(b.remaining, initial - 500)

    def test_release_returns_tokens(self):
        b = list(self.tb._budgets.values())[0]
        self.tb.request("gpt-4", 1000)
        used_before = b.used_tokens
        self.tb.release("gpt-4", 500)
        self.assertEqual(b.used_tokens, used_before - 500)

    def test_cost_tracked(self):
        self.tb.request("gpt-4", 1000)
        cost = self.tb.total_cost("gpt-4")
        self.assertAlmostEqual(cost, 0.03, places=5)

    def test_alert_fired_at_threshold(self):
        alerts = []
        self.tb.on_alert(lambda b: alerts.append(b.budget_id))
        self.tb.request("gpt-4", 8_500)  # >80%
        self.assertGreater(len(alerts), 0)

    def test_disable_budget(self):
        bid = list(self.tb._budgets.keys())[0]
        self.tb.disable_budget(bid)
        req = self.tb.request("gpt-4", 100)
        self.assertTrue(req.granted)  # no active budget → always grant

    def test_enable_budget(self):
        bid = list(self.tb._budgets.keys())[0]
        self.tb.disable_budget(bid)
        self.tb.enable_budget(bid)
        self.tb.request("gpt-4", 9_999)
        req = self.tb.request("gpt-4", 200)
        self.assertFalse(req.granted)

    def test_enqueue_and_flush(self):
        from agent.token_budget_v2 import Priority
        self.tb.enqueue("gpt-4", 100, Priority.HIGH)
        self.tb.enqueue("gpt-4", 200, Priority.LOW)
        self.assertEqual(self.tb.queue_depth(), 2)
        results = self.tb.flush_queue()
        self.assertEqual(len(results), 2)
        self.assertEqual(self.tb.queue_depth(), 0)

    def test_queue_priority_order(self):
        from agent.token_budget_v2 import Priority
        self.tb.enqueue("gpt-4", 1, Priority.BATCH)
        self.tb.enqueue("gpt-4", 2, Priority.CRITICAL)
        # CRITICAL should be first after sort
        self.assertEqual(self.tb._queue[0].priority, Priority.CRITICAL)

    def test_model_budgets_listed(self):
        budgets = self.tb.model_budgets("gpt-4")
        self.assertEqual(len(budgets), 1)

    def test_all_budgets_listed(self):
        budgets = self.tb.all_budgets()
        self.assertEqual(len(budgets), 1)

    def test_request_history(self):
        self.tb.request("gpt-4", 100)
        hist = self.tb.request_history("gpt-4")
        self.assertGreater(len(hist), 0)

    def test_cost_breakdown(self):
        self.tb.request("gpt-4", 1000)
        bd = self.tb.cost_breakdown()
        self.assertIn("gpt-4", bd)

    def test_stats(self):
        self.tb.request("gpt-4", 100)
        s = self.tb.stats()
        self.assertGreater(s["total_granted"], 0)
        self.assertIn("budgets", s)

# ════════════════════════════════════════════════════════
# AGENT MEMORY V2
# ════════════════════════════════════════════════════════
class TestAgentMemoryV2(unittest.TestCase):
    def setUp(self):
        from agent.agent_memory_v2 import AgentMemoryV2
        self.am = AgentMemoryV2(working_capacity=5, db_path=":memory:")

    def test_store_and_get(self):
        e = self.am.store("The sky is blue", tags=["fact"])
        got = self.am.get(e.memory_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.content, "The sky is blue")

    def test_access_count_increments(self):
        e = self.am.store("Access me")
        self.am.get(e.memory_id)
        self.am.get(e.memory_id)
        self.assertEqual(e.access_count, 2)

    def test_retrieve_semantic(self):
        self.am.store("Python is a programming language", tags=["tech"])
        self.am.store("The weather is sunny today", tags=["weather"])
        results = self.am.retrieve("programming language", top_k=1)
        self.assertGreater(len(results), 0)

    def test_retrieve_by_type(self):
        from agent.agent_memory_v2 import MemoryType
        self.am.store("Episode 1", memory_type=MemoryType.EPISODIC)
        self.am.store("Fact 1", memory_type=MemoryType.SEMANTIC)
        results = self.am.retrieve("", memory_type=MemoryType.EPISODIC,
                                   strategy="recency")
        self.assertTrue(all(r.memory_type == MemoryType.EPISODIC for r in results))

    def test_retrieve_by_tag(self):
        self.am.store("Tagged memory", tags=["special"])
        results = self.am.retrieve("", tags=["special"], strategy="recency")
        self.assertGreater(len(results), 0)

    def test_retrieve_recency(self):
        for i in range(5):
            self.am.store(f"Memory {i}")
        results = self.am.retrieve("", top_k=3, strategy="recency")
        self.assertEqual(len(results), 3)

    def test_retrieve_salience(self):
        from agent.agent_memory_v2 import MemoryImportance
        self.am.store("Critical fact", importance=MemoryImportance.CRITICAL)
        self.am.store("Trivial detail", importance=MemoryImportance.TRIVIAL)
        results = self.am.retrieve("", top_k=1, strategy="salience")
        self.assertEqual(results[0].importance, MemoryImportance.CRITICAL)

    def test_forget(self):
        e = self.am.store("Forget me")
        self.assertTrue(self.am.forget(e.memory_id))
        self.assertIsNone(self.am.get(e.memory_id))

    def test_reinforce(self):
        e = self.am.store("Important fact")
        self.am.reinforce(e.memory_id, 0.5)
        self.assertAlmostEqual(e.reinforcement, 0.5)

    def test_decay_pass(self):
        from agent.agent_memory_v2 import AgentMemoryV2, MemoryType
        # Store with very fast decay
        am = AgentMemoryV2(db_path=":memory:")
        e = am.store("Trivial detail")
        e.decay_rate = 1000.0  # very fast decay → salience ≈ 0
        removed = am.decay_pass(min_salience=0.99)
        self.assertGreater(removed, 0)

    def test_working_memory_set_get(self):
        self.am.set_working("key1", "value1")
        self.assertEqual(self.am.get_working("key1"), "value1")

    def test_working_memory_ttl_expiry(self):
        self.am.set_working("expiring", "val", ttl_s=0.01)
        time.sleep(0.05)
        self.assertIsNone(self.am.get_working("expiring"))

    def test_working_memory_capacity_eviction(self):
        for i in range(7):
            self.am.set_working(f"k{i}", i)
        self.assertLessEqual(len(self.am.working_snapshot()), 5)

    def test_working_memory_delete(self):
        self.am.set_working("del_me", 123)
        self.am.delete_working("del_me")
        self.assertIsNone(self.am.get_working("del_me"))

    def test_working_snapshot(self):
        self.am.set_working("a", 1)
        self.am.set_working("b", 2)
        snap = self.am.working_snapshot()
        self.assertIn("a", snap)

    def test_consolidation(self):
        from agent.agent_memory_v2 import MemoryType
        for i in range(4):
            self.am.store(f"Episode {i}", memory_type=MemoryType.EPISODIC,
                          tags=["group1"])
        count = self.am.consolidate(min_count=3)
        self.assertGreater(count, 0)

    def test_consolidation_creates_semantic(self):
        from agent.agent_memory_v2 import MemoryType
        for i in range(3):
            self.am.store(f"Event {i}", memory_type=MemoryType.EPISODIC,
                          tags=["daily"])
        self.am.consolidate(min_count=3)
        semantic = self.am.count(MemoryType.SEMANTIC)
        self.assertGreater(semantic, 0)

    def test_search_by_tag(self):
        self.am.store("Tagged one", tags=["alpha"])
        self.am.store("Tagged two", tags=["alpha"])
        self.am.store("No tag")
        results = self.am.search_by_tag("alpha")
        self.assertEqual(len(results), 2)

    def test_stats(self):
        self.am.store("Stat test")
        s = self.am.stats()
        self.assertEqual(s["total_memories"], 1)
        self.assertIn("by_type", s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v53: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
