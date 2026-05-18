"""OMNI AGENT v15 - Test Suite: LLMJudge, DocumentQA, StateMachine, AgentSwarm"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ═══════════════════════════════════════════════════════════
# LLM JUDGE
# ═══════════════════════════════════════════════════════════
class TestLLMJudge(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.llm_judge import LLMJudge
        self.j = LLMJudge(db_path=os.path.join(td,"j.db"))

    def test_judge_no_llm(self):
        v = _run(self.j.judge("What is 2+2?", "The answer is 4."))
        self.assertIsNotNone(v); self.assertIsNotNone(v.id)

    def test_judge_score_range(self):
        v = _run(self.j.judge("Q", "A"))
        self.assertGreaterEqual(v.overall_score, 0)
        self.assertLessEqual(v.overall_score, 10)

    def test_judge_with_llm(self):
        from agent.llm_judge import LLMJudge
        def llm(p):
            return '{"scores":{"accuracy":{"score":8.0,"rationale":"good"},"relevance":{"score":7.5,"rationale":"ok"},"clarity":{"score":9.0,"rationale":"clear"},"completeness":{"score":7.0,"rationale":"mostly"}},"overall_score":7.9,"overall_rationale":"solid answer"}'
        j = LLMJudge(llm_fn=llm, db_path=os.path.join(tempfile.mkdtemp(),"j.db"))
        v = _run(j.judge("What is gravity?", "Gravity is a force."))
        self.assertGreater(v.overall_score, 0)
        self.assertGreater(len(v.scores), 0)

    def test_judge_has_scores(self):
        v = _run(self.j.judge("Q","A"))
        self.assertIsInstance(v.scores, list)

    def test_judge_weighted_score(self):
        v = _run(self.j.judge("Q","A"))
        self.assertIsInstance(v.weighted_score, float)

    def test_judge_to_dict(self):
        v = _run(self.j.judge("Q","A"))
        d = v.to_dict()
        for k in ["id","rubric_name","overall_score","weighted_score","scores"]:
            self.assertIn(k, d)

    def test_custom_rubric(self):
        from agent.llm_judge import LLMJudge, Criterion
        td = tempfile.mkdtemp()
        j = LLMJudge(db_path=os.path.join(td,"j.db"))
        j.register_rubric("code_review", [
            Criterion("correctness","Code is correct",weight=3.0),
            Criterion("style","Follows style guide",weight=1.0),
        ])
        v = _run(j.judge("Write a sort function","def sort(x): return sorted(x)", rubric_name="code_review"))
        self.assertIsNotNone(v)

    def test_compare_no_llm(self):
        cmp = _run(self.j.compare("Q","Response A","Response B"))
        self.assertIsNotNone(cmp); self.assertIn(cmp.winner, ["A","B","tie"])

    def test_compare_with_llm(self):
        from agent.llm_judge import LLMJudge
        def llm(p): return '{"winner":"A","score_a":8.5,"score_b":6.0,"rationale":"A is more accurate"}'
        j = LLMJudge(llm_fn=llm, db_path=os.path.join(tempfile.mkdtemp(),"j.db"))
        cmp = _run(j.compare("Q","Better answer","Worse answer"))
        self.assertEqual(cmp.winner,"A"); self.assertGreater(cmp.score_a, cmp.score_b)

    def test_compare_to_dict(self):
        cmp = _run(self.j.compare("Q","A","B"))
        d = cmp.to_dict()
        for k in ["id","winner","score_a","score_b"]: self.assertIn(k,d)

    def test_batch_judge(self):
        items = [{"input":"Q1","output":"A1"},{"input":"Q2","output":"A2"},{"input":"Q3","output":"A3"}]
        results = _run(self.j.batch_judge(items))
        self.assertEqual(len(results), 3)
        self.assertTrue(all(hasattr(r,"overall_score") for r in results))

    def test_leaderboard_empty(self):
        lb = self.j.leaderboard()
        self.assertIsInstance(lb, list)

    def test_leaderboard_updated(self):
        _run(self.j.judge("Q","A",model_name="test_model"))
        lb = self.j.leaderboard()
        names = [r["model_name"] for r in lb]
        self.assertIn("test_model", names)

    def test_calibrate_insufficient_data(self):
        result = self.j.calibrate([{"verdict_id":"fake","human_score":8.0}])
        self.assertIsNone(result["r"])

    def test_calibrate_with_data(self):
        v1 = _run(self.j.judge("Q1","A1"))
        v2 = _run(self.j.judge("Q2","A2"))
        labels = [{"verdict_id":v1.id,"human_score":8.0},{"verdict_id":v2.id,"human_score":6.0}]
        result = self.j.calibrate(labels)
        self.assertIn("r", result); self.assertIn("n", result); self.assertEqual(result["n"],2)

    def test_stats(self):
        _run(self.j.judge("Q","A"))
        stats = self.j.stats()
        self.assertGreaterEqual(stats["total_verdicts"],1)

    def test_get_verdicts(self):
        _run(self.j.judge("Q","A"))
        verdicts = self.j.get_verdicts(limit=10)
        self.assertGreater(len(verdicts), 0)

    def test_elo_updated_after_compare(self):
        from agent.llm_judge import LLMJudge
        def llm(p): return '{"winner":"A","score_a":9.0,"score_b":5.0,"rationale":"A wins"}'
        j = LLMJudge(llm_fn=llm, db_path=os.path.join(tempfile.mkdtemp(),"j.db"))
        _run(j.compare("Q","A resp","B resp",model_a="ModelA",model_b="ModelB"))
        lb = j.leaderboard()
        elos = {r["model_name"]: r["elo"] for r in lb}
        self.assertIn("ModelA", elos); self.assertGreater(elos["ModelA"], 1200)

# ═══════════════════════════════════════════════════════════
# DOCUMENT QA
# ═══════════════════════════════════════════════════════════
class TestChunking(unittest.TestCase):
    def test_fixed_chunks(self):
        from agent.document_qa import _chunk_fixed
        text = " ".join([f"word{i}" for i in range(200)])
        chunks = _chunk_fixed(text, chunk_size=50, overlap=10)
        self.assertGreater(len(chunks), 1)

    def test_paragraph_chunks(self):
        from agent.document_qa import _chunk_paragraphs
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = _chunk_paragraphs(text)
        self.assertGreater(len(chunks), 0)

    def test_sentence_chunks(self):
        from agent.document_qa import _chunk_sentences
        text = "First sentence. Second sentence. Third sentence. Fourth sentence."
        chunks = _chunk_sentences(text, max_chunk=5)
        self.assertGreater(len(chunks), 1)

    def test_keyword_boost(self):
        from agent.document_qa import _kw_boost
        boost = _kw_boost("python programming", "python is a programming language")
        self.assertGreater(boost, 0)

    def test_no_keyword_boost(self):
        from agent.document_qa import _kw_boost
        boost = _kw_boost("cooking recipes", "python is a programming language")
        self.assertEqual(boost, 0.0)

class TestDocumentQA(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.document_qa import DocumentQA
        self.qa = DocumentQA(db_path=os.path.join(td,"qa.db"))

    def test_ingest_returns_id(self):
        doc_id = self.qa.ingest("Test Doc","This is test content about Python.")
        self.assertIsNotNone(doc_id)

    def test_ingest_creates_chunks(self):
        self.qa.ingest("Doc","Content with multiple sentences. Second sentence. Third one.")
        stats = self.qa.stats()
        self.assertGreater(stats["chunks"], 0)

    def test_list_docs(self):
        self.qa.ingest("DocA","Content A")
        self.qa.ingest("DocB","Content B")
        docs = self.qa.list_docs()
        titles = [d.title for d in docs]
        self.assertIn("DocA", titles); self.assertIn("DocB", titles)

    def test_get_doc(self):
        doc_id = self.qa.ingest("FindMe","Some content")
        doc = self.qa.get_doc(doc_id)
        self.assertIsNotNone(doc); self.assertEqual(doc.title,"FindMe")

    def test_get_doc_missing(self):
        self.assertIsNone(self.qa.get_doc("ghost_id"))

    def test_retrieve_returns_results(self):
        self.qa.ingest("PythonDoc","Python is a high-level programming language created by Guido.")
        results = self.qa.retrieve("Python programming")
        self.assertGreater(len(results), 0)

    def test_retrieve_scored(self):
        self.qa.ingest("Doc","Machine learning models need data.")
        results = self.qa.retrieve("machine learning")
        for score, chunk in results:
            self.assertGreaterEqual(score, 0.0)
            self.assertIsNotNone(chunk.text)

    def test_query_no_llm(self):
        self.qa.ingest("Doc","Python was created in 1991 by Guido van Rossum.")
        result = _run(self.qa.query("When was Python created?"))
        self.assertIsNotNone(result); self.assertIsNotNone(result.answer)

    def test_query_has_citations(self):
        self.qa.ingest("SourceDoc","Machine learning is a branch of AI.")
        result = _run(self.qa.query("What is machine learning?"))
        self.assertIsInstance(result.citations, list)

    def test_query_with_llm(self):
        from agent.document_qa import DocumentQA
        def llm(p): return '{"answer":"Python was created in 1991.","confidence":0.95,"follow_up_query":""}'
        qa = DocumentQA(llm_fn=llm, db_path=os.path.join(tempfile.mkdtemp(),"qa.db"))
        qa.ingest("History","Python was created in 1991 by Guido van Rossum.")
        result = _run(qa.query("When was Python created?"))
        self.assertIn("1991", result.answer)

    def test_query_confidence(self):
        self.qa.ingest("Doc","Content here.")
        result = _run(self.qa.query("Question?"))
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)

    def test_citation_fields(self):
        self.qa.ingest("Cited","Relevant content for testing citations.")
        result = _run(self.qa.query("test citations"))
        if result.citations:
            cit = result.citations[0]; d = cit.to_dict()
            for k in ["doc_id","doc_title","chunk_id","score","excerpt"]: self.assertIn(k,d)

    def test_delete_doc(self):
        doc_id = self.qa.ingest("ToDelete","Temporary content.")
        self.qa.delete_doc(doc_id)
        self.assertIsNone(self.qa.get_doc(doc_id))

    def test_delete_removes_chunks(self):
        doc_id = self.qa.ingest("ToDelete2","Temporary content for deletion.")
        before = self.qa.stats()["chunks"]
        self.qa.delete_doc(doc_id)
        after = self.qa.stats()["chunks"]
        self.assertLess(after, before)

    def test_doc_id_filter(self):
        id1 = self.qa.ingest("Doc1","Alpha content about cats.")
        id2 = self.qa.ingest("Doc2","Beta content about dogs.")
        results = self.qa.retrieve("cats", doc_ids=[id1])
        for _, chunk in results:
            self.assertEqual(chunk.doc_id, id1)

    def test_fixed_strategy(self):
        doc_id = self.qa.ingest("Fixed","Word "*300, strategy="fixed", chunk_size=50, overlap=10)
        doc = self.qa.get_doc(doc_id)
        self.assertGreater(doc.chunk_count, 1)

    def test_sentence_strategy(self):
        doc_id = self.qa.ingest("Sents","First. Second. Third. Fourth. Fifth.", strategy="sentence")
        self.assertIsNotNone(doc_id)

    def test_qa_result_to_dict(self):
        self.qa.ingest("D","Content")
        result = _run(self.qa.query("Q?"))
        d = result.to_dict()
        for k in ["question","answer","confidence","citations","latency_ms"]: self.assertIn(k,d)

    def test_stats(self):
        self.qa.ingest("S","Content")
        s = self.qa.stats()
        self.assertIn("documents",s); self.assertIn("chunks",s)
        self.assertGreaterEqual(s["documents"],1)

# ═══════════════════════════════════════════════════════════
# STATE MACHINE
# ═══════════════════════════════════════════════════════════
class TestStateMachine(unittest.TestCase):
    def _make_sm(self, td=None):
        from agent.state_machine import StateMachine, State, Transition
        db = os.path.join(td or tempfile.mkdtemp(),"sm.db")
        sm = StateMachine("test_"+str(id(self)), initial_state="idle", db_path=db)
        sm.add_state(State("idle","Waiting"))
        sm.add_state(State("running","Busy"))
        sm.add_state(State("done","Finished", is_terminal=True))
        sm.add_transition(Transition("start","idle","running"))
        sm.add_transition(Transition("finish","running","done"))
        sm.add_transition(Transition("reset","done","idle"))
        return sm

    def test_initial_state(self):
        sm = self._make_sm(); self.assertEqual(sm.current_state,"idle")

    def test_trigger_success(self):
        sm = self._make_sm(); ok = _run(sm.trigger("start"))
        self.assertTrue(ok); self.assertEqual(sm.current_state,"running")

    def test_trigger_chain(self):
        sm = self._make_sm()
        _run(sm.trigger("start")); _run(sm.trigger("finish"))
        self.assertEqual(sm.current_state,"done")

    def test_trigger_invalid_event(self):
        sm = self._make_sm(); ok = _run(sm.trigger("finish"))
        self.assertFalse(ok); self.assertEqual(sm.current_state,"idle")

    def test_trigger_wrong_state(self):
        sm = self._make_sm()
        ok = _run(sm.trigger("reset"))  # can't reset from idle
        self.assertFalse(ok)

    def test_guard_blocks(self):
        from agent.state_machine import StateMachine, State, Transition
        td = tempfile.mkdtemp()
        sm = StateMachine("guard_test", initial_state="s1",
                           db_path=os.path.join(td,"sm.db"))
        sm.add_state(State("s1")); sm.add_state(State("s2"))
        sm.add_transition(Transition("go","s1","s2",
                            guard=lambda ctx: ctx.get("allowed",False)))
        ok = _run(sm.trigger("go",{"allowed":False}))
        self.assertFalse(ok); self.assertEqual(sm.current_state,"s1")

    def test_guard_passes(self):
        from agent.state_machine import StateMachine, State, Transition
        td = tempfile.mkdtemp()
        sm = StateMachine("guard_pass", initial_state="s1",
                           db_path=os.path.join(td,"sm.db"))
        sm.add_state(State("s1")); sm.add_state(State("s2"))
        sm.add_transition(Transition("go","s1","s2",
                            guard=lambda ctx: ctx.get("allowed",False)))
        ok = _run(sm.trigger("go",{"allowed":True}))
        self.assertTrue(ok); self.assertEqual(sm.current_state,"s2")

    def test_entry_action_called(self):
        from agent.state_machine import StateMachine, State, Transition
        td = tempfile.mkdtemp(); called=[]
        sm = StateMachine("entry_test", initial_state="s1",
                           db_path=os.path.join(td,"sm.db"))
        sm.add_state(State("s1"))
        sm.add_state(State("s2", entry_action=lambda ctx: called.append("entered_s2")))
        sm.add_transition(Transition("go","s1","s2"))
        _run(sm.trigger("go")); self.assertIn("entered_s2", called)

    def test_exit_action_called(self):
        from agent.state_machine import StateMachine, State, Transition
        td = tempfile.mkdtemp(); called=[]
        sm = StateMachine("exit_test", initial_state="s1",
                           db_path=os.path.join(td,"sm.db"))
        sm.add_state(State("s1", exit_action=lambda ctx: called.append("exited_s1")))
        sm.add_state(State("s2"))
        sm.add_transition(Transition("go","s1","s2"))
        _run(sm.trigger("go")); self.assertIn("exited_s1", called)

    def test_transition_action_called(self):
        from agent.state_machine import StateMachine, State, Transition
        td = tempfile.mkdtemp(); called=[]
        sm = StateMachine("ta_test", initial_state="s1",
                           db_path=os.path.join(td,"sm.db"))
        sm.add_state(State("s1")); sm.add_state(State("s2"))
        sm.add_transition(Transition("go","s1","s2",
                            action=lambda ctx: called.append("action_fired")))
        _run(sm.trigger("go")); self.assertIn("action_fired", called)

    def test_is_terminal(self):
        sm = self._make_sm()
        _run(sm.trigger("start")); _run(sm.trigger("finish"))
        self.assertTrue(sm.is_terminal())

    def test_not_terminal(self):
        sm = self._make_sm(); self.assertFalse(sm.is_terminal())

    def test_available_events(self):
        sm = self._make_sm()
        events = sm.available_events()
        self.assertIn("start", events); self.assertNotIn("finish", events)

    def test_can_trigger(self):
        sm = self._make_sm()
        self.assertTrue(sm.can_trigger("start"))
        self.assertFalse(sm.can_trigger("finish"))

    def test_history_recorded(self):
        sm = self._make_sm()
        _run(sm.trigger("start"))
        h = sm.history()
        self.assertGreater(len(h), 0)

    def test_status(self):
        sm = self._make_sm()
        s = sm.status()
        for k in ["machine_id","current_state","is_terminal","available_events"]:
            self.assertIn(k, s)

    def test_reset(self):
        sm = self._make_sm()
        _run(sm.trigger("start")); sm.reset("idle")
        self.assertEqual(sm.current_state,"idle")

    def test_context_preserved(self):
        sm = self._make_sm()
        _run(sm.trigger("start", context={"user":"alice"}))
        self.assertIn("user", sm._context)

    def test_priority_ordering(self):
        from agent.state_machine import StateMachine, State, Transition
        td = tempfile.mkdtemp()
        sm = StateMachine("prio_test", initial_state="s1",
                           db_path=os.path.join(td,"sm.db"))
        sm.add_state(State("s1")); sm.add_state(State("s2")); sm.add_state(State("s3"))
        sm.add_transition(Transition("go","s1","s2",priority=1))
        sm.add_transition(Transition("go","s1","s3",priority=10))
        _run(sm.trigger("go")); self.assertEqual(sm.current_state,"s3")

    def test_persistence(self):
        from agent.state_machine import StateMachine, State, Transition
        td = tempfile.mkdtemp(); db=os.path.join(td,"sm.db"); mid="persist_sm"
        sm = StateMachine(mid, initial_state="s1", db_path=db)
        sm.add_state(State("s1")); sm.add_state(State("s2"))
        sm.add_transition(Transition("go","s1","s2"))
        _run(sm.trigger("go"))
        sm2 = StateMachine(mid, initial_state="s1", db_path=db)
        self.assertEqual(sm2.current_state,"s2")

    def test_trigger_sync(self):
        sm = self._make_sm()
        ok = sm.trigger_sync("start")
        self.assertTrue(ok); self.assertEqual(sm.current_state,"running")

# ═══════════════════════════════════════════════════════════
# AGENT SWARM
# ═══════════════════════════════════════════════════════════
class TestAgentSwarm(unittest.TestCase):
    def setUp(self):
        from agent.agent_swarm import AgentSwarm
        self.swarm = AgentSwarm(timeout_s=5.0)

    def _add_echo(self, name="echo"):
        return self.swarm.add_worker(name, lambda task, cfg: task)
        self.swarm.add_worker(name, lambda task, cfg: task)

    def _add_const(self, name, val):
        self.swarm.add_worker(name, lambda task, cfg: val)

    def test_add_worker(self):
        w = self._add_echo("w1")
        self.assertIn(w.id, {w.id for w in self.swarm.workers()})

    def test_broadcast_result(self):
        self._add_echo("e1"); self._add_echo("e2")
        r = _run(self.swarm.broadcast("hello"))
        self.assertIsNotNone(r); self.assertEqual(len(r.worker_results), 2)

    def test_broadcast_all_succeed(self):
        self._add_echo("e1"); self._add_echo("e2")
        r = _run(self.swarm.broadcast("task"))
        self.assertEqual(r.success_count, 2); self.assertEqual(r.failure_count, 0)

    def test_vote_majority(self):
        self._add_const("a","yes"); self._add_const("b","yes"); self._add_const("c","no")
        r = _run(self.swarm.broadcast("vote?",aggregate="vote"))
        self.assertEqual(r.aggregate,"yes")

    def test_average(self):
        self.swarm.add_worker("w1",lambda t,c: 10.0)
        self.swarm.add_worker("w2",lambda t,c: 20.0)
        r = _run(self.swarm.broadcast("avg",aggregate="average"))
        self.assertAlmostEqual(float(r.aggregate),15.0)

    def test_first_aggregate(self):
        self.swarm.add_worker("w1",lambda t,c: "first_result")
        r = _run(self.swarm.broadcast("task",aggregate="first"))
        self.assertEqual(r.aggregate,"first_result")

    def test_merge_aggregate(self):
        self.swarm.add_worker("w1",lambda t,c: [1,2])
        self.swarm.add_worker("w2",lambda t,c: [3,4])
        r = _run(self.swarm.broadcast("merge",aggregate="merge"))
        self.assertIsInstance(r.aggregate,list)
        self.assertEqual(sorted(r.aggregate),[1,2,3,4])

    def test_send_to_named(self):
        self.swarm.add_worker("target",lambda t,c: f"processed:{t}")
        r = _run(self.swarm.send("hello","target"))
        self.assertTrue(r.success); self.assertEqual(r.result,"processed:hello")

    def test_send_missing_worker(self):
        with self.assertRaises(ValueError):
            _run(self.swarm.send("task","nonexistent"))

    def test_race_returns_first(self):
        self.swarm.add_worker("slow",lambda t,c: "slow")
        self.swarm.add_worker("fast",lambda t,c: "fast")
        r = _run(self.swarm.race("go"))
        self.assertTrue(r.success)

    def test_deactivate_worker(self):
        w = self.swarm.add_worker("inactive",lambda t,c: "nope")
        self.swarm.deactivate_worker(w.id)
        r = _run(self.swarm.broadcast("task"))
        # inactive worker should not have responded
        self.assertEqual(r.success_count, 0)

    def test_activate_worker(self):
        w = self.swarm.add_worker("reactive",lambda t,c: "yes")
        self.swarm.deactivate_worker(w.id)
        self.swarm.activate_worker(w.id)
        r = _run(self.swarm.broadcast("task"))
        self.assertEqual(r.success_count, 1)

    def test_remove_worker(self):
        w = self.swarm.add_worker("removable",lambda t,c: "x")
        ok = self.swarm.remove_worker(w.id); self.assertTrue(ok)
        r = _run(self.swarm.broadcast("task"))
        self.assertEqual(r.success_count, 0)

    def test_worker_failure_counted(self):
        def fail(t,c): raise RuntimeError("boom")
        w = self.swarm.add_worker("failer",fail)
        _run(self.swarm.broadcast("task"))
        self.assertEqual(w.failure_count,1)

    def test_worker_success_counted(self):
        w = self.swarm.add_worker("succeeder",lambda t,c: "ok")
        _run(self.swarm.broadcast("task"))
        self.assertEqual(w.success_count,1)

    def test_async_worker(self):
        async def aworker(task,cfg):
            await asyncio.sleep(0.01); return "async_result"
        self.swarm.add_worker("async_w",aworker)
        r = _run(self.swarm.broadcast("task"))
        self.assertEqual(r.worker_results[0].result,"async_result")

    def test_map_reduce(self):
        self.swarm.add_worker("w",lambda t,c: t.upper())
        r = _run(self.swarm.map_reduce(
            items=["a","b","c"],
            task_fn=lambda x: x,
            aggregate="first",
        ))
        self.assertIsNotNone(r)

    def test_map_reduce_custom_reduce(self):
        self.swarm.add_worker("w",lambda t,c: len(t))
        r = _run(self.swarm.map_reduce(
            items=["hello","world","!"],
            task_fn=lambda x: x,
            reduce_fn=lambda results: sum(r.result for r in results if r.success),
        ))
        self.assertEqual(r.aggregate, 5+5+1)

    def test_on_result_callback(self):
        fired=[]
        def cb(wr): fired.append(wr.worker_name)
        self.swarm.on_result(cb)
        self.swarm.add_worker("cb_worker",lambda t,c: "val")
        _run(self.swarm.broadcast("task"))
        self.assertIn("cb_worker",fired)

    def test_stats(self):
        self.swarm.add_worker("s",lambda t,c: "x")
        s=self.swarm.stats()
        self.assertIn("total_workers",s); self.assertIn("active_workers",s)
        self.assertGreaterEqual(s["total_workers"],1)

    def test_worker_to_dict(self):
        w=self.swarm.add_worker("d",lambda t,c: "x"); d=w.to_dict()
        for k in ["id","name","active","success_count","error_rate"]: self.assertIn(k,d)

    def test_swarm_result_to_dict(self):
        self.swarm.add_worker("w",lambda t,c: "r")
        r=_run(self.swarm.broadcast("task")); d=r.to_dict()
        for k in ["task","aggregate","worker_results","duration_ms"]: self.assertIn(k,d)

    def test_no_workers_broadcast(self):
        r=_run(self.swarm.broadcast("task"))
        self.assertEqual(r.success_count,0); self.assertIsNone(r.aggregate)

    def test_history_tracked(self):
        self.swarm.add_worker("w",lambda t,c: "v")
        _run(self.swarm.broadcast("t1")); _run(self.swarm.broadcast("t2"))
        self.assertGreaterEqual(len(self.swarm.history()),2)

if __name__=="__main__":
    loader=unittest.TestLoader()
    suite=loader.loadTestsFromModule(__import__(__name__))
    runner=unittest.TextTestRunner(verbosity=2)
    result=runner.run(suite)
    total=result.testsRun; failed=len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v15: {total-failed}/{total} passed")
    if failed:
        for t,tb in result.failures+result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
