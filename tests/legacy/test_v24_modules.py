"""OMNI AGENT v24: PersonaEngine, KnowledgeBase, TaskScheduler, FeedbackLoop"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# PERSONA ENGINE
# ════════════════════════════════════════════════════════
class TestPersonaEngine(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.persona_engine import PersonaEngine
        self.pe = PersonaEngine(db_path=os.path.join(td, "pe.db"))

    def _make_personas(self):
        self.pe.create("professor",
                        description="Academic expert",
                        system_prompt="You are a knowledgeable professor.",
                        tone="technical",
                        keywords=["explain","define","theory","concept"],
                        tags=["education","science"])
        self.pe.create("buddy",
                        description="Casual friendly helper",
                        system_prompt="You are a friendly buddy.",
                        tone="casual",
                        keywords=["help","quick","easy","fun"],
                        tags=["general","chat"])

    def test_create_persona(self):
        p = self.pe.create("test_p", description="Test", system_prompt="You are test.")
        self.assertIsNotNone(p.id)
        self.assertEqual(p.name, "test_p")

    def test_get_persona(self):
        self._make_personas()
        p = self.pe.get("professor")
        self.assertIsNotNone(p)
        self.assertEqual(p.tone, "technical")

    def test_list_all(self):
        self._make_personas()
        personas = self.pe.list()
        self.assertGreaterEqual(len(personas), 2)

    def test_list_by_tag(self):
        self._make_personas()
        edu = self.pe.list(tag="education")
        self.assertTrue(all("education" in p.tags for p in edu))

    def test_list_by_tone(self):
        self._make_personas()
        casual = self.pe.list(tone="casual")
        self.assertTrue(all(p.tone == "casual" for p in casual))

    def test_activate(self):
        self._make_personas()
        ok = self.pe.activate("professor")
        self.assertTrue(ok)
        self.assertEqual(self.pe.active_persona.name, "professor")

    def test_deactivate(self):
        self._make_personas()
        self.pe.activate("professor")
        self.pe.deactivate()
        self.assertIsNone(self.pe.active_persona)

    def test_activate_nonexistent(self):
        ok = self.pe.activate("nonexistent")
        self.assertFalse(ok)

    def test_route_by_keyword(self):
        self._make_personas()
        p = self.pe.route("Can you explain the theory of relativity?")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "professor")

    def test_route_by_keyword_buddy(self):
        self._make_personas()
        p = self.pe.route("Quick help with something fun!")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "buddy")

    def test_route_fallback(self):
        self._make_personas()
        p = self.pe.route("xyzzy completely unknown query", fallback="buddy")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "buddy")

    def test_inject_system_prompt(self):
        self._make_personas()
        msgs = [{"role": "user", "content": "Hello"}]
        result = self.pe.prepare_messages("professor", msgs)
        self.assertEqual(result[0]["role"], "system")
        self.assertIn("professor", result[0]["content"].lower())

    def test_prepare_increments_call_count(self):
        self._make_personas()
        msgs = [{"role": "user", "content": "Hello"}]
        self.pe.prepare_messages("professor", msgs)
        self.assertEqual(self.pe.get("professor").call_count, 1)

    def test_blend(self):
        self._make_personas()
        blended = self.pe.blend("professor", "buddy", weight_a=0.7)
        self.assertIn("professor", blended.name)
        self.assertIn("professor", blended.system_prompt)
        self.assertIn("buddy", blended.system_prompt)

    def test_blend_keywords_merged(self):
        self._make_personas()
        blended = self.pe.blend("professor", "buddy")
        kws = blended.keywords
        self.assertTrue(any(k in kws for k in ["explain","help"]))

    def test_record_satisfaction(self):
        self._make_personas()
        self.pe.record_satisfaction("professor", 0.9)
        p = self.pe.get("professor")
        self.assertAlmostEqual(p.avg_satisfaction, 0.9, places=2)

    def test_record_satisfaction_clamped(self):
        self._make_personas()
        self.pe.record_satisfaction("professor", 1.5)  # > 1.0 → clamped to 1.0
        p = self.pe.get("professor")
        self.assertLessEqual(p.avg_satisfaction, 1.0)

    def test_activate_hook(self):
        activated = []
        self.pe.add_activate_hook(lambda p: activated.append(p.name))
        self._make_personas()
        self.pe.activate("professor")
        self.assertIn("professor", activated)

    def test_delete(self):
        self._make_personas()
        ok = self.pe.delete("buddy")
        self.assertTrue(ok)
        self.assertIsNone(self.pe.get("buddy"))

    def test_update_system_prompt(self):
        self._make_personas()
        self.pe.update_system_prompt("professor", "You are an updated professor.")
        p = self.pe.get("professor")
        self.assertIn("updated", p.system_prompt)

    def test_stats(self):
        self._make_personas()
        s = self.pe.stats()
        for k in ["total_personas","total_calls"]: self.assertIn(k, s)

    def test_to_dict(self):
        self._make_personas()
        d = self.pe.get("professor").to_dict()
        for k in ["id","name","description","tone","tags","call_count"]: self.assertIn(k, d)

    def test_persistence(self):
        td = tempfile.mkdtemp()
        from agent.persona_engine import PersonaEngine
        db = os.path.join(td, "pe.db")
        pe1 = PersonaEngine(db_path=db)
        pe1.create("persist_p", system_prompt="Persist me", tone="formal")
        pe2 = PersonaEngine(db_path=db)
        p = pe2.get("persist_p")
        self.assertIsNotNone(p)
        self.assertEqual(p.system_prompt, "Persist me")

# ════════════════════════════════════════════════════════
# KNOWLEDGE BASE
# ════════════════════════════════════════════════════════
class TestKnowledgeBase(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.knowledge_base import KnowledgeBase
        self.kb = KnowledgeBase(db_path=os.path.join(td, "kb.db"))

    def test_add_fact(self):
        f = self.kb.add_fact("Python", "created_by", "Guido van Rossum")
        self.assertIsNotNone(f.id)
        self.assertEqual(f.entity, "Python")

    def test_get_entity_facts(self):
        self.kb.add_fact("Python", "paradigm", "OOP")
        self.kb.add_fact("Python", "type", "interpreted")
        facts = self.kb.get_entity_facts("Python")
        self.assertGreaterEqual(len(facts), 2)

    def test_get_entity_facts_attribute_filter(self):
        self.kb.add_fact("Python", "paradigm", "OOP")
        self.kb.add_fact("Python", "type", "interpreted")
        facts = self.kb.get_entity_facts("Python", attribute="paradigm")
        self.assertTrue(all(f.attribute == "paradigm" for f in facts))

    def test_fact_confidence(self):
        f = self.kb.add_fact("Earth", "shape", "sphere", confidence=0.99)
        self.assertAlmostEqual(f.confidence, 0.99, places=2)

    def test_update_fact(self):
        f = self.kb.add_fact("Sun", "type", "star")
        updated = self.kb.update_fact(f.id, value="yellow dwarf star", confidence=0.98)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.value, "yellow dwarf star")
        self.assertEqual(updated.version, 2)

    def test_fact_ttl_expiry(self):
        f = self.kb.add_fact("temp", "status", "active", ttl=0.01)
        time.sleep(0.05)
        self.assertTrue(f.expired)
        facts = self.kb.get_entity_facts("temp")
        self.assertEqual(len(facts), 0)

    def test_add_concept(self):
        c = self.kb.add_concept("recursion",
                                  definition="A function calling itself",
                                  synonyms=["recursive","self-referential"],
                                  domain="programming")
        self.assertIsNotNone(c.id)
        self.assertEqual(c.name, "recursion")

    def test_get_concept(self):
        self.kb.add_concept("OOP", definition="Object oriented programming")
        c = self.kb.get_concept("OOP")
        self.assertIsNotNone(c)
        self.assertEqual(c.definition, "Object oriented programming")

    def test_add_concept_idempotent(self):
        self.kb.add_concept("idempotent_test", definition="First")
        c2 = self.kb.add_concept("idempotent_test", definition="Second")
        self.assertEqual(c2.definition, "First")  # original preserved

    def test_link_concepts(self):
        self.kb.add_concept("Python")
        self.kb.add_concept("programming-language")
        rel = self.kb.link("Python", "programming-language", rel_type="is-a")
        self.assertEqual(rel.rel_type, "is-a")

    def test_get_relations(self):
        self.kb.add_concept("A"); self.kb.add_concept("B")
        self.kb.link("A", "B", rel_type="related-to")
        ca = self.kb.get_concept("A")
        rels = self.kb.get_relations(ca.id)
        self.assertGreater(len(rels), 0)

    def test_ancestors(self):
        self.kb.add_concept("Animal")
        self.kb.add_concept("Mammal", parent="Animal")
        self.kb.add_concept("Dog",    parent="Mammal")
        ancestors = self.kb.ancestors("Dog")
        self.assertIn("Mammal", ancestors)
        self.assertIn("Animal", ancestors)

    def test_descendants(self):
        self.kb.add_concept("Vehicle")
        self.kb.add_concept("Car",   parent="Vehicle")
        self.kb.add_concept("Truck", parent="Vehicle")
        children = self.kb.descendants("Vehicle")
        self.assertIn("Car", children)
        self.assertIn("Truck", children)

    def test_search(self):
        self.kb.add_fact("Python", "description", "high level language for data science")
        results = self.kb.search("data science language")
        self.assertGreater(len(results), 0)

    def test_search_concept(self):
        self.kb.add_concept("machine learning",
                              definition="algorithms that learn from data")
        results = self.kb.search("learning algorithms data")
        self.assertGreater(len(results), 0)

    def test_export(self):
        self.kb.add_fact("X","a","b"); self.kb.add_concept("X")
        exp = self.kb.export()
        self.assertIn("facts", exp); self.assertIn("concepts", exp)
        self.assertGreater(len(exp["facts"]), 0)

    def test_stats(self):
        self.kb.add_fact("A","b","c"); self.kb.add_concept("D")
        s = self.kb.stats()
        for k in ["facts","concepts","relationships","total_nodes"]: self.assertIn(k, s)

    def test_fact_to_dict(self):
        f = self.kb.add_fact("E","f","g")
        d = f.to_dict()
        for k in ["id","type","entity","attribute","value","confidence"]: self.assertIn(k, d)

    def test_concept_to_dict(self):
        c = self.kb.add_concept("H", definition="test")
        d = c.to_dict()
        for k in ["id","type","name","definition","domain"]: self.assertIn(k, d)

    def test_relationship_to_dict(self):
        self.kb.add_concept("I"); self.kb.add_concept("J")
        r = self.kb.link("I","J","related-to")
        d = r.to_dict()
        for k in ["id","from","to","type","weight"]: self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# TASK SCHEDULER
# ════════════════════════════════════════════════════════
class TestTaskScheduler(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.task_scheduler import TaskScheduler
        self.sched = TaskScheduler(db_path=os.path.join(td,"ts.db"),
                                    tick_interval=0.05)

    def test_schedule_once(self):
        j = self.sched.schedule_once("test_once", lambda ctx: "ok", delay_s=0)
        self.assertEqual(j.mode, "once")
        self.assertIsNotNone(j.id)

    def test_schedule_interval(self):
        j = self.sched.schedule_interval("test_interval", lambda ctx: None,
                                           interval_s=10.0)
        self.assertEqual(j.mode, "interval")

    def test_schedule_cron(self):
        j = self.sched.schedule_cron("test_cron", lambda ctx: None,
                                      cron_expr="0 12 * * *")
        self.assertEqual(j.mode, "cron")
        self.assertGreater(j.next_run, time.time())

    def test_cancel_job(self):
        j = self.sched.schedule_once("cancel_me", lambda ctx: None, delay_s=60)
        ok = self.sched.cancel(j.id)
        self.assertTrue(ok)
        from agent.task_scheduler import JobState
        self.assertEqual(j.state, JobState.CANCELLED)

    def test_pause_resume_job(self):
        j = self.sched.schedule_interval("pause_me", lambda ctx: None, interval_s=10)
        self.sched.pause_job(j.id)
        from agent.task_scheduler import JobState
        self.assertEqual(j.state, JobState.PAUSED)
        self.sched.resume_job(j.id)
        self.assertEqual(j.state, JobState.PENDING)

    def test_execute_once_job(self):
        results = []
        j = self.sched.schedule_once("exec_once", lambda ctx: results.append(1), delay_s=0)
        _run(self.sched._execute_job(j))
        self.assertEqual(len(results), 1)
        from agent.task_scheduler import JobState
        self.assertEqual(j.state, JobState.COMPLETED)

    def test_execute_interval_reschedules(self):
        j = self.sched.schedule_interval("exec_interval", lambda ctx: None,
                                           interval_s=5.0, run_immediately=True)
        next_before = j.next_run
        _run(self.sched._execute_job(j))
        self.assertGreater(j.next_run, next_before)
        from agent.task_scheduler import JobState
        self.assertEqual(j.state, JobState.PENDING)

    def test_context_passed_to_job(self):
        received = {}
        def fn(ctx): received.update(ctx)
        j = self.sched.schedule_once("ctx_job", fn, delay_s=0, context={"x": 42})
        _run(self.sched._execute_job(j))
        self.assertEqual(received.get("x"), 42)

    def test_async_job(self):
        results = []
        async def async_fn(ctx): await asyncio.sleep(0.01); results.append("async")
        j = self.sched.schedule_once("async_job", async_fn, delay_s=0)
        _run(self.sched._execute_job(j))
        self.assertIn("async", results)

    def test_retry_on_failure(self):
        calls = [0]
        def flaky(ctx):
            calls[0] += 1
            if calls[0] < 2: raise RuntimeError("not yet")
        j = self.sched.schedule_once("flaky_job", flaky, delay_s=0)
        j.max_retries = 2; j.retry_delay = 0.01
        _run(self.sched._execute_job(j))
        self.assertGreaterEqual(calls[0], 2)

    def test_job_history_logged(self):
        j = self.sched.schedule_once("hist_job", lambda ctx: "out", delay_s=0)
        _run(self.sched._execute_job(j))
        self.assertGreater(len(j.history), 0)

    def test_run_count_increments(self):
        j = self.sched.schedule_interval("cnt_job", lambda ctx: None,
                                           interval_s=0.01, run_immediately=True)
        _run(self.sched._execute_job(j))
        _run(self.sched._execute_job(j))
        self.assertEqual(j.run_count, 2)

    def test_scheduler_start_stop(self):
        async def run():
            await self.sched.start()
            await asyncio.sleep(0.1)
            await self.sched.stop()
        _run(run())
        self.assertFalse(self.sched._running)

    def test_scheduler_executes_due_job(self):
        results = []
        self.sched.schedule_once("due_job", lambda ctx: results.append(True), delay_s=0)
        async def run():
            await self.sched.start()
            await asyncio.sleep(0.3)
            await self.sched.stop()
        _run(run())
        self.assertGreater(len(results), 0)

    def test_jobs_list(self):
        self.sched.schedule_once("j1", lambda ctx: None, delay_s=60)
        self.sched.schedule_interval("j2", lambda ctx: None, interval_s=30)
        jobs = self.sched.jobs()
        self.assertGreaterEqual(len(jobs), 2)

    def test_jobs_filter_by_tag(self):
        self.sched.schedule_once("tagged", lambda ctx: None, delay_s=60, tags=["nightly"])
        self.sched.schedule_once("untagged", lambda ctx: None, delay_s=60)
        tagged = self.sched.jobs(tag="nightly")
        self.assertTrue(all("nightly" in j.tags for j in tagged))

    def test_stats(self):
        j = self.sched.schedule_once("s_job", lambda ctx: None, delay_s=0)
        _run(self.sched._execute_job(j))
        s = self.sched.stats()
        for k in ["total_runs","success_rate","total_jobs"]: self.assertIn(k, s)

    def test_job_to_dict(self):
        j = self.sched.schedule_once("dict_job", lambda ctx: None, delay_s=0)
        d = j.to_dict()
        for k in ["id","name","mode","state","priority","run_count"]: self.assertIn(k, d)

    def test_cron_parse_hourly(self):
        from agent.task_scheduler import _parse_cron
        next_t = _parse_cron("0 * * * *")
        self.assertGreater(next_t, time.time())

    def test_next_interval(self):
        from agent.task_scheduler import _next_interval
        now = time.time()
        next_t = _next_interval(now - 5, 10.0)
        self.assertAlmostEqual(next_t, now + 5, delta=2)

# ════════════════════════════════════════════════════════
# FEEDBACK LOOP
# ════════════════════════════════════════════════════════
class TestFeedbackLoop(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.feedback_loop import FeedbackLoop
        self.fl = FeedbackLoop(db_path=os.path.join(td,"fl.db"),
                                low_score_threshold=-0.5)

    def test_annotate_rating(self):
        ann = self.fl.annotate("resp_1", "rating", 4, model_id="gpt-4o")
        self.assertIsNotNone(ann.id)
        self.assertAlmostEqual(ann.reward, 0.5, places=2)  # (4-3)/2

    def test_annotate_thumbs_up(self):
        ann = self.fl.annotate("resp_2", "thumbs", 1)
        self.assertAlmostEqual(ann.reward, 1.0, places=2)

    def test_annotate_thumbs_down(self):
        ann = self.fl.annotate("resp_3", "thumbs", -1)
        self.assertAlmostEqual(ann.reward, -1.0, places=2)

    def test_annotate_label(self):
        ann = self.fl.annotate("resp_4", "label", "helpful")
        self.assertEqual(ann.reward, 0.0)  # labels don't produce reward

    def test_annotate_correction(self):
        ann = self.fl.annotate("resp_5", "correction", "The correct answer is 42")
        self.assertEqual(ann.annotation_type.value, "correction")

    def test_compare(self):
        pair = self.fl.compare(
            prompt="Explain Python",
            response_a="Python is easy to learn.",
            response_b="Python is a programming language.",
            preferred="a", model_a="gpt-4", model_b="claude")
        self.assertEqual(pair.preferred, "a")

    def test_batch_annotate(self):
        items = [{"response_id": f"r{i}", "annotation_type": "rating",
                   "value": 3+i%2, "model_id": "m1"} for i in range(5)]
        anns = self.fl.batch_annotate(items)
        self.assertEqual(len(anns), 5)

    def test_get_annotations_by_response(self):
        self.fl.annotate("target_resp", "rating", 5)
        self.fl.annotate("other_resp",  "rating", 2)
        anns = self.fl.get_annotations(response_id="target_resp")
        self.assertEqual(len(anns), 1)
        self.assertEqual(anns[0].response_id, "target_resp")

    def test_get_annotations_by_model(self):
        self.fl.annotate("r1", "rating", 4, model_id="model-A")
        self.fl.annotate("r2", "rating", 3, model_id="model-B")
        anns = self.fl.get_annotations(model_id="model-A")
        self.assertTrue(all(a.model_id == "model-A" for a in anns))

    def test_avg_reward_rating(self):
        self.fl.annotate("r1", "rating", 5, model_id="m1")
        self.fl.annotate("r2", "rating", 5, model_id="m1")
        avg = self.fl.avg_reward(model_id="m1")
        self.assertAlmostEqual(avg, 1.0, places=2)

    def test_avg_reward_mixed(self):
        self.fl.annotate("r1", "rating", 5, model_id="m2")
        self.fl.annotate("r2", "rating", 1, model_id="m2")
        avg = self.fl.avg_reward(model_id="m2")
        self.assertAlmostEqual(avg, 0.0, places=2)

    def test_avg_reward_thumbs(self):
        self.fl.annotate("r1", "thumbs",  1, model_id="m3")
        self.fl.annotate("r2", "thumbs", -1, model_id="m3")
        avg = self.fl.avg_reward(model_id="m3")
        self.assertAlmostEqual(avg, 0.0, places=2)

    def test_reward_trend_returns_buckets(self):
        self.fl.annotate("r1", "rating", 4)
        trend = self.fl.reward_trend(buckets=4)
        self.assertEqual(len(trend), 4)
        for bucket in trend:
            self.assertIn("avg_reward", bucket)

    def test_model_leaderboard(self):
        self.fl.annotate("r1", "rating", 5, model_id="best_model")
        self.fl.annotate("r2", "rating", 2, model_id="worst_model")
        lb = self.fl.model_leaderboard()
        names = [e["model"] for e in lb]
        self.assertIn("best_model", names)
        self.assertIn("worst_model", names)
        # best_model should rank higher
        best_idx  = names.index("best_model")
        worst_idx = names.index("worst_model")
        self.assertLess(best_idx, worst_idx)

    def test_export_rlhf(self):
        self.fl.compare("Q", "A1", "A2", preferred="a")
        self.fl.compare("Q", "A1", "A2", preferred="b")
        pairs = self.fl.export_rlhf()
        self.assertEqual(len(pairs), 2)
        self.assertIn("prompt", pairs[0])
        self.assertIn("chosen", pairs[0])
        self.assertIn("rejected", pairs[0])

    def test_rlhf_chosen_rejected(self):
        self.fl.compare("Q", "Good answer", "Bad answer", preferred="a")
        pairs = self.fl.export_rlhf()
        self.assertEqual(pairs[0]["chosen"], "Good answer")
        self.assertEqual(pairs[0]["rejected"], "Bad answer")

    def test_alert_hook_triggered(self):
        alerts = []
        self.fl.add_alert_hook(lambda ann: alerts.append(ann.reward))
        self.fl.annotate("r1", "rating", 1)  # reward = -1.0, below threshold
        self.assertGreater(len(alerts), 0)
        self.assertLess(alerts[0], 0)

    def test_stats(self):
        self.fl.annotate("r1", "rating", 4)
        self.fl.annotate("r2", "thumbs", 1)
        s = self.fl.stats()
        for k in ["total_annotations","total_pairs","by_type"]: self.assertIn(k, s)

    def test_to_dict(self):
        ann = self.fl.annotate("r1", "rating", 3)
        d = ann.to_dict()
        for k in ["id","response_id","type","value","reward"]: self.assertIn(k, d)

    def test_preference_pair_to_dict(self):
        pair = self.fl.compare("P","A","B","a")
        d = pair.to_dict()
        for k in ["id","preferred","model_a","model_b"]: self.assertIn(k, d)

    def test_rating_reward_scale(self):
        from agent.feedback_loop import Annotation, AnnotationType
        for rating, expected in [(1,-1.0),(2,-0.5),(3,0.0),(4,0.5),(5,1.0)]:
            ann = Annotation(id="x", response_id="y",
                              annotation_type=AnnotationType.RATING, value=rating)
            self.assertAlmostEqual(ann.reward, expected, places=2)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v24: {total-failed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
