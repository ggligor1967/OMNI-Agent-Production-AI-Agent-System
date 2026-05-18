"""OMNI AGENT v65: TokenizerV2, PipelineRegistryV2, SecretsManagerV2, ExperimentTrackerV2"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# TOKENIZER V2
# ════════════════════════════════════════════════════════
class TestTokenizerV2(unittest.TestCase):
    def setUp(self):
        from agent.tokenizer_v2 import TokenizerV2
        self.tk = TokenizerV2()

    def test_create_vocab(self):
        v = self.tk.create_vocab("test")
        self.assertIsNotNone(v.vocab_id)
        self.assertGreater(v.size, 0)  # special tokens added

    def test_build_vocab(self):
        corpus = ["hello world", "hello python", "python is great"]
        v = self.tk.build_vocab(corpus, name="corpus")
        self.assertIn("hello", v.token_to_id)
        self.assertIn("python", v.token_to_id)

    def test_vocab_min_freq(self):
        corpus = ["hello world", "hello again", "rare token only once"]
        v = self.tk.build_vocab(corpus, name="freq", min_freq=2)
        self.assertIn("hello", v.token_to_id)
        self.assertNotIn("rare", v.token_to_id)

    def test_word_tokenize(self):
        tokens = self.tk.tokenize("Hello World Python")
        self.assertEqual(tokens, ["hello", "world", "python"])

    def test_char_tokenize(self):
        tokens = self.tk.tokenize("abc", mode="char")
        self.assertEqual(tokens, ["a", "b", "c"])

    def test_encode_decode(self):
        corpus = ["the quick brown fox"]
        v = self.tk.build_vocab(corpus, name="enc")
        ids = self.tk.encode("the quick brown fox", v.vocab_id)
        decoded = self.tk.decode(ids, v.vocab_id)
        self.assertIn("quick", decoded)

    def test_unk_token(self):
        v = self.tk.build_vocab(["hello world"], name="unk")
        ids = self.tk.encode("unknown_word_xyz", v.vocab_id)
        unk_id = v.special_tokens["<unk>"]
        self.assertIn(unk_id, ids)

    def test_bos_eos(self):
        from agent.tokenizer_v2 import TokenizerV2, TokenizerConfig
        cfg = TokenizerConfig(add_bos=True, add_eos=True)
        tk  = TokenizerV2(config=cfg)
        v   = tk.build_vocab(["hello world"], name="beos")
        ids = tk.encode("hello", v.vocab_id)
        self.assertEqual(ids[0], v.special_tokens["<bos>"])
        self.assertEqual(ids[-1], v.special_tokens["<eos>"])

    def test_truncation(self):
        from agent.tokenizer_v2 import TokenizerV2, TokenizerConfig
        cfg = TokenizerConfig(max_length=3, truncation=True)
        tk  = TokenizerV2(config=cfg)
        v   = tk.build_vocab(["a b c d e f"], name="trunc")
        ids = tk.encode("a b c d e f", v.vocab_id)
        self.assertEqual(len(ids), 3)

    def test_padding(self):
        from agent.tokenizer_v2 import TokenizerV2, TokenizerConfig
        cfg = TokenizerConfig(max_length=5, padding=True, truncation=False)
        tk  = TokenizerV2(config=cfg)
        v   = tk.build_vocab(["a b c"], name="pad")
        ids = tk.encode("a", v.vocab_id)
        self.assertEqual(len(ids), 5)

    def test_batch_encode(self):
        v   = self.tk.build_vocab(["hello world foo bar"], name="batch")
        batch = self.tk.encode_batch(["hello", "world"], v.vocab_id)
        self.assertEqual(len(batch), 2)

    def test_batch_decode(self):
        v    = self.tk.build_vocab(["hello world"], name="bdec")
        ids  = self.tk.encode("hello world", v.vocab_id)
        outs = self.tk.decode_batch([ids], v.vocab_id)
        self.assertEqual(len(outs), 1)

    def test_save_load_vocab(self):
        v    = self.tk.build_vocab(["save load test vocab"], name="sl")
        data = self.tk.save_vocab(v.vocab_id)
        v2   = self.tk.load_vocab(data)
        self.assertEqual(v.size, v2.size)

    def test_bpe_training(self):
        corpus = ["low lower lowest", "new newer newest"]
        merges = self.tk.train_bpe(corpus, num_merges=5)
        self.assertIsInstance(merges, list)
        self.assertGreater(len(merges), 0)

    def test_token_frequencies(self):
        corpus = ["hello world hello python"]
        freq   = self.tk.token_frequencies(corpus)
        self.assertEqual(freq.get("hello"), 2)

    def test_stats(self):
        self.tk.build_vocab(["test stats"], name="stats")
        s = self.tk.stats()
        self.assertGreater(s["vocabs"], 0)


# ════════════════════════════════════════════════════════
# PIPELINE REGISTRY V2
# ════════════════════════════════════════════════════════
class TestPipelineRegistryV2(unittest.TestCase):
    def setUp(self):
        from agent.pipeline_registry_v2 import PipelineRegistryV2
        self.pr = PipelineRegistryV2(db_path=":memory:")

    def test_register_pipeline(self):
        p = self.pr.register("etl_pipeline")
        self.assertIsNotNone(p.pipeline_id)

    def test_find_by_name(self):
        self.pr.register("my_pipe", version="1.0")
        p = self.pr.find("my_pipe")
        self.assertIsNotNone(p)

    def test_find_by_version(self):
        self.pr.register("vp", version="2.0")
        p = self.pr.find("vp", version="2.0")
        self.assertIsNotNone(p)

    def test_add_and_execute_steps(self):
        p = self.pr.register("steptest")
        self.pr.add_step(p.pipeline_id, "double", lambda x, ctx: x * 2)
        self.pr.activate(p.pipeline_id)
        run = self.pr.execute(p.pipeline_id, 5)
        self.assertEqual(run.output_data, 10)

    def test_chained_steps(self):
        p = self.pr.register("chain")
        self.pr.add_step(p.pipeline_id, "add1",  lambda x, ctx: x + 1)
        self.pr.add_step(p.pipeline_id, "mul2",  lambda x, ctx: x * 2)
        self.pr.activate(p.pipeline_id)
        run = self.pr.execute(p.pipeline_id, 3)
        self.assertEqual(run.output_data, 8)  # (3+1)*2

    def test_step_error_skip(self):
        p = self.pr.register("errskip")
        self.pr.add_step(p.pipeline_id, "fail",
                          lambda x, ctx: (_ for _ in ()).throw(ValueError("err")),
                          on_error="skip")
        self.pr.add_step(p.pipeline_id, "ok", lambda x, ctx: x + 1)
        self.pr.activate(p.pipeline_id)
        run = self.pr.execute(p.pipeline_id, 5)
        self.assertEqual(run.output_data, 6)

    def test_step_error_default(self):
        p = self.pr.register("errdef")
        self.pr.add_step(p.pipeline_id, "fail",
                          lambda x, ctx: (_ for _ in ()).throw(ValueError("err")),
                          on_error="default", default_value=0)
        self.pr.add_step(p.pipeline_id, "use", lambda x, ctx: x + 10)
        self.pr.activate(p.pipeline_id)
        run = self.pr.execute(p.pipeline_id, 5)
        self.assertEqual(run.output_data, 10)

    def test_step_error_raise(self):
        p = self.pr.register("errraise")
        self.pr.add_step(p.pipeline_id, "fail",
                          lambda x, ctx: (_ for _ in ()).throw(RuntimeError("stop")),
                          on_error="raise")
        self.pr.activate(p.pipeline_id)
        run = self.pr.execute(p.pipeline_id, 1)
        self.assertEqual(run.status, "failed")

    def test_disable_step(self):
        p = self.pr.register("disstep")
        s = self.pr.add_step(p.pipeline_id, "skipped",
                              lambda x, ctx: x * 999)
        self.pr.disable_step(p.pipeline_id, s.step_id)
        self.pr.add_step(p.pipeline_id, "ok", lambda x, ctx: x + 1)
        self.pr.activate(p.pipeline_id)
        run = self.pr.execute(p.pipeline_id, 5)
        self.assertEqual(run.output_data, 6)

    def test_remove_step(self):
        p = self.pr.register("remstep")
        s = self.pr.add_step(p.pipeline_id, "to_remove",
                              lambda x, ctx: x * 99)
        ok = self.pr.remove_step(p.pipeline_id, s.step_id)
        self.assertTrue(ok)
        self.assertEqual(len(p.steps), 0)

    def test_context_passed(self):
        p = self.pr.register("ctxtest")
        self.pr.add_step(p.pipeline_id, "use_ctx",
                          lambda x, ctx: x + ctx["offset"])
        self.pr.activate(p.pipeline_id)
        run = self.pr.execute(p.pipeline_id, 10, context={"offset": 5})
        self.assertEqual(run.output_data, 15)

    def test_disabled_pipeline_raises(self):
        p = self.pr.register("distest")
        self.pr.disable(p.pipeline_id)
        with self.assertRaises(RuntimeError):
            self.pr.execute(p.pipeline_id, 1)

    def test_clone_pipeline(self):
        p  = self.pr.register("orig", version="1.0")
        self.pr.add_step(p.pipeline_id, "s1", lambda x, ctx: x)
        p2 = self.pr.clone(p.pipeline_id, new_version="2.0")
        self.assertEqual(len(p2.steps), len(p.steps))
        self.assertEqual(p2.version, "2.0")

    def test_run_count(self):
        p = self.pr.register("rc_test")
        self.pr.add_step(p.pipeline_id, "noop", lambda x, ctx: x)
        self.pr.activate(p.pipeline_id)
        self.pr.execute(p.pipeline_id, 1)
        self.pr.execute(p.pipeline_id, 2)
        self.assertEqual(p.run_count, 2)

    def test_hooks(self):
        pre_calls = []; post_calls = []
        self.pr.on_before_run(lambda p, r: pre_calls.append(1))
        self.pr.on_after_run(lambda p, r: post_calls.append(1))
        p = self.pr.register("hook_test")
        self.pr.activate(p.pipeline_id)
        self.pr.execute(p.pipeline_id, 1)
        self.assertEqual(len(pre_calls), 1)
        self.assertEqual(len(post_calls), 1)

    def test_list_by_tag(self):
        self.pr.register("tagged_pipe", tags=["ml", "etl"])
        self.pr.register("other_pipe", tags=["data"])
        result = self.pr.list(tag="ml")
        self.assertEqual(len(result), 1)

    def test_stats(self):
        self.pr.register("sp")
        s = self.pr.stats()
        self.assertGreater(s["pipelines"], 0)


# ════════════════════════════════════════════════════════
# SECRETS MANAGER V2
# ════════════════════════════════════════════════════════
class TestSecretsManagerV2(unittest.TestCase):
    def setUp(self):
        from agent.secrets_manager_v2 import SecretsManagerV2
        self.sm = SecretsManagerV2(master_key="testkey123",
                                    db_path=":memory:")

    def test_store_and_get(self):
        e = self.sm.store("db_password", "secret123")
        val = self.sm.get(e.secret_id)
        self.assertEqual(val, "secret123")

    def test_encryption(self):
        e = self.sm.store("enc_key", "mysecretvalue")
        # Encrypted stored value should differ from plaintext
        self.assertNotEqual(e.encrypted_value, "mysecretvalue")

    def test_get_by_name(self):
        self.sm.store("api_key_prod", "sk-abc123")
        val = self.sm.get_by_name("api_key_prod")
        self.assertEqual(val, "sk-abc123")

    def test_update_secret(self):
        e = self.sm.store("to_update", "v1")
        self.sm.update(e.secret_id, "v2")
        val = self.sm.get(e.secret_id)
        self.assertEqual(val, "v2")
        self.assertEqual(e.version, 2)

    def test_version_history(self):
        e = self.sm.store("versioned", "v1", max_versions=5)
        self.sm.update(e.secret_id, "v2")
        self.sm.update(e.secret_id, "v3")
        versions = self.sm.get_versions(e.secret_id)
        self.assertGreaterEqual(len(versions), 2)

    def test_get_specific_version(self):
        e = self.sm.store("spec_ver", "original")
        self.sm.update(e.secret_id, "updated")
        old_val = self.sm.get_version(e.secret_id, 1)
        self.assertEqual(old_val, "original")

    def test_ttl_expiry(self):
        e = self.sm.store("short_lived", "expires", ttl_s=0.01)
        time.sleep(0.02)
        val = self.sm.get(e.secret_id)
        self.assertIsNone(val)

    def test_delete_secret(self):
        e  = self.sm.store("del_me", "value")
        ok = self.sm.delete(e.secret_id)
        self.assertTrue(ok)
        self.assertIsNone(self.sm.get(e.secret_id))

    def test_rotate_secret(self):
        e = self.sm.store("rotatable", "old_value")
        self.sm.rotate(e.secret_id, new_value="new_value")
        val = self.sm.get(e.secret_id)
        self.assertEqual(val, "new_value")
        self.assertEqual(e.version, 2)

    def test_custom_rotator(self):
        e = self.sm.store("auto_rotate", "v0")
        self.sm.register_rotator(e.secret_id, lambda: "auto_rotated_v1")
        self.sm.rotate(e.secret_id)
        val = self.sm.get(e.secret_id)
        self.assertEqual(val, "auto_rotated_v1")

    def test_acl_allowed(self):
        e = self.sm.store("acl_test", "secret",
                           allowed_accessors=["alice", "bob"])
        val = self.sm.get(e.secret_id, accessor="alice")
        self.assertEqual(val, "secret")

    def test_acl_denied(self):
        e = self.sm.store("acl_deny", "secret",
                           allowed_accessors=["alice"])
        val = self.sm.get(e.secret_id, accessor="eve")
        self.assertIsNone(val)

    def test_grant_revoke(self):
        e = self.sm.store("grant_test", "secret",
                           allowed_accessors=["alice"])
        self.sm.grant_access(e.secret_id, "bob")
        self.assertEqual(self.sm.get(e.secret_id, accessor="bob"), "secret")
        self.sm.revoke_access(e.secret_id, "bob")
        self.assertIsNone(self.sm.get(e.secret_id, accessor="bob"))

    def test_generate_token(self):
        tok = self.sm.generate_token(32)
        self.assertEqual(len(tok), 32)

    def test_generate_password(self):
        pwd = self.sm.generate_password(16)
        self.assertEqual(len(pwd), 16)

    def test_generate_api_key(self):
        key = self.sm.generate_api_key("sk")
        self.assertTrue(key.startswith("sk-"))

    def test_audit_log(self):
        e = self.sm.store("audit_s", "val")
        self.sm.get(e.secret_id, accessor="tester")
        log = self.sm.audit_log(e.secret_id)
        self.assertGreater(len(log), 0)

    def test_expire_secrets(self):
        self.sm.store("exp1", "v", ttl_s=0.01)
        time.sleep(0.02)
        expired = self.sm.expire_secrets()
        self.assertGreater(len(expired), 0)

    def test_stats(self):
        self.sm.store("st1", "v1")
        self.sm.store("st2", "v2")
        s = self.sm.stats()
        self.assertGreaterEqual(s["total"], 2)


# ════════════════════════════════════════════════════════
# EXPERIMENT TRACKER V2
# ════════════════════════════════════════════════════════
class TestExperimentTrackerV2(unittest.TestCase):
    def setUp(self):
        from agent.experiment_tracker_v2 import ExperimentTrackerV2
        self.et = ExperimentTrackerV2(db_path=":memory:")

    def test_create_experiment(self):
        exp = self.et.create_experiment("mnist_training")
        self.assertIsNotNone(exp.experiment_id)

    def test_find_experiment(self):
        self.et.create_experiment("find_me")
        exp = self.et.find_experiment("find_me")
        self.assertIsNotNone(exp)

    def test_start_run(self):
        exp = self.et.create_experiment("exp1")
        run = self.et.start_run(exp.experiment_id, run_name="run1")
        from agent.experiment_tracker_v2 import RunStatus
        self.assertEqual(run.status, RunStatus.RUNNING)

    def test_end_run(self):
        from agent.experiment_tracker_v2 import RunStatus
        exp = self.et.create_experiment("exp2")
        run = self.et.start_run(exp.experiment_id)
        self.et.end_run(run.run_id)
        self.assertEqual(run.status, RunStatus.FINISHED)

    def test_fail_run(self):
        from agent.experiment_tracker_v2 import RunStatus
        exp = self.et.create_experiment("exp3")
        run = self.et.start_run(exp.experiment_id)
        self.et.fail_run(run.run_id)
        self.assertEqual(run.status, RunStatus.FAILED)

    def test_log_params(self):
        exp = self.et.create_experiment("p_exp")
        run = self.et.start_run(exp.experiment_id)
        self.et.log_params(run.run_id, {"lr": 0.01, "batch_size": 32})
        self.assertEqual(run.params["lr"], 0.01)

    def test_log_metric(self):
        exp = self.et.create_experiment("m_exp")
        run = self.et.start_run(exp.experiment_id)
        self.et.log_metric(run.run_id, "accuracy", 0.85, step=1)
        self.assertAlmostEqual(run.latest_metric("accuracy"), 0.85)

    def test_log_metrics_steps(self):
        exp = self.et.create_experiment("ms_exp")
        run = self.et.start_run(exp.experiment_id)
        for i in range(5):
            self.et.log_metric(run.run_id, "loss", 1.0 - i * 0.1, step=i)
        history = self.et.get_metric_history(run.run_id, "loss")
        self.assertEqual(len(history), 5)

    def test_log_tags(self):
        exp = self.et.create_experiment("t_exp")
        run = self.et.start_run(exp.experiment_id)
        self.et.log_tag(run.run_id, "framework", "pytorch")
        self.assertEqual(run.tags["framework"], "pytorch")

    def test_log_artifact(self):
        exp = self.et.create_experiment("a_exp")
        run = self.et.start_run(exp.experiment_id)
        art = self.et.log_artifact(run.run_id, "model.pkl",
                                    "/models/run1/model.pkl",
                                    artifact_type="model")
        self.assertEqual(len(run.artifacts), 1)

    def test_nested_runs(self):
        exp = self.et.create_experiment("nested")
        parent = self.et.start_run(exp.experiment_id, run_name="parent")
        child  = self.et.start_run(exp.experiment_id, run_name="child",
                                    parent_run_id=parent.run_id)
        self.assertEqual(child.parent_run_id, parent.run_id)

    def test_best_run_maximize(self):
        from agent.experiment_tracker_v2 import MetricDirection
        exp = self.et.create_experiment("best_exp")
        r1  = self.et.start_run(exp.experiment_id)
        r2  = self.et.start_run(exp.experiment_id)
        self.et.log_metric(r1.run_id, "acc", 0.80)
        self.et.log_metric(r2.run_id, "acc", 0.95)
        best = self.et.best_run(exp.experiment_id, "acc",
                                 MetricDirection.MAXIMIZE)
        self.assertEqual(best.run_id, r2.run_id)

    def test_best_run_minimize(self):
        from agent.experiment_tracker_v2 import MetricDirection
        exp = self.et.create_experiment("min_exp")
        r1  = self.et.start_run(exp.experiment_id)
        r2  = self.et.start_run(exp.experiment_id)
        self.et.log_metric(r1.run_id, "loss", 0.5)
        self.et.log_metric(r2.run_id, "loss", 0.2)
        best = self.et.best_run(exp.experiment_id, "loss",
                                 MetricDirection.MINIMIZE)
        self.assertEqual(best.run_id, r2.run_id)

    def test_compare_runs(self):
        exp = self.et.create_experiment("cmp_exp")
        r1  = self.et.start_run(exp.experiment_id, run_name="r1")
        r2  = self.et.start_run(exp.experiment_id, run_name="r2")
        self.et.log_metric(r1.run_id, "acc", 0.80)
        self.et.log_metric(r2.run_id, "acc", 0.90)
        cmp = self.et.compare_runs([r1.run_id, r2.run_id], metrics=["acc"])
        self.assertEqual(len(cmp), 2)

    def test_group_by_param(self):
        exp = self.et.create_experiment("grp_exp")
        for lr in [0.01, 0.01, 0.001]:
            r = self.et.start_run(exp.experiment_id)
            self.et.log_param(r.run_id, "lr", lr)
            self.et.log_metric(r.run_id, "acc", 0.8 + lr)
        groups = self.et.group_by_param(exp.experiment_id, "lr", "acc")
        self.assertIn(0.01, groups)
        self.assertEqual(len(groups[0.01]), 2)

    def test_search_runs_metric_filter(self):
        exp = self.et.create_experiment("search_exp")
        r1  = self.et.start_run(exp.experiment_id)
        r2  = self.et.start_run(exp.experiment_id)
        self.et.log_metric(r1.run_id, "acc", 0.60)
        self.et.log_metric(r2.run_id, "acc", 0.90)
        found = self.et.search_runs(exp.experiment_id,
                                     metric_filters={"acc": (0.8, 1.0)})
        self.assertEqual(len(found), 1)

    def test_list_runs_filter_status(self):
        from agent.experiment_tracker_v2 import RunStatus
        exp = self.et.create_experiment("list_exp")
        r   = self.et.start_run(exp.experiment_id)
        self.et.end_run(r.run_id)
        runs = self.et.list_runs(exp.experiment_id, status=RunStatus.FINISHED)
        self.assertEqual(len(runs), 1)

    def test_stats(self):
        exp = self.et.create_experiment("stat_exp")
        self.et.start_run(exp.experiment_id)
        s = self.et.stats()
        self.assertGreater(s["experiments"], 0)
        self.assertGreater(s["runs"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v65: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
