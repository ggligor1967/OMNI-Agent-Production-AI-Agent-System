"""OMNI AGENT v32: ConversationManager, ToolRegistry, RateLimiter, EventBus"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# CONVERSATION MANAGER
# ════════════════════════════════════════════════════════
class TestConversationManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.conversation_manager import ConversationManager
        self.cm = ConversationManager(
            db_path=os.path.join(td,"conv.db"),
            max_messages=50, max_tokens=2000)

    def test_create_session(self):
        sess = self.cm.create("alice")
        self.assertIsNotNone(sess)
        self.assertEqual(sess.user_id, "alice")

    def test_system_prompt_added_as_message(self):
        sess = self.cm.create("alice", system_prompt="You are helpful.")
        self.assertTrue(any(m.role == "system" for m in sess.messages))

    def test_append_message(self):
        sess = self.cm.create("bob")
        msg = self.cm.append(sess.id, "user", "Hello!")
        self.assertIsNotNone(msg)
        self.assertEqual(msg.content, "Hello!")

    def test_message_roles(self):
        sess = self.cm.create("carol")
        self.cm.append(sess.id, "user", "Hi")
        self.cm.append(sess.id, "assistant", "Hello!")
        self.cm.append(sess.id, "tool", "result", name="search")
        roles = [m.role for m in sess.messages]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)

    def test_get_context_format(self):
        sess = self.cm.create("dave", system_prompt="Be concise.")
        self.cm.append(sess.id, "user", "Question?")
        ctx = self.cm.get_context(sess.id)
        self.assertIsInstance(ctx, list)
        self.assertTrue(all("role" in m and "content" in m for m in ctx))

    def test_get_context_max_messages(self):
        sess = self.cm.create("eve")
        for i in range(10):
            self.cm.append(sess.id, "user", f"msg {i}")
        ctx = self.cm.get_context(sess.id, max_messages=3)
        # Should have at most 3 non-system messages
        non_sys = [m for m in ctx if m["role"] != "system"]
        self.assertLessEqual(len(non_sys), 3)

    def test_turn_count(self):
        sess = self.cm.create("frank")
        self.cm.append(sess.id, "user", "Q1")
        self.cm.append(sess.id, "assistant", "A1")
        self.cm.append(sess.id, "user", "Q2")
        self.assertEqual(sess.turn_count, 2)

    def test_total_tokens(self):
        sess = self.cm.create("grace")
        self.cm.append(sess.id, "user", "word " * 20)
        self.assertGreater(sess.total_tokens, 0)

    def test_trim_drops_oldest(self):
        cm_td = tempfile.mkdtemp()
        from agent.conversation_manager import ConversationManager
        cm = ConversationManager(db_path=os.path.join(cm_td,"t.db"),
                                  max_messages=5, max_tokens=99999)
        sess = cm.create("trim_user")
        for i in range(8):
            cm.append(sess.id, "user", f"message {i}")
        self.assertLessEqual(len(sess.messages), 5)

    def test_trim_keeps_system(self):
        cm_td = tempfile.mkdtemp()
        from agent.conversation_manager import ConversationManager
        cm = ConversationManager(db_path=os.path.join(cm_td,"ts.db"),
                                  max_messages=4, max_tokens=99999)
        sess = cm.create("sys_user", system_prompt="KEEP ME")
        for i in range(6):
            cm.append(sess.id, "user", f"msg {i}")
        sys_msgs = [m for m in sess.messages if m.role == "system"]
        self.assertGreater(len(sys_msgs), 0)
        self.assertEqual(sys_msgs[0].content, "KEEP ME")

    def test_on_message_hook(self):
        received = []
        self.cm.on("on_message", lambda s, m: received.append(m.id))
        sess = self.cm.create("hook_user")
        self.cm.append(sess.id, "user", "hookme")
        self.assertGreater(len(received), 0)

    def test_on_trim_hook(self):
        td = tempfile.mkdtemp()
        from agent.conversation_manager import ConversationManager
        cm = ConversationManager(db_path=os.path.join(td,"th.db"),
                                  max_messages=3, max_tokens=99999)
        trimmed = []
        cm.on("on_trim", lambda s, m: trimmed.append(m.id))
        sess = cm.create("trim_hook")
        for i in range(5): cm.append(sess.id, "user", f"m{i}")
        self.assertGreater(len(trimmed), 0)

    def test_branch_session(self):
        sess = self.cm.create("branch_user")
        self.cm.append(sess.id, "user", "msg1")
        self.cm.append(sess.id, "assistant", "ans1")
        branch = self.cm.branch(sess.id, at_index=1)
        self.assertIsNotNone(branch)
        self.assertNotEqual(branch.id, sess.id)

    def test_search_messages(self):
        sess = self.cm.create("search_user")
        self.cm.append(sess.id, "user", "Find the needle in the haystack")
        results = self.cm.search("needle")
        self.assertGreater(len(results), 0)

    def test_list_sessions(self):
        for i in range(3):
            self.cm.create(f"list_user_{i}")
        sessions = self.cm.list_sessions()
        self.assertGreaterEqual(len(sessions), 3)

    def test_list_sessions_by_user(self):
        self.cm.create("specific_user", tags=["test"])
        sessions = self.cm.list_sessions(user_id="specific_user")
        self.assertGreater(len(sessions), 0)

    def test_export(self):
        sess = self.cm.create("export_user")
        self.cm.append(sess.id, "user", "hello")
        data = self.cm.export(sess.id)
        self.assertIsNotNone(data)
        self.assertIn("messages", data)

    def test_persistence_reload(self):
        td = tempfile.mkdtemp()
        from agent.conversation_manager import ConversationManager
        cm1 = ConversationManager(db_path=os.path.join(td,"pr.db"))
        sess = cm1.create("persist_user")
        cm1.append(sess.id, "user", "persisted message")
        cm2 = ConversationManager(db_path=os.path.join(td,"pr.db"))
        loaded = cm2.get(sess.id)
        self.assertIsNotNone(loaded)
        contents = [m.content for m in loaded.messages]
        self.assertIn("persisted message", contents)

    def test_session_to_dict(self):
        sess = self.cm.create("dict_user")
        d = sess.to_dict()
        for k in ["id","user_id","turn_count","message_count","total_tokens"]:
            self.assertIn(k, d)

    def test_message_to_dict(self):
        sess = self.cm.create("msgdict_user")
        msg = self.cm.append(sess.id, "user", "test")
        d = msg.to_dict()
        for k in ["id","role","content","token_count"]:
            self.assertIn(k, d)

    def test_stats(self):
        self.cm.create("stats_user")
        s = self.cm.stats()
        for k in ["sessions","messages","active_sessions"]:
            self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# TOOL REGISTRY
# ════════════════════════════════════════════════════════
class TestToolRegistry(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.tool_registry import ToolRegistry
        self.reg = ToolRegistry(db_path=os.path.join(td,"tools.db"))

    def _add_basic(self):
        def add(x: int, y: int) -> int:
            """Add two integers."""
            return x + y
        return self.reg.register(add)

    def test_register_and_call(self):
        self._add_basic()
        result = _run(self.reg.call("add", {"x": 3, "y": 4}))
        self.assertTrue(result.success)
        self.assertEqual(result.output, 7)

    def test_schema_inferred(self):
        spec = self._add_basic()
        self.assertIn("x", spec.schema["properties"])
        self.assertIn("y", spec.schema["properties"])
        self.assertIn("x", spec.schema["required"])

    def test_required_params(self):
        def greet(name: str, greeting: str = "Hello") -> str:
            return f"{greeting}, {name}!"
        self.reg.register(greet)
        self.assertIn("name", self.reg.get("greet").schema["required"])
        self.assertNotIn("greeting", self.reg.get("greet").schema["required"])

    def test_missing_required_fails(self):
        self._add_basic()
        result = _run(self.reg.call("add", {"x": 1}))
        self.assertFalse(result.success)
        self.assertIn("y", result.error)

    def test_unknown_tool_fails(self):
        result = _run(self.reg.call("no_such_tool", {}))
        self.assertFalse(result.success)

    def test_disabled_tool_fails(self):
        self._add_basic()
        self.reg.disable("add")
        result = _run(self.reg.call("add", {"x": 1, "y": 2}))
        self.assertFalse(result.success)
        self.reg.enable("add")

    def test_alias(self):
        def multiply(a: int, b: int) -> int: return a * b
        self.reg.register(multiply, aliases=["mul", "times"])
        result = _run(self.reg.call("mul", {"a": 3, "b": 4}))
        self.assertEqual(result.output, 12)

    def test_async_tool(self):
        async def async_add(x: int, y: int) -> int:
            await asyncio.sleep(0.01)
            return x + y
        self.reg.register(async_add)
        result = _run(self.reg.call("async_add", {"x": 5, "y": 6}))
        self.assertEqual(result.output, 11)

    def test_timeout(self):
        async def slow(x: int) -> int:
            await asyncio.sleep(5)
            return x
        self.reg.register(slow, timeout_s=0.05)
        result = _run(self.reg.call("slow", {"x": 1}))
        self.assertFalse(result.success)
        self.assertIn("Timeout", result.error)

    def test_exception_captured(self):
        def boom(x: int) -> int: raise ValueError("bad!")
        self.reg.register(boom)
        result = _run(self.reg.call("boom", {"x": 1}))
        self.assertFalse(result.success)
        self.assertIn("bad!", result.error)

    def test_call_count(self):
        self._add_basic()
        _run(self.reg.call("add", {"x": 1, "y": 2}))
        _run(self.reg.call("add", {"x": 3, "y": 4}))
        self.assertEqual(self.reg.get("add").call_count, 2)

    def test_pre_hook(self):
        calls = []
        self.reg.before_call(lambda s, a: calls.append(s.name))
        self._add_basic()
        _run(self.reg.call("add", {"x": 1, "y": 2}))
        self.assertIn("add", calls)

    def test_post_hook(self):
        results = []
        self.reg.after_call(lambda s, r: results.append(r.success))
        self._add_basic()
        _run(self.reg.call("add", {"x": 1, "y": 2}))
        self.assertIn(True, results)

    def test_batch_call(self):
        self._add_basic()
        calls = [{"name": "add", "args": {"x": i, "y": i}} for i in range(3)]
        results = _run(self.reg.call_batch(calls))
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.success for r in results))

    def test_unregister(self):
        self._add_basic()
        ok = self.reg.unregister("add")
        self.assertTrue(ok)
        result = _run(self.reg.call("add", {"x": 1, "y": 2}))
        self.assertFalse(result.success)

    def test_list_tools(self):
        self._add_basic()
        tools = self.reg.list_tools()
        self.assertTrue(any(t.name == "add" for t in tools))

    def test_openai_schema_format(self):
        self._add_basic()
        schemas = self.reg.openai_tools()
        self.assertGreater(len(schemas), 0)
        self.assertEqual(schemas[0]["type"], "function")
        self.assertIn("function", schemas[0])

    def test_result_to_dict(self):
        self._add_basic()
        r = _run(self.reg.call("add", {"x": 1, "y": 2}))
        d = r.to_dict()
        for k in ["call_id","tool","output","error","latency_ms","success"]:
            self.assertIn(k, d)

    def test_stats(self):
        self._add_basic()
        _run(self.reg.call("add", {"x": 1, "y": 2}))
        s = self.reg.stats()
        for k in ["total_calls","registered_tools","avg_latency_ms"]:
            self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# RATE LIMITER
# ════════════════════════════════════════════════════════
class TestRateLimiter(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.rate_limiter import RateLimiter, Algorithm
        self.RL = RateLimiter
        self.ALG = Algorithm
        self.rl = RateLimiter(db_path=os.path.join(td,"rl.db"))

    def test_sliding_window_allows(self):
        self.rl.add_rule("sw", limit=5, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        r = self.rl.check("user:alice")
        self.assertTrue(r.allowed)

    def test_sliding_window_blocks_on_limit(self):
        self.rl.add_rule("sw_block", limit=3, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        for _ in range(3): self.rl.check("user:block")
        r = self.rl.check("user:block")
        self.assertFalse(r.allowed)

    def test_sliding_window_remaining(self):
        self.rl.add_rule("sw_rem", limit=5, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        r = self.rl.check("user:rem")
        self.assertEqual(r.remaining, 4)

    def test_token_bucket_allows_burst(self):
        self.rl.add_rule("tb", limit=3, window_s=60, burst=2,
                          algorithm=self.ALG.TOKEN_BUCKET)
        results = [self.rl.check("tb_user") for _ in range(5)]
        self.assertTrue(results[0].allowed)

    def test_token_bucket_blocks_after_burst(self):
        self.rl.add_rule("tb_block", limit=2, window_s=60, burst=0,
                          algorithm=self.ALG.TOKEN_BUCKET)
        for _ in range(2): self.rl.check("tb_blk")
        r = self.rl.check("tb_blk")
        self.assertFalse(r.allowed)

    def test_fixed_window_allows(self):
        self.rl.add_rule("fw", limit=5, window_s=60,
                          algorithm=self.ALG.FIXED_WINDOW)
        r = self.rl.check("fw_user")
        self.assertTrue(r.allowed)

    def test_fixed_window_blocks(self):
        self.rl.add_rule("fw_block", limit=3, window_s=60,
                          algorithm=self.ALG.FIXED_WINDOW)
        for _ in range(3): self.rl.check("fw_blk")
        r = self.rl.check("fw_blk")
        self.assertFalse(r.allowed)

    def test_leaky_bucket_allows(self):
        self.rl.add_rule("lb", limit=5, window_s=60, burst=3,
                          algorithm=self.ALG.LEAKY_BUCKET)
        r = self.rl.check("lb_user")
        self.assertTrue(r.allowed)

    def test_no_rule_always_allows(self):
        r = self.rl.check("no_rule_key")
        self.assertTrue(r.allowed)
        self.assertEqual(r.rule_name, "no_rule")

    def test_wildcard_key_pattern(self):
        self.rl.add_rule("wild", limit=2, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW,
                          key_pattern="user:*")
        r = self.rl.check("user:alice")
        self.assertTrue(r.allowed)

    def test_key_pattern_doesnt_match_other(self):
        self.rl.add_rule("scoped", limit=2, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW,
                          key_pattern="admin:*")
        # user:alice should not match admin:*
        r = self.rl.check("user:alice")
        # No matching rule → always allowed
        self.assertTrue(r.allowed)

    def test_retry_after_positive_on_block(self):
        self.rl.add_rule("ra", limit=1, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        self.rl.check("ra_user")
        r = self.rl.check("ra_user")
        self.assertFalse(r.allowed)
        self.assertGreater(r.retry_after_s, 0)

    def test_peek_does_not_consume(self):
        self.rl.add_rule("peek_rule", limit=3, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        r1 = self.rl.peek("peek_user")
        r2 = self.rl.peek("peek_user")
        self.assertEqual(r1.remaining, r2.remaining)

    def test_reset_clears_state(self):
        self.rl.add_rule("rst", limit=2, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        self.rl.check("rst_user"); self.rl.check("rst_user")
        r = self.rl.check("rst_user")
        self.assertFalse(r.allowed)
        self.rl.reset("rst_user")
        r2 = self.rl.check("rst_user")
        self.assertTrue(r2.allowed)

    def test_daily_quota(self):
        self.rl.set_quota("quota_user", 3)
        self.rl.add_rule("any", limit=100, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        for _ in range(3): self.rl.check("quota_user")
        r = self.rl.check("quota_user")
        self.assertFalse(r.allowed)

    def test_result_headers(self):
        self.rl.add_rule("hdr", limit=5, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        r = self.rl.check("hdr_user")
        h = r.headers()
        self.assertIn("X-RateLimit-Limit", h)
        self.assertIn("X-RateLimit-Remaining", h)

    def test_result_to_dict(self):
        self.rl.add_rule("td", limit=5, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        r = self.rl.check("td_user")
        d = r.to_dict()
        for k in ["allowed","key","rule","remaining","retry_after_s"]:
            self.assertIn(k, d)

    def test_remove_rule(self):
        self.rl.add_rule("rem", limit=1, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        ok = self.rl.remove_rule("rem")
        self.assertTrue(ok)
        r = self.rl.check("rem_user")
        self.assertTrue(r.allowed)   # no rule → allowed

    def test_stats(self):
        self.rl.add_rule("st_rule", limit=5, window_s=60,
                          algorithm=self.ALG.SLIDING_WINDOW)
        self.rl.check("st_user")
        s = self.rl.stats()
        for k in ["rules","tracked_keys"]:
            self.assertIn(k, s)

# ════════════════════════════════════════════════════════
# EVENT BUS
# ════════════════════════════════════════════════════════
class TestEventBus(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.event_bus import EventBus
        self.bus = EventBus(db_path=os.path.join(td,"eb.db"), persist=True)

    def test_publish_returns_event(self):
        e = _run(self.bus.publish("test.event", {"key": "val"}))
        self.assertEqual(e.type, "test.event")
        self.assertIsNotNone(e.id)

    def test_handler_called(self):
        received = []
        self.bus.subscribe("agent.done", lambda e: received.append(e.type))
        _run(self.bus.publish("agent.done", {}))
        self.assertEqual(received, ["agent.done"])

    def test_async_handler(self):
        received = []
        async def ahandler(e): received.append(e.type)
        self.bus.subscribe("async.event", ahandler)
        _run(self.bus.publish("async.event", {}))
        self.assertEqual(received, ["async.event"])

    def test_wildcard_pattern(self):
        received = []
        self.bus.subscribe("agent.*", lambda e: received.append(e.type))
        _run(self.bus.publish("agent.started", {}))
        _run(self.bus.publish("agent.done",    {}))
        self.assertIn("agent.started", received)
        self.assertIn("agent.done", received)

    def test_wildcard_doesnt_match_other(self):
        received = []
        self.bus.subscribe("agent.*", lambda e: received.append(e.type))
        _run(self.bus.publish("system.event", {}))
        self.assertNotIn("system.event", received)

    def test_priority_order(self):
        order = []
        self.bus.subscribe("ordered", lambda e: order.append(1), priority=1)
        self.bus.subscribe("ordered", lambda e: order.append(3), priority=3)
        self.bus.subscribe("ordered", lambda e: order.append(2), priority=2)
        _run(self.bus.publish("ordered", {}))
        self.assertEqual(order, [1, 2, 3])

    def test_once_handler_fires_once(self):
        count = [0]
        self.bus.subscribe("once.event", lambda e: count.__setitem__(0, count[0]+1), once=True)
        _run(self.bus.publish("once.event", {}))
        _run(self.bus.publish("once.event", {}))
        self.assertEqual(count[0], 1)

    def test_filter_fn(self):
        received = []
        self.bus.subscribe("filtered", lambda e: received.append(e.payload.get("x")),
                            filter_fn=lambda e: e.payload.get("x", 0) > 5)
        _run(self.bus.publish("filtered", {"x": 3}))
        _run(self.bus.publish("filtered", {"x": 10}))
        self.assertNotIn(3, received)
        self.assertIn(10, received)

    def test_handler_error_goes_to_dlq(self):
        def bad_handler(e): raise RuntimeError("boom")
        self.bus.subscribe("bad.event", bad_handler)
        _run(self.bus.publish("bad.event", {}))
        dlq = self.bus.dlq()
        self.assertGreater(len(dlq), 0)

    def test_handler_error_doesnt_block_others(self):
        received = []
        def bad(e): raise RuntimeError("oops")
        self.bus.subscribe("multi", bad, priority=1)
        self.bus.subscribe("multi", lambda e: received.append("ok"), priority=2)
        _run(self.bus.publish("multi", {}))
        self.assertIn("ok", received)

    def test_unsubscribe(self):
        received = []
        sub = self.bus.subscribe("unsub.event", lambda e: received.append(e.type))
        self.bus.unsubscribe(sub.id)
        _run(self.bus.publish("unsub.event", {}))
        self.assertEqual(received, [])

    def test_pre_hook(self):
        hooks = []
        self.bus.before_publish(lambda e: hooks.append(e.type))
        _run(self.bus.publish("pre.event", {}))
        self.assertIn("pre.event", hooks)

    def test_post_hook(self):
        hooks = []
        self.bus.after_publish(lambda e: hooks.append(e.type))
        _run(self.bus.publish("post.event", {}))
        self.assertIn("post.event", hooks)

    def test_payload_passed_to_handler(self):
        received = []
        self.bus.subscribe("data.event", lambda e: received.append(e.payload))
        _run(self.bus.publish("data.event", {"value": 42}))
        self.assertEqual(received[0]["value"], 42)

    def test_correlation_id(self):
        e = _run(self.bus.publish("corr.event", {}, correlation_id="abc-123"))
        self.assertEqual(e.correlation_id, "abc-123")

    def test_replay(self):
        received = []
        _run(self.bus.publish("replay.event", {"n": 1}))
        _run(self.bus.publish("replay.event", {"n": 2}))
        self.bus.subscribe("replay.event", lambda e: received.append(e.payload.get("n")))
        count = _run(self.bus.replay("replay.event", since=0))
        self.assertGreater(count, 0)

    def test_replay_dlq(self):
        def bad(e): raise RuntimeError("fail")
        self.bus.subscribe("dlq.event", bad)
        _run(self.bus.publish("dlq.event", {}))
        self.assertGreater(len(self.bus._dlq), 0)

    def test_list_subscriptions(self):
        self.bus.subscribe("list.*", lambda e: None)
        subs = self.bus.list_subscriptions()
        self.assertGreater(len(subs), 0)
        self.assertIn("pattern", subs[0])

    def test_event_to_dict(self):
        e = _run(self.bus.publish("dict.event", {"x": 1}))
        d = e.to_dict()
        for k in ["id","type","payload","source","created_at"]:
            self.assertIn(k, d)

    def test_stats(self):
        _run(self.bus.publish("stats.event", {}))
        s = self.bus.stats()
        for k in ["subscriptions","dlq_in_memory","topic_counts"]:
            self.assertIn(k, s)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v32: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
