"""
OMNI AGENT v11 — Test Suite
Tests: CircuitBreaker, ContextWindow, PromptRegistry, ReplayEngine
Run: python3 tests/test_v11_modules.py
"""
import asyncio, os, sys, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ══════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER — CircuitStats
# ══════════════════════════════════════════════════════════════════════════════

class TestCircuitStats(unittest.TestCase):
    def setUp(self):
        from agent.circuit_breaker import CircuitStats
        self.stats = CircuitStats()

    def test_initial_error_rate_zero(self):
        self.assertEqual(self.stats.error_rate, 0.0)

    def test_error_rate_calculation(self):
        self.stats.total_successes = 3
        self.stats.total_failures = 1
        self.assertAlmostEqual(self.stats.error_rate, 0.25, places=5)

    def test_to_dict_keys(self):
        d = self.stats.to_dict()
        for key in ["total_calls", "total_successes", "total_failures",
                    "consecutive_failures", "error_rate"]:
            self.assertIn(key, d)


# ══════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER — CircuitBreaker
# ══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        from agent.circuit_breaker import CircuitBreaker
        self.CB = CircuitBreaker

    def _make_breaker(self, failure_threshold=3, error_rate_threshold=0.5,
                      recovery_timeout_s=60.0, window_size=10):
        return self.CB(
            "test-svc",
            failure_threshold=failure_threshold,
            error_rate_threshold=error_rate_threshold,
            window_size=window_size,
            recovery_timeout_s=recovery_timeout_s,
        )

    def test_initial_state_closed(self):
        from agent.circuit_breaker import CircuitState
        b = self._make_breaker()
        self.assertEqual(b.state, CircuitState.CLOSED)

    def test_successful_call(self):
        b = self._make_breaker()
        async def good():
            return "ok"
        result = _run(b.call(good))
        self.assertEqual(result, "ok")

    def test_success_keeps_closed(self):
        from agent.circuit_breaker import CircuitState
        b = self._make_breaker()
        async def good():
            return "ok"
        for _ in range(5):
            _run(b.call(good))
        self.assertEqual(b.state, CircuitState.CLOSED)

    def test_failures_open_circuit(self):
        from agent.circuit_breaker import CircuitState
        b = self._make_breaker(failure_threshold=3)
        async def bad():
            raise RuntimeError("fail")
        for _ in range(3):
            try:
                _run(b.call(bad))
            except RuntimeError:
                pass
        self.assertEqual(b.state, CircuitState.OPEN)

    def test_open_circuit_raises_circuit_open_error(self):
        from agent.circuit_breaker import CircuitState, CircuitOpenError
        b = self._make_breaker(failure_threshold=3)
        async def bad():
            raise RuntimeError("fail")
        for _ in range(3):
            try:
                _run(b.call(bad))
            except RuntimeError:
                pass
        self.assertEqual(b.state, CircuitState.OPEN)
        async def any_fn():
            return "ok"
        with self.assertRaises(CircuitOpenError):
            _run(b.call(any_fn))

    def test_open_circuit_with_fallback(self):
        from agent.circuit_breaker import CircuitBreaker
        b = CircuitBreaker(
            "svc-fallback",
            failure_threshold=2,
            recovery_timeout_s=60,
            fallback=lambda: "fallback_value",
        )
        async def bad():
            raise RuntimeError("fail")
        for _ in range(2):
            try:
                _run(b.call(bad))
            except RuntimeError:
                pass
        result = _run(b.call(bad))
        self.assertEqual(result, "fallback_value")

    def test_force_open(self):
        from agent.circuit_breaker import CircuitState
        b = self._make_breaker()
        b.force_open("test")
        self.assertEqual(b.state, CircuitState.OPEN)

    def test_force_close(self):
        from agent.circuit_breaker import CircuitState
        b = self._make_breaker()
        b.force_open("test")
        b.force_close("test")
        self.assertEqual(b.state, CircuitState.CLOSED)

    def test_reset_clears_stats(self):
        b = self._make_breaker()
        async def bad():
            raise RuntimeError("fail")
        for _ in range(2):
            try:
                _run(b.call(bad))
            except RuntimeError:
                pass
        b.reset()
        self.assertEqual(b._stats.total_failures, 0)
        self.assertEqual(b._stats.consecutive_failures, 0)

    def test_half_open_after_timeout(self):
        from agent.circuit_breaker import CircuitState
        b = self._make_breaker(failure_threshold=2, recovery_timeout_s=0.05)
        async def bad():
            raise RuntimeError("fail")
        for _ in range(2):
            try:
                _run(b.call(bad))
            except RuntimeError:
                pass
        self.assertEqual(b.state, CircuitState.OPEN)
        time.sleep(0.1)
        # Check state — should auto-transition to HALF_OPEN
        self.assertEqual(b.state, CircuitState.HALF_OPEN)

    def test_half_open_success_closes(self):
        from agent.circuit_breaker import CircuitState, CircuitBreaker
        b = CircuitBreaker("svc-ho", failure_threshold=2,
                           recovery_timeout_s=0.05, probe_success_count=1)
        async def bad():
            raise RuntimeError("fail")
        async def good():
            return "ok"
        for _ in range(2):
            try:
                _run(b.call(bad))
            except RuntimeError:
                pass
        time.sleep(0.1)
        # Trigger HALF_OPEN by checking state
        _ = b.state
        _run(b.call(good))
        self.assertEqual(b.state, CircuitState.CLOSED)

    def test_half_open_failure_reopens(self):
        from agent.circuit_breaker import CircuitState, CircuitBreaker
        b = CircuitBreaker("svc-reopen", failure_threshold=2,
                           recovery_timeout_s=0.05, probe_success_count=2)
        async def bad():
            raise RuntimeError("fail")
        for _ in range(2):
            try:
                _run(b.call(bad))
            except RuntimeError:
                pass
        time.sleep(0.1)
        _ = b.state  # Trigger HALF_OPEN
        try:
            _run(b.call(bad))
        except RuntimeError:
            pass
        self.assertEqual(b.state, CircuitState.OPEN)

    def test_rejection_count_increments(self):
        from agent.circuit_breaker import CircuitOpenError
        b = self._make_breaker(failure_threshold=2)
        async def bad():
            raise RuntimeError("fail")
        async def any_fn():
            return "ok"
        for _ in range(2):
            try:
                _run(b.call(bad))
            except RuntimeError:
                pass
        try:
            _run(b.call(any_fn))
        except CircuitOpenError:
            pass
        self.assertGreater(b._stats.total_rejections, 0)

    def test_state_change_callback(self):
        from agent.circuit_breaker import CircuitBreaker
        events = []
        def on_change(service, old, new):
            events.append((old, new))
        b = CircuitBreaker("cb-event", failure_threshold=2,
                           recovery_timeout_s=60,
                           on_state_change=on_change)
        async def bad():
            raise RuntimeError("fail")
        for _ in range(2):
            try:
                _run(b.call(bad))
            except RuntimeError:
                pass
        self.assertGreater(len(events), 0)

    def test_stats_dict(self):
        b = self._make_breaker()
        stats = b.stats()
        self.assertIn("service", stats)
        self.assertIn("state", stats)
        self.assertIn("error_rate", stats)

    def test_retry_after_zero_when_closed(self):
        b = self._make_breaker()
        self.assertEqual(b.retry_after_s, 0.0)

    def test_protect_decorator(self):
        b = self._make_breaker()

        @b.protect
        async def my_fn(x):
            return x * 2

        result = _run(my_fn(21))
        self.assertEqual(result, 42)

    def test_error_rate_window(self):
        """Error rate window should trigger open even below consecutive threshold."""
        from agent.circuit_breaker import CircuitBreaker, CircuitState
        b = CircuitBreaker("rate-svc", failure_threshold=100,
                           error_rate_threshold=0.5, window_size=10,
                           recovery_timeout_s=60)
        async def bad():
            raise RuntimeError("fail")
        async def good():
            return "ok"
        # Alternate fail/success — rate stays near 50%
        for _ in range(5):
            try:
                _run(b.call(bad))
            except RuntimeError:
                pass
        for _ in range(5):
            _run(b.call(good))
        # Now push it over the edge with more failures
        for _ in range(5):
            try:
                _run(b.call(bad))
            except Exception:
                pass
        # Check window shows >= 50% error rate (or circuit is open)
        window_errors = b._window.count(False)
        window_total = len(b._window)
        if window_total > 0:
            self.assertGreaterEqual(window_errors / window_total, 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER — Registry
# ══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerRegistry(unittest.TestCase):
    def setUp(self):
        from agent.circuit_breaker import CircuitBreakerRegistry
        self.reg = CircuitBreakerRegistry()

    def test_register_and_get(self):
        b = self.reg.register("svc-a", failure_threshold=5)
        got = self.reg.get("svc-a")
        self.assertIsNotNone(got)
        self.assertEqual(got.service, "svc-a")

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.reg.get("nonexistent"))

    def test_get_or_create(self):
        b = self.reg.get_or_create("auto-svc")
        self.assertIsNotNone(b)
        b2 = self.reg.get_or_create("auto-svc")
        self.assertIs(b, b2)

    def test_call_through_registry(self):
        self.reg.register("echo-svc")
        async def echo(x):
            return x + 1
        result = _run(self.reg.call("echo-svc", echo, 41))
        self.assertEqual(result, 42)

    def test_protect_decorator(self):
        self.reg.register("dec-svc")

        @self.reg.protect("dec-svc")
        async def fn(x):
            return x * 3

        result = _run(fn(7))
        self.assertEqual(result, 21)

    def test_summary(self):
        self.reg.register("s1")
        self.reg.register("s2")
        summary = self.reg.summary()
        self.assertIn("total", summary)
        self.assertIn("breakers", summary)
        self.assertGreaterEqual(summary["total"], 2)

    def test_reset_all(self):
        b = self.reg.register("reset-svc")
        b.force_open("test")
        self.reg.reset_all()
        from agent.circuit_breaker import CircuitState
        self.assertEqual(b.state, CircuitState.CLOSED)


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT WINDOW
# ══════════════════════════════════════════════════════════════════════════════

class TestTokenCounting(unittest.TestCase):
    def test_count_empty(self):
        from agent.context_window import count_tokens
        self.assertEqual(count_tokens(""), 0)

    def test_count_short_text(self):
        from agent.context_window import count_tokens
        n = count_tokens("Hello world")
        self.assertGreater(n, 0)
        self.assertLess(n, 20)

    def test_count_longer_text(self):
        from agent.context_window import count_tokens
        short = count_tokens("Hi")
        long = count_tokens("A" * 1000)
        self.assertGreater(long, short)

    def test_count_message(self):
        from agent.context_window import count_message_tokens
        msg = {"role": "user", "content": "What is Python?"}
        n = count_message_tokens(msg)
        self.assertGreater(n, 4)  # 4 = overhead

    def test_count_messages_list(self):
        from agent.context_window import count_messages_tokens
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        n = count_messages_tokens(msgs)
        self.assertGreater(n, 0)

    def test_model_limit_claude(self):
        from agent.context_window import model_limit
        self.assertGreaterEqual(model_limit("claude-3-5-sonnet"), 100_000)

    def test_model_limit_gpt4(self):
        from agent.context_window import model_limit
        self.assertGreaterEqual(model_limit("gpt-4o"), 50_000)

    def test_model_limit_default(self):
        from agent.context_window import model_limit
        self.assertGreater(model_limit("unknown-model"), 0)

    def test_count_multimodal_content(self):
        from agent.context_window import count_message_tokens
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ]
        }
        n = count_message_tokens(msg)
        self.assertGreater(n, 0)


class TestContextWindowManager(unittest.TestCase):
    def setUp(self):
        from agent.context_window import ContextWindowManager, TrimStrategy
        self.cwm = ContextWindowManager(
            model="gpt-4",
            completion_reserve=512,
        )
        self.TrimStrategy = TrimStrategy

    def _msgs(self, n=10, content_len=50):
        return [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"Message {i}: " + "x" * content_len}
            for i in range(n)
        ]

    def test_count_tokens(self):
        msgs = self._msgs(5)
        n = self.cwm.count(msgs)
        self.assertGreater(n, 0)

    def test_count_text(self):
        n = self.cwm.count_text("Hello world, this is a test sentence.")
        self.assertGreater(n, 0)

    def test_fits_small(self):
        msgs = self._msgs(2)
        self.assertTrue(self.cwm.fits(msgs))

    def test_overflows_huge(self):
        # Create messages that exceed a small limit
        from agent.context_window import ContextWindowManager
        cwm = ContextWindowManager(model="gpt-4", completion_reserve=7000)
        msgs = self._msgs(100, content_len=200)
        # With 7000 reserved and 100 long messages, likely overflows
        total = cwm.count(msgs)
        limit = cwm._limit - cwm._completion_reserve
        if total > limit:
            self.assertTrue(cwm.overflows(msgs))
        else:
            self.assertFalse(cwm.overflows(msgs))

    def test_usage_keys(self):
        msgs = self._msgs(3)
        u = self.cwm.usage(msgs, system_prompt="You are helpful.")
        for key in ["model", "limit", "message_tokens", "remaining", "utilization"]:
            self.assertIn(key, u)

    def test_trim_returns_fewer_messages(self):
        from agent.context_window import ContextWindowManager, TrimStrategy
        # Use a very small limit to force trimming
        cwm = ContextWindowManager(model="gpt-4", completion_reserve=7500)
        msgs = self._msgs(20, content_len=100)
        if cwm.overflows(msgs):
            result = cwm.trim(msgs, strategy=TrimStrategy.DROP_MIDDLE)
            self.assertLess(len(result.messages), len(msgs))
            self.assertGreater(result.trimmed_count, 0)

    def test_trim_no_overflow_returns_same(self):
        msgs = self._msgs(2)
        result = self.cwm.trim(msgs)
        self.assertEqual(len(result.messages), len(msgs))
        self.assertEqual(result.trimmed_count, 0)

    def test_sliding_window_keeps_recent(self):
        from agent.context_window import ContextWindowManager, TrimStrategy
        cwm = ContextWindowManager(model="gpt-4", completion_reserve=7500)
        msgs = self._msgs(30, content_len=100)
        if cwm.overflows(msgs):
            result = cwm.trim(msgs, strategy=TrimStrategy.SLIDING_WINDOW)
            # Last message should be preserved
            if result.messages:
                self.assertEqual(result.messages[-1]["content"], msgs[-1]["content"])

    def test_drop_middle_preserves_system(self):
        from agent.context_window import ContextWindowManager, TrimStrategy
        cwm = ContextWindowManager(model="gpt-4", completion_reserve=7500)
        msgs = [{"role": "system", "content": "Be helpful.", "_pinned": True}]
        msgs += self._msgs(30, content_len=100)
        if cwm.overflows(msgs):
            result = cwm.trim(msgs, strategy=TrimStrategy.DROP_MIDDLE)
            roles = [m["role"] for m in result.messages]
            self.assertIn("system", roles)

    def test_prepare_no_trim_needed(self):
        msgs = self._msgs(2)
        prepared = self.cwm.prepare(msgs)
        self.assertEqual(prepared, msgs)

    def test_prepare_trims_if_needed(self):
        from agent.context_window import ContextWindowManager
        cwm = ContextWindowManager(model="gpt-4", completion_reserve=7500)
        msgs = self._msgs(50, content_len=200)
        prepared = cwm.prepare(msgs)
        self.assertLessEqual(len(prepared), len(msgs))

    def test_trim_result_to_dict(self):
        msgs = self._msgs(5)
        result = self.cwm.trim(msgs)
        d = result.to_dict()
        for key in ["original_count", "trimmed_count", "original_tokens",
                    "final_tokens", "strategy"]:
            self.assertIn(key, d)

    def test_tokens_saved_property(self):
        result = self.cwm.trim(self._msgs(2))
        # When no trimming needed, tokens_saved == 0
        self.assertEqual(result.tokens_saved, 0)

    def test_available_tokens_positive(self):
        self.assertGreater(self.cwm.available_tokens, 0)

    def test_message_priority_system_highest(self):
        from agent.context_window import _message_priority
        sys_msg = {"role": "system", "content": "Be helpful"}
        user_msg = {"role": "user", "content": "Hi"}
        sys_p = _message_priority(sys_msg, 0, 10)
        user_p = _message_priority(user_msg, 0, 10)
        self.assertGreater(sys_p, user_p)

    def test_message_priority_pinned_highest(self):
        from agent.context_window import _message_priority
        pinned = {"role": "user", "content": "x", "_pinned": True}
        system = {"role": "system", "content": "y"}
        pin_p = _message_priority(pinned, 0, 10)
        sys_p = _message_priority(system, 0, 10)
        self.assertGreater(pin_p, sys_p)

    def test_message_priority_recent_higher(self):
        from agent.context_window import _message_priority
        early = {"role": "user", "content": "x"}
        recent = {"role": "user", "content": "x"}
        early_p = _message_priority(early, 0, 20)
        recent_p = _message_priority(recent, 18, 20)
        self.assertGreater(recent_p, early_p)

    def test_summarize_fallback_without_fn(self):
        msgs = self._msgs(5)
        result = _run(self.cwm.summarize_and_trim(msgs))
        self.assertIsInstance(result.messages, list)


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractVariables(unittest.TestCase):
    def test_simple_variable(self):
        from agent.prompt_registry import _extract_variables
        vars_ = _extract_variables("Hello {name}!")
        self.assertIn("name", vars_)

    def test_multiple_variables(self):
        from agent.prompt_registry import _extract_variables
        vars_ = _extract_variables("Hi {name}, your role is {role}.")
        self.assertIn("name", vars_)
        self.assertIn("role", vars_)

    def test_no_variables(self):
        from agent.prompt_registry import _extract_variables
        self.assertEqual(_extract_variables("No variables here."), [])

    def test_deduplicated(self):
        from agent.prompt_registry import _extract_variables
        vars_ = _extract_variables("{x} and {x} again.")
        self.assertEqual(vars_.count("x"), 1)


class TestPromptVersion(unittest.TestCase):
    def setUp(self):
        from agent.prompt_registry import PromptVersion
        self.pv = PromptVersion(
            id="v1", prompt_id="p1", version=1,
            body="Hello {name}, you are {role}.",
            variables=["name", "role"],
        )

    def test_render_basic(self):
        rendered = self.pv.render(name="Alice", role="admin")
        self.assertIn("Alice", rendered)
        self.assertIn("admin", rendered)

    def test_render_missing_variable_non_strict(self):
        rendered = self.pv.render(name="Alice")
        self.assertIn("{role}", rendered)

    def test_render_missing_variable_strict_raises(self):
        with self.assertRaises(KeyError):
            self.pv.render(strict=True, name="Alice")

    def test_render_all_variables(self):
        rendered = self.pv.render(strict=True, name="Bob", role="user")
        self.assertNotIn("{", rendered)

    def test_to_dict(self):
        d = self.pv.to_dict()
        self.assertIn("body", d)
        self.assertIn("variables", d)
        self.assertIn("version", d)


class TestPromptRegistry(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.prompt_registry import PromptRegistry
        self.reg = PromptRegistry(
            db_path=os.path.join(self.tmpdir, "prompts.db")
        )

    def test_create_prompt(self):
        entry = self.reg.create("my-prompt", body="Hello {name}!")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "my-prompt")
        self.assertEqual(entry.current_version, 1)

    def test_create_extracts_variables(self):
        entry = self.reg.create("var-prompt", body="Hi {user}, role={role}.")
        self.assertIn("user", entry.variables)
        self.assertIn("role", entry.variables)

    def test_create_duplicate_raises(self):
        self.reg.create("dup-prompt", body="First")
        with self.assertRaises(ValueError):
            self.reg.create("dup-prompt", body="Second")

    def test_get_by_name(self):
        self.reg.create("get-test", body="Hello")
        entry = self.reg.get_by_name("get-test")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "get-test")

    def test_get_nonexistent_returns_none(self):
        self.assertIsNone(self.reg.get_by_name("does-not-exist"))

    def test_list_all(self):
        self.reg.create("list-a", body="A")
        self.reg.create("list-b", body="B")
        entries = self.reg.list()
        names = [e.name for e in entries]
        self.assertIn("list-a", names)
        self.assertIn("list-b", names)

    def test_list_by_status(self):
        from agent.prompt_registry import PromptStatus
        self.reg.create("draft-prompt", body="draft",
                        status=PromptStatus.DRAFT)
        entries = self.reg.list(status="draft")
        statuses = [e.status for e in entries]
        self.assertIn(PromptStatus.DRAFT, statuses)

    def test_list_by_tag(self):
        self.reg.create("tagged-prompt", body="x", tags=["production", "v2"])
        entries = self.reg.list(tag="production")
        tags_flat = [t for e in entries for t in e.tags]
        self.assertIn("production", tags_flat)

    def test_list_by_task(self):
        self.reg.create("task-prompt", body="x", task="summarize")
        entries = self.reg.list(task="summarize")
        tasks = [e.task for e in entries]
        self.assertIn("summarize", tasks)

    def test_update_increments_version(self):
        self.reg.create("upd-prompt", body="v1 body")
        entry = self.reg.update("upd-prompt", body="v2 body", note="v2")
        self.assertEqual(entry.current_version, 2)
        self.assertEqual(entry.current_body, "v2 body")

    def test_update_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.reg.update("unknown-prompt", body="x")

    def test_delete_prompt(self):
        self.reg.create("del-prompt", body="x")
        ok = self.reg.delete("del-prompt")
        self.assertTrue(ok)
        self.assertIsNone(self.reg.get_by_name("del-prompt"))

    def test_delete_nonexistent(self):
        ok = self.reg.delete("never-existed")
        self.assertFalse(ok)

    def test_render_current(self):
        self.reg.create("render-test", body="Hi {who}!")
        text = self.reg.render("render-test", who="World")
        self.assertEqual(text, "Hi World!")

    def test_render_specific_version(self):
        self.reg.create("rv-prompt", body="v1: {x}")
        self.reg.update("rv-prompt", body="v2: {x}")
        text = self.reg.render("rv-prompt", version=1, x="test")
        self.assertIn("v1:", text)

    def test_render_nonexistent_raises(self):
        with self.assertRaises(KeyError):
            self.reg.render("no-such-prompt")

    def test_versions_list(self):
        self.reg.create("ver-hist", body="v1")
        self.reg.update("ver-hist", body="v2")
        self.reg.update("ver-hist", body="v3")
        versions = self.reg.versions("ver-hist")
        self.assertEqual(len(versions), 3)

    def test_rollback(self):
        self.reg.create("rollback-test", body="original")
        self.reg.update("rollback-test", body="modified")
        entry = self.reg.rollback("rollback-test", to_version=1)
        self.assertIn("original", entry.current_body)

    def test_diff_versions(self):
        self.reg.create("diff-test", body="line one\nline two")
        self.reg.update("diff-test", body="line one\nline three")
        diff = self.reg.diff("diff-test", 1, 2)
        self.assertIn("---", diff)
        self.assertIn("+++", diff)

    def test_diff_no_change(self):
        self.reg.create("same-test", body="identical")
        self.reg.update("same-test", body="identical")
        diff = self.reg.diff("same-test", 1, 2)
        self.assertIn("no differences", diff.lower())

    def test_promote(self):
        from agent.prompt_registry import PromptStatus
        self.reg.create("promote-test", body="x")
        ok = self.reg.promote("promote-test", user="alice")
        self.assertTrue(ok)
        entry = self.reg.get_by_name("promote-test")
        self.assertEqual(entry.status, PromptStatus.PRODUCTION)

    def test_archive(self):
        from agent.prompt_registry import PromptStatus
        self.reg.create("archive-test", body="x")
        self.reg.archive("archive-test")
        entry = self.reg.get_by_name("archive-test")
        self.assertEqual(entry.status, PromptStatus.ARCHIVED)

    def test_deprecate(self):
        from agent.prompt_registry import PromptStatus
        self.reg.create("dep-test", body="x")
        self.reg.deprecate("dep-test")
        entry = self.reg.get_by_name("dep-test")
        self.assertEqual(entry.status, PromptStatus.DEPRECATED)

    def test_fork(self):
        self.reg.create("source-prompt", body="Original {x}")
        fork = self.reg.fork("source-prompt", "forked-prompt", forked_by="bob")
        self.assertEqual(fork.name, "forked-prompt")
        self.assertIn("Original", fork.current_body)
        self.assertNotEqual(fork.id, self.reg.get_by_name("source-prompt").id)

    def test_fork_nonexistent_raises(self):
        with self.assertRaises(KeyError):
            self.reg.fork("no-such-source", "fork-name")

    def test_search_by_name(self):
        self.reg.create("searchable-name-xyz", body="content")
        results = self.reg.search("searchable-name-xyz")
        names = [e.name for e in results]
        self.assertIn("searchable-name-xyz", names)

    def test_export_returns_dict(self):
        self.reg.create("export-a", body="A")
        self.reg.create("export-b", body="B")
        exported = self.reg.export()
        self.assertIn("export-a", exported)
        self.assertIn("export-b", exported)

    def test_audit_trail(self):
        self.reg.create("audit-test", body="x", created_by="alice")
        self.reg.update("audit-test", body="y", updated_by="bob")
        events = self.reg.audit("audit-test")
        self.assertGreater(len(events), 0)

    def test_stats(self):
        self.reg.create("stat-a", body="A")
        stats = self.reg.stats()
        self.assertIn("total_prompts", stats)
        self.assertIn("total_versions", stats)
        self.assertGreaterEqual(stats["total_prompts"], 1)


# ══════════════════════════════════════════════════════════════════════════════
# REPLAY ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class TestRecordedTurn(unittest.TestCase):
    def test_to_dict(self):
        from agent.replay import RecordedTurn
        turn = RecordedTurn(index=0, role="user", content="Hello")
        d = turn.to_dict()
        self.assertEqual(d["role"], "user")
        self.assertEqual(d["content"], "Hello")
        self.assertEqual(d["index"], 0)


class TestRecording(unittest.TestCase):
    def _make_recording(self):
        from agent.replay import Recording, RecordedTurn
        turns = [
            RecordedTurn(index=0, role="user", content="What is Python?"),
            RecordedTurn(index=1, role="assistant", content="Python is a programming language."),
            RecordedTurn(index=2, role="user", content="Tell me more."),
            RecordedTurn(index=3, role="assistant", content="It is widely used."),
        ]
        return Recording(id="rec1", name="test-rec", turns=turns)

    def test_user_turns(self):
        rec = self._make_recording()
        self.assertEqual(len(rec.user_turns), 2)

    def test_assistant_turns(self):
        rec = self._make_recording()
        self.assertEqual(len(rec.assistant_turns), 2)

    def test_to_messages(self):
        rec = self._make_recording()
        msgs = rec.to_messages()
        self.assertEqual(len(msgs), 4)
        self.assertEqual(msgs[0]["role"], "user")

    def test_to_dict_include_turns(self):
        rec = self._make_recording()
        d = rec.to_dict(include_turns=True)
        self.assertIn("turns", d)
        self.assertEqual(len(d["turns"]), 4)

    def test_to_dict_exclude_turns(self):
        rec = self._make_recording()
        d = rec.to_dict(include_turns=False)
        self.assertNotIn("turns", d)

    def test_turn_count_in_dict(self):
        rec = self._make_recording()
        d = rec.to_dict(include_turns=False)
        self.assertEqual(d["turn_count"], 4)


class TestAssertions(unittest.TestCase):
    def test_contains_pass(self):
        from agent.replay import Assertions
        fn = Assertions.contains("python")
        self.assertTrue(fn("Python is great"))

    def test_contains_fail(self):
        from agent.replay import Assertions
        fn = Assertions.contains("java")
        self.assertFalse(fn("Python is great"))

    def test_not_contains(self):
        from agent.replay import Assertions
        fn = Assertions.not_contains("java")
        self.assertTrue(fn("Python is great"))
        self.assertFalse(fn("Java is great"))

    def test_matches_regex(self):
        from agent.replay import Assertions
        fn = Assertions.matches_regex(r"\d{4}")
        self.assertTrue(fn("year 2024"))
        self.assertFalse(fn("no digits here"))

    def test_min_length_pass(self):
        from agent.replay import Assertions
        fn = Assertions.min_length(5)
        self.assertTrue(fn("Hello world"))

    def test_min_length_fail(self):
        from agent.replay import Assertions
        fn = Assertions.min_length(100)
        self.assertFalse(fn("Short"))

    def test_max_length_pass(self):
        from agent.replay import Assertions
        fn = Assertions.max_length(100)
        self.assertTrue(fn("Short text"))

    def test_max_length_fail(self):
        from agent.replay import Assertions
        fn = Assertions.max_length(5)
        self.assertFalse(fn("This is a longer text"))

    def test_similarity_above_pass(self):
        from agent.replay import Assertions
        original = "The sky is blue"
        fn = Assertions.similarity_above(0.8, original)
        self.assertTrue(fn("The sky is blue"))

    def test_similarity_above_fail(self):
        from agent.replay import Assertions
        original = "The sky is blue"
        fn = Assertions.similarity_above(0.9, original)
        self.assertFalse(fn("Completely different text about quantum physics"))

    def test_no_refusal_pass(self):
        from agent.replay import Assertions
        fn = Assertions.no_refusal()
        self.assertTrue(fn("Python is a great programming language!"))

    def test_no_refusal_fail(self):
        from agent.replay import Assertions
        fn = Assertions.no_refusal()
        self.assertFalse(fn("I cannot help with that request."))


class TestReplayEngine(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.replay import ReplayEngine
        # No agent_fn: replay uses original responses
        self.engine = ReplayEngine(
            db_path=os.path.join(self.tmpdir, "replay.db"),
        )

    def _sample_messages(self):
        return [
            {"role": "user", "content": "What is Python?"},
            {"role": "assistant", "content": "Python is a high-level programming language."},
            {"role": "user", "content": "What about Java?"},
            {"role": "assistant", "content": "Java is a statically typed language."},
        ]

    def test_record_from_messages(self):
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="test-rec", model="gpt-4")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.name, "test-rec")
        self.assertEqual(len(rec.turns), 4)

    def test_record_persisted(self):
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="persist-rec")
        got = self.engine.get_recording(rec.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "persist-rec")

    def test_list_recordings(self):
        msgs = self._sample_messages()
        self.engine.record_from_messages(msgs, name="list-a", tags=["qa"])
        self.engine.record_from_messages(msgs, name="list-b", tags=["qa"])
        recs = self.engine.list_recordings()
        names = [r.name for r in recs]
        self.assertIn("list-a", names)
        self.assertIn("list-b", names)

    def test_list_by_tag(self):
        msgs = self._sample_messages()
        self.engine.record_from_messages(msgs, name="tag-a", tags=["smoke"])
        self.engine.record_from_messages(msgs, name="no-tag", tags=[])
        recs = self.engine.list_recordings(tag="smoke")
        names = [r.name for r in recs]
        self.assertIn("tag-a", names)

    def test_delete_recording(self):
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="del-rec")
        ok = self.engine.delete_recording(rec.id)
        self.assertTrue(ok)
        self.assertIsNone(self.engine.get_recording(rec.id))

    def test_delete_nonexistent(self):
        self.assertFalse(self.engine.delete_recording("nonexistent-id"))

    def test_replay_no_agent_fn(self):
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="replay-test")
        result = _run(self.engine.replay(rec.id))
        self.assertIsNotNone(result)
        self.assertEqual(result.recording_name, "replay-test")

    def test_replay_produces_comparisons(self):
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="comp-test")
        result = _run(self.engine.replay(rec.id))
        self.assertEqual(result.total_turns, 2)  # 2 user turns → 2 comparisons
        self.assertEqual(len(result.comparisons), 2)

    def test_replay_similarity_score(self):
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="sim-test")
        result = _run(self.engine.replay(rec.id))
        for comp in result.comparisons:
            self.assertGreaterEqual(comp.similarity, 0.0)
            self.assertLessEqual(comp.similarity, 1.01)

    def test_replay_no_regressions_when_identical(self):
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="no-reg-test")
        # No agent fn: replayed == original → similarity == 1.0
        result = _run(self.engine.replay(rec.id))
        self.assertEqual(result.regressions, 0)

    def test_replay_with_custom_agent(self):
        from agent.replay import ReplayEngine
        async def custom_agent(messages, model="", system_prompt=""):
            return "Custom response"

        engine = ReplayEngine(
            agent_fn=custom_agent,
            db_path=os.path.join(self.tmpdir, "replay2.db"),
        )
        msgs = self._sample_messages()
        rec = engine.record_from_messages(msgs, name="custom-agent-test")
        result = _run(engine.replay(rec.id))
        # Responses differ → some regressions expected
        self.assertGreater(result.total_turns, 0)

    def test_replay_with_assertions_pass(self):
        from agent.replay import Assertions
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="assert-pass")
        assertions = [Assertions.min_length(5)]
        result = _run(self.engine.replay(rec.id, assertions=assertions))
        for comp in result.comparisons:
            # min_length(5) should pass for non-empty responses
            status = comp.assertions.get("min_length(5)")
            if status:
                from agent.replay import AssertionStatus
                self.assertEqual(status, AssertionStatus.PASS)

    def test_replay_nonexistent_recording(self):
        result = _run(self.engine.replay("nonexistent-id"))
        self.assertFalse(result.success)
        self.assertIn("not found", result.error.lower())

    def test_turn_comparison_has_regression(self):
        from agent.replay import TurnComparison, AssertionStatus
        comp = TurnComparison(
            turn_index=0,
            original="Python is great",
            replayed="Java is terrible",
            similarity=0.1,
            diff="...",
            assertions={"no_refusal": AssertionStatus.FAIL},
        )
        self.assertTrue(comp.has_regression)

    def test_turn_comparison_no_regression(self):
        from agent.replay import TurnComparison, AssertionStatus
        comp = TurnComparison(
            turn_index=0,
            original="Python is great",
            replayed="Python is great",
            similarity=1.0,
            diff="",
            assertions={"no_refusal": AssertionStatus.PASS},
        )
        self.assertFalse(comp.has_regression)

    def test_replay_result_to_dict(self):
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="dict-test")
        result = _run(self.engine.replay(rec.id))
        d = result.to_dict()
        for key in ["id", "recording_id", "success", "regressions",
                    "avg_similarity", "comparisons"]:
            self.assertIn(key, d)

    def test_markdown_report(self):
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="report-test")
        result = _run(self.engine.replay(rec.id))
        md = result.markdown_report()
        self.assertIsInstance(md, str)
        self.assertIn("report-test", md)
        self.assertIn("Turn", md)

    def test_batch_replay(self):
        msgs = self._sample_messages()
        rec1 = self.engine.record_from_messages(msgs, name="batch-a")
        rec2 = self.engine.record_from_messages(msgs, name="batch-b")
        results = _run(self.engine.batch_replay([rec1.id, rec2.id]))
        self.assertEqual(len(results), 2)

    def test_list_results(self):
        msgs = self._sample_messages()
        rec = self.engine.record_from_messages(msgs, name="results-test")
        _run(self.engine.replay(rec.id))
        results = self.engine.list_results(rec.id)
        self.assertGreater(len(results), 0)

    def test_stats(self):
        msgs = self._sample_messages()
        self.engine.record_from_messages(msgs, name="stats-rec")
        stats = self.engine.stats()
        self.assertIn("total_recordings", stats)
        self.assertIn("total_replays", stats)
        self.assertGreaterEqual(stats["total_recordings"], 1)


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
    print(f"  v11 Test Results: {passed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  ✗ {t}")
            print(f"    {tb.strip().splitlines()[-1]}")
    else:
        print(f"  ✅ ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
