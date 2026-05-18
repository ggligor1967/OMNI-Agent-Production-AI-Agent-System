"""OMNI AGENT v56: ABTestingV2, ContentModerator, KnowledgeGraphV2, ContextWindowManager"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# A/B TESTING V2
# ════════════════════════════════════════════════════════
class TestABTestingV2(unittest.TestCase):
    def setUp(self):
        from agent.ab_testing_v2 import ABTestingV2, AllocationStrategy
        self.ab = ABTestingV2(db_path=":memory:")
        self.exp = self.ab.create_experiment(
            "test_exp",
            variants=[
                {"id": "ctrl", "name": "Control", "weight": 1.0, "is_control": True},
                {"id": "var1", "name": "Variant1", "weight": 1.0},
            ],
            strategy=AllocationStrategy.HASH_USER,
            metrics=["conversion", "revenue"],
        )

    def _start(self):
        self.ab.start(self.exp.experiment_id)

    def test_create_experiment(self):
        self.assertIsNotNone(self.exp.experiment_id)
        self.assertEqual(len(self.exp.variants), 2)

    def test_start_experiment(self):
        from agent.ab_testing_v2 import ExperimentStatus
        self._start()
        self.assertEqual(self.exp.status, ExperimentStatus.RUNNING)

    def test_assign_user(self):
        self._start()
        asn = self.ab.assign(self.exp.experiment_id, "user_001")
        self.assertIsNotNone(asn)
        self.assertIn(asn.variant_id, ["ctrl", "var1"])

    def test_assignment_is_sticky(self):
        self._start()
        asn1 = self.ab.assign(self.exp.experiment_id, "sticky_user")
        asn2 = self.ab.assign(self.exp.experiment_id, "sticky_user")
        self.assertEqual(asn1.variant_id, asn2.variant_id)

    def test_assignment_deterministic(self):
        self._start()
        asn1 = self.ab.assign(self.exp.experiment_id, "det_user_42")
        # Same hash → same variant for same user+experiment name combo
        asn2 = self.ab.assign(self.exp.experiment_id, "det_user_42")
        self.assertEqual(asn1.variant_id, asn2.variant_id)

    def test_no_assignment_when_not_running(self):
        asn = self.ab.assign(self.exp.experiment_id, "user_999")
        self.assertIsNone(asn)

    def test_rollout_gate(self):
        from agent.ab_testing_v2 import ABTestingV2, AllocationStrategy
        ab = ABTestingV2(db_path=":memory:")
        exp = ab.create_experiment("rollout",
            variants=[{"id": "ctrl", "is_control": True}, {"id": "v1"}],
            strategy=AllocationStrategy.HASH_USER,
            rollout_pct=0.0)  # 0% rollout
        ab.start(exp.experiment_id)
        asn = ab.assign(exp.experiment_id, "any_user")
        self.assertIsNone(asn)

    def test_record_metric(self):
        self._start()
        self.ab.assign(self.exp.experiment_id, "u1")
        self.ab.record(self.exp.experiment_id, "u1", "conversion", 1.0)
        stats = self.ab._stats[self.exp.experiment_id]
        vid = self.ab._assignments[self.exp.experiment_id]["u1"].variant_id
        self.assertEqual(len(stats[vid].observations.get("conversion", [])), 1)

    def test_analyze_significance(self):
        self._start()
        # Assign many users and record conversions
        ctrl_users = [f"c{i}" for i in range(30)]
        var_users  = [f"v{i}" for i in range(30)]
        for u in ctrl_users:
            self.ab._assignments[self.exp.experiment_id][u] = \
                type("A", (), {"variant_id": "ctrl"})()
            self.ab._stats[self.exp.experiment_id]["ctrl"].observations \
                .setdefault("conversion", []).append(0.1)
        for u in var_users:
            self.ab._assignments[self.exp.experiment_id][u] = \
                type("A", (), {"variant_id": "var1"})()
            self.ab._stats[self.exp.experiment_id]["var1"].observations \
                .setdefault("conversion", []).append(0.5)
        results = self.ab.analyze(self.exp.experiment_id, "conversion")
        self.assertGreater(len(results), 0)

    def test_conclude_experiment(self):
        from agent.ab_testing_v2 import ExperimentStatus
        self._start()
        self.ab.conclude(self.exp.experiment_id)
        self.assertEqual(self.exp.status, ExperimentStatus.CONCLUDED)

    def test_pause_experiment(self):
        from agent.ab_testing_v2 import ExperimentStatus
        self._start()
        self.ab.pause(self.exp.experiment_id)
        self.assertEqual(self.exp.status, ExperimentStatus.PAUSED)

    def test_get_variant(self):
        self._start()
        self.ab.assign(self.exp.experiment_id, "uv1")
        v = self.ab.get_variant(self.exp.experiment_id, "uv1")
        self.assertIsNotNone(v)

    def test_list_experiments(self):
        exps = self.ab.list_experiments()
        self.assertEqual(len(exps), 1)

    def test_list_experiments_by_status(self):
        from agent.ab_testing_v2 import ExperimentStatus
        self._start()
        running = self.ab.list_experiments(status=ExperimentStatus.RUNNING)
        self.assertEqual(len(running), 1)

    def test_summary(self):
        self._start()
        s = self.ab.summary(self.exp.experiment_id)
        self.assertIn("experiment", s)
        self.assertIn("variants", s)

    def test_stats(self):
        self._start()
        s = self.ab.stats()
        self.assertEqual(s["experiments"], 1)
        self.assertEqual(s["running"], 1)

# ════════════════════════════════════════════════════════
# CONTENT MODERATOR
# ════════════════════════════════════════════════════════
class TestContentModerator(unittest.TestCase):
    def setUp(self):
        from agent.content_moderator import ContentModerator
        self.cm = ContentModerator(db_path=":memory:", load_builtins=True)

    def test_safe_text_passes(self):
        r = self.cm.moderate("Hello, how are you today?")
        self.assertTrue(r.safe)

    def test_email_redacted(self):
        r = self.cm.moderate("Contact me at user@example.com")
        self.assertIn("REDACTED", r.clean_text)

    def test_ssn_redacted(self):
        r = self.cm.moderate("My SSN is 123-45-6789")
        self.assertIn("REDACTED", r.clean_text)

    def test_spam_keyword_flagged(self):
        from agent.content_moderator import ModerationAction
        r = self.cm.moderate("Click here for free money!")
        self.assertIn(r.action, [ModerationAction.FLAG, ModerationAction.BLOCK])

    def test_self_harm_blocked(self):
        from agent.content_moderator import ModerationAction
        r = self.cm.moderate("I want to kill myself tonight")
        self.assertEqual(r.action, ModerationAction.BLOCK)

    def test_violence_threat_blocked(self):
        from agent.content_moderator import ModerationAction
        r = self.cm.moderate("I'll kill you if you do that")
        self.assertEqual(r.action, ModerationAction.BLOCK)

    def test_custom_rule_keyword(self):
        from agent.content_moderator import ViolationCategory, Severity, ModerationAction
        self.cm.add_rule(ViolationCategory.CUSTOM, Severity.HIGH,
                         ModerationAction.BLOCK, keywords=["forbidden_word"])
        r = self.cm.moderate("This contains forbidden_word here")
        self.assertFalse(r.safe)

    def test_custom_rule_pattern(self):
        from agent.content_moderator import ViolationCategory, Severity, ModerationAction
        self.cm.add_rule(ViolationCategory.CUSTOM, Severity.MEDIUM,
                         ModerationAction.FLAG, pattern=r"badpattern\d+")
        r = self.cm.moderate("Found badpattern123 in text")
        self.assertGreater(len(r.violations), 0)

    def test_disable_rule(self):
        from agent.content_moderator import ViolationCategory, Severity, ModerationAction
        rule = self.cm.add_rule(ViolationCategory.CUSTOM, Severity.LOW,
                                ModerationAction.FLAG, keywords=["softkw"])
        self.cm.disable_rule(rule.rule_id)
        r = self.cm.moderate("This has softkw in it")
        custom = [v for v in r.violations if v.rule_id == rule.rule_id]
        self.assertEqual(len(custom), 0)

    def test_enable_rule(self):
        from agent.content_moderator import (ContentModerator, ViolationCategory,
                                              Severity, ModerationAction)
        cm = ContentModerator(db_path=":memory:", load_builtins=False)
        rule = cm.add_rule(ViolationCategory.CUSTOM, Severity.LOW,
                           ModerationAction.FLAG, keywords=["testkw"],
                           min_confidence=0.0)
        cm.disable_rule(rule.rule_id)
        cm.enable_rule(rule.rule_id)
        r = cm.moderate("This has testkw in it")
        custom = [v for v in r.violations if v.rule_id == rule.rule_id]
        self.assertGreater(len(custom), 0)

    def test_custom_scorer(self):
        self.cm.add_scorer(lambda text: [
            {"category": "spam", "confidence": 0.9, "action": "flag"}
        ] if "SPAM" in text else [])
        r = self.cm.moderate("This is SPAM content")
        self.assertGreater(len(r.violations), 0)

    def test_is_safe(self):
        self.assertTrue(self.cm.is_safe("Just a normal message."))
        self.assertFalse(self.cm.is_safe("I want to kill myself"))

    def test_moderate_batch(self):
        texts = ["Hello", "Free money click here", "Normal text"]
        results = self.cm.moderate_batch(texts)
        self.assertEqual(len(results), 3)

    def test_categories_in_result(self):
        r = self.cm.moderate("Contact user@example.com for free money!")
        self.assertGreater(len(r.categories), 0)

    def test_audit_log(self):
        self.cm.moderate("test text")
        log = self.cm.audit_log()
        self.assertGreater(len(log), 0)

    def test_category_breakdown(self):
        self.cm.moderate("user@example.com")
        bd = self.cm.category_breakdown()
        self.assertIn("personal_info", bd)

    def test_stats(self):
        self.cm.moderate("hello")
        s = self.cm.stats()
        self.assertGreater(s["moderated"], 0)
        self.assertIn("rules", s)

# ════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH V2
# ════════════════════════════════════════════════════════
class TestKnowledgeGraphV2(unittest.TestCase):
    def setUp(self):
        from agent.knowledge_graph_v2 import KnowledgeGraphV2
        self.kg = KnowledgeGraphV2(db_path=":memory:")

    def test_add_and_get_node(self):
        n = self.kg.add_node("Python", node_id="python")
        self.assertEqual(self.kg.get_node("python").label, "Python")

    def test_find_by_label_exact(self):
        self.kg.add_node("JavaScript", node_id="js")
        results = self.kg.find_by_label("JavaScript")
        self.assertEqual(len(results), 1)

    def test_find_by_label_partial(self):
        self.kg.add_node("TypeScript", node_id="ts")
        results = self.kg.find_by_label("script", exact=False)
        self.assertGreater(len(results), 0)

    def test_add_edge(self):
        from agent.knowledge_graph_v2 import EdgeType
        self.kg.add_node("Dog", node_id="dog")
        self.kg.add_node("Animal", node_id="animal")
        e = self.kg.add_edge("dog", "animal", EdgeType.IS_A)
        self.assertIsNotNone(e)

    def test_neighbors(self):
        from agent.knowledge_graph_v2 import EdgeType
        self.kg.add_node("A", node_id="a")
        self.kg.add_node("B", node_id="b")
        self.kg.add_edge("a", "b", EdgeType.RELATED_TO)
        nbrs = self.kg.neighbors("a", direction="out")
        self.assertEqual(len(nbrs), 1)
        self.assertEqual(nbrs[0].label, "B")

    def test_bidirectional_edge(self):
        from agent.knowledge_graph_v2 import EdgeType
        self.kg.add_node("X", node_id="x")
        self.kg.add_node("Y", node_id="y")
        self.kg.add_edge("x", "y", EdgeType.SYNONYMOUS, bidirectional=True)
        nbrs_y = self.kg.neighbors("y", direction="out")
        self.assertTrue(any(n.node_id == "x" for n in nbrs_y))

    def test_bfs(self):
        from agent.knowledge_graph_v2 import EdgeType
        self.kg.add_node("root", node_id="r")
        self.kg.add_node("child1", node_id="c1")
        self.kg.add_node("child2", node_id="c2")
        self.kg.add_edge("r", "c1", EdgeType.PART_OF)
        self.kg.add_edge("r", "c2", EdgeType.PART_OF)
        bfs = self.kg.bfs("r", max_depth=1)
        self.assertGreaterEqual(len(bfs), 3)

    def test_dfs(self):
        from agent.knowledge_graph_v2 import EdgeType
        self.kg.add_node("root", node_id="dr")
        self.kg.add_node("leaf", node_id="dl")
        self.kg.add_edge("dr", "dl", EdgeType.CAUSES)
        dfs = self.kg.dfs("dr")
        self.assertGreaterEqual(len(dfs), 2)

    def test_shortest_path(self):
        from agent.knowledge_graph_v2 import EdgeType
        self.kg.add_node("A", node_id="spa")
        self.kg.add_node("B", node_id="spb")
        self.kg.add_node("C", node_id="spc")
        self.kg.add_edge("spa", "spb", EdgeType.PRECEDES, weight=1.0)
        self.kg.add_edge("spb", "spc", EdgeType.PRECEDES, weight=1.0)
        path = self.kg.shortest_path("spa", "spc")
        self.assertIsNotNone(path)
        self.assertEqual(path.length, 2)

    def test_shortest_path_not_found(self):
        self.kg.add_node("Isolated1", node_id="iso1")
        self.kg.add_node("Isolated2", node_id="iso2")
        path = self.kg.shortest_path("iso1", "iso2")
        self.assertIsNone(path)

    def test_match_triples(self):
        from agent.knowledge_graph_v2 import EdgeType
        self.kg.add_node("Python", node_id="py")
        self.kg.add_node("Language", node_id="lang")
        self.kg.add_edge("py", "lang", EdgeType.IS_A)
        triples = self.kg.match_triples(subject="Python", predicate=EdgeType.IS_A)
        self.assertGreater(len(triples), 0)

    def test_delete_node(self):
        self.kg.add_node("ToDelete", node_id="td")
        self.assertTrue(self.kg.delete_node("td"))
        self.assertIsNone(self.kg.get_node("td"))

    def test_delete_edge(self):
        from agent.knowledge_graph_v2 import EdgeType
        self.kg.add_node("N1", node_id="n1")
        self.kg.add_node("N2", node_id="n2")
        e = self.kg.add_edge("n1", "n2", EdgeType.RELATED_TO)
        self.assertTrue(self.kg.delete_edge(e.edge_id))

    def test_update_node(self):
        self.kg.add_node("Upd", node_id="upd")
        self.kg.update_node("upd", label="Updated")
        self.assertEqual(self.kg.get_node("upd").label, "Updated")

    def test_inference_rule(self):
        from agent.knowledge_graph_v2 import EdgeType
        self.kg.add_node("Cat", node_id="cat")
        self.kg.add_node("Animal", node_id="anim")
        self.kg.add_node("LivingThing", node_id="living")
        self.kg.add_edge("cat", "anim", EdgeType.IS_A)
        self.kg.add_edge("anim", "living", EdgeType.IS_A)
        # Transitivity rule
        def transitivity(g):
            new = []
            for e1 in g._edges.values():
                if e1.edge_type != EdgeType.IS_A:
                    continue
                for e2 in g._edges.values():
                    if e2.edge_type != EdgeType.IS_A:
                        continue
                    if e1.target_id == e2.source_id:
                        new.append((e1.source_id, EdgeType.IS_A, e2.target_id, 0.7))
            return new
        self.kg.add_inference_rule(transitivity)
        added = self.kg.run_inference()
        self.assertGreater(added, 0)

    def test_stats(self):
        self.kg.add_node("N", node_id="n_stat")
        s = self.kg.stats()
        self.assertEqual(s["nodes"], 1)
        self.assertEqual(s["edges"], 0)

# ════════════════════════════════════════════════════════
# CONTEXT WINDOW MANAGER
# ════════════════════════════════════════════════════════
class TestContextWindowManager(unittest.TestCase):
    def setUp(self):
        from agent.context_window_manager import ContextWindowManager, TruncationStrategy
        self.cw = ContextWindowManager(
            max_tokens=200, reserved_tokens=20,
            strategy=TruncationStrategy.DROP_OLDEST)

    def test_add_and_list(self):
        self.cw.add_user("Hello")
        items = self.cw.list_items()
        self.assertEqual(len(items), 1)

    def test_system_prompt_pinned(self):
        self.cw.add_system("You are helpful.")
        items = self.cw.list_items()
        self.assertTrue(items[0].pinned)

    def test_pack_within_budget(self):
        self.cw.add_system("Short system.")
        self.cw.add_user("Short user message.")
        result = self.cw.pack()
        self.assertFalse(result.truncated)

    def test_pack_drops_when_over_budget(self):
        from agent.context_window_manager import ContextWindowManager, TruncationStrategy
        cw = ContextWindowManager(max_tokens=50, reserved_tokens=5,
                                  strategy=TruncationStrategy.DROP_OLDEST)
        for i in range(20):
            cw.add_user(f"Message number {i} with some content here")
        result = cw.pack()
        self.assertTrue(result.truncated)
        self.assertGreater(len(result.dropped_items), 0)

    def test_pinned_items_never_dropped(self):
        from agent.context_window_manager import ContextWindowManager, TruncationStrategy
        cw = ContextWindowManager(max_tokens=50, reserved_tokens=5,
                                  strategy=TruncationStrategy.DROP_OLDEST)
        cw.add_system("Must keep this system prompt always.")
        for i in range(10):
            cw.add_user(f"Extra message {i} here extra content")
        result = cw.pack()
        pinned_ids = {i.item_id for i in result.items if i.pinned}
        self.assertGreater(len(pinned_ids), 0)

    def test_drop_lowest_priority_strategy(self):
        from agent.context_window_manager import ContextWindowManager, TruncationStrategy
        cw = ContextWindowManager(max_tokens=60, reserved_tokens=5,
                                  strategy=TruncationStrategy.DROP_LOWEST)
        cw.add_user("High priority content here", priority=1)
        cw.add_user("Low priority old content here too much text", priority=9)
        cw.add_user("Medium priority normal text here today", priority=5)
        result = cw.pack()
        if result.truncated:
            # Highest-priority items should be retained
            retained_priorities = [i.priority for i in result.items]
            self.assertIn(1, retained_priorities)

    def test_middle_out_strategy(self):
        from agent.context_window_manager import ContextWindowManager, TruncationStrategy
        cw = ContextWindowManager(max_tokens=80, reserved_tokens=5,
                                  strategy=TruncationStrategy.MIDDLE_OUT)
        for i in range(10):
            cw.add_user(f"Message {i:02d} here with content to pack")
        result = cw.pack()
        self.assertIsNotNone(result)

    def test_summarize_strategy(self):
        from agent.context_window_manager import ContextWindowManager, TruncationStrategy
        summaries = []
        def summarize(items):
            summaries.append(len(items))
            return f"Summary of {len(items)} items"
        cw = ContextWindowManager(max_tokens=60, reserved_tokens=5,
                                  strategy=TruncationStrategy.SUMMARIZE,
                                  summarize_fn=summarize)
        for i in range(8):
            cw.add_user(f"Message {i} with sufficient content length here")
        result = cw.pack()
        self.assertIsNotNone(result)

    def test_type_budget_respected(self):
        from agent.context_window_manager import ContextItemType
        self.cw.set_type_budget(ContextItemType.DOCUMENT, 20)
        self.cw.add_document("Short doc")
        self.cw.add_document("Another document that is quite long and has lots of content added")
        result = self.cw.pack()
        doc_tokens = sum(i.tokens for i in result.items
                         if i.item_type == ContextItemType.DOCUMENT)
        self.assertLessEqual(doc_tokens, 25)

    def test_remove_item(self):
        item = self.cw.add_user("Remove me")
        self.assertTrue(self.cw.remove(item.item_id))
        self.assertEqual(len(self.cw.list_items()), 0)

    def test_pin_unpin(self):
        item = self.cw.add_user("Pin test")
        self.cw.pin(item.item_id)
        self.assertTrue(item.pinned)
        self.cw.unpin(item.item_id)
        self.assertFalse(item.pinned)

    def test_clear_keeps_pinned(self):
        self.cw.add_system("Pinned system")
        self.cw.add_user("Non-pinned user")
        self.cw.clear(keep_pinned=True)
        items = self.cw.list_items()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].pinned)

    def test_to_messages_format(self):
        self.cw.add_system("System prompt")
        self.cw.add_user("User message")
        msgs = self.cw.to_messages(pack=False)
        roles = [m["role"] for m in msgs]
        self.assertIn("system", roles)
        self.assertIn("user", roles)

    def test_utilization(self):
        self.cw.add_user("Some content here")
        u = self.cw.utilization()
        self.assertGreater(u, 0)
        self.assertLessEqual(u, 1.0)

    def test_stats(self):
        self.cw.add_user("test")
        self.cw.pack()
        s = self.cw.stats()
        self.assertEqual(s["items"], 1)
        self.assertGreater(s["packs_performed"], 0)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v56: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
