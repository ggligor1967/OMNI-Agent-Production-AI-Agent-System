"""OMNI AGENT v14 - Test Suite: TaskPlanner, MemoryGraph, MultiAgentDebate, ToolComposer"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ═══════════════════════════════════════════════════════════
# TASK PLANNER
# ═══════════════════════════════════════════════════════════
class TestTopologicalWaves(unittest.TestCase):
    def setUp(self):
        from agent.task_planner import Task, _topological_waves
        self.Task=Task; self.waves=_topological_waves
    def _t(self,tid,deps=None):
        return self.Task(id=tid,plan_id="p",title=tid,depends_on=deps or [])
    def test_single(self):
        self.assertEqual(len(self.waves([self._t("a")])),1)
    def test_sequential(self):
        w=self.waves([self._t("a"),self._t("b",["a"])])
        self.assertEqual(len(w),2); self.assertEqual(w[0][0].id,"a")
    def test_parallel(self):
        self.assertEqual(len(self.waves([self._t("a"),self._t("b"),self._t("c")])[0]),3)
    def test_diamond(self):
        ts=[self._t("a"),self._t("b",["a"]),self._t("c",["a"]),self._t("d",["b","c"])]
        self.assertEqual(len(self.waves(ts)),3)
    def test_cycle_raises(self):
        from agent.task_planner import CyclicDependencyError
        with self.assertRaises(CyclicDependencyError):
            self.waves([self._t("a",["b"]),self._t("b",["a"])])

class TestTaskPlanner(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.mkdtemp()
        from agent.task_planner import TaskPlanner,TaskStatus,PlanStatus
        self.p=TaskPlanner(db_path=os.path.join(self.td,"p.db"))
        self.TS=TaskStatus; self.PS=PlanStatus
    def test_create(self):
        plan=self.p.create_plan("G",[{"title":"A"},{"title":"B"}])
        self.assertEqual(len(plan.tasks),2)
    def test_dep_resolution(self):
        plan=self.p.create_plan("G",[{"title":"A"},{"title":"B","depends_on":["A"]}])
        b=next(t for t in plan.tasks if t.title=="B")
        a_id=next(t.id for t in plan.tasks if t.title=="A")
        self.assertIn(a_id,b.depends_on)
    def test_cycle_raises(self):
        from agent.task_planner import CyclicDependencyError
        with self.assertRaises(CyclicDependencyError):
            self.p.create_plan("G",[{"title":"A","depends_on":["B"]},{"title":"B","depends_on":["A"]}])
    def test_get(self):
        plan=self.p.create_plan("G",[{"title":"T"}])
        self.assertIsNotNone(self.p.get_plan(plan.id))
    def test_get_missing(self):
        self.assertIsNone(self.p.get_plan("ghost"))
    def test_cancel(self):
        plan=self.p.create_plan("G",[{"title":"T"}])
        self.assertTrue(self.p.cancel_plan(plan.id))
        self.assertEqual(self.p.get_plan(plan.id).status,self.PS.CANCELLED)
    def test_execute_no_executor(self):
        plan=self.p.create_plan("G",[{"title":"T1"},{"title":"T2"}])
        r=_run(self.p.execute(plan.id))
        self.assertEqual(r.status,self.PS.DONE); self.assertEqual(r.done_count,2)
    def test_execute_with_executor(self):
        seen=[]
        from agent.task_planner import TaskPlanner
        p=TaskPlanner(executor=lambda t,c: seen.append(t.title) or "ok",
                       db_path=os.path.join(self.td,"p2.db"))
        plan=p.create_plan("G",[{"title":"S1"},{"title":"S2"}])
        _run(p.execute(plan.id)); self.assertEqual(len(seen),2)
    def test_sequential_order(self):
        order=[]
        from agent.task_planner import TaskPlanner
        p=TaskPlanner(executor=lambda t,c: order.append(t.title) or t.title,
                       db_path=os.path.join(self.td,"p3.db"))
        plan=p.create_plan("G",[{"title":"First"},{"title":"Second","depends_on":["First"]}])
        _run(p.execute(plan.id)); self.assertEqual(order,["First","Second"])
    def test_result_propagation(self):
        from agent.task_planner import TaskPlanner
        def ex(task,ctx):
            if task.title=="B": return f"got:{list(ctx['dep_results'].values())}"
            return "A_out"
        p=TaskPlanner(executor=ex,db_path=os.path.join(self.td,"p4.db"))
        plan=p.create_plan("G",[{"title":"A"},{"title":"B","depends_on":["A"]}])
        r=_run(p.execute(plan.id))
        b=next(t for t in r.tasks if t.title=="B")
        self.assertIn("A_out",str(b.result))
    def test_failed_task(self):
        from agent.task_planner import TaskPlanner
        def ex(t,c): raise RuntimeError("fail")
        p=TaskPlanner(executor=ex,max_parallel=1,db_path=os.path.join(self.td,"p5.db"))
        plan=p.create_plan("G",[{"title":"T","max_retries":0}])
        r=_run(p.execute(plan.id)); self.assertEqual(r.status,self.PS.FAILED)
    def test_approve(self):
        plan=self.p.create_plan("G",[{"title":"T","requires_approval":True}])
        task=plan.tasks[0]; task.status=self.TS.WAITING
        self.p._store.save_tasks([task])
        self.assertTrue(self.p.approve_task(plan.id,task.id))
    def test_decompose(self):
        plan=_run(self.p.decompose("Build X",lambda p: '[{"title":"A"},{"title":"B","depends_on":["A"]}]'))
        self.assertEqual(len(plan.tasks),2)
    def test_progress_pct(self):
        plan=self.p.create_plan("G",[{"title":"T1"},{"title":"T2"}])
        _run(self.p.execute(plan.id)); self.assertEqual(self.p.get_plan(plan.id).progress_pct,100.0)
    def test_to_dict(self):
        plan=self.p.create_plan("G",[{"title":"T"}]); d=plan.to_dict()
        for k in ["id","goal","tasks","progress_pct"]: self.assertIn(k,d)
    def test_stats(self):
        self.p.create_plan("G",[{"title":"T"}])
        self.assertGreaterEqual(self.p.stats()["total_plans"],1)
    def test_persistence(self):
        plan=self.p.create_plan("Persist",[{"title":"PT"}])
        from agent.task_planner import TaskPlanner
        p2=TaskPlanner(db_path=os.path.join(self.td,"p.db"))
        self.assertEqual(p2.get_plan(plan.id).goal,"Persist")

# ═══════════════════════════════════════════════════════════
# MEMORY GRAPH
# ═══════════════════════════════════════════════════════════
class TestEntityDecay(unittest.TestCase):
    def test_pinned(self):
        from agent.memory_graph import Entity
        e=Entity(id="e",name="t",importance=0.9,pinned=True,last_accessed=time.time()-86400*30)
        self.assertEqual(e.decayed_importance(),0.9)
    def test_recent(self):
        from agent.memory_graph import Entity
        e=Entity(id="e",name="t",importance=1.0,last_accessed=time.time()-60)
        self.assertGreater(e.decayed_importance(),0.9)
    def test_old(self):
        from agent.memory_graph import Entity
        e=Entity(id="e",name="t",importance=1.0,last_accessed=time.time()-86400*90)
        self.assertLess(e.decayed_importance(),0.5)
    def test_access_slows(self):
        from agent.memory_graph import Entity
        t=time.time()-86400*10
        lo=Entity(id="e1",name="a",importance=1.0,access_count=0,last_accessed=t)
        hi=Entity(id="e2",name="b",importance=1.0,access_count=20,last_accessed=t)
        self.assertGreater(hi.decayed_importance(),lo.decayed_importance())

class TestMemoryGraph(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.mkdtemp()
        from agent.memory_graph import MemoryGraph
        self.m=MemoryGraph(db_path=os.path.join(self.td,"m.db"))
    def test_remember(self):
        e=self.m.remember("Alice",entity_type="person"); self.assertEqual(e.name,"Alice")
    def test_idempotent(self):
        self.m.remember("Bob"); self.m.remember("Bob",description="updated")
        self.assertEqual(len([e for e in self.m.list_entities() if e.name=="Bob"]),1)
    def test_get(self):
        self.m.remember("Charlie"); self.assertIsNotNone(self.m.get_entity("Charlie"))
    def test_get_missing(self):
        self.assertIsNone(self.m.get_entity("Ghost"))
    def test_relate(self):
        self.m.remember("A"); self.m.remember("B",entity_type="company")
        r=self.m.relate("A","B","works_at"); self.assertIsNotNone(r)
    def test_relate_missing(self):
        self.m.remember("A"); self.assertIsNone(self.m.relate("A","Missing","knows"))
    def test_neighbors(self):
        self.m.remember("Alice"); self.m.remember("Bob")
        self.m.relate("Alice","Bob","knows")
        ns=self.m.neighbors("Alice"); self.assertIn("Bob",[e.name for _,e in ns])
    def test_reinforce(self):
        self.m.remember("Dave",importance=0.5); self.m.reinforce("Dave",boost=0.2)
        self.assertGreater(self.m.get_entity("Dave").importance,0.5)
    def test_reinforce_caps(self):
        self.m.remember("Eve",importance=0.95); self.m.reinforce("Eve",boost=0.5)
        self.assertLessEqual(self.m.get_entity("Eve").importance,1.0)
    def test_recall(self):
        self.m.remember("Python",description="programming language")
        self.m.remember("Cooking",description="making food")
        names=[e.name for _,e in self.m.recall("software development")]
        self.assertIn("Python",names)
    def test_recall_scores(self):
        self.m.remember("T",description="test")
        for score,_ in self.m.recall("test"): self.assertIsInstance(score,float)
    def test_forget(self):
        self.m.remember("Eph",importance=0.01)
        e=self.m.get_entity("Eph"); e.last_accessed=time.time()-86400*100
        self.m._store.save_entity(e); self.assertGreater(self.m.forget(threshold=0.05),0)
    def test_forget_pinned(self):
        self.m.remember("Keep",importance=0.01,pinned=True)
        e=self.m.get_entity("Keep"); e.last_accessed=time.time()-86400*100
        self.m._store.save_entity(e); self.m.forget(threshold=0.5)
        self.assertIsNotNone(self.m.get_entity("Keep"))
    def test_delete(self):
        self.m.remember("Temp"); self.m.delete("Temp")
        self.assertIsNone(self.m.get_entity("Temp"))
    def test_working_memory(self):
        self.m.remember("H",importance=1.0,pinned=True)
        self.m.remember("L",importance=0.1)
        wm=self.m.working_memory(top_k=5); self.assertTrue(wm[0].pinned)
    def test_list_by_type(self):
        self.m.remember("C",entity_type="concept"); self.m.remember("P",entity_type="person")
        self.assertTrue(all(e.entity_type=="concept" for e in self.m.list_entities(entity_type="concept")))
    def test_stats(self):
        self.m.remember("S1"); self.m.remember("S2")
        self.assertGreaterEqual(self.m.stats()["entities"],2)
    def test_persistence(self):
        self.m.remember("Persist",importance=0.8)
        from agent.memory_graph import MemoryGraph
        m2=MemoryGraph(db_path=os.path.join(self.td,"m.db"))
        self.assertIsNotNone(m2.get_entity("Persist"))
    def test_to_dict(self):
        e=self.m.remember("D")
        for k in ["id","name","entity_type","importance","decayed_importance"]: self.assertIn(k,e.to_dict())

# ═══════════════════════════════════════════════════════════
# MULTI-AGENT DEBATE
# ═══════════════════════════════════════════════════════════
class TestDebate(unittest.TestCase):
    def setUp(self):
        from agent.multi_agent_debate import MultiAgentDebate
        self.d=MultiAgentDebate()
    def test_run(self):
        r=_run(self.d.run("Tabs vs spaces?",rounds=1)); self.assertIsNotNone(r.id)
    def test_has_turns(self):
        r=_run(self.d.run("Topic",rounds=1)); self.assertGreater(len(r.turns),0)
    def test_opening_type(self):
        r=_run(self.d.run("Topic",rounds=1)); self.assertIn("opening",{t.turn_type for t in r.turns})
    def test_rebuttal_type(self):
        r=_run(self.d.run("Topic",rounds=2)); self.assertIn("rebuttal",{t.turn_type for t in r.turns})
    def test_rounds_count(self):
        self.assertEqual(_run(self.d.run("T",rounds=3)).rounds_conducted,3)
    def test_no_consensus_no_llm(self):
        self.assertEqual(_run(self.d.run("T",synthesize=True)).consensus,"")
    def test_consensus_with_llm(self):
        from agent.multi_agent_debate import MultiAgentDebate
        d=MultiAgentDebate(llm_fn=lambda p: "Both sides have merit.")
        r=_run(d.run("T",rounds=1,synthesize=True)); self.assertIn("merit",r.consensus)
    def test_custom_panel(self):
        from agent.multi_agent_debate import MultiAgentDebate,Debater
        d=MultiAgentDebate()
        panel=[Debater(id="x",name="Expert",system_prompt="You are an expert.")]
        r=_run(d.run("T",panel=panel,rounds=1))
        self.assertEqual(r.turns[0].debater_name,"Expert")
    def test_transcript(self):
        r=_run(self.d.run("UniqueXYZ123",rounds=1)); self.assertIn("UniqueXYZ123",r.transcript())
    def test_to_dict(self):
        r=_run(self.d.run("T",rounds=1)); d=r.to_dict()
        for k in ["id","topic","turns","consensus","rounds_conducted","duration_ms"]: self.assertIn(k,d)
    def test_history(self):
        before=len(self.d.history())
        _run(self.d.run("T1",rounds=1)); _run(self.d.run("T2",rounds=1))
        self.assertEqual(len(self.d.history()),before+2)
    def test_history_limit(self):
        for i in range(5): _run(self.d.run(f"T{i}",rounds=1))
        self.assertLessEqual(len(self.d.history(limit=3)),3)
    def test_red_team(self):
        r=_run(self.d.run("Security",preset="red_team",rounds=1)); self.assertGreater(len(r.turns),0)
    def test_presets_exist(self):
        from agent.multi_agent_debate import PRESETS
        for k in ["expert_panel","red_team","socratic"]: self.assertIn(k,PRESETS)
    def test_duration_positive(self):
        r=_run(self.d.run("T",rounds=1)); self.assertGreater(r.duration_ms,0)
    def test_async_llm(self):
        from agent.multi_agent_debate import MultiAgentDebate
        async def llm(p): return "async response"
        d=MultiAgentDebate(llm_fn=llm)
        r=_run(d.run("T",rounds=1,synthesize=False)); self.assertGreater(len(r.turns),0)

# ═══════════════════════════════════════════════════════════
# TOOL COMPOSER
# ═══════════════════════════════════════════════════════════
class TestToolComposer(unittest.TestCase):
    def setUp(self):
        from agent.tool_composer import ToolComposer
        self.c=ToolComposer()
    def test_register(self):
        self.c.register_tool("echo",lambda **kw: kw.get("input"))
        self.assertIn("echo",self.c._tools)
    def test_decorator(self):
        @self.c.tool("up")
        def up(**kw): return str(kw.get("input","")).upper()
        self.assertIn("up",self.c._tools)
    def test_build_pipeline(self):
        p=self.c.build_pipeline("p",[{"name":"s","tool":"noop"}]); self.assertEqual(p.name,"p")
    def test_run_single(self):
        self.c.register_tool("dbl",lambda **kw: kw.get("input","")*2)
        p=self.c.build_pipeline("p",[{"name":"d","tool":"dbl"}])
        r=_run(self.c.run(p.id,initial_input="ab")); self.assertEqual(r.final_output,"abab")
    def test_chain(self):
        self.c.register_tool("ax",lambda **kw: str(kw.get("input",""))+"x")
        self.c.register_tool("ay",lambda **kw: str(kw.get("input",""))+"y")
        p=self.c.build_pipeline("c",[{"name":"a","tool":"ax"},{"name":"b","tool":"ay"}])
        r=_run(self.c.run(p.id,initial_input="")); self.assertEqual(r.final_output,"xy")
    def test_chain_convenience(self):
        self.c.register_tool("t1",lambda **kw: str(kw.get("input",""))+"1")
        self.c.register_tool("t2",lambda **kw: str(kw.get("input",""))+"2")
        r=_run(self.c.run_chain(["t1","t2"],initial_input=""))
        self.assertEqual(r.final_output,"12")
    def test_parallel(self):
        self.c.register_tool("pa",lambda **kw: "a")
        self.c.register_tool("pb",lambda **kw: "b")
        r=_run(self.c.run_parallel(["pa","pb"]))
        self.assertEqual(sorted(r.final_output),["a","b"])
    def test_dry_run(self):
        called=[]
        self.c.register_tool("se",lambda **kw: called.append(1) or kw.get("input"))
        p=self.c.build_pipeline("p",[{"name":"s","tool":"se"}])
        _run(self.c.run(p.id,initial_input="x",dry_run=True)); self.assertEqual(len(called),0)
    def test_transform(self):
        self.c.register_tool("noop",lambda **kw: kw.get("input"))
        tx=self.c.transform("up",lambda x: str(x).upper())
        p=self.c.build_pipeline("p",[{"name":"n","tool":"noop"},tx])
        r=_run(self.c.run(p.id,initial_input="hello")); self.assertEqual(r.final_output,"HELLO")
    def test_filter_pass(self):
        self.c.register_tool("noop",lambda **kw: kw.get("input"))
        fx=self.c.filter_step("f",lambda x: x is not None)
        p=self.c.build_pipeline("p",[{"name":"n","tool":"noop"},fx])
        r=_run(self.c.run(p.id,initial_input="keep")); self.assertEqual(r.final_output,"keep")
    def test_filter_block(self):
        fx=self.c.filter_step("f",lambda x: False)
        p=self.c.build_pipeline("p",[fx])
        r=_run(self.c.run(p.id,initial_input="data")); self.assertIsNone(r.final_output)
    def test_branch_true(self):
        self.c.register_tool("yes",lambda **kw: "yes")
        self.c.register_tool("no",lambda **kw: "no")
        br=self.c.branch("b",lambda x: True,self.c.step("y",tool="yes"),self.c.step("n",tool="no"))
        p=self.c.build_pipeline("p",[br])
        r=_run(self.c.run(p.id)); self.assertEqual(r.final_output,"yes")
    def test_branch_false(self):
        self.c.register_tool("yes",lambda **kw: "yes")
        self.c.register_tool("no",lambda **kw: "no")
        br=self.c.branch("b",lambda x: False,self.c.step("y",tool="yes"),self.c.step("n",tool="no"))
        p=self.c.build_pipeline("p",[br])
        r=_run(self.c.run(p.id)); self.assertEqual(r.final_output,"no")
    def test_fallback(self):
        def broken(**kw): raise RuntimeError("err")
        self.c.register_tool("broken",broken)
        p=self.c.build_pipeline("p",[{"name":"s","tool":"broken","fallback":"safe","max_retries":0}])
        r=_run(self.c.run(p.id)); self.assertEqual(r.final_output,"safe")
    def test_missing_tool_fails(self):
        p=self.c.build_pipeline("p",[{"name":"s","tool":"no_such"}])
        r=_run(self.c.run(p.id)); self.assertEqual(r.status.value,"failed")
    def test_async_tool(self):
        async def at(**kw): await asyncio.sleep(0.01); return "async_ok"
        self.c.register_tool("at",at)
        p=self.c.build_pipeline("p",[{"name":"a","tool":"at"}])
        r=_run(self.c.run(p.id)); self.assertEqual(r.final_output,"async_ok")
    def test_trace(self):
        self.c.register_tool("t",lambda **kw: "v")
        p=self.c.build_pipeline("p",[{"name":"step1","tool":"t"}])
        r=_run(self.c.run(p.id)); self.assertEqual(r.traces[0].step_name,"step1")
    def test_pipeline_not_found(self):
        with self.assertRaises(KeyError): _run(self.c.run("ghost"))
    def test_history(self):
        p=self.c.build_pipeline("p",[])
        _run(self.c.run(p.id)); _run(self.c.run(p.id))
        self.assertGreaterEqual(len(self.c.history()),2)
    def test_stats(self):
        self.c.register_tool("t",lambda **kw: None)
        s=self.c.stats(); self.assertIn("t",s["registered_tools"])
    def test_result_to_dict(self):
        p=self.c.build_pipeline("p",[])
        r=_run(self.c.run(p.id,initial_input="x")); d=r.to_dict()
        for k in ["pipeline_id","pipeline_name","status","traces","duration_ms"]: self.assertIn(k,d)

if __name__=="__main__":
    loader=unittest.TestLoader()
    suite=loader.loadTestsFromModule(__import__(__name__))
    runner=unittest.TextTestRunner(verbosity=2)
    result=runner.run(suite)
    total=result.testsRun; failed=len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v14: {total-failed}/{total} passed")
    if failed:
        for t,tb in result.failures+result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
