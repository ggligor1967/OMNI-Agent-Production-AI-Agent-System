"""OMNI AGENT v40: GraphEngine, DocumentStore, CryptoUtils, StateStore"""
import json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# GRAPH ENGINE
# ════════════════════════════════════════════════════════
class TestGraphEngine(unittest.TestCase):
    def setUp(self):
        from agent.graph_engine import GraphEngine, GraphMode, Node, Edge
        td = tempfile.mkdtemp()
        self.GE = GraphEngine; self.GM = GraphMode
        self.g = GraphEngine(db_path=os.path.join(td,"g.db"))

    def _make_simple(self):
        for n in "ABCDE": self.g.add_node(n)
        self.g.add_edge("A","B",1); self.g.add_edge("B","C",2)
        self.g.add_edge("A","D",10); self.g.add_edge("D","C",1)
        self.g.add_edge("C","E",3)

    def test_add_node(self):
        n = self.g.add_node("X", label="Node X")
        self.assertTrue(self.g.has_node("X"))
        self.assertEqual(n.label, "Node X")

    def test_add_edge(self):
        self.g.add_node("A"); self.g.add_node("B")
        self.g.add_edge("A","B",5.0)
        self.assertTrue(self.g.has_edge("A","B"))
        self.assertEqual(self.g.get_edge("A","B").weight, 5.0)

    def test_auto_create_nodes_on_edge(self):
        self.g.add_edge("X","Y")
        self.assertTrue(self.g.has_node("X"))
        self.assertTrue(self.g.has_node("Y"))

    def test_remove_node(self):
        self.g.add_node("R"); self.g.add_edge("R","X")
        ok = self.g.remove_node("R")
        self.assertTrue(ok)
        self.assertFalse(self.g.has_node("R"))

    def test_remove_edge(self):
        self.g.add_edge("A","B")
        ok = self.g.remove_edge("A","B")
        self.assertTrue(ok)
        self.assertFalse(self.g.has_edge("A","B"))

    def test_out_neighbors(self):
        self.g.add_edge("A","B"); self.g.add_edge("A","C")
        self.assertIn("B", self.g.out_neighbors("A"))
        self.assertIn("C", self.g.out_neighbors("A"))

    def test_in_neighbors(self):
        self.g.add_edge("X","Z"); self.g.add_edge("Y","Z")
        inn = self.g.in_neighbors("Z")
        self.assertIn("X", inn); self.assertIn("Y", inn)

    def test_bfs_order(self):
        self._make_simple()
        bfs = self.g.bfs("A")
        self.assertEqual(bfs[0], "A")
        self.assertIn("B", bfs); self.assertIn("C", bfs)

    def test_dfs_visits_all(self):
        self._make_simple()
        dfs = self.g.dfs("A")
        for n in "ABCDE": self.assertIn(n, dfs)

    def test_shortest_path_dijkstra(self):
        self._make_simple()
        path, cost = self.g.shortest_path("A","E")
        self.assertEqual(path[0], "A")
        self.assertEqual(path[-1], "E")
        self.assertAlmostEqual(cost, 6.0)  # A→B(1)→C(2)→E(3)

    def test_shortest_path_no_route(self):
        self.g.add_node("Iso")
        path, cost = self.g.shortest_path("A","Iso")
        self.assertEqual(path, [])
        self.assertEqual(cost, float("inf"))

    def test_bfs_path_unweighted(self):
        self.g.add_edge("S","M"); self.g.add_edge("M","T")
        self.g.add_edge("S","T",100)  # direct but longer
        path = self.g.bfs_path("S","T")
        self.assertEqual(path[0],"S"); self.assertEqual(path[-1],"T")

    def test_all_paths(self):
        self.g.add_edge("P","Q"); self.g.add_edge("P","R")
        self.g.add_edge("Q","S"); self.g.add_edge("R","S")
        paths = self.g.all_paths("P","S")
        self.assertGreaterEqual(len(paths), 2)

    def test_cycle_detection_with_cycle(self):
        self.g.add_edge("A","B"); self.g.add_edge("B","C")
        self.g.add_edge("C","A")
        has_cycle, cycle = self.g.has_cycle()
        self.assertTrue(has_cycle)

    def test_cycle_detection_no_cycle(self):
        self.g.add_edge("X","Y"); self.g.add_edge("Y","Z")
        has_cycle, _ = self.g.has_cycle()
        self.assertFalse(has_cycle)

    def test_topological_sort(self):
        g2 = self.GE(db_path=tempfile.mktemp(suffix=".db"))
        g2.add_edge("A","C"); g2.add_edge("B","C")
        g2.add_edge("C","D")
        topo = g2.topological_sort()
        self.assertIsNotNone(topo)
        self.assertLess(topo.index("A"), topo.index("C"))
        self.assertLess(topo.index("C"), topo.index("D"))

    def test_topological_sort_returns_none_on_cycle(self):
        self.g.add_edge("A","B"); self.g.add_edge("B","A")
        self.assertIsNone(self.g.topological_sort())

    def test_scc(self):
        self.g.add_edge("A","B"); self.g.add_edge("B","C")
        self.g.add_edge("C","A")  # SCC: A,B,C
        self.g.add_node("D")
        sccs = self.g.strongly_connected_components()
        sizes = sorted([len(s) for s in sccs], reverse=True)
        self.assertEqual(sizes[0], 3)

    def test_mst(self):
        from agent.graph_engine import GraphMode
        g2 = self.GE(mode=GraphMode.UNDIRECTED,
                      db_path=tempfile.mktemp(suffix=".db"))
        g2.add_edge("A","B",1); g2.add_edge("A","C",3)
        g2.add_edge("B","C",2); g2.add_edge("B","D",5)
        g2.add_edge("C","D",4)
        mst = g2.minimum_spanning_tree()
        mst_cost = sum(e.weight for e in mst)
        self.assertEqual(mst_cost, 7.0)  # 1+2+4

    def test_pagerank_sum(self):
        self._make_simple()
        pr = self.g.pagerank()
        self.assertAlmostEqual(sum(pr.values()), 1.0, places=3)

    def test_degree(self):
        self.g.add_edge("A","B"); self.g.add_edge("C","A")
        self.assertGreaterEqual(self.g.degree("A"), 1)

    def test_to_dot(self):
        self.g.add_edge("A","B")
        dot = self.g.to_dot()
        self.assertIn("digraph", dot)
        self.assertIn("->", dot)

    def test_to_dict(self):
        self.g.add_node("A"); self.g.add_edge("A","B")
        d = self.g.to_dict()
        self.assertIn("nodes", d); self.assertIn("edges", d)

    def test_stats(self):
        self.g.add_edge("A","B")
        s = self.g.stats()
        self.assertGreaterEqual(s["nodes"], 2)
        self.assertGreaterEqual(s["edges"], 1)

# ════════════════════════════════════════════════════════
# DOCUMENT STORE
# ════════════════════════════════════════════════════════
class TestDocumentStore(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.document_store import DocumentStore
        self.ds = DocumentStore(db_path=os.path.join(td,"ds.db"))

    def _seed(self):
        self.ds.insert("users",{"name":"Alice","age":30,"plan":"pro"})
        self.ds.insert("users",{"name":"Bob","age":25,"plan":"free"})
        self.ds.insert("users",{"name":"Charlie","age":35,"plan":"pro"})

    def test_insert_returns_id(self):
        doc = self.ds.insert("col",{"x":1})
        self.assertIn("_id",doc)

    def test_find_all(self):
        self._seed()
        docs = self.ds.find("users")
        self.assertEqual(len(docs), 3)

    def test_find_by_field(self):
        self._seed()
        docs = self.ds.find("users",{"plan":"pro"})
        self.assertEqual(len(docs), 2)

    def test_find_gt(self):
        self._seed()
        docs = self.ds.find("users",{"age":{"$gt":28}})
        self.assertEqual(len(docs), 2)

    def test_find_lte(self):
        self._seed()
        docs = self.ds.find("users",{"age":{"$lte":25}})
        self.assertEqual(len(docs), 1)

    def test_find_in(self):
        self._seed()
        docs = self.ds.find("users",{"plan":{"$in":["pro","enterprise"]}})
        self.assertEqual(len(docs), 2)

    def test_find_regex(self):
        self._seed()
        docs = self.ds.find("users",{"name":{"$regex":"^A"}})
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["name"],"Alice")

    def test_find_and(self):
        self._seed()
        docs = self.ds.find("users",{"$and":[{"plan":"pro"},{"age":{"$gt":31}}]})
        self.assertEqual(len(docs), 1)

    def test_find_or(self):
        self._seed()
        docs = self.ds.find("users",{"$or":[{"age":{"$gt":32}},{"plan":"free"}]})
        self.assertEqual(len(docs), 2)

    def test_find_sort(self):
        self._seed()
        docs = self.ds.find("users",sort=[("age",1)])
        self.assertEqual(docs[0]["name"],"Bob")

    def test_find_limit_skip(self):
        self._seed()
        docs = self.ds.find("users",sort=[("age",1)],limit=2,skip=1)
        self.assertEqual(len(docs), 2)

    def test_find_one(self):
        self._seed()
        doc = self.ds.find_one("users",{"name":"Alice"})
        self.assertIsNotNone(doc)
        self.assertEqual(doc["name"],"Alice")

    def test_count(self):
        self._seed()
        self.assertEqual(self.ds.count("users",{"plan":"pro"}), 2)

    def test_update_set(self):
        self._seed()
        n = self.ds.update("users",{"name":"Alice"},{"$set":{"plan":"enterprise"}})
        self.assertEqual(n, 1)
        doc = self.ds.find_one("users",{"name":"Alice"})
        self.assertEqual(doc["plan"],"enterprise")

    def test_update_inc(self):
        self._seed()
        self.ds.update("users",{"name":"Bob"},{"$inc":{"age":1}})
        doc = self.ds.find_one("users",{"name":"Bob"})
        self.assertEqual(doc["age"], 26)

    def test_update_push(self):
        self.ds.insert("items",{"tags":[]})
        self.ds.update("items",{},{"$push":{"tags":"new_tag"}})
        doc = self.ds.find_one("items",{})
        self.assertIn("new_tag", doc["tags"])

    def test_upsert_inserts(self):
        n = self.ds.update("users",{"name":"Dave"},
                            {"$set":{"plan":"free"}}, upsert=True)
        self.assertEqual(n, 1)
        doc = self.ds.find_one("users",{"name":"Dave"})
        self.assertIsNotNone(doc)

    def test_delete(self):
        self._seed()
        n = self.ds.delete("users",{"plan":"free"})
        self.assertEqual(n, 1)
        self.assertEqual(self.ds.count("users",{"plan":"free"}), 0)

    def test_delete_one(self):
        self._seed()
        ok = self.ds.delete_one("users",{"plan":"pro"})
        self.assertTrue(ok)
        self.assertEqual(self.ds.count("users",{"plan":"pro"}), 1)

    def test_unique_index_raises(self):
        self.ds.create_index("uniq","email",unique=True)
        self.ds.insert("uniq",{"email":"a@b.com"})
        with self.assertRaises(ValueError):
            self.ds.insert("uniq",{"email":"a@b.com"})

    def test_aggregate_group_count(self):
        self._seed()
        result = self.ds.aggregate("users",[
            {"$group":{"_id":"$plan","count":{"$sum":1}}}])
        totals = {r["_id"]: r["count"] for r in result}
        self.assertEqual(totals.get("pro"), 2)
        self.assertEqual(totals.get("free"), 1)

    def test_aggregate_match(self):
        self._seed()
        result = self.ds.aggregate("users",[
            {"$match":{"plan":"pro"}},
            {"$count":"total"}])
        self.assertEqual(result[0]["total"], 2)

    def test_aggregate_sort_limit(self):
        self._seed()
        result = self.ds.aggregate("users",[
            {"$sort":{"age":-1}},{"$limit":1}])
        self.assertEqual(result[0]["name"],"Charlie")

    def test_change_hook(self):
        ops = []
        self.ds.on_change(lambda c,op,d: ops.append(op))
        self.ds.insert("ch",{"x":1})
        self.assertIn("insert",ops)

    def test_drop_collection(self):
        self._seed()
        n = self.ds.drop_collection("users")
        self.assertEqual(n, 3)
        self.assertEqual(self.ds.count("users",{}), 0)

    def test_stats(self):
        self._seed()
        s = self.ds.stats()
        for k in ["total","by_collection"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# CRYPTO UTILS
# ════════════════════════════════════════════════════════
class TestCryptoUtils(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.crypto_utils import CryptoUtils
        self.cu = CryptoUtils(db_path=os.path.join(td,"cu.db"))

    def test_sha256(self):
        h = self.cu.sha256("hello")
        self.assertEqual(len(h), 64)

    def test_sha256_deterministic(self):
        self.assertEqual(self.cu.sha256("x"), self.cu.sha256("x"))

    def test_sha256_different_inputs(self):
        self.assertNotEqual(self.cu.sha256("a"), self.cu.sha256("b"))

    def test_sha512_length(self):
        self.assertEqual(len(self.cu.sha512("data")), 128)

    def test_md5(self):
        self.assertEqual(len(self.cu.md5("test")), 32)

    def test_blake2b(self):
        h = self.cu.blake2b("data")
        self.assertEqual(len(h), 64)

    def test_fingerprint_length(self):
        fp = self.cu.fingerprint("content", length=8)
        self.assertEqual(len(fp), 8)

    def test_hmac_sign_verify(self):
        sig = self.cu.hmac_sign("key","message")
        self.assertTrue(self.cu.hmac_verify("key","message",sig))

    def test_hmac_wrong_key(self):
        sig = self.cu.hmac_sign("key","message")
        self.assertFalse(self.cu.hmac_verify("wrong","message",sig))

    def test_encrypt_decrypt(self):
        enc = self.cu.encrypt(b"secret data","my-key")
        dec = self.cu.decrypt(enc,"my-key")
        self.assertEqual(dec, b"secret data")

    def test_encrypt_string(self):
        enc = self.cu.encrypt("hello world","key")
        dec = self.cu.decrypt(enc,"key")
        self.assertEqual(dec.decode(), "hello world")

    def test_decrypt_wrong_key_raises(self):
        enc = self.cu.encrypt(b"data","correct-key")
        with self.assertRaises(ValueError):
            self.cu.decrypt(enc,"wrong-key")

    def test_nonce_random(self):
        e1 = self.cu.encrypt(b"x","k")
        e2 = self.cu.encrypt(b"x","k")
        self.assertNotEqual(e1["nonce"], e2["nonce"])

    def test_derive_key(self):
        result = self.cu.derive_key("password")
        self.assertIn("key",result); self.assertIn("salt",result)

    def test_derive_key_deterministic_with_salt(self):
        from agent.crypto_utils import b64url_decode, b64url_encode
        r1 = self.cu.derive_key("pw")
        salt = b64url_decode(r1["salt"])
        from agent.crypto_utils import pbkdf2
        k2, _ = pbkdf2("pw", salt)
        self.assertEqual(b64url_decode(r1["key"]), k2)

    def test_hash_password(self):
        stored = self.cu.hash_password("hunter2")
        self.assertIn(":", stored)

    def test_verify_password_correct(self):
        stored = self.cu.hash_password("mypass")
        self.assertTrue(self.cu.verify_password("mypass", stored))

    def test_verify_password_wrong(self):
        stored = self.cu.hash_password("mypass")
        self.assertFalse(self.cu.verify_password("wrongpass", stored))

    def test_jwt_encode_decode(self):
        token = self.cu.jwt_encode({"user":1},"secret")
        payload = self.cu.jwt_decode(token,"secret")
        self.assertEqual(payload["user"], 1)

    def test_jwt_has_exp(self):
        token = self.cu.jwt_encode({"x":1},"s",exp_s=100)
        payload = self.cu.jwt_decode(token,"s")
        self.assertIn("exp",payload)

    def test_jwt_expired_raises(self):
        token = self.cu.jwt_encode({"x":1},"s",exp_s=-1)
        with self.assertRaises(ValueError):
            self.cu.jwt_decode(token,"s")

    def test_jwt_wrong_secret_raises(self):
        token = self.cu.jwt_encode({"x":1},"correct")
        with self.assertRaises(ValueError):
            self.cu.jwt_decode(token,"wrong")

    def test_jwt_verify(self):
        token = self.cu.jwt_encode({"x":1},"secret")
        self.assertTrue(self.cu.jwt_verify(token,"secret"))
        self.assertFalse(self.cu.jwt_verify(token,"bad"))

    def test_random_token_hex(self):
        t = self.cu.random_token(16,"hex")
        self.assertEqual(len(t), 32)

    def test_random_token_unique(self):
        self.assertNotEqual(self.cu.random_token(), self.cu.random_token())

    def test_wrap_unwrap_key(self):
        key = b"my-secret-key-32bytes-padded-abc"
        wrapped = self.cu.wrap_key(key,"wrapping-key")
        unwrapped = self.cu.unwrap_key(wrapped,"wrapping-key")
        self.assertEqual(unwrapped, key)

    def test_constant_compare(self):
        self.assertTrue(self.cu.constant_compare("abc","abc"))
        self.assertFalse(self.cu.constant_compare("abc","xyz"))

    def test_hkdf(self):
        out = self.cu.hkdf("input-key-material")
        self.assertEqual(len(out), 43)  # 32 bytes → base64url ~43 chars

    def test_stats(self):
        self.cu.encrypt(b"x","k")
        s = self.cu.stats()
        for k in ["stored_keys","operations"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# STATE STORE
# ════════════════════════════════════════════════════════
class TestStateStore(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.state_store import StateStore
        self.ss = StateStore(db_path=os.path.join(td,"ss.db"))

    def test_set_get(self):
        self.ss.set("k","v")
        self.assertEqual(self.ss.get("k"), "v")

    def test_get_missing(self):
        self.assertIsNone(self.ss.get("no_such_key"))

    def test_get_default(self):
        self.assertEqual(self.ss.get("missing","default"), "default")

    def test_delete(self):
        self.ss.set("del_k","v")
        ok = self.ss.delete("del_k")
        self.assertTrue(ok)
        self.assertIsNone(self.ss.get("del_k"))

    def test_ttl_expiry(self):
        self.ss.set("exp_k","v",ttl_s=0.01)
        time.sleep(0.02)
        self.assertIsNone(self.ss.get("exp_k"))

    def test_cas_success(self):
        self.ss.set("cas_k", 0)
        ok = self.ss.cas("cas_k", 0, 1)
        self.assertTrue(ok)
        self.assertEqual(self.ss.get("cas_k"), 1)

    def test_cas_fail_wrong_expected(self):
        self.ss.set("cas_f", 5)
        ok = self.ss.cas("cas_f", 0, 99)
        self.assertFalse(ok)
        self.assertEqual(self.ss.get("cas_f"), 5)

    def test_watch_fires(self):
        changes = []
        self.ss.watch("watched_k",
                       lambda k,o,n: changes.append((o,n)))
        self.ss.set("watched_k","new_val")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0][1],"new_val")

    def test_prefix_watch(self):
        seen = []
        self.ss.watch_prefix("config/",
                              lambda k,o,n: seen.append(k))
        self.ss.set("config/host","localhost")
        self.ss.set("config/port",8080)
        self.assertEqual(len(seen), 2)

    def test_transaction_commits(self):
        with self.ss.transaction() as tx:
            tx.set("tx_a",1)
            tx.set("tx_b",2)
        self.assertEqual(self.ss.get("tx_a"), 1)
        self.assertEqual(self.ss.get("tx_b"), 2)

    def test_transaction_rollback_on_exception(self):
        try:
            with self.ss.transaction() as tx:
                tx.set("rb_a",1)
                raise RuntimeError("abort")
        except RuntimeError: pass
        self.assertIsNone(self.ss.get("rb_a"))

    def test_transaction_delete(self):
        self.ss.set("to_del","exists")
        with self.ss.transaction() as tx:
            tx.delete("to_del")
        self.assertIsNone(self.ss.get("to_del"))

    def test_get_many(self):
        self.ss.set("m1","a"); self.ss.set("m2","b")
        result = self.ss.get_many(["m1","m2","m3"])
        self.assertEqual(result["m1"],"a")
        self.assertIsNone(result["m3"])

    def test_set_many(self):
        self.ss.set_many({"sm1":1,"sm2":2,"sm3":3})
        self.assertEqual(self.ss.get("sm2"), 2)

    def test_delete_many(self):
        self.ss.set("dm1","a"); self.ss.set("dm2","b")
        n = self.ss.delete_many(["dm1","dm2","dm3"])
        self.assertEqual(n, 2)

    def test_keys_prefix(self):
        self.ss.set("app/a",1); self.ss.set("app/b",2)
        self.ss.set("other/c",3)
        keys = self.ss.keys("app/")
        self.assertIn("app/a",keys); self.assertIn("app/b",keys)
        self.assertNotIn("other/c",keys)

    def test_lease_acquire(self):
        lid = self.ss.try_lock("resource","worker1",ttl_s=10)
        self.assertIsNotNone(lid)

    def test_lease_exclusive(self):
        self.ss.try_lock("res2","owner1",ttl_s=30)
        lid2 = self.ss.try_lock("res2","owner2",ttl_s=30)
        self.assertIsNone(lid2)

    def test_lease_release(self):
        lid = self.ss.try_lock("res3","owner",ttl_s=10)
        ok = self.ss.release_lock(lid)
        self.assertTrue(ok)
        lid2 = self.ss.try_lock("res3","new_owner",ttl_s=10)
        self.assertIsNotNone(lid2)

    def test_snapshot_and_restore(self):
        self.ss.set("snap_k","snap_v")
        sid = self.ss.save_snapshot()
        self.ss.delete("snap_k")
        self.assertIsNone(self.ss.get("snap_k"))
        n = self.ss.restore_snapshot(sid)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(self.ss.get("snap_k"),"snap_v")

    def test_history(self):
        self.ss.set("hist_k","v1")
        self.ss.set("hist_k","v2")
        self.ss.set("hist_k","v3")
        h = self.ss.history("hist_k")
        self.assertGreaterEqual(len(h), 2)

    def test_revision_increments(self):
        e1 = self.ss.set("rev_k","a")
        e2 = self.ss.set("rev_k","b")
        self.assertGreater(e2.revision, e1.revision)

    def test_sweep_expired(self):
        self.ss.set("sw_k","v",ttl_s=0.01)
        time.sleep(0.02)
        n = self.ss.sweep()
        self.assertGreaterEqual(n, 1)

    def test_stats(self):
        self.ss.set("x",1)
        s = self.ss.stats()
        for k in ["in_memory","revision","watches"]: self.assertIn(k,s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v40: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
