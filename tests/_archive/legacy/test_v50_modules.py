"""OMNI AGENT v50: EmbeddingPipeline, GovernanceEngine, ReplayBuffer, ConfigStore"""
import asyncio, os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# EMBEDDING PIPELINE
# ════════════════════════════════════════════════════════
class TestEmbeddingPipeline(unittest.TestCase):
    def setUp(self):
        from agent.embedding_pipeline import EmbeddingPipeline
        self.ep = EmbeddingPipeline(dim=8, db_path=":memory:")

    def test_ingest_returns_chunks(self):
        chunks = self.ep.ingest("Hello world. This is a test.", title="doc1")
        self.assertGreater(len(chunks), 0)

    def test_chunks_have_embeddings(self):
        chunks = self.ep.ingest("Some text here for embedding test.")
        for c in chunks:
            self.assertIsNotNone(c.embedding)
            self.assertEqual(len(c.embedding), 8)

    def test_search_returns_results(self):
        self.ep.ingest("Python is a great programming language.", title="py")
        results = self.ep.search("programming language")
        self.assertGreater(len(results), 0)

    def test_search_top_k(self):
        for i in range(5):
            self.ep.ingest(f"Document {i} about topic {i}.", doc_id=f"doc{i}")
        results = self.ep.search("document topic", top_k=3)
        self.assertLessEqual(len(results), 3)

    def test_search_result_has_rank(self):
        self.ep.ingest("Ranked search result test.")
        results = self.ep.search("search")
        self.assertEqual(results[0].rank, 1)

    def test_search_by_embedding(self):
        chunks = self.ep.ingest("Embedding lookup test.")
        emb = chunks[0].embedding
        results = self.ep.search_by_embedding(emb, top_k=1)
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].score, 1.0, places=4)

    def test_delete_doc(self):
        self.ep.ingest("Delete me.", doc_id="del_doc")
        removed = self.ep.delete_doc("del_doc")
        self.assertGreater(removed, 0)
        self.assertEqual(self.ep.get_doc_chunks("del_doc"), [])

    def test_get_doc_chunks(self):
        self.ep.ingest("Get my chunks.", doc_id="my_doc")
        chunks = self.ep.get_doc_chunks("my_doc")
        self.assertGreater(len(chunks), 0)

    def test_get_chunk(self):
        chunks = self.ep.ingest("Get single chunk.")
        c = self.ep.get_chunk(chunks[0].chunk_id)
        self.assertIsNotNone(c)

    def test_list_docs(self):
        self.ep.ingest("Doc A.", doc_id="a")
        self.ep.ingest("Doc B.", doc_id="b")
        docs = self.ep.list_docs()
        self.assertEqual(len(docs), 2)

    def test_doc_filter_search(self):
        self.ep.ingest("Target document.", doc_id="target")
        self.ep.ingest("Other document.",  doc_id="other")
        results = self.ep.search("document", doc_filter=["target"])
        self.assertTrue(all(r.chunk.doc_id == "target" for r in results))

    def test_embed_returns_vector(self):
        emb = self.ep.embed("some text")
        self.assertEqual(len(emb), 8)

    def test_reembed_doc(self):
        self.ep.ingest("Reembed me.", doc_id="re_doc")
        count = self.ep.reembed_doc("re_doc")
        self.assertGreater(count, 0)

    def test_chunker_fixed_words(self):
        from agent.embedding_pipeline import EmbeddingPipeline, ChunkStrategy
        ep = EmbeddingPipeline(chunk_strategy=ChunkStrategy.FIXED_WORDS,
                                chunk_size=5, overlap=1, db_path=":memory:")
        chunks = ep.ingest(" ".join([f"word{i}" for i in range(20)]))
        self.assertGreater(len(chunks), 1)

    def test_chunker_paragraph(self):
        from agent.embedding_pipeline import EmbeddingPipeline, ChunkStrategy
        ep = EmbeddingPipeline(chunk_strategy=ChunkStrategy.PARAGRAPH, db_path=":memory:")
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        chunks = ep.ingest(text)
        self.assertGreaterEqual(len(chunks), 2)

    def test_chunker_recursive(self):
        from agent.embedding_pipeline import EmbeddingPipeline, ChunkStrategy
        ep = EmbeddingPipeline(chunk_strategy=ChunkStrategy.RECURSIVE,
                                chunk_size=10, db_path=":memory:")
        chunks = ep.ingest("Word " * 50)
        self.assertGreater(len(chunks), 0)

    def test_cosine_sim_same_vector(self):
        from agent.embedding_pipeline import cosine_sim
        v = [1.0, 0.0, 0.0, 0.0]
        self.assertAlmostEqual(cosine_sim(v, v), 1.0)

    def test_cosine_sim_orthogonal(self):
        from agent.embedding_pipeline import cosine_sim
        a = [1.0, 0.0]; b = [0.0, 1.0]
        self.assertAlmostEqual(cosine_sim(a, b), 0.0)

    def test_stats(self):
        self.ep.ingest("Stats test document.")
        s = self.ep.stats()
        self.assertEqual(s["docs"], 1)
        self.assertGreater(s["chunks"], 0)
        self.assertEqual(s["dim"], 8)

# ════════════════════════════════════════════════════════
# GOVERNANCE ENGINE
# ════════════════════════════════════════════════════════
class TestGovernanceEngine(unittest.TestCase):
    def setUp(self):
        from agent.governance_engine import GovernanceEngine, PolicyEffect
        self.gov = GovernanceEngine(default_effect=PolicyEffect.ALLOW,
                                    db_path=":memory:")
        self.PolicyEffect = PolicyEffect

    def test_default_allow(self):
        result = self.gov.evaluate({"action": "read"})
        self.assertTrue(result.allowed)

    def test_deny_rule_blocks(self):
        from agent.governance_engine import PolicyScope
        self.gov.add_rule("block_all", self.PolicyEffect.DENY)
        result = self.gov.evaluate({"action": "read"})
        self.assertFalse(result.allowed)

    def test_allow_rule_permits(self):
        from agent.governance_engine import PolicyScope
        self.gov.add_rule("allow_read", self.PolicyEffect.ALLOW,
                          conditions={"action": "read"})
        result = self.gov.evaluate({"action": "read"})
        self.assertTrue(result.allowed)

    def test_deny_wins_over_allow(self):
        self.gov.add_rule("allow_all", self.PolicyEffect.ALLOW, priority=0)
        self.gov.add_rule("deny_all",  self.PolicyEffect.DENY,  priority=10)
        result = self.gov.evaluate({"action": "anything"})
        self.assertFalse(result.allowed)

    def test_warn_does_not_block(self):
        from agent.governance_engine import PolicyEffect
        self.gov.add_rule("warn_rule", PolicyEffect.WARN)
        result = self.gov.evaluate({})
        self.assertTrue(result.allowed)
        self.assertGreater(len(result.warnings), 0)

    def test_condition_eq(self):
        self.gov.add_rule("deny_write", self.PolicyEffect.DENY,
                          conditions={"action": "write"})
        self.assertFalse(self.gov.is_allowed({"action": "write"}))
        self.assertTrue(self.gov.is_allowed({"action": "read"}))

    def test_condition_gt(self):
        self.gov.add_rule("deny_large", self.PolicyEffect.DENY,
                          conditions={"size": {"op": "gt", "value": 100}})
        self.assertFalse(self.gov.is_allowed({"size": 200}))
        self.assertTrue(self.gov.is_allowed({"size": 50}))

    def test_condition_in(self):
        self.gov.add_rule("deny_admins", self.PolicyEffect.DENY,
                          conditions={"role": {"op": "in", "value": ["admin", "root"]}})
        self.assertFalse(self.gov.is_allowed({"role": "admin"}))
        self.assertTrue(self.gov.is_allowed({"role": "user"}))

    def test_scope_user(self):
        from agent.governance_engine import PolicyScope
        self.gov.add_rule("deny_alice", self.PolicyEffect.DENY,
                          scope=PolicyScope.USER, scope_value="alice")
        self.assertFalse(self.gov.is_allowed({"user_id": "alice"}))
        self.assertTrue(self.gov.is_allowed({"user_id": "bob"}))

    def test_scope_role(self):
        from agent.governance_engine import PolicyScope
        self.gov.add_rule("deny_guest", self.PolicyEffect.DENY,
                          scope=PolicyScope.ROLE, scope_value="guest")
        self.assertFalse(self.gov.is_allowed({"roles": ["guest"]}))
        self.assertTrue(self.gov.is_allowed({"roles": ["admin"]}))

    def test_scope_resource(self):
        from agent.governance_engine import PolicyScope
        self.gov.add_rule("deny_secret", self.PolicyEffect.DENY,
                          scope=PolicyScope.RESOURCE, scope_value="secret/*")
        self.assertFalse(self.gov.is_allowed({"resource": "secret/data"}))
        self.assertTrue(self.gov.is_allowed({"resource": "public/data"}))

    def test_disable_rule(self):
        r = self.gov.add_rule("deny_all", self.PolicyEffect.DENY)
        self.gov.disable_rule(r.rule_id)
        self.assertTrue(self.gov.is_allowed({}))

    def test_enable_rule(self):
        r = self.gov.add_rule("deny_all", self.PolicyEffect.DENY)
        self.gov.disable_rule(r.rule_id)
        self.gov.enable_rule(r.rule_id)
        self.assertFalse(self.gov.is_allowed({}))

    def test_remove_rule(self):
        r = self.gov.add_rule("deny_all", self.PolicyEffect.DENY)
        self.gov.remove_rule(r.rule_id)
        self.assertTrue(self.gov.is_allowed({}))

    def test_custom_fn(self):
        r = self.gov.add_rule("custom", self.PolicyEffect.DENY)
        self.gov.add_custom_fn(r.rule_id, lambda ctx: ctx.get("value", 0) > 50)
        self.assertFalse(self.gov.is_allowed({"value": 100}))
        self.assertTrue(self.gov.is_allowed({"value": 10}))

    def test_violation_recorded(self):
        self.gov.add_rule("deny_all", self.PolicyEffect.DENY)
        self.gov.evaluate({})
        violations = self.gov.violations()
        self.assertGreater(len(violations), 0)

    def test_resolve_violation(self):
        self.gov.add_rule("deny_all", self.PolicyEffect.DENY)
        self.gov.evaluate({})
        v = self.gov.violations()[0]
        self.assertTrue(self.gov.resolve_violation(v.violation_id))
        open_v = self.gov.violations(resolved=False)
        self.assertEqual(len(open_v), 0)

    def test_audit_log(self):
        self.gov.evaluate({"action": "read"})
        log = self.gov.audit_log()
        self.assertGreater(len(log), 0)

    def test_list_rules(self):
        self.gov.add_rule("r1", self.PolicyEffect.ALLOW)
        self.gov.add_rule("r2", self.PolicyEffect.DENY)
        rules = self.gov.list_rules()
        self.assertEqual(len(rules), 2)

    def test_stats(self):
        self.gov.add_rule("deny_all", self.PolicyEffect.DENY)
        self.gov.evaluate({})
        s = self.gov.stats()
        self.assertEqual(s["evaluations"], 1)
        self.assertEqual(s["denials"], 1)
        self.assertGreater(s["violations"], 0)

# ════════════════════════════════════════════════════════
# REPLAY BUFFER
# ════════════════════════════════════════════════════════
class TestReplayBuffer(unittest.TestCase):
    def setUp(self):
        from agent.replay_buffer import ReplayBuffer, SamplingStrategy
        self.SamplingStrategy = SamplingStrategy
        self.rb = ReplayBuffer(capacity=100, seed=42, db_path=":memory:")

    def _add(self, n=10, reward_fn=None):
        for i in range(n):
            r = reward_fn(i) if reward_fn else float(i)
            self.rb.add(state=i, action=i % 3, reward=r,
                        next_state=i + 1, done=(i == n - 1))

    def test_add_experience(self):
        e = self.rb.add(0, 1, 1.0, 1, False)
        self.assertEqual(len(self.rb), 1)
        self.assertIsNotNone(e.exp_id)

    def test_circular_capacity(self):
        from agent.replay_buffer import ReplayBuffer
        rb = ReplayBuffer(capacity=5, db_path=":memory:", seed=0)
        for i in range(10):
            rb.add(i, 0, 0.0, i + 1)
        self.assertEqual(len(rb), 5)

    def test_sample_uniform(self):
        self._add(20)
        batch = self.rb.sample(5)
        self.assertEqual(len(batch), 5)

    def test_sample_capped_to_buffer_size(self):
        self._add(3)
        batch = self.rb.sample(10)
        self.assertEqual(len(batch), 3)

    def test_sample_prioritized(self):
        from agent.replay_buffer import ReplayBuffer, SamplingStrategy
        rb = ReplayBuffer(capacity=100, strategy=SamplingStrategy.PRIORITIZED,
                          seed=0, db_path=":memory:")
        rb.add(0, 0, 1.0, 1, priority=0.1)
        rb.add(1, 0, 2.0, 2, priority=10.0)
        batch = rb.sample(10)
        self.assertEqual(len(batch), 2)

    def test_sample_recency(self):
        from agent.replay_buffer import ReplayBuffer, SamplingStrategy
        rb = ReplayBuffer(capacity=100, strategy=SamplingStrategy.RECENCY,
                          seed=0, db_path=":memory:")
        self._add.__func__(self, 10) if False else [
            rb.add(i, 0, float(i), i + 1) for i in range(10)]
        batch = rb.sample(5)
        self.assertEqual(len(batch), 5)

    def test_sample_reward(self):
        from agent.replay_buffer import ReplayBuffer, SamplingStrategy
        rb = ReplayBuffer(capacity=100, strategy=SamplingStrategy.REWARD,
                          seed=0, db_path=":memory:")
        rb.add(0, 0, 100.0, 1)
        rb.add(1, 0, 0.01, 2)
        batch = rb.sample(5)
        self.assertGreater(len(batch), 0)

    def test_update_priority(self):
        e = self.rb.add(0, 0, 1.0, 1)
        self.rb.update_priority(e.exp_id, 5.0)
        got = self.rb.get(e.exp_id)
        self.assertAlmostEqual(got.priority, 5.0)

    def test_update_priorities_batch(self):
        e1 = self.rb.add(0, 0, 1.0, 1)
        e2 = self.rb.add(1, 0, 2.0, 2)
        self.rb.update_priorities({e1.exp_id: 3.0, e2.exp_id: 4.0})
        self.assertAlmostEqual(self.rb.get(e1.exp_id).priority, 3.0)

    def test_n_step_returns(self):
        self._add(5, reward_fn=lambda i: 1.0)
        n_step = self.rb.n_step_returns(n=3, gamma=0.99)
        self.assertEqual(len(n_step), len(self.rb))

    def test_n_step_accumulates_reward(self):
        for _ in range(3):
            self.rb.add(0, 0, 1.0, 1, done=False)
        n_step = self.rb.n_step_returns(n=3, gamma=1.0)
        # First exp should accumulate 3 rewards
        self.assertAlmostEqual(n_step[0].reward, 3.0, places=1)

    def test_filter(self):
        self._add(10)
        done_exps = self.rb.filter(lambda e: e.done)
        self.assertGreater(len(done_exps), 0)
        self.assertTrue(all(e.done for e in done_exps))

    def test_by_episode(self):
        self._add(5)
        eps = self.rb.by_episode(0)
        self.assertGreater(len(eps), 0)

    def test_latest(self):
        self._add(10)
        latest = self.rb.latest(3)
        self.assertEqual(len(latest), 3)

    def test_mean_reward(self):
        for r in [1.0, 2.0, 3.0]: self.rb.add(0, 0, r, 1)
        self.assertAlmostEqual(self.rb.mean_reward(), 2.0)

    def test_max_min_reward(self):
        for r in [1.0, 5.0, 3.0]: self.rb.add(0, 0, r, 1)
        self.assertEqual(self.rb.max_reward(), 5.0)
        self.assertEqual(self.rb.min_reward(), 1.0)

    def test_is_full(self):
        from agent.replay_buffer import ReplayBuffer
        rb = ReplayBuffer(capacity=3, db_path=":memory:", seed=0)
        for i in range(3): rb.add(i, 0, 0.0, i + 1)
        self.assertTrue(rb.is_full())

    def test_clear(self):
        self._add(5)
        self.rb.clear()
        self.assertEqual(len(self.rb), 0)

    def test_add_episode(self):
        transitions = [(i, 0, float(i), i + 1, i == 4) for i in range(5)]
        exps = self.rb.add_episode(transitions)
        self.assertEqual(len(exps), 5)

    def test_stats(self):
        self._add(5)
        s = self.rb.stats()
        self.assertEqual(s["size"], 5)
        self.assertIn("mean_reward", s)
        self.assertIn("episodes", s)

# ════════════════════════════════════════════════════════
# CONFIG STORE
# ════════════════════════════════════════════════════════
class TestConfigStore(unittest.TestCase):
    def setUp(self):
        from agent.config_store import ConfigStore, ConfigType
        self.cs = ConfigStore(env_prefix="TEST_", db_path=":memory:")
        self.ConfigType = ConfigType

    def test_define_and_get(self):
        self.cs.define("app.name", self.ConfigType.STRING, default="MyApp")
        self.assertEqual(self.cs.get("app.name"), "MyApp")

    def test_set_and_get(self):
        self.cs.define("port", self.ConfigType.INT, default=8000)
        self.cs.set("port", 9000)
        self.assertEqual(self.cs.get("port"), 9000)

    def test_type_coercion_int(self):
        self.cs.define("count", self.ConfigType.INT)
        self.cs.set("count", "42")
        self.assertEqual(self.cs.get("count"), 42)

    def test_type_coercion_bool(self):
        self.cs.define("enabled", self.ConfigType.BOOL)
        self.cs.set("enabled", "true")
        self.assertTrue(self.cs.get("enabled"))

    def test_type_coercion_float(self):
        self.cs.define("rate", self.ConfigType.FLOAT)
        self.cs.set("rate", "3.14")
        self.assertAlmostEqual(self.cs.get("rate"), 3.14)

    def test_validation_min(self):
        from agent.config_store import ConfigError
        self.cs.define("timeout", self.ConfigType.INT, min_value=1)
        with self.assertRaises(ConfigError):
            self.cs.set("timeout", 0)

    def test_validation_max(self):
        from agent.config_store import ConfigError
        self.cs.define("threads", self.ConfigType.INT, max_value=32)
        with self.assertRaises(ConfigError):
            self.cs.set("threads", 100)

    def test_validation_choices(self):
        from agent.config_store import ConfigError
        self.cs.define("level", self.ConfigType.STRING,
                       choices=["debug", "info", "error"])
        with self.assertRaises(ConfigError):
            self.cs.set("level", "verbose")

    def test_validation_pattern(self):
        from agent.config_store import ConfigError
        self.cs.define("code", self.ConfigType.STRING, pattern=r"[A-Z]{3}")
        self.cs.set("code", "ABC")  # ok
        with self.assertRaises(ConfigError):
            self.cs.set("code", "abc")

    def test_required_fails_on_validate(self):
        self.cs.define("must_have", self.ConfigType.STRING, required=True)
        errors = self.cs.validate_all()
        self.assertIn("must_have", errors)

    def test_watcher_called_on_change(self):
        changes = []
        self.cs.define("x", self.ConfigType.INT, default=0)
        self.cs.watch("x", lambda k, old, new: changes.append((old, new)))
        self.cs.set("x", 1)
        self.assertEqual(changes, [(0, 1)])

    def test_global_watcher(self):
        seen = []
        self.cs.define("y", self.ConfigType.STRING)
        self.cs.watch_all(lambda k, o, n: seen.append(k))
        self.cs.set("y", "hello")
        self.assertIn("y", seen)

    def test_history_tracked(self):
        self.cs.define("v", self.ConfigType.INT, default=1)
        self.cs.set("v", 2)
        self.cs.set("v", 3)
        hist = self.cs.history("v")
        self.assertEqual(len(hist), 2)

    def test_rollback(self):
        self.cs.define("v", self.ConfigType.INT, default=1)
        self.cs.set("v", 2)
        self.cs.set("v", 3)
        self.cs.rollback("v", steps=1)
        self.assertEqual(self.cs.get("v"), 2)

    def test_delete_key(self):
        self.cs.define("temp_del", self.ConfigType.STRING)
        self.cs.set("temp_del", "y")
        self.assertTrue(self.cs.delete("temp_del"))
        self.assertIsNone(self.cs.get("temp_del"))

    def test_reset_to_default(self):
        self.cs.define("reset_me", self.ConfigType.INT, default=10)
        self.cs.set("reset_me", 99)
        self.cs.reset("reset_me")
        self.assertEqual(self.cs.get("reset_me"), 10)

    def test_env_override(self, monkeypatch=None):
        import os
        self.cs.define("debug", self.ConfigType.BOOL, default=False)
        os.environ["TEST_DEBUG"] = "true"
        try:
            self.assertTrue(self.cs.get("debug"))
        finally:
            del os.environ["TEST_DEBUG"]

    def test_namespace_get(self):
        self.cs.define("db.host", self.ConfigType.STRING, default="localhost")
        self.cs.define("db.port", self.ConfigType.INT, default=5432)
        ns = self.cs.get_namespace("db")
        self.assertIn("host", ns)
        self.assertIn("port", ns)

    def test_export_masks_secrets(self):
        self.cs.define("api_token", self.ConfigType.SECRET)
        self.cs.set("api_token", "super_secret")
        exported = self.cs.export()
        self.assertEqual(exported.get("api_token"), "***")

    def test_set_many(self):
        self.cs.define("a", self.ConfigType.INT)
        self.cs.define("b", self.ConfigType.STRING)
        self.cs.set_many({"a": 1, "b": "hello"})
        self.assertEqual(self.cs.get("a"), 1)
        self.assertEqual(self.cs.get("b"), "hello")

    def test_stats(self):
        self.cs.define("x", self.ConfigType.INT, default=1)
        s = self.cs.stats()
        self.assertIn("keys", s)
        self.assertIn("schemas", s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v50: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
