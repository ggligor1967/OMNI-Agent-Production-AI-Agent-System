"""
OMNI AGENT v7 — Test Suite
Tests: Auth, Sandbox, Notifications, RateLimiter, Webhooks, Jobs, CronSchedule
Run: python3 tests/test_v7_modules.py
"""
import asyncio, os, sys, tempfile, time, unittest, json, hmac, hashlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ══════════════════════════════════════════════════════════════════════════════
# AUTH TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestAuthRoles(unittest.TestCase):
    def test_roles_defined(self):
        from agent.auth import Role
        self.assertIn("admin", [r.value for r in Role])
        self.assertIn("developer", [r.value for r in Role])
        self.assertIn("user", [r.value for r in Role])

    def test_role_ordering(self):
        from agent.auth import Role
        roles = list(Role)
        self.assertGreater(len(roles), 2)


class TestAPIKeyManager(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.auth import AuthManager, Role
        self.auth = AuthManager(secret="test_secret_32chars_xxxxxxxxxx",
                                db_path=os.path.join(self.tmpdir, "auth.db"))
        self.Role = Role

    def test_create_key(self):
        key, info = self.auth.create_api_key("user1", self.Role.DEVELOPER, "Test key")
        self.assertTrue(key.startswith("omni_"))
        self.assertIsNotNone(info.key_id)
        self.assertEqual(info.role, self.Role.DEVELOPER)

    def test_validate_valid_key(self):
        key, info = self.auth.create_api_key("user1", self.Role.DEVELOPER)
        ctx = self.auth.authenticate(api_key=key)
        self.assertTrue(ctx.authenticated)
        self.assertEqual(ctx.user_id, "user1")

    def test_validate_invalid_key(self):
        ctx = self.auth.authenticate(api_key="omni_invalid_key_123")
        self.assertFalse(ctx.authenticated)

    def test_revoke_key(self):
        key, info = self.auth.create_api_key("user1", self.Role.USER)
        self.auth.revoke_key(info.key_id)
        ctx = self.auth.authenticate(api_key=key)
        self.assertFalse(ctx.authenticated)

    def test_list_keys(self):
        self.auth.create_api_key("user1", self.Role.DEVELOPER)
        self.auth.create_api_key("user2", self.Role.USER)
        keys = self.auth.list_keys()
        self.assertGreaterEqual(len(keys), 2)

    def test_key_not_stored_plaintext(self):
        key, info = self.auth.create_api_key("user1", self.Role.ADMIN)
        keys = self.auth.list_keys()
        matching = [k for k in keys if k.get("key_id") == info.key_id]
        if matching:
            self.assertNotIn(key, str(matching[0]))


class TestJWTTokens(unittest.TestCase):
    def setUp(self):
        from agent.auth import AuthManager, Role, create_jwt, verify_jwt
        self.secret = "test_secret_32chars_xxxxxxxxxx"
        self.auth = AuthManager(secret=self.secret)
        self.Role = Role
        self.create_jwt = create_jwt
        self.verify_jwt = verify_jwt

    def test_create_and_verify(self):
        token = self.create_jwt({"sub": "user1", "role": "developer"}, self.secret)
        payload = self.verify_jwt(token, self.secret)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["sub"], "user1")
        self.assertEqual(payload["role"], "developer")

    def test_expired_token(self):
        token = self.create_jwt({"sub": "user1"}, self.secret, expires_in=-1)
        payload = self.verify_jwt(token, self.secret)
        self.assertIsNone(payload)

    def test_tampered_token(self):
        token = self.create_jwt({"sub": "user1", "role": "admin"}, self.secret)
        parts = token.split(".")
        if len(parts) == 3:
            tampered = parts[0] + "." + parts[1] + ".invalidsig"
            self.assertIsNone(self.verify_jwt(tampered, self.secret))

    def test_invalid_token_format(self):
        self.assertIsNone(self.verify_jwt("not.a.jwt", self.secret))
        self.assertIsNone(self.verify_jwt("", self.secret))


# ══════════════════════════════════════════════════════════════════════════════
# SANDBOX TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestSandboxAST(unittest.TestCase):
    def setUp(self):
        from agent.sandbox import scan_code
        self.scan = scan_code

    def test_safe_code_passes(self):
        issues = self.scan("x = 1 + 2\nprint(x)")
        self.assertEqual(len(issues), 0)

    def test_blocked_import(self):
        issues = self.scan("import os\nos.system('rm -rf /')")
        self.assertGreater(len(issues), 0)

    def test_blocked_import_from(self):
        issues = self.scan("from subprocess import run")
        self.assertGreater(len(issues), 0)

    def test_open_file_blocked(self):
        issues = self.scan("open('/etc/passwd').read()")
        self.assertGreater(len(issues), 0)

    def test_exec_blocked(self):
        issues = self.scan("exec('import os')")
        self.assertGreater(len(issues), 0)

    def test_math_allowed(self):
        issues = self.scan("import math\nresult = math.sqrt(16)\nprint(result)")
        self.assertEqual(len(issues), 0)

    def test_list_comprehension_allowed(self):
        issues = self.scan("[x**2 for x in range(10)]")
        self.assertEqual(len(issues), 0)


class TestCodeSandbox(unittest.TestCase):
    def setUp(self):
        from agent.sandbox import Sandbox
        self.sandbox = Sandbox(max_seconds=5.0)

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_basic_execution(self):
        result = self._run(self.sandbox.run_python("print('hello')"))
        self.assertTrue(result.success)
        self.assertIn("hello", result.stdout)

    def test_math_computation(self):
        result = self._run(self.sandbox.run_python("print(2 ** 10)"))
        self.assertTrue(result.success)
        self.assertIn("1024", result.stdout)

    def test_syntax_error(self):
        result = self._run(self.sandbox.run_python("def broken(:"))
        self.assertFalse(result.success)

    def test_unsafe_code_blocked(self):
        result = self._run(self.sandbox.run_python("import os; os.system('ls')"))
        self.assertFalse(result.success)

    def test_timeout_enforced(self):
        from agent.sandbox import Sandbox
        sandbox = Sandbox(max_seconds=0.5)
        result = self._run(sandbox.run_python("import time; time.sleep(99)"))
        self.assertFalse(result.success)

    def test_output_captured(self):
        result = self._run(self.sandbox.run_python(
            "\n".join(f'print("line {i}")' for i in range(3))))
        self.assertTrue(result.success)
        for i in range(3):
            self.assertIn(f"line {i}", result.stdout)

    def test_execution_history(self):
        self._run(self.sandbox.run_python("x = 42"))
        self._run(self.sandbox.run_python("y = 43"))
        history = self.sandbox.get_history()
        self.assertGreaterEqual(len(history), 2)


# ══════════════════════════════════════════════════════════════════════════════
# RATE LIMITER TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestRatePolicy(unittest.TestCase):
    def test_effective_limit_with_burst(self):
        from agent.rate_limiter import RatePolicy
        p = RatePolicy("test", requests=10, window_s=60, burst=5)
        self.assertEqual(p.effective_limit, 15)

    def test_effective_limit_no_burst(self):
        from agent.rate_limiter import RatePolicy
        p = RatePolicy("test", requests=10, window_s=60)
        self.assertEqual(p.effective_limit, 10)


class TestRateLimiter(unittest.TestCase):
    def setUp(self):
        from agent.rate_limiter import RateLimiter, RatePolicy
        self.rl = RateLimiter(backend="memory")
        self.rl.register(RatePolicy("strict", requests=3, window_s=60, burst=0, hard_limit=True))
        self.rl.register(RatePolicy("soft",   requests=3, window_s=60, burst=0, hard_limit=False))

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_allows_within_limit(self):
        for _ in range(3):
            result = self._run(self.rl.check("strict", "user:a"))
            self.assertTrue(result.allowed)

    def test_blocks_at_limit(self):
        for _ in range(3):
            self._run(self.rl.check("strict", "user:b"))
        result = self._run(self.rl.check("strict", "user:b"))
        self.assertFalse(result.allowed)

    def test_soft_limit_allows_even_over(self):
        for _ in range(5):
            result = self._run(self.rl.check("soft", "user:c"))
            self.assertTrue(result.allowed)

    def test_remaining_decrements(self):
        r1 = self._run(self.rl.check("strict", "user:d"))
        r2 = self._run(self.rl.check("strict", "user:d"))
        self.assertGreater(r1.remaining, r2.remaining)

    def test_different_keys_independent(self):
        for _ in range(3):
            self._run(self.rl.check("strict", "user:e1"))
        r = self._run(self.rl.check("strict", "user:e2"))
        self.assertTrue(r.allowed)

    def test_reset_clears_counter(self):
        for _ in range(3):
            self._run(self.rl.check("strict", "user:f"))
        self._run(self.rl.reset("strict", "user:f"))
        r = self._run(self.rl.check("strict", "user:f"))
        self.assertTrue(r.allowed)

    def test_check_multi(self):
        from agent.rate_limiter import RatePolicy
        self.rl.register(RatePolicy("p1", requests=10, window_s=60))
        self.rl.register(RatePolicy("p2", requests=10, window_s=60))
        results = self._run(self.rl.check_multi([("p1","key:x"), ("p2","key:x")]))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.allowed for r in results))

    def test_unknown_policy_fails_open(self):
        r = self._run(self.rl.check("nonexistent_policy", "key"))
        self.assertTrue(r.allowed)

    def test_headers_format(self):
        r = self._run(self.rl.check("strict", "user:g"))
        headers = r.headers()
        self.assertIn("X-RateLimit-Limit", headers)
        self.assertIn("X-RateLimit-Remaining", headers)
        self.assertIn("X-RateLimit-Reset", headers)

    def test_retry_after_on_block(self):
        for _ in range(3):
            self._run(self.rl.check("strict", "user:h"))
        r = self._run(self.rl.check("strict", "user:h"))
        self.assertFalse(r.allowed)
        self.assertGreater(r.retry_after_s, 0)
        self.assertIn("Retry-After", r.headers())

    def test_stats(self):
        self._run(self.rl.check("strict", "user:i"))
        stats = self.rl.stats()
        self.assertIn("total_events", stats)
        self.assertGreater(stats["total_events"], 0)

    def test_recent_events(self):
        self._run(self.rl.check("strict", "user:j"))
        events = self.rl.recent_events(limit=10)
        self.assertGreater(len(events), 0)

    def test_get_usage(self):
        self._run(self.rl.check("strict", "user:k"))
        usage = self._run(self.rl.get_usage("strict", "user:k"))
        self.assertGreaterEqual(usage["used"], 1)
        self.assertIn("remaining", usage)

    def test_list_policies(self):
        policies = self.rl.list_policies()
        names = {p["name"] for p in policies}
        self.assertIn("strict", names)
        self.assertIn("soft", names)

    def test_burst_allows_extra(self):
        from agent.rate_limiter import RatePolicy
        self.rl.register(RatePolicy("bursting", requests=2, window_s=60, burst=3))
        # Should allow 5 total (2 + 3 burst)
        results = [self._run(self.rl.check("bursting", "user:burst")) for _ in range(5)]
        self.assertTrue(all(r.allowed for r in results))
        r6 = self._run(self.rl.check("bursting", "user:burst"))
        self.assertFalse(r6.allowed)

    def test_convenience_check_user(self):
        r = self._run(self.rl.check_user("my_user", "api_default"))
        self.assertIsNotNone(r)

    def test_convenience_check_ip(self):
        r = self._run(self.rl.check_ip("192.168.1.1"))
        self.assertIsNotNone(r)

    def test_convenience_check_model(self):
        r = self._run(self.rl.check_model("gpt-4", "user_999"))
        self.assertIsNotNone(r)


class TestRateResult(unittest.TestCase):
    def test_to_dict(self):
        from agent.rate_limiter import RateResult
        r = RateResult(True, "api", "user:x", 5, 60, 55, 30.0)
        d = r.to_dict()
        self.assertIn("allowed", d)
        self.assertIn("remaining", d)
        self.assertTrue(d["allowed"])

    def test_headers_allowed(self):
        from agent.rate_limiter import RateResult
        r = RateResult(True, "api", "user:x", 5, 60, 55, 30.0)
        h = r.headers()
        self.assertNotIn("Retry-After", h)

    def test_headers_blocked(self):
        from agent.rate_limiter import RateResult
        r = RateResult(False, "api", "user:x", 60, 60, 0, 30.0, retry_after_s=30.0)
        h = r.headers()
        self.assertIn("Retry-After", h)


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestWebhookPayload(unittest.TestCase):
    def _make_payload(self, event="test.event", data=None):
        from agent.webhooks import WebhookPayload
        return WebhookPayload(id="pay001", event=event,
                              timestamp=time.time(), data=data or {"key": "val"})

    def test_to_json(self):
        p = self._make_payload()
        j = json.loads(p.to_json())
        self.assertEqual(j["event"], "test.event")
        self.assertIn("data", j)
        self.assertIn("timestamp", j)

    def test_sign_format(self):
        p = self._make_payload()
        sig = p.sign("my_secret")
        self.assertTrue(sig.startswith("sha256="))

    def test_sign_deterministic(self):
        p = self._make_payload()
        self.assertEqual(p.sign("s"), p.sign("s"))

    def test_sign_different_secret(self):
        p = self._make_payload()
        self.assertNotEqual(p.sign("secret1"), p.sign("secret2"))


class TestWebhookDefinition(unittest.TestCase):
    def _make_hook(self, events=None):
        from agent.webhooks import Webhook
        return Webhook(id="h1", url="https://example.com/hook",
                      secret="s3cr3t", events=set(events or ["*"]))

    def test_matches_wildcard(self):
        h = self._make_hook(["*"])
        self.assertTrue(h.matches("anything"))

    def test_matches_specific(self):
        h = self._make_hook(["chat.complete", "eval.done"])
        self.assertTrue(h.matches("chat.complete"))
        self.assertFalse(h.matches("chat.error"))

    def test_disabled_irrelevant_to_matches(self):
        from agent.webhooks import Webhook
        h = Webhook(id="h2", url="u", secret="s", events={"*"}, enabled=False)
        self.assertTrue(h.matches("any"))  # matches checks filter only

    def test_to_dict_masks_secret(self):
        h = self._make_hook()
        d = h.to_dict(mask_secret=True)
        self.assertEqual(d["secret"], "***")

    def test_to_dict_exposes_secret(self):
        h = self._make_hook()
        d = h.to_dict(mask_secret=False)
        self.assertEqual(d["secret"], "s3cr3t")


class TestWebhookDispatcher(unittest.TestCase):
    def setUp(self):
        from agent.webhooks import WebhookDispatcher
        self.dispatcher = WebhookDispatcher()

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_register_webhook(self):
        hook_id = self.dispatcher.register("https://example.com", "secret",
                                            events=["chat.complete"])
        self.assertIsNotNone(hook_id)
        hook = self.dispatcher.get(hook_id)
        self.assertIsNotNone(hook)

    def test_list_webhooks(self):
        self.dispatcher.register("https://a.com", "s1", events=["*"])
        self.dispatcher.register("https://b.com", "s2", events=["eval.done"])
        hooks = self.dispatcher.list_webhooks()
        self.assertGreaterEqual(len(hooks), 2)

    def test_delete_webhook(self):
        hook_id = self.dispatcher.register("https://c.com", "s")
        ok = self.dispatcher.delete(hook_id)
        self.assertTrue(ok)
        self.assertIsNone(self.dispatcher.get(hook_id))

    def test_enable_disable(self):
        hook_id = self.dispatcher.register("https://d.com", "s")
        self.dispatcher.disable(hook_id)
        self.assertFalse(self.dispatcher.get(hook_id).enabled)
        self.dispatcher.enable(hook_id)
        self.assertTrue(self.dispatcher.get(hook_id).enabled)

    def test_update_webhook(self):
        hook_id = self.dispatcher.register("https://e.com", "s", events=["*"])
        ok = self.dispatcher.update(hook_id, events=["chat.complete"])
        self.assertTrue(ok)
        hook = self.dispatcher.get(hook_id)
        self.assertNotIn("*", hook.events)

    def test_update_nonexistent(self):
        ok = self.dispatcher.update("nonexistent_id", url="https://x.com")
        self.assertFalse(ok)

    def test_dispatch_no_matching_hooks(self):
        records = self._run(self.dispatcher.dispatch("chat.complete", {"test": True}))
        self.assertEqual(len(records), 0)

    def test_dispatch_disabled_hook_skipped(self):
        hook_id = self.dispatcher.register("https://f.com", "s", events=["*"])
        self.dispatcher.disable(hook_id)
        # Will still attempt delivery (fails to connect but tests skip logic)
        records = self._run(self.dispatcher.dispatch("any.event", {}))
        # Disabled hook should not be in records
        self.assertEqual(len(records), 0)

    def test_signature_verification(self):
        from agent.webhooks import WebhookDispatcher
        body = b'{"event":"test"}'
        secret = "mysecret"
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(WebhookDispatcher.verify_signature(body, secret, sig))
        self.assertFalse(WebhookDispatcher.verify_signature(body, secret, "sha256=wrongsig"))

    def test_stats_empty(self):
        stats = self.dispatcher.stats()
        self.assertIn("total_deliveries", stats)
        self.assertEqual(stats["total_deliveries"], 0)

    def test_delivery_history_empty(self):
        h = self.dispatcher.delivery_history()
        self.assertEqual(h, [])

    def test_background_worker_start_stop(self):
        async def _run():
            await self.dispatcher.start_worker()
            await asyncio.sleep(0.05)
            await self.dispatcher.stop_worker()
        asyncio.get_event_loop().run_until_complete(_run())

    def test_dispatch_background_queued(self):
        async def _run():
            await self.dispatcher.start_worker()
            await self.dispatcher.dispatch_background("agent.start", {"info": "test"})
            await asyncio.sleep(0.1)
            await self.dispatcher.stop_worker()
        asyncio.get_event_loop().run_until_complete(_run())


class TestWebhookEvents(unittest.TestCase):
    def test_event_enum_values(self):
        from agent.webhooks import WebhookEvent
        self.assertEqual(WebhookEvent.CHAT_COMPLETE, "chat.complete")
        self.assertEqual(WebhookEvent.WILDCARD, "*")

    def test_wildcard_matches_all(self):
        from agent.webhooks import Webhook, WebhookEvent
        h = Webhook(id="x", url="u", secret="s", events={WebhookEvent.WILDCARD})
        for event in [WebhookEvent.CHAT_COMPLETE, WebhookEvent.EVAL_DONE, "custom.event"]:
            self.assertTrue(h.matches(event))


# ══════════════════════════════════════════════════════════════════════════════
# JOBS TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestCronSchedule(unittest.TestCase):
    def setUp(self):
        from agent.jobs import CronSchedule
        self.Cron = CronSchedule

    def test_every_minute(self):
        c = self.Cron("* * * * *")
        self.assertTrue(c.matches(time.time()))

    def test_specific_minute(self):
        import datetime
        # Find a time where minute == 30
        t = datetime.datetime.now().replace(minute=30, second=0, microsecond=0)
        c = self.Cron("30 * * * *")
        self.assertTrue(c.matches(t.timestamp()))
        c2 = self.Cron("31 * * * *")
        self.assertFalse(c2.matches(t.timestamp()))

    def test_step_expression(self):
        import datetime
        c = self.Cron("*/5 * * * *")  # every 5 minutes
        for minute in [0, 5, 10, 15, 55]:
            t = datetime.datetime.now().replace(minute=minute, second=0, microsecond=0)
            self.assertTrue(c.matches(t.timestamp()), f"minute={minute} should match */5")
        for minute in [1, 3, 7, 23]:
            t = datetime.datetime.now().replace(minute=minute, second=0, microsecond=0)
            self.assertFalse(c.matches(t.timestamp()), f"minute={minute} should not match */5")

    def test_range_expression(self):
        import datetime
        c = self.Cron("0 9-17 * * *")  # 9am-5pm
        t_in  = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        t_out = datetime.datetime.now().replace(hour=22, minute=0, second=0, microsecond=0)
        self.assertTrue(c.matches(t_in.timestamp()))
        self.assertFalse(c.matches(t_out.timestamp()))

    def test_invalid_expression_raises(self):
        with self.assertRaises(ValueError):
            self.Cron("* * * *")   # only 4 fields

    def test_next_run_is_future(self):
        c = self.Cron("* * * * *")
        next_ts = c.next_run()
        self.assertGreater(next_ts, time.time())


class TestJobStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.jobs import JobStore, Job, JobStatus
        self.store = JobStore(os.path.join(self.tmpdir, "jobs.db"))
        self.JobStatus = JobStatus
        self.Job = Job

    def _make_job(self, job_type="test_job", **kwargs):
        import uuid
        return self.Job(id=str(uuid.uuid4())[:8], job_type=job_type,
                       payload={"x": 1}, **kwargs)

    def test_save_and_get(self):
        job = self._make_job()
        self.store.save(job)
        got = self.store.get(job.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.job_type, "test_job")

    def test_get_nonexistent(self):
        self.assertIsNone(self.store.get("nope"))

    def test_claim_next_pending(self):
        job = self._make_job()
        self.store.save(job)
        claimed = self.store.claim_next("worker-1")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, self.JobStatus.RUNNING)

    def test_claim_next_empty(self):
        claimed = self.store.claim_next("worker-1")
        self.assertIsNone(claimed)

    def test_claim_respects_scheduled_at(self):
        job = self._make_job()
        job.scheduled_at = time.time() + 9999
        self.store.save(job)
        claimed = self.store.claim_next("worker-1")
        self.assertIsNone(claimed)

    def test_claim_priority_order(self):
        from agent.jobs import JobPriority
        lo = self._make_job("low_job"); lo.priority = int(JobPriority.LOW)
        hi = self._make_job("high_job"); hi.priority = int(JobPriority.HIGH)
        self.store.save(lo); self.store.save(hi)
        claimed = self.store.claim_next("w")
        self.assertEqual(claimed.job_type, "high_job")

    def test_update_status_completed(self):
        job = self._make_job()
        self.store.save(job)
        self.store.update_status(job.id, self.JobStatus.COMPLETED,
                                 result={"done": True}, progress=100)
        got = self.store.get(job.id)
        self.assertEqual(got.status, self.JobStatus.COMPLETED)
        self.assertEqual(got.progress, 100)
        self.assertIsNotNone(got.completed_at)

    def test_cancel_pending(self):
        job = self._make_job()
        self.store.save(job)
        ok = self.store.cancel(job.id)
        self.assertTrue(ok)
        got = self.store.get(job.id)
        self.assertEqual(got.status, self.JobStatus.CANCELLED)

    def test_cancel_running_fails(self):
        job = self._make_job()
        self.store.save(job)
        self.store.claim_next("w")  # moves to running
        # cancel while running should fail (only pending/retrying allowed)
        ok = self.store.cancel(job.id)
        self.assertFalse(ok)

    def test_list_by_status(self):
        j1 = self._make_job("t1"); self.store.save(j1)
        j2 = self._make_job("t2"); self.store.save(j2)
        self.store.update_status(j1.id, self.JobStatus.COMPLETED)
        pending = self.store.list_jobs(status=self.JobStatus.PENDING)
        completed = self.store.list_jobs(status=self.JobStatus.COMPLETED)
        self.assertEqual(len(pending), 1)
        self.assertGreaterEqual(len(completed), 1)

    def test_stats(self):
        for _ in range(3): self.store.save(self._make_job())
        stats = self.store.stats()
        self.assertIn("by_status", stats)
        self.assertGreaterEqual(stats["by_status"].get("pending", 0), 3)


class TestJobQueue(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.jobs import JobQueue, JobPriority, JobStatus
        self.queue = JobQueue(db_path=os.path.join(self.tmpdir, "jq.db"))
        self.JobPriority = JobPriority
        self.JobStatus = JobStatus
        self.executed = []

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_submit_returns_id(self):
        job_id = self._run(self.queue.submit("my_job", {"x": 1}))
        self.assertIsNotNone(job_id)
        self.assertIsInstance(job_id, str)

    def test_submit_and_get(self):
        job_id = self._run(self.queue.submit("my_job", {"data": "test"}))
        job = self.queue.get_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job.job_type, "my_job")

    def test_submit_with_delay(self):
        job_id = self._run(self.queue.submit("delayed_job", {}, delay_s=100))
        job = self.queue.get_job(job_id)
        self.assertGreater(job.scheduled_at, time.time() + 50)

    def test_cancel_pending(self):
        job_id = self._run(self.queue.submit("cancel_me", {}))
        ok = self.queue.cancel(job_id)
        self.assertTrue(ok)
        job = self.queue.get_job(job_id)
        self.assertEqual(job.status, self.JobStatus.CANCELLED)

    def test_list_jobs(self):
        for _ in range(3):
            self._run(self.queue.submit("list_test", {}))
        jobs = self.queue.list_jobs()
        self.assertGreaterEqual(len(jobs), 3)

    def test_handler_registration(self):
        @self.queue.handler("echo_job")
        async def handler(ctx):
            return {"echoed": ctx.payload.get("msg")}
        self.assertIn("echo_job", self.queue._handlers)

    def test_end_to_end_execution(self):
        results = []

        @self.queue.handler("result_job")
        async def handler(ctx):
            results.append(ctx.payload["value"])
            return {"done": True}

        async def _run():
            job_id = await self.queue.submit("result_job", {"value": 42})
            await self.queue.start(workers=1, poll_interval=0.05)
            await asyncio.sleep(0.3)
            await self.queue.stop()
            return job_id

        job_id = asyncio.get_event_loop().run_until_complete(_run())
        self.assertIn(42, results)
        job = self.queue.get_job(job_id)
        self.assertEqual(job.status, self.JobStatus.COMPLETED)

    def test_failed_job_retries(self):
        attempts = []

        @self.queue.handler("failing_job")
        async def handler(ctx):
            attempts.append(ctx.attempt)
            raise ValueError("always fail")

        async def _run():
            job_id = await self.queue.submit("failing_job", {}, max_attempts=2)
            await self.queue.start(workers=1, poll_interval=0.05)
            await asyncio.sleep(1.0)  # allow retries with backoff
            await self.queue.stop()
            return job_id

        job_id = asyncio.get_event_loop().run_until_complete(_run())
        self.assertGreaterEqual(len(attempts), 1)
        job = self.queue.get_job(job_id)
        self.assertIn(job.status, [self.JobStatus.DEAD, self.JobStatus.RETRYING,
                                   self.JobStatus.FAILED])

    def test_dead_letter_queue(self):
        dlq = self.queue.dead_letter_queue()
        self.assertIsInstance(dlq, list)

    def test_stats(self):
        self._run(self.queue.submit("stat_job", {}))
        stats = self.queue.stats()
        self.assertIn("by_status", stats)

    def test_progress_update(self):
        from agent.jobs import JobContext

        @self.queue.handler("progress_job")
        async def handler(ctx):
            ctx.update_progress(50, "halfway")
            ctx.update_progress(100, "done")
            return {"complete": True}

        async def _run():
            job_id = await self.queue.submit("progress_job", {})
            await self.queue.start(workers=1, poll_interval=0.05)
            await asyncio.sleep(0.3)
            await self.queue.stop()
            return job_id

        job_id = asyncio.get_event_loop().run_until_complete(_run())
        job = self.queue.get_job(job_id)
        self.assertEqual(job.status, self.JobStatus.COMPLETED)

    def test_sync_handler(self):
        synced = []

        self.queue.register_handler("sync_job", lambda ctx: synced.append(1) or {"ok": True})

        async def _run():
            job_id = await self.queue.submit("sync_job", {})
            await self.queue.start(workers=1, poll_interval=0.05)
            await asyncio.sleep(0.2)
            await self.queue.stop()
            return job_id

        asyncio.get_event_loop().run_until_complete(_run())
        self.assertGreater(len(synced), 0)

    def test_cron_schedule_method(self):
        job_id = self.queue.schedule_cron("cron_job", {"x": 1}, cron="*/5 * * * *")
        job = self.queue.get_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job.recur_cron, "*/5 * * * *")
        self.assertGreater(job.scheduled_at, time.time())


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun
    failed = len(result.failures) + len(result.errors)
    passed = total - failed
    print(f"\n{'='*60}")
    print(f"  v7 Test Results: {passed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  ✗ {t}")
            print(f"    {tb.splitlines()[-1]}")
    else:
        print(f"  ✅ ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
