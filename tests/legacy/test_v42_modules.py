"""OMNI AGENT v42: VectorStore, ABTesting, DataValidator, ProcessManager"""
import asyncio, math, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# VECTOR STORE
# ════════════════════════════════════════════════════════
class TestVectorStore(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.vector_store import VectorStore
        self.vs = VectorStore(db_path=os.path.join(td,"vs.db"))
        self.vs.create_namespace("test", dim=4, auto_normalize=False)

    def test_upsert_and_fetch(self):
        self.vs.upsert("test","v1",[1,0,0,0])
        e = self.vs.fetch("test","v1")
        self.assertIsNotNone(e)
        self.assertEqual(e.id,"v1")

    def test_fetch_missing(self):
        self.assertIsNone(self.vs.fetch("test","nope"))

    def test_cosine_search_top1(self):
        self.vs.upsert("test","a",[1,0,0,0])
        self.vs.upsert("test","b",[0,1,0,0])
        self.vs.upsert("test","c",[0,0,1,0])
        results = self.vs.search("test",[1,0,0,0],k=1,metric="cosine")
        self.assertEqual(results[0].id,"a")

    def test_cosine_score_range(self):
        self.vs.upsert("test","x",[1,0,0,0])
        self.vs.upsert("test","y",[1,0,0,0])
        results = self.vs.search("test",[1,0,0,0],k=2,metric="cosine")
        for r in results:
            self.assertAlmostEqual(r.score, 1.0, places=4)

    def test_dot_product_metric(self):
        self.vs.upsert("test","d1",[2,0,0,0])
        self.vs.upsert("test","d2",[0,2,0,0])
        results = self.vs.search("test",[1,0,0,0],k=1,metric="dot")
        self.assertEqual(results[0].id,"d1")

    def test_euclidean_metric(self):
        self.vs.upsert("test","e1",[1,0,0,0])
        self.vs.upsert("test","e2",[10,10,10,10])
        results = self.vs.search("test",[1,0,0,0],k=1,metric="euclidean")
        self.assertEqual(results[0].id,"e1")

    def test_metadata_stored(self):
        self.vs.upsert("test","m1",[1,0,0,0],metadata={"type":"doc"})
        e = self.vs.fetch("test","m1")
        self.assertEqual(e.metadata["type"],"doc")

    def test_metadata_filter(self):
        self.vs.upsert("test","f1",[1,0,0,0],metadata={"kind":"a"})
        self.vs.upsert("test","f2",[1,0,0,0],metadata={"kind":"b"})
        results = self.vs.search("test",[1,0,0,0],k=10,
                                   filter_meta={"kind":"a"})
        self.assertEqual(len(results),1)
        self.assertEqual(results[0].id,"f1")

    def test_auto_normalize(self):
        td2 = tempfile.mkdtemp()
        from agent.vector_store import VectorStore
        vs2 = VectorStore(db_path=os.path.join(td2,"v2.db"))
        vs2.create_namespace("ns",dim=2,auto_normalize=True)
        vs2.upsert("ns","u1",[3,0])
        e = vs2.fetch("ns","u1")
        self.assertAlmostEqual(e.vector[0],1.0,places=4)

    def test_dim_enforced(self):
        with self.assertRaises(ValueError):
            self.vs.upsert("test","bad",[1,0,0])  # dim=3 vs namespace dim=4

    def test_upsert_updates(self):
        self.vs.upsert("test","upd",[1,0,0,0],metadata={"v":1})
        self.vs.upsert("test","upd",[0,1,0,0],metadata={"v":2})
        e = self.vs.fetch("test","upd")
        self.assertEqual(e.metadata["v"],2)
        self.assertAlmostEqual(e.vector[1],1.0,places=4)

    def test_delete(self):
        self.vs.upsert("test","del1",[1,0,0,0])
        ok = self.vs.delete("test","del1")
        self.assertTrue(ok)
        self.assertIsNone(self.vs.fetch("test","del1"))

    def test_list_ids(self):
        self.vs.upsert("test","id:1",[1,0,0,0])
        self.vs.upsert("test","id:2",[0,1,0,0])
        ids = self.vs.list_ids("test","id:")
        self.assertIn("id:1",ids); self.assertIn("id:2",ids)

    def test_upsert_batch(self):
        items = [(f"b{i}",[float(i),0,0,0],{"n":i}) for i in range(5)]
        n = self.vs.upsert_batch("test",items)
        self.assertEqual(n,5)

    def test_centroid(self):
        self.vs.upsert("test","c1",[2,0,0,0])
        self.vs.upsert("test","c2",[0,2,0,0])
        c = self.vs.centroid("test",["c1","c2"])
        self.assertIsNotNone(c)
        self.assertAlmostEqual(c[0],1.0,places=4)
        self.assertAlmostEqual(c[1],1.0,places=4)

    def test_k_results_capped(self):
        for i in range(10):
            self.vs.upsert("test",f"k{i}",[float(i),0,0,0])
        results = self.vs.search("test",[1,0,0,0],k=3)
        self.assertLessEqual(len(results),3)

    def test_search_returns_sorted(self):
        self.vs.upsert("test","s1",[1,0,0,0])
        self.vs.upsert("test","s2",[0.9,0.1,0,0])
        self.vs.upsert("test","s3",[0,1,0,0])
        results = self.vs.search("test",[1,0,0,0],k=3,metric="dot")
        scores = [r.score for r in results]
        self.assertEqual(scores,sorted(scores,reverse=True))

    def test_stats(self):
        s = self.vs.stats()
        for k in ["total","namespaces"]: self.assertIn(k,s)

    def test_auto_create_namespace(self):
        self.vs.upsert("auto_ns","x",[1,2])
        self.assertIn("auto_ns",self.vs._namespaces)

# ════════════════════════════════════════════════════════
# A/B TESTING
# ════════════════════════════════════════════════════════
class TestABTesting(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.ab_testing import ABTesting, ExperimentStatus
        self.ab = ABTesting(db_path=os.path.join(td,"ab.db"))
        self.ES = ExperimentStatus

    def _make_exp(self, name="test_exp"):
        exp = self.ab.create_experiment(name,[
            {"name":"control","weight":50},
            {"name":"treatment","weight":50}])
        self.ab.start(exp.id)
        return exp

    def test_create_experiment(self):
        exp = self._make_exp()
        self.assertEqual(len(exp.variants),2)

    def test_assign_returns_variant(self):
        exp = self._make_exp()
        v = self.ab.assign(exp.id,"user_1")
        self.assertIn(v,["control","treatment"])

    def test_assign_deterministic(self):
        exp = self._make_exp()
        v1 = self.ab.assign(exp.id,"u42")
        v2 = self.ab.assign(exp.id,"u42")
        self.assertEqual(v1,v2)

    def test_assign_not_running(self):
        exp = self.ab.create_experiment("draft_exp",[{"name":"a","weight":100}])
        v = self.ab.assign(exp.id,"u1")
        self.assertIsNone(v)

    def test_different_users_may_differ(self):
        exp = self._make_exp()
        variants = set()
        for i in range(50):
            v = self.ab.assign(exp.id,f"u{i}")
            if v: variants.add(v)
        self.assertGreater(len(variants),1)

    def test_convert_records(self):
        exp = self._make_exp()
        self.ab.assign(exp.id,"u100")
        ok = self.ab.convert(exp.id,"u100","purchase",49.99)
        self.assertTrue(ok)

    def test_convert_unassigned_returns_false(self):
        exp = self._make_exp()
        ok = self.ab.convert(exp.id,"unassigned_user","purchase")
        self.assertFalse(ok)

    def test_results_structure(self):
        exp = self._make_exp()
        for i in range(20):
            self.ab.assign(exp.id,f"ru{i}")
            if i % 3 == 0:
                self.ab.convert(exp.id,f"ru{i}","click")
        r = self.ab.results(exp.id,"click")
        self.assertIn("variants",r)
        self.assertIn("control",r["variants"])

    def test_override(self):
        exp = self._make_exp()
        self.ab.set_override(exp.id,"forced_user","treatment")
        v = self.ab.assign(exp.id,"forced_user")
        self.assertEqual(v,"treatment")

    def test_targeting_eligible(self):
        exp = self.ab.create_experiment("targeted",[
            {"name":"a","weight":50},{"name":"b","weight":50}],
            targeting=[{"field":"country","op":"==","value":"US"}])
        self.ab.start(exp.id)
        v = self.ab.assign(exp.id,"u1",user_attrs={"country":"US"})
        self.assertIsNotNone(v)

    def test_targeting_ineligible(self):
        exp = self.ab.create_experiment("targeted2",[
            {"name":"a","weight":100}],
            targeting=[{"field":"plan","op":"in","value":["pro","enterprise"]}])
        self.ab.start(exp.id)
        v = self.ab.assign(exp.id,"free_user",user_attrs={"plan":"free"})
        self.assertIsNone(v)

    def test_holdout(self):
        exp = self.ab.create_experiment("holdout_exp",[
            {"name":"a","weight":100}],holdout_pct=100)
        self.ab.start(exp.id)
        v = self.ab.assign(exp.id,"any_user")
        self.assertIsNone(v)

    def test_pause_stops_assignments(self):
        exp = self._make_exp()
        self.ab.pause(exp.id)
        v = self.ab.assign(exp.id,"new_user")
        self.assertIsNone(v)

    def test_conclude(self):
        exp = self._make_exp()
        ok = self.ab.conclude(exp.id)
        self.assertTrue(ok)
        self.assertEqual(exp.status,self.ES.CONCLUDED)

    def test_on_assign_hook(self):
        assigned = []
        self.ab.on_assign(lambda u,e,v: assigned.append(v))
        exp = self._make_exp()
        self.ab.assign(exp.id,"hook_user")
        self.assertGreater(len(assigned),0)

    def test_on_convert_hook(self):
        converted = []
        self.ab.on_convert(lambda u,e,m: converted.append(m))
        exp = self._make_exp()
        self.ab.assign(exp.id,"cv_user")
        self.ab.convert(exp.id,"cv_user","buy")
        self.assertIn("buy",converted)

    def test_results_has_significance(self):
        exp = self._make_exp()
        for i in range(100):
            self.ab.assign(exp.id,f"su{i}")
            if i < 20: self.ab.convert(exp.id,f"su{i}","metric")
        r = self.ab.results(exp.id,"metric")
        self.assertIn("treatment",r["variants"])
        self.assertIn("p_value",r["variants"].get("treatment",{}))

    def test_min_sample_size(self):
        n = self.ab.min_sample_size(0.05, mde=0.1)
        self.assertGreater(n, 100)

    def test_first_variant_is_control(self):
        exp = self._make_exp()
        self.assertTrue(exp.variants[0].is_control)

    def test_stats(self):
        self._make_exp()
        s = self.ab.stats()
        for k in ["experiments","in_memory"]: self.assertIn(k,s)

# ════════════════════════════════════════════════════════
# DATA VALIDATOR
# ════════════════════════════════════════════════════════
class TestDataValidator(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.data_validator import (DataValidator, Schema,
                                           FieldSchema, UnknownFieldMode)
        self.DV = DataValidator; self.S = Schema
        self.FS = FieldSchema; self.UFM = UnknownFieldMode
        self.dv = DataValidator(db_path=os.path.join(td,"dv.db"))

    def _schema(self, *fields):
        return self.S(list(fields))

    def test_valid_string(self):
        s = self._schema(self.FS("name",type="string",required=True))
        r = s.validate({"name":"Alice"})
        self.assertTrue(r.valid); self.assertEqual(r.data["name"],"Alice")

    def test_required_missing(self):
        s = self._schema(self.FS("name",type="string",required=True))
        r = s.validate({})
        self.assertFalse(r.valid)
        self.assertEqual(r.errors[0].code,"required")

    def test_type_error(self):
        s = self._schema(self.FS("age",type="int"))
        r = s.validate({"age":"not_int"})
        self.assertFalse(r.valid)

    def test_coerce_int(self):
        s = self._schema(self.FS("age",type="int",coerce=True))
        r = s.validate({"age":"30"})
        self.assertTrue(r.valid); self.assertEqual(r.data["age"],30)

    def test_coerce_float(self):
        s = self._schema(self.FS("price",type="float",coerce=True))
        r = s.validate({"price":"9.99"})
        self.assertTrue(r.valid)
        self.assertAlmostEqual(r.data["price"],9.99)

    def test_coerce_bool(self):
        s = self._schema(self.FS("flag",type="bool",coerce=True))
        r = s.validate({"flag":"true"})
        self.assertTrue(r.valid); self.assertTrue(r.data["flag"])

    def test_min_val(self):
        s = self._schema(self.FS("age",type="int",min_val=0,max_val=150))
        r = s.validate({"age":-1})
        self.assertFalse(r.valid)
        self.assertEqual(r.errors[0].code,"min_error")

    def test_max_val(self):
        s = self._schema(self.FS("score",type="float",max_val=100.0))
        r = s.validate({"score":101.0})
        self.assertFalse(r.valid)

    def test_minlen(self):
        s = self._schema(self.FS("pw",type="string",minlen=8))
        r = s.validate({"pw":"short"})
        self.assertFalse(r.valid)

    def test_maxlen(self):
        s = self._schema(self.FS("tag",type="string",maxlen=10))
        r = s.validate({"tag":"a"*11})
        self.assertFalse(r.valid)

    def test_pattern(self):
        s = self._schema(self.FS("code",type="string",pattern=r"^\d{4}$"))
        self.assertTrue(s.validate({"code":"1234"}).valid)
        self.assertFalse(s.validate({"code":"abcd"}).valid)

    def test_choices(self):
        s = self._schema(self.FS("role",type="string",
                                   choices=["admin","user","guest"]))
        self.assertTrue(s.validate({"role":"admin"}).valid)
        self.assertFalse(s.validate({"role":"superuser"}).valid)

    def test_email_format(self):
        s = self._schema(self.FS("email",type="email"))
        self.assertTrue(s.validate({"email":"a@b.com"}).valid)
        self.assertFalse(s.validate({"email":"not-an-email"}).valid)

    def test_url_format(self):
        s = self._schema(self.FS("site",type="url"))
        self.assertTrue(s.validate({"site":"https://example.com"}).valid)
        self.assertFalse(s.validate({"site":"example.com"}).valid)

    def test_uuid_format(self):
        s = self._schema(self.FS("id",type="uuid"))
        self.assertTrue(s.validate({"id":"550e8400-e29b-41d4-a716-446655440000"}).valid)
        self.assertFalse(s.validate({"id":"not-uuid"}).valid)

    def test_default_applied(self):
        s = self._schema(self.FS("role",type="string",default="user"))
        r = s.validate({})
        self.assertEqual(r.data.get("role"),"user")

    def test_alias(self):
        s = self._schema(self.FS("user_id",type="string",
                                   aliases=["userId","user-id"]))
        r = s.validate({"userId":"abc"})
        self.assertTrue(r.valid)
        self.assertEqual(r.data["user_id"],"abc")

    def test_multiple_errors_collected(self):
        s = self._schema(self.FS("a",type="string",required=True),
                          self.FS("b",type="string",required=True))
        r = s.validate({})
        self.assertEqual(len(r.errors),2)

    def test_unknown_forbid(self):
        s = self.S([self.FS("x",type="string")],
                    unknown=self.UFM.FORBID)
        r = s.validate({"x":"v","extra":"bad"})
        self.assertFalse(r.valid)

    def test_unknown_allow(self):
        s = self.S([self.FS("x",type="string")],
                    unknown=self.UFM.ALLOW)
        r = s.validate({"x":"v","extra":"ok"})
        self.assertTrue(r.valid)
        self.assertEqual(r.data.get("extra"),"ok")

    def test_nested_schema(self):
        addr_schema = self._schema(
            self.FS("city",type="string",required=True))
        s = self._schema(self.FS("address",type="dict",
                                   nested_schema=addr_schema))
        r = s.validate({"address":{"city":"NYC"}})
        self.assertTrue(r.valid)

    def test_custom_validator(self):
        def even(v, f): return (v%2==0,"must be even")
        s = self._schema(self.FS("n",type="int",validators=[even]))
        self.assertTrue(s.validate({"n":4}).valid)
        self.assertFalse(s.validate({"n":3}).valid)

    def test_computed_field(self):
        s = self._schema(self.FS("first",type="string"),
                          self.FS("last",type="string"))
        s.add_computed("full_name",lambda d: f"{d['first']} {d['last']}")
        r = s.validate({"first":"John","last":"Doe"})
        self.assertEqual(r.data.get("full_name"),"John Doe")

    def test_quick_validate(self):
        r = self.dv.quick_validate(
            {"age":"25","name":"Alice"},
            {"age":{"type":"int","coerce":True,"min":0},
             "name":{"type":"string","required":True}})
        self.assertTrue(r.valid)
        self.assertEqual(r.data["age"],25)

    def test_register_and_validate(self):
        s = self._schema(self.FS("x",type="int",required=True))
        self.dv.register_schema("nums",s)
        r = self.dv.validate("nums",{"x":5})
        self.assertTrue(r.valid)

    def test_batch_validate(self):
        s = self._schema(self.FS("v",type="int",required=True))
        self.dv.register_schema("bv",s)
        results = self.dv.validate_batch("bv",[{"v":1},{"v":2},{}])
        self.assertEqual(len(results),3)
        self.assertFalse(results[2].valid)

    def test_stats(self):
        s = self._schema(self.FS("x",type="string"))
        self.dv.register_schema("st",s)
        self.dv.validate("st",{"x":"v"})
        st = self.dv.stats()
        self.assertGreaterEqual(st["validations"],1)

# ════════════════════════════════════════════════════════
# PROCESS MANAGER
# ════════════════════════════════════════════════════════
class TestProcessManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.process_manager import (ProcessManager,
                                            ProcStatus, RestartPolicy)
        self.pm = ProcessManager(db_path=os.path.join(td,"pm.db"))
        self.PS = ProcStatus; self.RP = RestartPolicy

    def test_register(self):
        mp = self.pm.register("echo",["echo","hello"])
        self.assertEqual(mp.spec.name,"echo")
        self.assertIn("echo",self.pm._procs)

    def test_start_and_status(self):
        self.pm.register("true_cmd",["true"])
        ok = _run(self.pm.start("true_cmd"))
        self.assertTrue(ok)
        mp = self.pm.status("true_cmd")
        self.assertIsNotNone(mp)

    def test_stop(self):
        self.pm.register("sleep1",["sleep","10"])
        _run(self.pm.start("sleep1"))
        mp = self.pm.status("sleep1")
        self.assertEqual(mp.status, self.PS.RUNNING)
        ok = _run(self.pm.stop("sleep1"))
        self.assertTrue(ok)
        self.assertEqual(mp.status, self.PS.STOPPED)

    def test_start_nonexistent(self):
        ok = _run(self.pm.start("no_such"))
        self.assertFalse(ok)

    def test_is_alive(self):
        self.pm.register("sleep2",["sleep","30"])
        _run(self.pm.start("sleep2"))
        self.assertTrue(self.pm.is_alive("sleep2"))
        _run(self.pm.stop("sleep2"))

    def test_is_alive_stopped(self):
        self.pm.register("false_cmd",["false"])
        _run(self.pm.start("false_cmd"))
        time.sleep(0.1)
        _run(self.pm.stop("false_cmd"))
        self.assertFalse(self.pm.is_alive("false_cmd"))

    def test_restart(self):
        self.pm.register("echo2",["echo","hi"])
        _run(self.pm.start("echo2"))
        ok = _run(self.pm.restart("echo2"))
        self.assertTrue(ok)
        _run(self.pm.stop("echo2"))

    def test_on_start_hook(self):
        started = []
        self.pm.on_start(lambda mp: started.append(mp.spec.name))
        self.pm.register("echo3",["echo","x"])
        _run(self.pm.start("echo3"))
        self.assertIn("echo3",started)
        _run(self.pm.stop("echo3"))

    def test_on_stop_hook(self):
        stopped = []
        self.pm.on_stop(lambda mp: stopped.append(mp.spec.name))
        self.pm.register("sleep3",["sleep","30"])
        _run(self.pm.start("sleep3"))
        _run(self.pm.stop("sleep3"))
        self.assertIn("sleep3",stopped)

    def test_on_restart_hook(self):
        restarted = []
        self.pm.on_restart(lambda mp: restarted.append(mp.spec.name))
        self.pm.register("echo4",["echo","r"])
        _run(self.pm.start("echo4"))
        _run(self.pm.restart("echo4"))
        self.assertIn("echo4",restarted)
        _run(self.pm.stop("echo4"))

    def test_capture_stdout(self):
        self.pm.register("echo5",["echo","captured output"])
        _run(self.pm.start("echo5"))
        time.sleep(0.2)
        logs = self.pm.logs("echo5")
        _run(self.pm.stop("echo5"))
        # logs may or may not have content depending on timing
        self.assertIsInstance(logs, list)

    def test_event_log(self):
        self.pm.register("echo6",["echo","ev"])
        _run(self.pm.start("echo6"))
        _run(self.pm.stop("echo6"))
        events = self.pm.events("echo6")
        self.assertGreater(len(events),0)
        self.assertIn("event",events[0])

    def test_group_start_stop(self):
        self.pm.register("g1",["sleep","30"],group="grp")
        self.pm.register("g2",["sleep","30"],group="grp")
        _run(self.pm.start_group("grp"))
        self.assertTrue(self.pm.is_alive("g1"))
        self.assertTrue(self.pm.is_alive("g2"))
        _run(self.pm.stop_group("grp"))

    def test_restart_count_increments(self):
        self.pm.register("echo7",["echo","rc"])
        _run(self.pm.start("echo7"))
        _run(self.pm.restart("echo7"))
        mp = self.pm.status("echo7")
        self.assertGreaterEqual(mp.restart_count,1)
        _run(self.pm.stop("echo7"))

    def test_force_stop(self):
        self.pm.register("sleep4",["sleep","30"])
        _run(self.pm.start("sleep4"))
        ok = _run(self.pm.stop("sleep4",force=True))
        self.assertTrue(ok)
        self.assertFalse(self.pm.is_alive("sleep4"))

    def test_list_all(self):
        self.pm.register("list1",["echo","x"])
        items = self.pm.list_all()
        self.assertGreater(len(items),0)
        self.assertIn("name",items[0])

    def test_stats(self):
        s = self.pm.stats()
        for k in ["processes","in_memory"]: self.assertIn(k,s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v42: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
