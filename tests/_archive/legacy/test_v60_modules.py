"""OMNI AGENT v60: FeatureStore, DocumentIndexer, AccessControlV2, AuditLoggerV2"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# FEATURE STORE
# ════════════════════════════════════════════════════════
class TestFeatureStore(unittest.TestCase):
    def setUp(self):
        from agent.feature_store import FeatureStore
        self.fs = FeatureStore(db_path=":memory:")

    def test_register_feature(self):
        from agent.feature_store import FeatureType
        f = self.fs.register_feature("age", "users", FeatureType.INTEGER)
        self.assertEqual(f.name, "age")

    def test_register_group(self):
        g = self.fs.register_group("users", entity_type="user")
        self.assertIn("users", self.fs._groups)

    def test_ingest_and_get_online(self):
        self.fs.ingest("u1", {"age": 25, "score": 0.9})
        fv = self.fs.get_online("u1")
        self.assertIsNotNone(fv)
        self.assertEqual(fv.get("age"), 25)

    def test_get_online_selected_features(self):
        self.fs.ingest("u2", {"a": 1, "b": 2, "c": 3})
        fv = self.fs.get_online("u2", feature_names=["a", "c"])
        self.assertIn("a", fv.features)
        self.assertIn("c", fv.features)
        self.assertNotIn("b", fv.features)

    def test_default_value_on_miss(self):
        from agent.feature_store import FeatureType
        self.fs.register_feature("score", "test_g",
                                  FeatureType.FLOAT, default_value=0.0)
        self.fs.ingest("u3", {"score": None})
        # get_feature_value returns default when value is None
        fv = self.fs.get_online("u3")
        val = fv.features.get("score", 0.0) if fv else 0.0
        self.assertEqual(val, 0.0)

    def test_get_online_miss_returns_none(self):
        fv = self.fs.get_online("nonexistent_entity_xyz")
        self.assertIsNone(fv)

    def test_ingest_batch(self):
        records = [{"entity_id": f"e{i}", "features": {"x": i}} for i in range(5)]
        vecs = self.fs.ingest_batch(records)
        self.assertEqual(len(vecs), 5)
        self.assertEqual(self.fs.get_online("e0").get("x"), 0)

    def test_get_online_batch(self):
        self.fs.ingest("b1", {"v": 10})
        self.fs.ingest("b2", {"v": 20})
        result = self.fs.get_online_batch(["b1", "b2", "bX"])
        self.assertIn("b1", result)
        self.assertIn("b2", result)
        self.assertNotIn("bX", result)

    def test_get_offline_history(self):
        self.fs.ingest("h1", {"val": 1}, ts=1000.0)
        self.fs.ingest("h1", {"val": 2}, ts=2000.0)
        history = self.fs.get_offline("h1", limit=10)
        self.assertEqual(len(history), 2)

    def test_point_in_time(self):
        self.fs.ingest("pit1", {"v": 1}, ts=1000.0)
        self.fs.ingest("pit1", {"v": 2}, ts=3000.0)
        history = self.fs.get_offline("pit1", as_of=2000.0)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].get("v"), 1)

    def test_transform_on_ingest(self):
        from agent.feature_store import FeatureType
        self.fs.register_feature("upper_name", "t_group",
                                  FeatureType.STRING,
                                  transform=lambda x: x.upper())
        self.fs.ingest("t1", {"upper_name": "hello"})
        fv = self.fs.get_online("t1")
        self.assertEqual(fv.get("upper_name"), "HELLO")

    def test_source_materialization(self):
        self.fs.register_source("src_group", lambda eid: {"computed": len(eid)})
        fv = self.fs.materialize("entity_abc", "src_group")
        self.assertIsNotNone(fv)
        self.assertEqual(fv.get("computed"), len("entity_abc"))

    def test_materialize_batch(self):
        self.fs.register_source("mb_group", lambda eid: {"n": 42})
        vecs = self.fs.materialize_batch(["a", "b", "c"], "mb_group")
        self.assertEqual(len(vecs), 3)

    def test_on_demand_materialization(self):
        self.fs.register_group("od_group")
        self.fs.register_source("od_group", lambda eid: {"on_demand": True})
        fv = self.fs.get_online("fresh_entity", group_id="od_group")
        self.assertIsNotNone(fv)

    def test_ttl_expiry(self):
        self.fs.register_group("ttl_group", ttl_s=0.01)
        self.fs.ingest("ttl_e", {"x": 1}, feature_group="ttl_group")
        time.sleep(0.02)
        fv = self.fs.get_online("ttl_e", group_id="ttl_group")
        self.assertIsNone(fv)

    def test_feature_stats(self):
        from agent.feature_store import FeatureType
        self.fs.register_feature("price", "items", FeatureType.FLOAT)
        self.fs.ingest("i1", {"price": 10.0})
        self.fs.ingest("i2", {"price": 20.0})
        stats = self.fs.feature_stats("price")
        self.assertIsNotNone(stats)
        self.assertEqual(stats["count"], 2)
        self.assertAlmostEqual(stats["mean"], 15.0)

    def test_freshness(self):
        self.fs.ingest("fr1", {"x": 1})
        f = self.fs.freshness("fr1")
        self.assertIsNotNone(f)
        self.assertLess(f, 5.0)

    def test_list_features(self):
        from agent.feature_store import FeatureType
        self.fs.register_feature("f1", "g1", FeatureType.FLOAT)
        feats = self.fs.list_features(group="g1")
        self.assertEqual(len(feats), 1)

    def test_stats(self):
        self.fs.ingest("s1", {"x": 1})
        s = self.fs.stats()
        self.assertGreater(s["entities_online"], 0)
        self.assertGreater(s["ingest_count"], 0)

# ════════════════════════════════════════════════════════
# DOCUMENT INDEXER
# ════════════════════════════════════════════════════════
class TestDocumentIndexer(unittest.TestCase):
    def setUp(self):
        from agent.document_indexer import DocumentIndexer
        self.di = DocumentIndexer(db_path=":memory:")

    def test_index_doc(self):
        doc = self.di.index("Hello World", "This is a test document")
        self.assertIsNotNone(doc.doc_id)
        self.assertIn(doc.doc_id, self.di._docs)

    def test_basic_search(self):
        self.di.index("Python Guide", "Python is a programming language")
        results = self.di.search("Python programming")
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].title, "Python Guide")

    def test_bm25_ranking_order(self):
        self.di.index("A", "python python python")
        self.di.index("B", "python once")
        results = self.di.search("python")
        self.assertEqual(results[0].title, "A")

    def test_no_results_for_unknown(self):
        self.di.index("Doc", "Some content here")
        results = self.di.search("zzzzunknownterm9999")
        self.assertEqual(len(results), 0)

    def test_facet_filter(self):
        self.di.index("Doc1", "content", facets={"lang": "en"})
        self.di.index("Doc2", "content", facets={"lang": "fr"})
        results = self.di.search("content", facet_filter={"lang": "en"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Doc1")

    def test_tag_filter(self):
        self.di.index("Tagged", "content here", tags=["ai"])
        self.di.index("Untagged", "content here")
        results = self.di.search("content", tag_filter=["ai"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Tagged")

    def test_source_filter(self):
        self.di.index("S1", "text", source="blog")
        self.di.index("S2", "text", source="wiki")
        results = self.di.search("text", source_filter="blog")
        self.assertEqual(len(results), 1)

    def test_soft_delete(self):
        from agent.document_indexer import IndexStatus
        doc = self.di.index("Delete Me", "delete test content")
        self.di.delete(doc.doc_id, soft=True)
        results = self.di.search("delete test")
        self.assertEqual(len(results), 0)

    def test_hard_delete(self):
        doc = self.di.index("Hard Del", "hard delete test")
        self.di.delete(doc.doc_id, soft=False)
        self.assertNotIn(doc.doc_id, self.di._docs)

    def test_update_doc(self):
        doc = self.di.index("Old Title", "old content")
        self.di.update(doc.doc_id, title="New Title", content="new content")
        results = self.di.search("new content")
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].title, "New Title")

    def test_snippet_generated(self):
        self.di.index("Test", "The quick brown fox jumps over the lazy dog")
        results = self.di.search("fox")
        self.assertGreater(len(results[0].snippet), 0)

    def test_highlights_generated(self):
        self.di.index("HL", "The quick brown fox jumps over the lazy dog")
        results = self.di.search("fox")
        self.assertGreater(len(results[0].highlights), 0)

    def test_rank_assigned(self):
        self.di.index("R1", "ranking test content")
        self.di.index("R2", "ranking test")
        results = self.di.search("ranking test")
        self.assertEqual(results[0].rank, 1)
        self.assertEqual(results[1].rank, 2)

    def test_top_k_respected(self):
        for i in range(10):
            self.di.index(f"Doc{i}", "common term search")
        results = self.di.search("common term", top_k=3)
        self.assertLessEqual(len(results), 3)

    def test_get_doc(self):
        doc = self.di.index("GetMe", "get doc test")
        got = self.di.get_doc(doc.doc_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.title, "GetMe")

    def test_list_docs(self):
        self.di.index("L1", "list test")
        self.di.index("L2", "list test")
        docs = self.di.list_docs()
        self.assertGreaterEqual(len(docs), 2)

    def test_query_log(self):
        self.di.index("Q", "query log test content")
        self.di.search("query log")
        log = self.di.query_log()
        self.assertGreater(len(log), 0)

    def test_stats(self):
        self.di.index("St", "stats test")
        self.di.search("stats")
        s = self.di.stats()
        self.assertGreater(s["total_docs"], 0)
        self.assertGreater(s["queries"], 0)

# ════════════════════════════════════════════════════════
# ACCESS CONTROL V2
# ════════════════════════════════════════════════════════
class TestAccessControlV2(unittest.TestCase):
    def setUp(self):
        from agent.access_control_v2 import AccessControlV2
        self.ac = AccessControlV2(default_deny=True, db_path=":memory:")

    def test_deny_by_default(self):
        p = self.ac.create_principal("alice")
        d = self.ac.check(p.principal_id, "doc:1", __import__('agent.access_control_v2', fromlist=['Action']).Action.READ)
        self.assertFalse(d.allowed)

    def test_allow_with_permission(self):
        from agent.access_control_v2 import Action
        perm = self.ac.add_permission("document", Action.READ)
        role = self.ac.create_role("reader", permission_ids=[perm.permission_id])
        p    = self.ac.create_principal("bob")
        self.ac.assign_role(p.principal_id, role.role_id)
        d = self.ac.check(p.principal_id, "document", Action.READ)
        self.assertTrue(d.allowed)

    def test_wildcard_resource(self):
        from agent.access_control_v2 import Action
        perm = self.ac.add_permission("api/*", Action.READ)
        role = self.ac.create_role("api_reader", permission_ids=[perm.permission_id])
        p    = self.ac.create_principal("carol")
        self.ac.assign_role(p.principal_id, role.role_id)
        d = self.ac.check(p.principal_id, "api/v1/users", Action.READ)
        self.assertTrue(d.allowed)

    def test_role_inheritance(self):
        from agent.access_control_v2 import Action
        perm   = self.ac.add_permission("data", Action.READ)
        parent = self.ac.create_role("base", permission_ids=[perm.permission_id])
        child  = self.ac.create_role("child", parent_role_ids=[parent.role_id])
        p      = self.ac.create_principal("dave")
        self.ac.assign_role(p.principal_id, child.role_id)
        d = self.ac.check(p.principal_id, "data", Action.READ)
        self.assertTrue(d.allowed)

    def test_direct_permission(self):
        from agent.access_control_v2 import Action
        perm = self.ac.add_permission("secret", Action.READ)
        p    = self.ac.create_principal("eve")
        self.ac.grant_direct(p.principal_id, perm.permission_id)
        d = self.ac.check(p.principal_id, "secret", Action.READ)
        self.assertTrue(d.allowed)

    def test_revoke_role(self):
        from agent.access_control_v2 import Action
        perm = self.ac.add_permission("item", Action.READ)
        role = self.ac.create_role("r", permission_ids=[perm.permission_id])
        p    = self.ac.create_principal("frank")
        self.ac.assign_role(p.principal_id, role.role_id)
        self.ac.revoke_role(p.principal_id, role.role_id)
        self.assertFalse(self.ac.is_allowed(p.principal_id, "item", Action.READ))

    def test_deny_rule_overrides(self):
        from agent.access_control_v2 import Action
        perm = self.ac.add_permission("resource", Action.READ)
        role = self.ac.create_role("r2", permission_ids=[perm.permission_id])
        p    = self.ac.create_principal("grace")
        self.ac.assign_role(p.principal_id, role.role_id)
        self.ac.add_deny_rule(p.principal_id, "resource", Action.READ)
        d = self.ac.check(p.principal_id, "resource", Action.READ)
        self.assertFalse(d.allowed)

    def test_deactivated_principal_denied(self):
        from agent.access_control_v2 import Action
        perm = self.ac.add_permission("x", Action.READ)
        role = self.ac.create_role("rx", permission_ids=[perm.permission_id])
        p    = self.ac.create_principal("henry")
        self.ac.assign_role(p.principal_id, role.role_id)
        self.ac.deactivate_principal(p.principal_id)
        self.assertFalse(self.ac.is_allowed(p.principal_id, "x", Action.READ))

    def test_abac_condition(self):
        from agent.access_control_v2 import Action
        perm = self.ac.add_permission("abac_r", Action.READ)
        role = self.ac.create_role("abac_role", permission_ids=[perm.permission_id])
        p    = self.ac.create_principal("ivan", attributes={"dept": "eng"})
        self.ac.assign_role(p.principal_id, role.role_id)
        self.ac.add_abac_condition(
            lambda pr, res, act: pr.attributes.get("dept") == "eng")
        self.assertTrue(self.ac.is_allowed(p.principal_id, "abac_r", Action.READ))

    def test_abac_condition_blocks(self):
        from agent.access_control_v2 import Action
        perm = self.ac.add_permission("abac_r2", Action.READ)
        role = self.ac.create_role("abac_role2", permission_ids=[perm.permission_id])
        p    = self.ac.create_principal("julia", attributes={"dept": "hr"})
        self.ac.assign_role(p.principal_id, role.role_id)
        self.ac.add_abac_condition(
            lambda pr, res, act: pr.attributes.get("dept") == "eng")
        self.assertFalse(self.ac.is_allowed(p.principal_id, "abac_r2", Action.READ))

    def test_check_bulk(self):
        from agent.access_control_v2 import Action
        perm = self.ac.add_permission("bulk_r", Action.READ)
        role = self.ac.create_role("bulk_role", permission_ids=[perm.permission_id])
        p    = self.ac.create_principal("ken")
        self.ac.assign_role(p.principal_id, role.role_id)
        results = self.ac.check_bulk(p.principal_id, [
            ("bulk_r", Action.READ), ("other_r", Action.UPDATE)])
        self.assertTrue(results[0].allowed)
        self.assertFalse(results[1].allowed)

    def test_effective_permissions(self):
        from agent.access_control_v2 import Action
        perm = self.ac.add_permission("ep_r", Action.READ)
        role = self.ac.create_role("ep_role", permission_ids=[perm.permission_id])
        p    = self.ac.create_principal("lisa")
        self.ac.assign_role(p.principal_id, role.role_id)
        ep = self.ac.effective_permissions(p.principal_id)
        self.assertGreater(len(ep), 0)

    def test_audit_log(self):
        from agent.access_control_v2 import Action
        p = self.ac.create_principal("mike")
        self.ac.check(p.principal_id, "res", Action.READ)
        log = self.ac.audit_log(p.principal_id)
        self.assertGreater(len(log), 0)

    def test_stats(self):
        from agent.access_control_v2 import Action
        p = self.ac.create_principal("nancy")
        self.ac.check(p.principal_id, "res", Action.READ)
        s = self.ac.stats()
        self.assertGreater(s["checks"], 0)

# ════════════════════════════════════════════════════════
# AUDIT LOGGER V2
# ════════════════════════════════════════════════════════
class TestAuditLoggerV2(unittest.TestCase):
    def setUp(self):
        from agent.audit_logger_v2 import AuditLoggerV2
        self.al = AuditLoggerV2(db_path=":memory:")

    def test_log_entry(self):
        from agent.audit_logger_v2 import AuditEventType
        e = self.al.log(AuditEventType.AUTH, "login", actor_id="user1")
        self.assertIsNotNone(e.entry_id)
        self.assertGreater(len(e.entry_hash), 0)

    def test_sequence_increments(self):
        from agent.audit_logger_v2 import AuditEventType
        e1 = self.al.log(AuditEventType.ACCESS, "read", actor_id="u")
        e2 = self.al.log(AuditEventType.ACCESS, "write", actor_id="u")
        self.assertEqual(e2.sequence, e1.sequence + 1)

    def test_hash_chain(self):
        from agent.audit_logger_v2 import AuditEventType
        e1 = self.al.log(AuditEventType.SYSTEM, "start")
        e2 = self.al.log(AuditEventType.SYSTEM, "stop")
        self.assertEqual(e2.prev_hash, e1.entry_hash)

    def test_log_auth(self):
        from agent.audit_logger_v2 import AuditEventType
        e = self.al.log_auth("user1", "login")
        self.assertEqual(e.event_type, AuditEventType.AUTH)

    def test_log_access(self):
        from agent.audit_logger_v2 import AuditEventType
        e = self.al.log_access("user2", "doc:123")
        self.assertEqual(e.event_type, AuditEventType.ACCESS)
        self.assertEqual(e.resource, "doc:123")

    def test_log_data_write(self):
        from agent.audit_logger_v2 import AuditEventType
        e = self.al.log_data("user3", "table:users", "write")
        self.assertEqual(e.event_type, AuditEventType.DATA_WRITE)

    def test_log_security(self):
        from agent.audit_logger_v2 import AuditEventType
        e = self.al.log_security("user4", "brute_force_detected")
        self.assertEqual(e.event_type, AuditEventType.SECURITY)

    def test_query_by_actor(self):
        from agent.audit_logger_v2 import AuditEventType
        self.al.log(AuditEventType.ACCESS, "read", actor_id="actor_x")
        entries = self.al.get_by_actor("actor_x")
        self.assertGreater(len(entries), 0)

    def test_query_by_event_type(self):
        from agent.audit_logger_v2 import AuditEventType, AuditQuery
        self.al.log(AuditEventType.AUTH, "login", actor_id="u")
        self.al.log(AuditEventType.SYSTEM, "start")
        entries = self.al.query(AuditQuery(event_type=AuditEventType.AUTH))
        self.assertTrue(all(e.event_type == AuditEventType.AUTH for e in entries))

    def test_query_failures(self):
        from agent.audit_logger_v2 import AuditEventType, AuditOutcome
        self.al.log(AuditEventType.AUTH, "login",
                    outcome=AuditOutcome.FAILURE)
        failures = self.al.get_failures()
        self.assertGreater(len(failures), 0)

    def test_verify_chain_valid(self):
        from agent.audit_logger_v2 import AuditEventType
        for _ in range(5):
            self.al.log(AuditEventType.SYSTEM, "event")
        ok, bad = self.al.verify_chain()
        self.assertTrue(ok)
        self.assertIsNone(bad)

    def test_get_entry(self):
        from agent.audit_logger_v2 import AuditEventType
        e = self.al.log(AuditEventType.CONFIG, "change")
        got = self.al.get_entry(e.entry_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.action, "change")

    def test_details_persisted(self):
        from agent.audit_logger_v2 import AuditEventType
        e = self.al.log(AuditEventType.DATA_WRITE, "insert",
                        details={"table": "users", "rows": 10})
        got = self.al.get_entry(e.entry_id)
        self.assertEqual(got.details.get("table"), "users")

    def test_hook_fired(self):
        from agent.audit_logger_v2 import AuditEventType
        fired = []
        self.al.on_event(AuditEventType.SECURITY,
                         lambda e: fired.append(e.action))
        self.al.log_security("u", "intrusion_detected")
        self.assertIn("intrusion_detected", fired)

    def test_purge_old(self):
        from agent.audit_logger_v2 import AuditEventType
        self.al.log(AuditEventType.SYSTEM, "old", ts=1000.0)
        removed = self.al.purge_old(before_ts=2000.0)
        self.assertGreater(removed, 0)

    def test_export_json(self):
        from agent.audit_logger_v2 import AuditEventType
        self.al.log(AuditEventType.ACCESS, "read")
        exported = self.al.export_json()
        data = __import__("json").loads(exported)
        self.assertGreater(len(data), 0)

    def test_export_csv(self):
        from agent.audit_logger_v2 import AuditEventType
        self.al.log(AuditEventType.ACCESS, "read", actor_id="u")
        csv = self.al.export_csv()
        self.assertIn("entry_id", csv)

    def test_stats(self):
        from agent.audit_logger_v2 import AuditEventType
        self.al.log(AuditEventType.ACCESS, "read")
        s = self.al.stats()
        self.assertGreater(s["total_entries"], 0)
        self.assertGreater(s["current_sequence"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v60: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
