"""OMNI AGENT v16 Tests: PromptOptimizer, KnowledgeDistiller, AdaptiveSampler, AuditLogger"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ═══════════════════════════════════════════════════════════
# PROMPT OPTIMIZER
# ═══════════════════════════════════════════════════════════
class TestPromptOptimizer(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.prompt_optimizer import PromptOptimizer
        self.opt = PromptOptimizer(db_path=os.path.join(td,"po.db"))

    def test_similarity_helpers(self):
        from agent.prompt_optimizer import _jaccard, _trigrams
        self.assertEqual(_jaccard("abc","abc"), 1.0)
        self.assertEqual(_jaccard("abc","xyz"), 0.0)
        self.assertGreater(_jaccard("hello world","hello there"), 0.0)

    def test_optimize_returns_run(self):
        run = _run(self.opt.optimize("Summarise the text.", max_generations=1, population_size=2, survivors=1))
        self.assertIsNotNone(run); self.assertIsNotNone(run.id)

    def test_optimize_has_champion(self):
        run = _run(self.opt.optimize("Translate to French.", max_generations=1, population_size=2, survivors=1))
        self.assertIsNotNone(run.champion)

    def test_optimize_generations_tracked(self):
        run = _run(self.opt.optimize("Write a poem.", max_generations=2, population_size=2, survivors=1))
        self.assertGreaterEqual(run.generations, 1)

    def test_optimize_status_set(self):
        run = _run(self.opt.optimize("Q:", max_generations=1, population_size=2, survivors=1))
        self.assertIn(run.status, ["converged","max_gen","running"])

    def test_optimize_with_scorer(self):
        from agent.prompt_optimizer import PromptOptimizer
        td = tempfile.mkdtemp()
        scorer_calls = []
        def scorer(text, task):
            scorer_calls.append(text); return 7.5
        opt = PromptOptimizer(scorer_fn=scorer, db_path=os.path.join(td,"po2.db"))
        run = _run(opt.optimize("Base prompt.", max_generations=1, population_size=2, survivors=1))
        self.assertGreater(len(scorer_calls), 0)

    def test_optimize_with_llm(self):
        from agent.prompt_optimizer import PromptOptimizer
        td = tempfile.mkdtemp()
        def llm(p): return "Improved: " + p[:50]
        opt = PromptOptimizer(llm_fn=llm, db_path=os.path.join(td,"po3.db"))
        run = _run(opt.optimize("Summarise.", max_generations=1, population_size=2, survivors=1))
        self.assertIsNotNone(run.champion)

    def test_candidates_stored(self):
        run = _run(self.opt.optimize("Test.", max_generations=1, population_size=2, survivors=1))
        self.assertGreater(len(run.candidates), 0)

    def test_to_dict(self):
        run = _run(self.opt.optimize("Test.", max_generations=1, population_size=2, survivors=1))
        d = run.to_dict()
        for k in ["id","seed_prompt","generations","best_score","status","champion"]:
            self.assertIn(k, d)

    def test_list_runs(self):
        _run(self.opt.optimize("P1.", max_generations=1, population_size=2, survivors=1))
        runs = self.opt.list_runs()
        self.assertGreater(len(runs), 0)

    def test_stats(self):
        _run(self.opt.optimize("P.", max_generations=1, population_size=2, survivors=1))
        stats = self.opt.stats()
        self.assertIn("total_runs", stats); self.assertGreaterEqual(stats["total_runs"], 1)

    def test_convergence_status(self):
        run = _run(self.opt.optimize("Prompt.", max_generations=5, population_size=2,
                                      survivors=1, convergence_patience=1, convergence_threshold=100.0))
        self.assertEqual(run.status, "converged")

    def test_mutation_types_used(self):
        from agent.prompt_optimizer import MUTATION_PROMPTS
        self.assertGreater(len(MUTATION_PROMPTS), 3)
        self.assertIn("rephrase", MUTATION_PROMPTS)

    def test_diversity_penalty_applied(self):
        from agent.prompt_optimizer import PromptOptimizer, PromptCandidate
        opt = PromptOptimizer()
        survivors = [PromptCandidate(id="s1", text="hello world test prompt")]
        candidates = [PromptCandidate(id="c1", text="hello world test prompt duplicate", score=9.0),
                      PromptCandidate(id="c2", text="completely different content xyz", score=8.0)]
        penalised = opt._apply_diversity_penalty(candidates, survivors)
        self.assertEqual(len(penalised), 2)

# ═══════════════════════════════════════════════════════════
# KNOWLEDGE DISTILLER
# ═══════════════════════════════════════════════════════════
class TestKnowledgeDistiller(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.knowledge_distiller import KnowledgeDistiller
        self.kd = KnowledgeDistiller(db_path=os.path.join(td,"kd.db"))
        self.text = ("Python is a high-level programming language. "
                     "It was created by Guido van Rossum in 1991. "
                     "Python emphasises code readability. "
                     "Python supports multiple programming paradigms. "
                     "The language has a large standard library.")

    def test_distill_returns_result(self):
        r = _run(self.kd.distill("Python", self.text))
        self.assertIsNotNone(r); self.assertIsNotNone(r.source_id)

    def test_distill_extracts_facts(self):
        r = _run(self.kd.distill("Python", self.text))
        self.assertGreater(len(r.facts), 0)

    def test_distill_has_summary(self):
        r = _run(self.kd.distill("Python", self.text))
        self.assertIsNotNone(r.summary_short)
        self.assertGreater(len(r.summary_short), 0)

    def test_distill_with_llm_facts(self):
        from agent.knowledge_distiller import KnowledgeDistiller
        td = tempfile.mkdtemp()
        def llm(p):
            if "Extract" in p:
                return '[{"text":"Python was created in 1991","subject":"Python","predicate":"created","object":"1991","confidence":0.95,"topic":"technology"}]'
            if "question" in p.lower():
                return '[{"question":"When was Python created?","answer":"1991","confidence":0.95}]'
            return "Python is a programming language created in 1991."
        kd = KnowledgeDistiller(llm_fn=llm, db_path=os.path.join(td,"kd2.db"))
        r = _run(kd.distill("Python History", self.text))
        self.assertGreater(len(r.facts), 0)
        self.assertGreater(len(r.qa_pairs), 0)

    def test_fact_fields(self):
        r = _run(self.kd.distill("Python", self.text))
        for f in r.facts:
            d = f.to_dict()
            for k in ["id","text","confidence","source_id"]: self.assertIn(k, d)

    def test_fact_confidence_range(self):
        r = _run(self.kd.distill("Python", self.text))
        for f in r.facts:
            self.assertGreaterEqual(f.confidence, 0.0)
            self.assertLessEqual(f.confidence, 1.0)

    def test_qa_generated(self):
        r = _run(self.kd.distill("Python", self.text))
        self.assertGreater(len(r.qa_pairs), 0)

    def test_qa_fields(self):
        r = _run(self.kd.distill("Python", self.text))
        for q in r.qa_pairs:
            d = q.to_dict()
            for k in ["id","question","answer","source_id"]: self.assertIn(k, d)

    def test_get_facts(self):
        _run(self.kd.distill("Python", self.text))
        facts = self.kd.get_facts()
        self.assertGreater(len(facts), 0)

    def test_get_facts_min_confidence(self):
        _run(self.kd.distill("Python", self.text))
        facts = self.kd.get_facts(min_confidence=0.8)
        self.assertTrue(all(f.confidence >= 0.8 for f in facts))

    def test_get_qa(self):
        _run(self.kd.distill("Python", self.text))
        qa = self.kd.get_qa()
        self.assertIsInstance(qa, list)

    def test_get_sources(self):
        _run(self.kd.distill("PyDoc", self.text))
        sources = self.kd.get_sources()
        self.assertTrue(any(s["title"] == "PyDoc" for s in sources))

    def test_topics_inferred(self):
        r = _run(self.kd.distill("Python", self.text))
        self.assertIsInstance(r.topics, list)

    def test_contradiction_detection(self):
        from agent.knowledge_distiller import _detect_contradiction, Fact
        f1 = Fact(id="f1", text="Python is fast", subject="Python")
        f2 = Fact(id="f2", text="Python is not fast", subject="Python")
        contradicts = _detect_contradiction(f2, [f1])
        self.assertIn("f1", contradicts)

    def test_no_contradiction_diff_subject(self):
        from agent.knowledge_distiller import _detect_contradiction, Fact
        f1 = Fact(id="f1", text="Python is fast", subject="Python")
        f2 = Fact(id="f2", text="Java is not fast", subject="Java")
        contradicts = _detect_contradiction(f2, [f1])
        self.assertEqual(len(contradicts), 0)

    def test_to_dict(self):
        r = _run(self.kd.distill("Python", self.text))
        d = r.to_dict()
        for k in ["source_id","source_title","fact_count","summary_short"]:
            self.assertIn(k, d)

    def test_stats(self):
        _run(self.kd.distill("Python", self.text))
        s = self.kd.stats()
        for k in ["sources","facts","qa_pairs"]: self.assertIn(k, s)
        self.assertGreaterEqual(s["sources"], 1)

    def test_persistence(self):
        from agent.knowledge_distiller import KnowledgeDistiller
        td = tempfile.mkdtemp(); db = os.path.join(td,"kd.db")
        kd1 = KnowledgeDistiller(db_path=db)
        _run(kd1.distill("Persist", "Content about persistence."))
        kd2 = KnowledgeDistiller(db_path=db)
        facts = kd2.get_facts()
        self.assertGreater(len(facts), 0)

# ═══════════════════════════════════════════════════════════
# ADAPTIVE SAMPLER
# ═══════════════════════════════════════════════════════════
class TestSchedules(unittest.TestCase):
    def test_linear_start(self):
        from agent.adaptive_sampler import schedule_linear
        self.assertAlmostEqual(schedule_linear(0,5,1.0,0.1), 1.0)
    def test_linear_end(self):
        from agent.adaptive_sampler import schedule_linear
        self.assertAlmostEqual(schedule_linear(4,5,1.0,0.1), 0.1)
    def test_cosine_monotone(self):
        from agent.adaptive_sampler import schedule_cosine
        vals = [schedule_cosine(i,5) for i in range(5)]
        self.assertEqual(vals, sorted(vals, reverse=True))
    def test_exponential_positive(self):
        from agent.adaptive_sampler import schedule_exponential
        self.assertGreater(schedule_exponential(0,5), 0)
    def test_single_step(self):
        from agent.adaptive_sampler import schedule_linear
        self.assertEqual(schedule_linear(0,1), 1.0)

class TestDiversityMetrics(unittest.TestCase):
    def test_self_bleu_identical(self):
        from agent.adaptive_sampler import _self_bleu
        self.assertGreater(_self_bleu(["hello world","hello world"]), 0.5)
    def test_self_bleu_different(self):
        from agent.adaptive_sampler import _self_bleu
        self.assertLess(_self_bleu(["cat sat mat","dog ran far"]), 0.5)
    def test_self_bleu_single(self):
        from agent.adaptive_sampler import _self_bleu
        self.assertEqual(_self_bleu(["only one"]), 0.0)
    def test_deduplicate_removes_near_dupes(self):
        from agent.adaptive_sampler import _deduplicate
        texts = ["hello world foo","hello world foo","completely different xyz"]
        result = _deduplicate(texts, threshold=0.9)
        self.assertLess(len(result), len(texts))
    def test_deduplicate_keeps_unique(self):
        from agent.adaptive_sampler import _deduplicate
        texts = ["alpha beta","gamma delta","epsilon zeta"]
        result = _deduplicate(texts, threshold=0.9)
        self.assertEqual(len(result), 3)
    def test_text_entropy_nonzero(self):
        from agent.adaptive_sampler import _text_entropy
        self.assertGreater(_text_entropy("the quick brown fox"), 0)
    def test_text_entropy_empty(self):
        from agent.adaptive_sampler import _text_entropy
        self.assertEqual(_text_entropy(""), 0.0)

class TestAdaptiveSampler(unittest.TestCase):
    def setUp(self):
        from agent.adaptive_sampler import AdaptiveSampler
        self.s = AdaptiveSampler()

    def test_sample_returns_result(self):
        r = _run(self.s.sample("Write a haiku.", n=3))
        self.assertIsNotNone(r); self.assertIsNotNone(r.best)

    def test_sample_count(self):
        r = _run(self.s.sample("Prompt", n=4))
        self.assertLessEqual(len(r.samples), 4)

    def test_top_k(self):
        r = _run(self.s.sample("Prompt", n=5, top_k=2))
        self.assertLessEqual(len(r.samples), 2)

    def test_schedules_available(self):
        from agent.adaptive_sampler import SCHEDULES
        for s in ["linear","cosine","exponential"]: self.assertIn(s, SCHEDULES)

    def test_sample_all_schedules(self):
        for sched in ["linear","cosine","exponential"]:
            r = _run(self.s.sample("Test", n=2, schedule=sched))
            self.assertEqual(r.schedule_used, sched)

    def test_diversity_metric_present(self):
        r = _run(self.s.sample("Story opening", n=3))
        self.assertGreaterEqual(r.diversity, 0.0)
        self.assertLessEqual(r.diversity, 1.0)

    def test_sample_with_llm(self):
        from agent.adaptive_sampler import AdaptiveSampler
        call_count=[0]
        def llm(p): call_count[0]+=1; return f"Response {call_count[0]}"
        s = AdaptiveSampler(llm_fn=llm)
        r = _run(s.sample("Question?", n=3))
        self.assertGreater(call_count[0], 0)

    def test_sample_with_scorer(self):
        from agent.adaptive_sampler import AdaptiveSampler
        def scorer(text, prompt): return 8.0
        s = AdaptiveSampler(scorer_fn=scorer)
        r = _run(s.sample("Q", n=2))
        self.assertTrue(all(sa.score == 8.0 for sa in r.samples))

    def test_samples_ranked(self):
        from agent.adaptive_sampler import AdaptiveSampler
        counter=[0]
        def scorer(text, p): counter[0]+=1; return float(counter[0])
        s = AdaptiveSampler(scorer_fn=scorer)
        r = _run(s.sample("Q", n=3))
        scores = [sa.score for sa in r.samples]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_dedup_threshold(self):
        from agent.adaptive_sampler import AdaptiveSampler
        def llm(p): return "identical response text"
        s = AdaptiveSampler(llm_fn=llm)
        r = _run(s.sample("Q", n=4, dedup_threshold=0.9))
        self.assertLessEqual(len(r.samples), 4)

    def test_to_dict(self):
        r = _run(self.s.sample("Q", n=2))
        d = r.to_dict()
        for k in ["prompt","sample_count","best","diversity","schedule_used"]:
            self.assertIn(k, d)

    def test_history_tracked(self):
        _run(self.s.sample("Q1", n=2)); _run(self.s.sample("Q2", n=2))
        self.assertGreaterEqual(len(self.s.history()), 2)

    def test_stats(self):
        _run(self.s.sample("Q", n=2))
        st = self.s.stats()
        for k in ["total_runs","avg_diversity","avg_best_score"]: self.assertIn(k, st)

    def test_diversity_report(self):
        r = self.s.diversity_report(["hello world", "foo bar baz", "completely different"])
        for k in ["self_bleu","avg_entropy","unique_after_dedup_085","count"]:
            self.assertIn(k, r)

    def test_beam_search(self):
        beams = _run(self.s.beam_search("Once upon a time", beam_width=2, depth=2))
        self.assertGreater(len(beams), 0)

# ═══════════════════════════════════════════════════════════
# AUDIT LOGGER
# ═══════════════════════════════════════════════════════════
class TestAuditLogger(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.audit_logger import AuditLogger
        self.al = AuditLogger(db_path=os.path.join(td,"al.db"), secret_key="test_secret")

    def test_log_returns_id(self):
        eid = self.al.log("login","alice",resource="dashboard")
        self.assertIsNotNone(eid); self.assertGreater(len(eid), 0)

    def test_log_multiple(self):
        self.al.log("login","alice")
        self.al.log("query","alice",resource="reports")
        self.al.log("logout","alice")
        s = self.al.stats()
        self.assertGreaterEqual(s["total_entries"], 3)

    def test_verify_clean_chain(self):
        self.al.log("a1","u1"); self.al.log("a2","u2")
        r = self.al.verify()
        self.assertTrue(r.valid)
        self.assertEqual(r.entries_checked, 2)

    def test_verify_empty(self):
        r = self.al.verify()
        self.assertTrue(r.valid); self.assertEqual(r.entries_checked, 0)

    def test_verify_tampered_chain(self):
        from agent.audit_logger import AuditLogger
        td = tempfile.mkdtemp(); db = os.path.join(td,"al.db")
        al = AuditLogger(db_path=db, secret_key="test")
        al.log("action1","user1"); al.log("action2","user2")
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute("UPDATE audit_log SET action='TAMPERED' WHERE sequence=1")
        conn.commit(); conn.close()
        r = al.verify()
        self.assertFalse(r.valid)
        self.assertIsNotNone(r.first_broken_at)

    def test_query_by_actor(self):
        self.al.log("a1","alice"); self.al.log("a2","bob"); self.al.log("a3","alice")
        entries = self.al.query(actor="alice")
        self.assertTrue(all(e.actor == "alice" for e in entries))
        self.assertEqual(len(entries), 2)

    def test_query_by_action(self):
        self.al.log("login","u1"); self.al.log("logout","u1"); self.al.log("login","u2")
        entries = self.al.query(action="login")
        self.assertTrue(all(e.action == "login" for e in entries))

    def test_query_severity_filter(self):
        self.al.log("info_action","u1",severity="info")
        self.al.log("warn_action","u1",severity="warning")
        self.al.log("err_action","u1",severity="error")
        entries = self.al.query(severity_min="warning")
        sevs = {e.severity for e in entries}
        self.assertNotIn("info", [s.value for s in sevs])

    def test_compliance_tag_filter(self):
        self.al.log("gdpr_action","u1",compliance_tags=["GDPR"])
        self.al.log("hipaa_action","u1",compliance_tags=["HIPAA"])
        entries = self.al.query(compliance_tag="GDPR")
        self.assertTrue(all("GDPR" in e.compliance_tags for e in entries))

    def test_compliance_report(self):
        self.al.log("a1","u1",severity="info")
        self.al.log("a2","u2",severity="error")
        report = self.al.compliance_report()
        for k in ["total_events","by_severity","top_actors","by_outcome"]:
            self.assertIn(k, report)
        self.assertGreaterEqual(report["total_events"], 2)

    def test_lineage_tracking(self):
        lid = "lineage_001"
        self.al.track_lineage(lid, 1, "ingest",   "raw_file.csv",   "parsed_df")
        self.al.track_lineage(lid, 2, "transform","parsed_df",       "cleaned_df")
        self.al.track_lineage(lid, 3, "export",   "cleaned_df",      "output.csv")
        steps = self.al.get_lineage(lid)
        self.assertEqual(len(steps), 3)

    def test_lineage_order(self):
        lid = "order_test"
        self.al.track_lineage(lid,1,"s1","in","out")
        self.al.track_lineage(lid,3,"s3","in","out")
        self.al.track_lineage(lid,2,"s2","in","out")
        steps = self.al.get_lineage(lid)
        self.assertEqual([s["step"] for s in steps], [1,2,3])

    def test_entry_has_hashes(self):
        self.al.log("a","u")
        entries = self.al.query()
        self.assertGreater(len(entries[0].entry_hash), 0)
        self.assertGreater(len(entries[0].chain_hash), 0)

    def test_details_stored(self):
        self.al.log("a","u",details={"user_id":42,"ip":"1.2.3.4"})
        entries = self.al.query(actor="u")
        self.assertEqual(entries[0].details.get("user_id"), 42)

    def test_session_stored(self):
        self.al.log("a","u",session_id="sess_abc")
        entries = self.al.query(actor="u")
        self.assertEqual(entries[0].session_id, "sess_abc")

    def test_to_dict(self):
        self.al.log("a","u")
        entries = self.al.query()
        d = entries[0].to_dict()
        for k in ["id","sequence","action","actor","entry_hash","chain_hash","timestamp"]:
            self.assertIn(k, d)

    def test_stats(self):
        self.al.log("a","u",severity="info")
        self.al.log("b","u",severity="error")
        s = self.al.stats()
        for k in ["total_entries","by_severity"]: self.assertIn(k, s)
        self.assertGreaterEqual(s["total_entries"], 2)

    def test_verify_result_to_dict(self):
        self.al.log("a","u")
        r = self.al.verify()
        d = r.to_dict()
        for k in ["valid","entries_checked","first_broken_at","error"]: self.assertIn(k, d)

    def test_persistence(self):
        from agent.audit_logger import AuditLogger
        td = tempfile.mkdtemp(); db = os.path.join(td,"al.db")
        al1 = AuditLogger(db_path=db, secret_key="test")
        al1.log("persist_action","persist_user")
        al2 = AuditLogger(db_path=db, secret_key="test")
        r = al2.verify(); self.assertTrue(r.valid)
        entries = al2.query(actor="persist_user")
        self.assertEqual(len(entries), 1)

if __name__=="__main__":
    loader=unittest.TestLoader()
    suite=loader.loadTestsFromModule(__import__(__name__))
    runner=unittest.TextTestRunner(verbosity=2)
    result=runner.run(suite)
    total=result.testsRun; failed=len(result.failures)+len(result.errors)
    print(f"\n{'='*60}\n  v16: {total-failed}/{total} passed")
    if failed:
        for t,tb in result.failures+result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
