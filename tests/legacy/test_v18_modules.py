"""OMNI AGENT v18 Tests: FeedbackCollector, IntentClassifier, ChainOfThought, ModelEnsemble"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# FEEDBACK COLLECTOR
# ════════════════════════════════════════════════════════
class TestFeedbackCollector(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.feedback_collector import FeedbackCollector
        self.fc = FeedbackCollector(db_path=os.path.join(td, "fb.db"))

    def test_submit_returns_item(self):
        fb = self.fc.submit(session_id="s1", rating=5)
        self.assertIsNotNone(fb.id)

    def test_submit_star_rating(self):
        fb = self.fc.submit(rating=4)
        self.assertEqual(fb.rating, 4.0)

    def test_submit_thumbs_up(self):
        fb = self.fc.submit(thumbs="up")
        self.assertEqual(fb.thumbs, "up")

    def test_submit_thumbs_down(self):
        fb = self.fc.submit(thumbs="down")
        self.assertEqual(fb.thumbs, "down")

    def test_submit_comment(self):
        fb = self.fc.submit(comment="Great response, very helpful!")
        self.assertGreater(len(fb.comment), 0)

    def test_positive_sentiment(self):
        fb = self.fc.submit(comment="Great excellent helpful amazing response!")
        self.assertGreater(fb.sentiment, 0)

    def test_negative_sentiment(self):
        fb = self.fc.submit(comment="Terrible wrong useless awful response")
        self.assertLess(fb.sentiment, 0)

    def test_neutral_sentiment(self):
        fb = self.fc.submit(comment="The response was provided.")
        self.assertEqual(fb.sentiment, 0.0)

    def test_get_feedback_by_session(self):
        self.fc.submit(session_id="sess_abc", rating=3)
        self.fc.submit(session_id="sess_abc", rating=5)
        items = self.fc.get_feedback(session_id="sess_abc")
        self.assertEqual(len(items), 2)

    def test_get_feedback_by_model(self):
        self.fc.submit(model="gpt4", rating=4)
        self.fc.submit(model="claude", rating=5)
        items = self.fc.get_feedback(model="gpt4")
        self.assertTrue(all(fb.model == "gpt4" for fb in items))

    def test_stats_returns_data(self):
        self.fc.submit(rating=4, thumbs="up")
        self.fc.submit(rating=2, thumbs="down")
        s = self.fc.stats()
        for k in ["total", "avg_rating", "thumbs_up", "thumbs_down"]: self.assertIn(k, s)

    def test_stats_avg_rating(self):
        self.fc.submit(rating=4); self.fc.submit(rating=2)
        s = self.fc.stats()
        self.assertAlmostEqual(s["avg_rating"], 3.0, places=1)

    def test_satisfaction_rate(self):
        self.fc.submit(thumbs="up"); self.fc.submit(thumbs="up"); self.fc.submit(thumbs="down")
        s = self.fc.stats()
        self.assertAlmostEqual(s["satisfaction_rate"], 2/3, places=2)

    def test_trend_returns_list(self):
        self.fc.submit(rating=4)
        trend = self.fc.trend(days=7)
        self.assertIsInstance(trend, list)

    def test_export_structure(self):
        self.fc.submit(rating=5, comment="Perfect")
        export = self.fc.export()
        for k in ["stats", "trend", "items"]: self.assertIn(k, export)

    def test_to_dict(self):
        fb = self.fc.submit(rating=4, comment="Good")
        d = fb.to_dict()
        for k in ["id", "session_id", "rating", "thumbs", "comment", "sentiment"]:
            self.assertIn(k, d)

    def test_categories_stored(self):
        fb = self.fc.submit(categories=["helpfulness", "accuracy"])
        self.assertIn("helpfulness", fb.categories)

    def test_anomaly_no_drop(self):
        for _ in range(3): self.fc.submit(rating=5)
        result = self.fc.anomaly_check(threshold_drop=1.0)
        # Either None (no anomaly) or dict — just check it doesn't crash
        self.assertTrue(result is None or isinstance(result, dict))

    def test_persistence(self):
        from agent.feedback_collector import FeedbackCollector
        td = tempfile.mkdtemp()
        fc1 = FeedbackCollector(db_path=os.path.join(td, "fb.db"))
        fc1.submit(session_id="persist", rating=5)
        fc2 = FeedbackCollector(db_path=os.path.join(td, "fb.db"))
        items = fc2.get_feedback(session_id="persist")
        self.assertEqual(len(items), 1)

# ════════════════════════════════════════════════════════
# INTENT CLASSIFIER
# ════════════════════════════════════════════════════════
class TestIntentClassifier(unittest.TestCase):
    def setUp(self):
        from agent.intent_classifier import IntentClassifier
        self.clf = IntentClassifier(threshold=0.1)
        self.clf.register("greeting",   "User says hello",      examples=["Hi there", "Hello"])
        self.clf.register("complaint",  "User reports problem",  examples=["It's broken", "Not working"])
        self.clf.register("question",   "User asks for info",    examples=["How do I", "What is"])
        self.clf.register("goodbye",    "User says goodbye",     examples=["Bye", "See you"])

    def test_classify_returns_result(self):
        r = _run(self.clf.classify("Hello there!"))
        self.assertIsNotNone(r)

    def test_top_label_set(self):
        r = _run(self.clf.classify("Hi!"))
        self.assertIsNotNone(r.top_label)

    def test_top_score_range(self):
        r = _run(self.clf.classify("How do I reset my password?"))
        self.assertGreaterEqual(r.top_score, 0.0)
        self.assertLessEqual(r.top_score, 1.0)

    def test_all_scores_present(self):
        r = _run(self.clf.classify("Hello!"))
        self.assertGreater(len(r.all_scores), 0)

    def test_scores_sum_approx_one(self):
        r = _run(self.clf.classify("Test text"))
        total = sum(r.all_scores.values())
        self.assertAlmostEqual(total, 1.0, places=3)

    def test_classify_with_llm(self):
        from agent.intent_classifier import IntentClassifier
        def llm(p): return '{"scores": {"greeting": 0.9, "complaint": 0.05, "question": 0.03, "goodbye": 0.02}}'
        clf = IntentClassifier(llm_fn=llm, threshold=0.3)
        clf.register("greeting", "User greets", examples=["Hi"])
        clf.register("complaint", "User complains", examples=["Broken"])
        clf.register("question", "User asks", examples=["How?"])
        clf.register("goodbye", "User leaves", examples=["Bye"])
        r = _run(clf.classify("Hello there!"))
        self.assertEqual(r.top_label, "greeting")

    def test_fallback_below_threshold(self):
        from agent.intent_classifier import IntentClassifier
        clf = IntentClassifier(threshold=0.99, fallback_label="unknown")
        clf.register("greeting", "User greets")
        clf.register("complaint", "User complains")
        r = _run(clf.classify("asdfghjkl xyz"))
        self.assertTrue(r.is_fallback)

    def test_multi_label(self):
        r = _run(self.clf.classify("Hello, I have a question"))
        self.assertIsInstance(r.labels_above_threshold, list)

    def test_batch_classify(self):
        results = _run(self.clf.batch_classify(["Hello!", "Goodbye!", "How does this work?"]))
        self.assertEqual(len(results), 3)
        self.assertTrue(all(hasattr(r, "top_label") for r in results))

    def test_register_new_intent(self):
        self.clf.register("thanks", "User expresses gratitude", examples=["Thank you!"])
        self.assertIn("thanks", self.clf._intents)

    def test_unregister_intent(self):
        self.clf.register("temp", "Temporary intent")
        ok = self.clf.unregister("temp")
        self.assertTrue(ok)
        self.assertNotIn("temp", self.clf._intents)

    def test_record_actual(self):
        _run(self.clf.classify("Hi there!"))
        self.clf.record_actual("Hi there!", "greeting")
        self.assertGreater(len(self.clf._confusion), 0)

    def test_accuracy_after_record(self):
        _run(self.clf.classify("Hello there!"))
        self.clf.record_actual("Hello there!", "greeting")
        acc = self.clf.accuracy()
        self.assertIsNotNone(acc)

    def test_confusion_matrix_shape(self):
        cm = self.clf.confusion_matrix()
        self.assertIsInstance(cm, dict)

    def test_stats(self):
        _run(self.clf.classify("Hi"))
        s = self.clf.stats()
        for k in ["registered_intents", "total_classified", "fallback_rate"]:
            self.assertIn(k, s)

    def test_history_tracked(self):
        _run(self.clf.classify("Hello")); _run(self.clf.classify("Goodbye"))
        self.assertGreaterEqual(len(self.clf.history()), 2)

    def test_to_dict(self):
        r = _run(self.clf.classify("Hi there!"))
        d = r.to_dict()
        for k in ["text", "top_label", "top_score", "all_scores", "is_fallback"]:
            self.assertIn(k, d)

    def test_keyword_scoring_fallback(self):
        from agent.intent_classifier import _keyword_score, Intent
        intent = Intent(name="test", description="hello world greeting",
                         examples=["Hello there", "Hi world"])
        score = _keyword_score("Hello world", intent)
        self.assertGreater(score, 0)

    def test_softmax_sums_to_one(self):
        from agent.intent_classifier import _softmax
        result = _softmax({"a": 2.0, "b": 1.0, "c": 0.5})
        self.assertAlmostEqual(sum(result.values()), 1.0, places=4)

# ════════════════════════════════════════════════════════
# CHAIN OF THOUGHT
# ════════════════════════════════════════════════════════
class TestChainOfThought(unittest.TestCase):
    def setUp(self):
        from agent.chain_of_thought import ChainOfThought
        self.cot = ChainOfThought(verify_steps=False)

    def test_reason_returns_trace(self):
        trace = _run(self.cot.reason("What is 2+2?"))
        self.assertIsNotNone(trace)

    def test_trace_has_steps(self):
        trace = _run(self.cot.reason("Explain gravity."))
        self.assertGreater(len(trace.steps), 0)

    def test_trace_has_conclusion(self):
        trace = _run(self.cot.reason("Why is the sky blue?"))
        self.assertIsNotNone(trace.conclusion)

    def test_trace_has_confidence(self):
        trace = _run(self.cot.reason("Solve a problem."))
        self.assertGreaterEqual(trace.final_confidence, 0.0)
        self.assertLessEqual(trace.final_confidence, 1.0)

    def test_trace_status(self):
        trace = _run(self.cot.reason("Test problem."))
        self.assertEqual(trace.status, "completed")

    def test_styles(self):
        for style in ["step_by_step", "pros_cons", "socratic", "hypothesis"]:
            trace = _run(self.cot.reason("Test.", style=style))
            self.assertGreater(len(trace.steps), 0)

    def test_with_llm(self):
        from agent.chain_of_thought import ChainOfThought
        def llm(p):
            return '[{"step_type":"think","content":"Analysing...","confidence":0.9},{"step_type":"conclude","content":"Answer is X","confidence":0.85}]'
        cot = ChainOfThought(llm_fn=llm, verify_steps=False)
        trace = _run(cot.reason("What is the answer?"))
        self.assertGreater(len(trace.steps), 0)

    def test_with_verification(self):
        from agent.chain_of_thought import ChainOfThought
        verify_calls = []
        def llm(p):
            if "Evaluate this reasoning" in p:
                verify_calls.append(1)
                return '{"valid": true, "confidence": 0.85, "reason": "Looks good"}'
            return '[{"step_type":"think","content":"Step 1","confidence":0.8},{"step_type":"conclude","content":"Done","confidence":0.9}]'
        cot = ChainOfThought(llm_fn=llm, verify_steps=True)
        trace = _run(cot.reason("Problem"))
        self.assertGreater(len(verify_calls), 0)

    def test_max_depth_respected(self):
        from agent.chain_of_thought import ChainOfThought
        def llm(p): return '[' + ','.join([f'{{"step_type":"think","content":"Step {i}","confidence":0.8}}' for i in range(20)]) + ']'
        cot = ChainOfThought(llm_fn=llm, max_depth=5, verify_steps=False)
        trace = _run(cot.reason("Deep problem"))
        self.assertLessEqual(len(trace.steps), 6)  # max_depth + possible backtrack

    def test_contradiction_detection(self):
        from agent.chain_of_thought import _check_contradiction, ReasoningStep, StepType
        s1 = ReasoningStep("s1", 1, StepType.THINK, "Python is fast", valid=True)
        s2 = ReasoningStep("s2", 2, StepType.THINK, "Python is not fast")
        contradicts = _check_contradiction(s2, [s1])
        self.assertIn(1, contradicts)

    def test_no_contradiction_diff_subject(self):
        from agent.chain_of_thought import _check_contradiction, ReasoningStep, StepType
        s1 = ReasoningStep("s1", 1, StepType.THINK, "Python is fast and efficient", valid=True)
        s2 = ReasoningStep("s2", 2, StepType.THINK, "Java is not fast")
        contradicts = _check_contradiction(s2, [s1])
        self.assertEqual(len(contradicts), 0)

    def test_valid_steps_filter(self):
        trace = _run(self.cot.reason("Problem"))
        valid = trace.valid_steps
        self.assertTrue(all(s.valid for s in valid))

    def test_text_trace(self):
        trace = _run(self.cot.reason("Test"))
        text = trace.text_trace()
        self.assertIn("Problem:", text)

    def test_verify_answer_no_llm(self):
        result = _run(self.cot.verify_answer("What is 2+2?", "4"))
        self.assertIn("confidence", result)

    def test_verify_answer_with_llm(self):
        from agent.chain_of_thought import ChainOfThought
        def llm(p): return '{"correct": true, "confidence": 0.95, "reasoning": "Yes", "issues": []}'
        cot = ChainOfThought(llm_fn=llm)
        result = _run(cot.verify_answer("2+2", "4"))
        self.assertEqual(result["correct"], True)

    def test_history_tracked(self):
        _run(self.cot.reason("Q1")); _run(self.cot.reason("Q2"))
        self.assertGreaterEqual(len(self.cot.history()), 2)

    def test_stats(self):
        _run(self.cot.reason("Q"))
        s = self.cot.stats()
        for k in ["total_traces", "avg_valid_steps", "avg_confidence"]:
            self.assertIn(k, s)

    def test_to_dict(self):
        trace = _run(self.cot.reason("Q"))
        d = trace.to_dict()
        for k in ["id", "problem", "steps", "conclusion", "final_confidence", "status"]:
            self.assertIn(k, d)

    def test_step_to_dict(self):
        trace = _run(self.cot.reason("Q"))
        self.assertGreater(len(trace.steps), 0)
        d = trace.steps[0].to_dict()
        for k in ["id", "step_num", "step_type", "content", "confidence"]:
            self.assertIn(k, d)

# ════════════════════════════════════════════════════════
# MODEL ENSEMBLE
# ════════════════════════════════════════════════════════
class TestModelEnsemble(unittest.TestCase):
    def setUp(self):
        from agent.model_ensemble import ModelEnsemble
        self.ens = ModelEnsemble(disagreement_threshold=0.3, timeout_s=5.0)

    def _add_const(self, name, val):
        return self.ens.add_member(name, lambda p, v=val: v)

    def test_add_member(self):
        m = self._add_const("m1", "answer"); self.assertIsNotNone(m)

    def test_query_returns_result(self):
        self._add_const("a", "Paris")
        r = _run(self.ens.query("Capital of France?"))
        self.assertIsNotNone(r)

    def test_majority_vote(self):
        self._add_const("m1", "Paris"); self._add_const("m2", "Paris"); self._add_const("m3", "Lyon")
        r = _run(self.ens.query("Capital?", strategy="majority_vote"))
        self.assertIn("Paris", r.final_answer)

    def test_weighted_vote(self):
        from agent.model_ensemble import ModelEnsemble
        ens = ModelEnsemble()
        ens.add_member("heavy", lambda p: "Paris", weight=3.0)
        ens.add_member("light", lambda p: "Lyon",  weight=1.0)
        r = _run(ens.query("Capital?", strategy="weighted_vote"))
        self.assertIn("Paris", r.final_answer)

    def test_best_of_n(self):
        from agent.model_ensemble import ModelEnsemble
        ens = ModelEnsemble()
        ens.add_member("lo", lambda p: '{"confidence":0.3,"answer":"wrong"}')
        ens.add_member("hi", lambda p: '{"confidence":0.95,"answer":"Paris"}')
        r = _run(ens.query("Q", strategy="best_of_n"))
        self.assertIsNotNone(r.final_answer)

    def test_confidence_weighted(self):
        self._add_const("a", "A"); self._add_const("b", "B")
        r = _run(self.ens.query("Q", strategy="confidence_weighted"))
        self.assertIsNotNone(r.final_answer)

    def test_unanimous_agree(self):
        self._add_const("a", "Paris"); self._add_const("b", "Paris")
        r = _run(self.ens.query("Q", strategy="unanimous"))
        self.assertIn("Paris", r.final_answer)

    def test_fallback_chain(self):
        from agent.model_ensemble import ModelEnsemble
        ens = ModelEnsemble()
        def fail(p): raise RuntimeError("fail")
        ens.add_member("fail", fail, priority=10)
        ens.add_member("ok",   lambda p: "Success", priority=5)
        r = _run(ens.query("Q", strategy="fallback_chain"))
        self.assertIn("Success", r.final_answer)

    def test_hedge_strategy(self):
        self._add_const("a", "Answer A different words here")
        self._add_const("b", "Answer B completely different text")
        r = _run(self.ens.query("Q", strategy="hedge"))
        self.assertIsNotNone(r.final_answer)

    def test_agreement_score(self):
        self._add_const("a", "Paris"); self._add_const("b", "Paris")
        r = _run(self.ens.query("Q"))
        self.assertGreaterEqual(r.agreement_score, 0.0)
        self.assertLessEqual(r.agreement_score, 1.0)

    def test_disagreement_flag_high_variance(self):
        from agent.model_ensemble import ModelEnsemble
        ens = ModelEnsemble(disagreement_threshold=0.9)
        ens.add_member("a", lambda p: "completely different alpha beta")
        ens.add_member("b", lambda p: "totally unrelated gamma delta epsilon")
        r = _run(ens.query("Q"))
        self.assertTrue(r.disagreement_flag)

    def test_member_stats_updated(self):
        m = self.ens.add_member("stat_m", lambda p: "val")
        _run(self.ens.query("Q"))
        self.assertEqual(m.total_calls, 1)

    def test_remove_member(self):
        m = self._add_const("removable", "val")
        ok = self.ens.remove_member("removable")
        self.assertTrue(ok)
        self.assertNotIn(m.id, self.ens._members)

    def test_deactivate_member(self):
        m = self._add_const("inactive", "should not appear")
        self.ens.deactivate("inactive")
        r = _run(self.ens.query("Q"))
        self.assertTrue(all(mo.member_name != "inactive" for mo in r.member_outputs))

    def test_async_member(self):
        async def afn(p): await asyncio.sleep(0.01); return "async_ok"
        self.ens.add_member("async_m", afn)
        r = _run(self.ens.query("Q"))
        self.assertTrue(any(mo.text == "async_ok" for mo in r.member_outputs if mo.success))

    def test_error_handled(self):
        def bad(p): raise ValueError("oops")
        self.ens.add_member("bad_m", bad)
        self._add_const("good_m", "ok")
        r = _run(self.ens.query("Q"))
        self.assertIsNotNone(r.final_answer)

    def test_agreement_score_identical(self):
        from agent.model_ensemble import _agreement_score
        self.assertAlmostEqual(_agreement_score(["hello world", "hello world"]), 1.0)

    def test_agreement_score_different(self):
        from agent.model_ensemble import _agreement_score
        score = _agreement_score(["cat sat on mat", "dog ran down road"])
        self.assertLess(score, 0.5)

    def test_stats(self):
        self._add_const("s", "v")
        _run(self.ens.query("Q"))
        s = self.ens.stats()
        for k in ["member_count", "total_queries", "avg_agreement"]: self.assertIn(k, s)

    def test_to_dict(self):
        self._add_const("d", "v")
        r = _run(self.ens.query("Q"))
        d = r.to_dict()
        for k in ["prompt", "strategy", "final_answer", "agreement_score", "members"]:
            self.assertIn(k, d)

    def test_history(self):
        self._add_const("h", "v")
        _run(self.ens.query("Q1")); _run(self.ens.query("Q2"))
        self.assertGreaterEqual(len(self.ens.history()), 2)

    def test_extract_confidence(self):
        from agent.model_ensemble import _extract_confidence
        self.assertAlmostEqual(_extract_confidence('{"confidence": 0.87}'), 0.87)

    def test_extract_confidence_missing(self):
        from agent.model_ensemble import _extract_confidence
        self.assertIsNone(_extract_confidence("no confidence here"))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v18: {total-failed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
