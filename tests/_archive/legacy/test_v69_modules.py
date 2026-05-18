"""OMNI AGENT v69: StreamingPipelineV2, ConfigManagerV2, EmbeddingPipelineV2, ReplayManagerV2"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# STREAMING PIPELINE V2
# ════════════════════════════════════════════════════════
class TestStreamingPipelineV2(unittest.TestCase):
    def setUp(self):
        from agent.streaming_pipeline_v2 import StreamingPipelineV2
        self.sp = StreamingPipelineV2(db_path=":memory:")

    def test_ingest_record(self):
        r = self.sp.ingest(42)
        self.assertIsNotNone(r.record_id)
        self.assertEqual(len(self.sp._buffer), 1)

    def test_ingest_batch(self):
        count = self.sp.ingest_batch([1, 2, 3, 4, 5])
        self.assertEqual(count, 5)
        self.assertEqual(len(self.sp._buffer), 5)

    def test_map_stage(self):
        self.sp.map(lambda r: r.data * 2)
        self.sp.ingest(5)
        out = self.sp.process_all()
        self.assertEqual(out[0].data, 10)

    def test_filter_stage(self):
        self.sp.filter(lambda r: r.data > 3)
        for v in [1, 2, 3, 4, 5]:
            self.sp.ingest(v)
        out = self.sp.process_all()
        self.assertEqual(len(out), 2)  # 4 and 5

    def test_flatmap_stage(self):
        self.sp.flatmap(lambda r: [r.data, r.data * 10])
        self.sp.ingest(3)
        out = self.sp.process_all()
        self.assertEqual(len(out), 2)

    def test_chained_stages(self):
        self.sp.map(lambda r: r.data + 1).filter(lambda r: r.data > 3)
        for v in [1, 2, 3, 4]:
            self.sp.ingest(v)
        out = self.sp.process_all()
        self.assertEqual(len(out), 2)  # (3+1=4)>3, (4+1=5)>3

    def test_keyed_ingestion(self):
        self.sp.ingest(1, key="a")
        self.sp.ingest(2, key="a")
        self.sp.ingest(3, key="b")
        self.assertEqual(len(self.sp.keyed_records("a")), 2)
        self.assertEqual(len(self.sp.keyed_records("b")), 1)

    def test_tumbling_window(self):
        records = []
        now = time.time()
        for i in range(6):
            r = self.sp.ingest(float(i))
            r.ts = now + i
            records.append(r)
        windows = self.sp.tumbling_window(records, size_s=3.0)
        self.assertGreater(len(windows), 0)
        self.assertEqual(windows[0].aggregations["count"], 3)

    def test_sliding_window(self):
        records = []
        now = time.time()
        for i in range(5):
            r = self.sp.ingest(float(i))
            r.ts = now + i
            records.append(r)
        windows = self.sp.sliding_window(records, size_s=3.0, slide_s=1.0)
        self.assertGreater(len(windows), 1)

    def test_count_window(self):
        for i in range(10):
            self.sp.ingest(float(i))
        records = list(self.sp._buffer)
        windows = self.sp.count_window(records, count=3)
        self.assertEqual(len(windows), 4)  # ceil(10/3) = 4

    def test_session_window(self):
        records = []
        now = time.time()
        # Two sessions: 0,1,2 then gap, 10,11
        for ts in [0, 1, 2, 10, 11]:
            r = self.sp.ingest(float(ts))
            r.ts = now + ts
            records.append(r)
        windows = self.sp.session_window(records, gap_s=5.0)
        self.assertEqual(len(windows), 2)

    def test_aggregation_stats(self):
        records = []
        for v in [1.0, 2.0, 3.0, 4.0]:
            r = self.sp.ingest(v); records.append(r)
        windows = self.sp.count_window(records, count=4)
        agg = windows[0].aggregations
        self.assertAlmostEqual(agg["sum"], 10.0)
        self.assertAlmostEqual(agg["avg"], 2.5)
        self.assertEqual(agg["min"], 1.0)
        self.assertEqual(agg["max"], 4.0)

    def test_dlq_on_error(self):
        self.sp.add_stage("fail",
                           lambda r: (_ for _ in ()).throw(ValueError("err")),
                           on_error="dlq")
        self.sp.ingest(1)
        self.sp.process_all()
        self.assertEqual(len(self.sp.dlq()), 1)

    def test_error_skip(self):
        self.sp.add_stage("fail_skip",
                           lambda r: (_ for _ in ()).throw(ValueError("e")),
                           on_error="skip")
        self.sp.ingest(1)
        out = self.sp.process_all()  # should not raise
        self.assertEqual(len(out), 0)

    def test_stats(self):
        self.sp.ingest_batch([1, 2, 3])
        self.sp.process_all()
        s = self.sp.stats()
        self.assertGreater(s["processed"], 0)


# ════════════════════════════════════════════════════════
# CONFIG MANAGER V2
# ════════════════════════════════════════════════════════
class TestConfigManagerV2(unittest.TestCase):
    def setUp(self):
        from agent.config_manager_v2 import ConfigManagerV2
        self.cm = ConfigManagerV2(db_path=":memory:")

    def test_load_defaults(self):
        self.cm.load_defaults({"app": {"name": "omni", "debug": False}})
        self.assertEqual(self.cm.get("app.name"), "omni")

    def test_load_json(self):
        self.cm.load_json('{"db": {"host": "localhost", "port": 5432}}')
        self.assertEqual(self.cm.get("db.host"), "localhost")

    def test_set_get(self):
        self.cm.set("key.one", 42)
        self.assertEqual(self.cm.get("key.one"), 42)

    def test_default_fallback(self):
        val = self.cm.get("nonexistent.key", default="fallback")
        self.assertEqual(val, "fallback")

    def test_layer_priority(self):
        from agent.config_manager_v2 import ConfigSource
        self.cm.load_defaults({"x": "default"})
        self.cm.set("x", "runtime", source=ConfigSource.RUNTIME)
        self.cm.override("x", "override")
        self.assertEqual(self.cm.get("x"), "override")

    def test_schema_type_coercion(self):
        self.cm.register("port", schema_type=int)
        self.cm.set("port", "8080")
        self.assertEqual(self.cm.get("port"), 8080)

    def test_validator(self):
        self.cm.register("age",
            validators=[lambda v: "must be >= 0" if int(v) < 0 else None])
        with self.assertRaises(ValueError):
            self.cm.set("age", -1)

    def test_required_key_missing(self):
        self.cm.register("must_have", required=True)
        missing = self.cm.validate_required()
        self.assertIn("must_have", missing)

    def test_on_change_callback(self):
        changes = []
        self.cm.register("watched",
            on_change=lambda old, new: changes.append((old, new)))
        self.cm.set("watched", "v1")
        self.cm.set("watched", "v2")
        self.assertGreaterEqual(len(changes), 1)  # at least one change fired

    def test_secret_masking(self):
        self.cm.register("api_key", secret=True)
        self.cm.set("api_key", "sk-12345")
        dump = self.cm.dump(include_secrets=False)
        self.assertEqual(dump.get("api_key"), "***")

    def test_interpolation(self):
        self.cm.set("base_url", "http://localhost")
        self.cm.set("api_url", "${base_url}/api/v1")
        self.assertEqual(self.cm.get("api_url"), "http://localhost/api/v1")

    def test_namespace(self):
        self.cm.set("db.host", "localhost")
        self.cm.set("db.port", 5432)
        ns = self.cm.namespace("db")
        self.assertEqual(ns.get("host"), "localhost")
        ns.set("port", 5433)
        self.assertEqual(self.cm.get("db.port"), 5433)

    def test_snapshot_rollback(self):
        self.cm.set("rollback_key", "v1")
        snap = self.cm.snapshot()
        self.cm.set("rollback_key", "v2")
        self.assertEqual(self.cm.get("rollback_key"), "v2")
        self.cm.rollback(snap.snapshot_id)
        self.assertEqual(self.cm.get("rollback_key"), "v1")

    def test_diff_snapshots(self):
        self.cm.set("diff_key", "a")
        s1 = self.cm.snapshot()
        self.cm.set("diff_key", "b")
        s2 = self.cm.snapshot()
        diff = self.cm.diff(s1.snapshot_id, s2.snapshot_id)
        self.assertIn("diff_key", diff)

    def test_change_history(self):
        self.cm.set("hist_key", 1)
        self.cm.set("hist_key", 2)
        h = self.cm.change_history("hist_key")
        self.assertGreater(len(h), 0)

    def test_stats(self):
        self.cm.set("a", 1); self.cm.set("b", 2)
        s = self.cm.stats()
        self.assertGreater(s["total_keys"], 0)


# ════════════════════════════════════════════════════════
# EMBEDDING PIPELINE V2
# ════════════════════════════════════════════════════════
_DIM = 4
def _fake_embed(text: str) -> list:
    h = hash(text)
    return [(h >> i & 0xFF) / 255.0 for i in range(_DIM)]

class TestEmbeddingPipelineV2(unittest.TestCase):
    def setUp(self):
        from agent.embedding_pipeline_v2 import EmbeddingPipelineV2
        self.ep = EmbeddingPipelineV2(embed_fn=_fake_embed, db_path=":memory:",
                                       default_chunk_size=10, default_overlap=2)

    def test_add_document(self):
        doc = self.ep.add_document("Hello world", title="Test")
        self.assertIsNotNone(doc.doc_id)

    def test_dedup(self):
        d1 = self.ep.add_document("same content", dedup=True)
        d2 = self.ep.add_document("same content", dedup=True)
        self.assertEqual(d1.doc_id, d2.doc_id)

    def test_remove_document(self):
        doc = self.ep.add_document("to remove")
        ok  = self.ep.remove_document(doc.doc_id)
        self.assertTrue(ok)
        self.assertIsNone(self.ep.get_document(doc.doc_id))

    def test_chunk_fixed(self):
        from agent.embedding_pipeline_v2 import ChunkStrategy
        doc = self.ep.add_document("one two three four five six seven")
        chunks = self.ep.chunk_document(doc.doc_id, ChunkStrategy.FIXED,
                                         chunk_size=3, overlap=0)
        self.assertGreater(len(chunks), 1)

    def test_chunk_sentence(self):
        from agent.embedding_pipeline_v2 import ChunkStrategy
        doc = self.ep.add_document("First sentence. Second sentence. Third sentence.")
        chunks = self.ep.chunk_document(doc.doc_id, ChunkStrategy.SENTENCE,
                                         chunk_size=1, overlap=0)
        self.assertGreater(len(chunks), 1)

    def test_chunk_paragraph(self):
        from agent.embedding_pipeline_v2 import ChunkStrategy
        doc = self.ep.add_document("Para one.\n\nPara two.\n\nPara three.")
        chunks = self.ep.chunk_document(doc.doc_id, ChunkStrategy.PARAGRAPH)
        self.assertEqual(len(chunks), 3)

    def test_embed_chunk(self):
        doc   = self.ep.add_document("Embed me please")
        chunks = self.ep.chunk_document(doc.doc_id)
        c = self.ep.embed_chunk(chunks[0].chunk_id)
        self.assertIsNotNone(c.embedding)
        self.assertEqual(len(c.embedding), _DIM)

    def test_embed_document(self):
        doc = self.ep.add_document("Embed whole doc now here")
        chunks = self.ep.embed_document(doc.doc_id)
        embedded = [c for c in chunks if c.embedding]
        self.assertGreater(len(embedded), 0)

    def test_embed_all(self):
        self.ep.add_document("Doc A content here")
        self.ep.add_document("Doc B content there")
        count = self.ep.embed_all()
        self.assertGreater(count, 0)

    def test_vector_search(self):
        doc = self.ep.add_document("Python programming language features")
        self.ep.embed_document(doc.doc_id)
        results = self.ep.search("Python", top_k=3)
        self.assertGreater(len(results), 0)
        self.assertIsInstance(results[0].score, float)

    def test_keyword_search(self):
        self.ep.add_document("The quick brown fox jumps over the lazy dog")
        self.ep.chunk_document(
            list(self.ep._docs.values())[-1].doc_id)
        results = self.ep.keyword_search("fox", top_k=3)
        self.assertGreater(len(results), 0)

    def test_hybrid_search(self):
        doc = self.ep.add_document("Neural networks deep learning artificial intelligence")
        self.ep.embed_document(doc.doc_id)
        results = self.ep.hybrid_search("neural", top_k=3, alpha=0.5)
        self.assertGreater(len(results), 0)

    def test_build_index(self):
        doc = self.ep.add_document("Index test document content here")
        self.ep.embed_document(doc.doc_id)
        ix = self.ep.build_index("test_index")
        self.assertGreater(ix.chunk_count, 0)

    def test_stats(self):
        self.ep.add_document("Stats test content")
        self.ep.embed_all()
        s = self.ep.stats()
        self.assertGreater(s["documents"], 0)
        self.assertGreater(s["chunks"], 0)


# ════════════════════════════════════════════════════════
# REPLAY MANAGER V2
# ════════════════════════════════════════════════════════
class TestReplayManagerV2(unittest.TestCase):
    def setUp(self):
        from agent.replay_manager_v2 import ReplayManagerV2
        self.rm = ReplayManagerV2(db_path=":memory:")

    def test_append_event(self):
        e = self.rm.append("stream1", "user.created", payload={"id": 1})
        self.assertEqual(e.sequence, 1)
        self.assertEqual(e.stream_id, "stream1")

    def test_sequence_increment(self):
        e1 = self.rm.append("s1", "ev.a")
        e2 = self.rm.append("s1", "ev.b")
        self.assertEqual(e2.sequence, e1.sequence + 1)

    def test_get_events_basic(self):
        for i in range(5):
            self.rm.append("s2", "ev", payload=i)
        events = self.rm.get_events("s2")
        self.assertEqual(len(events), 5)

    def test_get_events_from_seq(self):
        for i in range(5):
            self.rm.append("s3", "ev", payload=i)
        events = self.rm.get_events("s3", from_seq=3)
        self.assertEqual(len(events), 3)

    def test_get_events_by_type(self):
        self.rm.append("s4", "user.login")
        self.rm.append("s4", "user.logout")
        self.rm.append("s4", "user.login")
        events = self.rm.get_events("s4", event_type="user.login")
        self.assertEqual(len(events), 2)

    def test_replay_calls_handler(self):
        from agent.replay_manager_v2 import ReplayStatus
        for i in range(3):
            self.rm.append("r1", "ev", payload=i)
        collected = []
        sess = self.rm.replay("r1", lambda e: collected.append(e.payload))
        self.assertEqual(len(collected), 3)
        self.assertEqual(sess.status, ReplayStatus.DONE)

    def test_replay_filter(self):
        for i in range(5):
            self.rm.append("r2", "ev", payload=i)
        collected = []
        self.rm.replay("r2", lambda e: collected.append(e.payload),
                        filter_fn=lambda e: e.payload > 2)
        self.assertEqual(collected, [3, 4])

    def test_fold_state_reconstruction(self):
        self.rm.append("fold1", "inc", payload=1)
        self.rm.append("fold1", "inc", payload=2)
        self.rm.append("fold1", "inc", payload=3)
        def reducer(state, e):
            state["total"] = state.get("total", 0) + e.payload
            return state
        state = self.rm.fold("fold1", reducer)
        self.assertEqual(state["total"], 6)

    def test_snapshot_and_catchup(self):
        for i in range(3):
            self.rm.append("snap1", "inc", payload=i + 1)
        def reducer(state, e):
            state["sum"] = state.get("sum", 0) + e.payload
            return state
        state_at_3 = self.rm.fold("snap1", reducer)
        self.rm.snapshot_state("snap1", state_at_3)
        self.rm.append("snap1", "inc", payload=10)
        final = self.rm.catchup("snap1", reducer)
        self.assertEqual(final["sum"], 16)  # 1+2+3+10

    def test_latest_events(self):
        for i in range(10):
            self.rm.append("lat1", "ev", payload=i)
        last = self.rm.latest("lat1", n=3)
        self.assertEqual(len(last), 3)
        self.assertEqual(last[-1].payload, 9)

    def test_projection(self):
        for i in range(4):
            self.rm.append("proj1", "ev", payload=i * 2)
        values = self.rm.project("proj1", lambda e: e.payload)
        self.assertEqual(values, [0, 2, 4, 6])

    def test_merge_streams(self):
        self.rm.append("m1", "ev", payload="a")
        self.rm.append("m2", "ev", payload="b")
        count = self.rm.merge_streams(["m1", "m2"], "merged")
        self.assertEqual(count, 2)
        events = self.rm.get_events("merged")
        self.assertEqual(len(events), 2)

    def test_append_batch(self):
        events_data = [
            {"event_type": "login",  "payload": {"user": "alice"}},
            {"event_type": "action", "payload": {"action": "view"}},
        ]
        events = self.rm.append_batch("batch1", events_data)
        self.assertEqual(len(events), 2)

    def test_replay_iter(self):
        for i in range(3):
            self.rm.append("iter1", "ev", payload=i)
        payloads = [e.payload for e in self.rm.replay_iter("iter1")]
        self.assertEqual(payloads, [0, 1, 2])

    def test_stats(self):
        self.rm.append("st1", "ev")
        self.rm.append("st2", "ev")
        s = self.rm.stats()
        self.assertGreater(s["streams"], 0)
        self.assertGreater(s["total_events"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v69: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
