"""OMNI AGENT v19 Tests: RoleManager, OutputFormatter, ConversationSummariser, CacheWarmer"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
def _run(c): return asyncio.get_event_loop().run_until_complete(c)

# ════════════════════════════════════════════════════════
# ROLE MANAGER
# ════════════════════════════════════════════════════════
class TestRoleManager(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.role_manager import RoleManager
        self.rm = RoleManager(db_path=os.path.join(td, "rm.db"), audit=True)

    def test_create_role(self):
        r = self.rm.create_role("editor", permissions=["posts:read","posts:write"])
        self.assertEqual(r.name, "editor")

    def test_assign_role(self):
        self.rm.create_role("viewer", permissions=["*:read"])
        self.rm.assign_role("alice", "viewer")
        self.assertIn("viewer", self.rm.get_roles("alice"))

    def test_check_allow(self):
        self.rm.create_role("admin", permissions=["*:*"])
        self.rm.assign_role("alice", "admin")
        d = self.rm.check("alice", "users", "delete")
        self.assertTrue(d.allowed)

    def test_check_deny_no_role(self):
        d = self.rm.check("unknown_actor", "reports", "write")
        self.assertFalse(d.allowed)

    def test_wildcard_resource(self):
        self.rm.create_role("all_reader", permissions=["*:read"])
        self.rm.assign_role("bob", "all_reader")
        self.assertTrue(self.rm.check("bob", "reports",  "read").allowed)
        self.assertTrue(self.rm.check("bob", "invoices", "read").allowed)
        self.assertFalse(self.rm.check("bob", "reports",  "write").allowed)

    def test_wildcard_action(self):
        self.rm.create_role("comment_mgr", permissions=["comments:*"])
        self.rm.assign_role("carol", "comment_mgr")
        self.assertTrue(self.rm.check("carol", "comments", "delete").allowed)
        self.assertFalse(self.rm.check("carol", "posts",    "read").allowed)

    def test_role_inheritance(self):
        self.rm.create_role("base", permissions=["posts:read"])
        self.rm.create_role("child", permissions=["posts:write"], parent="base")
        self.rm.assign_role("dave", "child")
        self.assertTrue(self.rm.check("dave", "posts", "read").allowed)   # from parent
        self.assertTrue(self.rm.check("dave", "posts", "write").allowed)  # own perm

    def test_deny_overrides_grant(self):
        self.rm.create_role("restricted", permissions=["*:*"], deny=["users:delete"])
        self.rm.assign_role("eve", "restricted")
        self.assertFalse(self.rm.check("eve", "users", "delete").allowed)
        self.assertTrue(self.rm.check("eve",  "posts", "write").allowed)

    def test_revoke_role(self):
        self.rm.create_role("temp_role", permissions=["temp:read"])
        self.rm.assign_role("frank", "temp_role")
        self.rm.revoke_role("frank", "temp_role")
        self.assertNotIn("temp_role", self.rm.get_roles("frank"))

    def test_add_permission(self):
        self.rm.create_role("base2", permissions=["posts:read"])
        self.rm.add_permission("base2", "posts:write")
        role = self.rm.get_role("base2")
        keys = [p.key for p in role.permissions]
        self.assertIn("posts:write", keys)

    def test_remove_permission(self):
        self.rm.create_role("edit2", permissions=["posts:read","posts:write"])
        self.rm.remove_permission("edit2", "posts:write")
        role = self.rm.get_role("edit2")
        keys = [p.key for p in role.permissions]
        self.assertNotIn("posts:write", keys)

    def test_effective_permissions(self):
        self.rm.create_role("p_base", permissions=["a:read"])
        self.rm.create_role("p_child", permissions=["b:write"], parent="p_base")
        self.rm.assign_role("grace", "p_child")
        perms = self.rm.get_effective_permissions("grace")
        self.assertIn("a:read", perms); self.assertIn("b:write", perms)

    def test_built_in_roles(self):
        roles = [r.name for r in self.rm.list_roles()]
        self.assertIn("superadmin", roles)
        self.assertIn("readonly", roles)

    def test_superadmin_check(self):
        self.rm.assign_role("zara", "superadmin")
        self.assertTrue(self.rm.check("zara", "anything", "delete").allowed)

    def test_policy_hook(self):
        def hook(actor, resource, action, ctx):
            if actor == "special": return True
            return None
        self.rm.add_policy_hook(hook)
        d = self.rm.check("special", "locked", "access")
        self.assertTrue(d.allowed)

    def test_audit_log(self):
        self.rm.create_role("audit_r", permissions=["logs:read"])
        self.rm.assign_role("henry", "audit_r")
        self.rm.check("henry", "logs", "read")
        log = self.rm.audit_log(actor="henry")
        self.assertGreater(len(log), 0)

    def test_delete_role(self):
        self.rm.create_role("deletable", permissions=["x:y"])
        ok = self.rm.delete_role("deletable")
        self.assertTrue(ok)
        self.assertIsNone(self.rm.get_role("deletable"))

    def test_stats(self):
        self.rm.create_role("r1", permissions=["x:read"])
        self.rm.assign_role("u1", "r1")
        self.rm.check("u1", "x", "read")
        s = self.rm.stats()
        for k in ["roles", "actors_with_roles", "decisions_logged"]: self.assertIn(k, s)

    def test_to_dict(self):
        r = self.rm.create_role("td_role", permissions=["a:b"], description="test")
        d = r.to_dict()
        for k in ["name", "description", "permissions", "deny", "parent"]: self.assertIn(k, d)

    def test_decision_to_dict(self):
        self.rm.create_role("dd_role", permissions=["x:read"])
        self.rm.assign_role("dd_user", "dd_role")
        d = self.rm.check("dd_user", "x", "read").to_dict()
        for k in ["allowed", "actor", "resource", "action", "matched_role"]: self.assertIn(k, d)

    def test_persistence(self):
        from agent.role_manager import RoleManager
        td = tempfile.mkdtemp(); db = os.path.join(td, "rm.db")
        rm1 = RoleManager(db_path=db)
        rm1.create_role("persist_role", permissions=["p:read"])
        rm1.assign_role("persist_user", "persist_role")
        rm2 = RoleManager(db_path=db)
        self.assertIsNotNone(rm2.get_role("persist_role"))
        self.assertIn("persist_role", rm2.get_roles("persist_user"))

# ════════════════════════════════════════════════════════
# OUTPUT FORMATTER
# ════════════════════════════════════════════════════════
class TestOutputFormatter(unittest.TestCase):
    def setUp(self):
        from agent.output_formatter import OutputFormatter
        self.fmt = OutputFormatter()

    def test_format_json_valid(self):
        result = self.fmt.format_json('{"name":"Alice","age":30}')
        self.assertTrue(result.valid); self.assertFalse(result.repaired)

    def test_format_json_dict(self):
        result = self.fmt.format_json({"name": "Bob", "score": 95})
        self.assertTrue(result.valid)

    def test_format_json_invalid_repaired(self):
        result = self.fmt.format_json('{"key":"val",}')   # trailing comma
        self.assertTrue(result.valid); self.assertTrue(result.repaired)

    def test_format_json_irreparable(self):
        result = self.fmt.format_json("not json at all %%##")
        self.assertFalse(result.valid)

    def test_schema_validation_pass(self):
        self.fmt.register_schema("person", {
            "type": "object", "required": ["name"],
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}})
        result = self.fmt.format_json({"name": "Alice", "age": 30}, schema="person")
        self.assertTrue(result.valid); self.assertEqual(len(result.errors), 0)

    def test_schema_validation_fail_missing(self):
        self.fmt.register_schema("p2", {"type": "object", "required": ["name"]})
        result = self.fmt.format_json({"age": 30}, schema="p2")
        self.assertFalse(result.valid); self.assertGreater(len(result.errors), 0)

    def test_schema_type_mismatch(self):
        self.fmt.register_schema("typed", {"type": "string"})
        result = self.fmt.format_json(42, schema="typed")
        self.assertFalse(result.valid)

    def test_table_markdown(self):
        rows = [{"name": "Alice", "score": 95}, {"name": "Bob", "score": 87}]
        table = self.fmt.table(rows, fmt="markdown")
        self.assertIn("|", table); self.assertIn("Alice", table); self.assertIn("score", table)

    def test_table_ascii(self):
        rows = [{"x": 1, "y": 2}]
        table = self.fmt.table(rows, fmt="ascii")
        self.assertIn("+", table)

    def test_table_empty(self):
        self.assertEqual(self.fmt.table([]), "")

    def test_list_unordered(self):
        lst = self.fmt.list_block(["apple", "banana", "cherry"])
        self.assertIn("- apple", lst); self.assertIn("- cherry", lst)

    def test_list_ordered(self):
        lst = self.fmt.list_block(["first", "second"], ordered=True)
        self.assertIn("1. first", lst); self.assertIn("2. second", lst)

    def test_code_block(self):
        cb = self.fmt.code_block("print('hello')", language="python")
        self.assertIn("```python", cb); self.assertIn("print('hello')", cb)

    def test_markdown_section(self):
        section = self.fmt.markdown_section("Results", "Content here", level=2)
        self.assertIn("## Results", section)

    def test_bold_italic(self):
        self.assertEqual(self.fmt.bold("text"), "**text**")
        self.assertEqual(self.fmt.italic("text"), "*text*")

    def test_render_template(self):
        result = self.fmt.render_template("Hello {{name}}!", {"name": "World"})
        self.assertEqual(result, "Hello World!")

    def test_to_html(self):
        html = self.fmt.to_html("## Title\n\n**bold** text")
        self.assertIn("<h2>", html); self.assertIn("<strong>", html)

    def test_to_csv(self):
        rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        csv_out = self.fmt.to_csv(rows)
        self.assertIn("a,b", csv_out); self.assertIn("1,2", csv_out)

    def test_truncate_smart(self):
        text = "First sentence. Second sentence. Third sentence. More words here."
        truncated = self.fmt.truncate(text, 30)
        self.assertLessEqual(len(truncated), 33)
        self.assertIn("…", truncated)

    def test_truncate_no_op(self):
        text = "Short."
        self.assertEqual(self.fmt.truncate(text, 100), text)

    def test_strip_markdown(self):
        md = "## Title\n\n**bold** and *italic* text"
        plain = self.fmt.strip_markdown(md)
        self.assertNotIn("**", plain); self.assertNotIn("##", plain)

    def test_validate_direct(self):
        errors = self.fmt.validate({"name": "Alice"}, {"type":"object","required":["name"]})
        self.assertEqual(len(errors), 0)

    def test_validate_enum(self):
        errors = self.fmt.validate("bad", {"enum": ["a","b","c"]})
        self.assertGreater(len(errors), 0)

    def test_to_dict(self):
        result = self.fmt.format_json({"x": 1})
        d = result.to_dict()
        for k in ["formatted","format_used","valid","errors","repaired"]: self.assertIn(k,d)

    def test_repair_from_markdown_block(self):
        text = '```json\n{"key": "value"}\n```'
        result = self.fmt.format_json(text, repair=True)
        self.assertTrue(result.valid)

# ════════════════════════════════════════════════════════
# CONVERSATION SUMMARISER
# ════════════════════════════════════════════════════════
class TestConversationSummariser(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.conversation_summariser import ConversationSummariser, Turn
        self.cs = ConversationSummariser(db_path=os.path.join(td, "cs.db"))
        self.Turn = Turn
        self.sid = "sess_001"
        self.turns = [
            Turn("user", "We need to decide on a database. I suggest PostgreSQL."),
            Turn("assistant", "PostgreSQL is a great choice for relational data."),
            Turn("user", "Agreed. Can you please write the schema for the users table?"),
            Turn("assistant", "Sure! Here's the schema: CREATE TABLE users (id SERIAL PRIMARY KEY, name TEXT)."),
        ]

    def test_add_turns(self):
        self.cs.add_turns(self.sid, self.turns)
        retrieved = self.cs.get_turns(self.sid)
        self.assertEqual(len(retrieved), 4)

    def test_summarise_returns_summary(self):
        self.cs.add_turns(self.sid, self.turns)
        s = _run(self.cs.summarise(self.sid))
        self.assertIsNotNone(s)

    def test_summary_has_fields(self):
        self.cs.add_turns(self.sid, self.turns)
        s = _run(self.cs.summarise(self.sid))
        self.assertIsNotNone(s.full_summary)
        self.assertIsNotNone(s.one_line)

    def test_summary_turns_count(self):
        self.cs.add_turns(self.sid, self.turns)
        s = _run(self.cs.summarise(self.sid))
        self.assertEqual(s.turns_summarised, 4)

    def test_compression_ratio(self):
        self.cs.add_turns(self.sid, self.turns)
        s = _run(self.cs.summarise(self.sid))
        self.assertGreaterEqual(s.compression_ratio, 0.0)
        self.assertLessEqual(s.compression_ratio, 1.0)

    def test_action_items_extracted(self):
        self.cs.add_turns(self.sid, self.turns)
        s = _run(self.cs.summarise(self.sid))
        self.assertIsInstance(s.action_items, list)

    def test_key_decisions_extracted(self):
        self.cs.add_turns(self.sid, self.turns)
        s = _run(self.cs.summarise(self.sid))
        self.assertIsInstance(s.key_decisions, list)

    def test_topics_inferred(self):
        self.cs.add_turns(self.sid, self.turns)
        s = _run(self.cs.summarise(self.sid))
        self.assertIsInstance(s.topics, list)

    def test_get_summary(self):
        self.cs.add_turns(self.sid, self.turns)
        _run(self.cs.summarise(self.sid))
        s = self.cs.get_summary(self.sid)
        self.assertIsNotNone(s)

    def test_versioning(self):
        self.cs.add_turns(self.sid, self.turns)
        _run(self.cs.summarise(self.sid))
        self.cs.add_turns(self.sid, [self.Turn("user", "New message")])
        s2 = _run(self.cs.summarise(self.sid))
        self.assertGreaterEqual(s2.version, 1)

    def test_empty_session(self):
        s = _run(self.cs.summarise("empty_session"))
        self.assertEqual(s.turns_summarised, 0)

    def test_with_llm(self):
        from agent.conversation_summariser import ConversationSummariser
        td = tempfile.mkdtemp()
        def llm(p):
            return ('{"full_summary":"Detailed summary here.","dense_summary":"Short summary.",'
                    '"one_line":"One line.","action_items":[{"id":"a1","text":"Write schema","assignee":"","deadline":""}],'
                    '"key_decisions":[{"id":"d1","text":"Use PostgreSQL","context":"DB choice"}],"topics":["technical"]}')
        cs = ConversationSummariser(llm_fn=llm, db_path=os.path.join(td, "cs2.db"))
        cs.add_turns("llm_sess", self.turns)
        s = _run(cs.summarise("llm_sess"))
        self.assertGreater(len(s.action_items), 0)
        self.assertGreater(len(s.key_decisions), 0)

    def test_participant_stats(self):
        self.cs.add_turns(self.sid, self.turns)
        stats = self.cs.participant_stats(self.sid)
        self.assertIn("by_role", stats); self.assertIn("user", stats["by_role"])

    def test_stats(self):
        self.cs.add_turns(self.sid, self.turns)
        _run(self.cs.summarise(self.sid))
        s = self.cs.stats()
        for k in ["sessions", "total_turns", "avg_compression_ratio"]: self.assertIn(k, s)

    def test_to_dict(self):
        self.cs.add_turns(self.sid, self.turns)
        s = _run(self.cs.summarise(self.sid))
        d = s.to_dict()
        for k in ["session_id","version","full_summary","one_line","action_items","topics","compression_ratio"]:
            self.assertIn(k, d)

    def test_persistence(self):
        from agent.conversation_summariser import ConversationSummariser, Turn
        td = tempfile.mkdtemp(); db = os.path.join(td, "cs.db")
        cs1 = ConversationSummariser(db_path=db)
        cs1.add_turns("p_sess", [Turn("user", "Hello"), Turn("assistant", "Hi there!")])
        _run(cs1.summarise("p_sess"))
        cs2 = ConversationSummariser(db_path=db)
        s = cs2.get_summary("p_sess")
        self.assertIsNotNone(s)

    def test_sliding_window(self):
        from agent.conversation_summariser import ConversationSummariser, Turn
        td = tempfile.mkdtemp()
        cs = ConversationSummariser(db_path=os.path.join(td,"cs.db"), window_turns=5)
        long_turns = [Turn("user" if i%2==0 else "assistant", f"Message {i}") for i in range(20)]
        cs.add_turns("window_sess", long_turns)
        s = _run(cs.summarise("window_sess"))
        self.assertLessEqual(s.turns_summarised, 6)

# ════════════════════════════════════════════════════════
# CACHE WARMER
# ════════════════════════════════════════════════════════
class TestCacheWarmer(unittest.TestCase):
    def setUp(self):
        td = tempfile.mkdtemp()
        from agent.cache_warmer import CacheWarmer
        self.cw = CacheWarmer(db_path=os.path.join(td, "cw.db"), default_ttl=60.0)

    def test_set_and_get_hit(self):
        self.cw.set("key1", "value1")
        val, hit = self.cw.get("key1")
        self.assertTrue(hit); self.assertEqual(val, "value1")

    def test_get_miss(self):
        val, hit = self.cw.get("nonexistent_key_xyz")
        self.assertFalse(hit); self.assertIsNone(val)

    def test_delete(self):
        self.cw.set("del_key", "val")
        self.cw.delete("del_key")
        _, hit = self.cw.get("del_key")
        self.assertFalse(hit)

    def test_ttl_expiry(self):
        from agent.cache_warmer import CacheEntry
        entry = CacheEntry(key="expired", value="x", ttl=0.001)
        self.cw._in_memory["expired"] = entry
        time.sleep(0.01)
        _, hit = self.cw.get("expired")
        self.assertFalse(hit)

    def test_flush_expired(self):
        from agent.cache_warmer import CacheEntry
        self.cw._in_memory["exp1"] = CacheEntry(key="exp1", value="a", ttl=0.001)
        time.sleep(0.01)
        n = self.cw.flush_expired()
        self.assertGreaterEqual(n, 1)

    def test_get_or_generate_cache_hit(self):
        self.cw.set("gen_key", "cached_value")
        val = _run(self.cw.get_or_generate("gen_key"))
        self.assertEqual(val, "cached_value")

    def test_get_or_generate_miss_with_fn(self):
        from agent.cache_warmer import CacheWarmer
        td = tempfile.mkdtemp()
        cw = CacheWarmer(generator_fn=lambda k: f"generated:{k}",
                          db_path=os.path.join(td,"cw2.db"))
        val = _run(cw.get_or_generate("new_prompt"))
        self.assertEqual(val, "generated:new_prompt")

    def test_get_or_generate_no_fn(self):
        val = _run(self.cw.get_or_generate("no_fn_key"))
        self.assertIsNone(val)

    def test_warm_list(self):
        from agent.cache_warmer import CacheWarmer
        td = tempfile.mkdtemp()
        counter = [0]
        def gen(k): counter[0] += 1; return f"ans:{k}"
        cw = CacheWarmer(generator_fn=gen, db_path=os.path.join(td,"cw3.db"))
        prompts = ["p1","p2","p3"]
        job = _run(cw.warm_list(prompts))
        self.assertTrue(job.completed)
        self.assertEqual(job.hits_generated, 3)
        self.assertEqual(counter[0], 3)

    def test_warm_top_k_no_fn(self):
        self.cw.set("k1","v1"); self.cw.get("k1"); self.cw.get("k1")
        job = _run(self.cw.warm_top_k(k=5))
        self.assertTrue(job.completed)

    def test_priority_stored(self):
        self.cw.set("pri_key", "value", priority=10)
        val, hit = self.cw.get("pri_key")
        self.assertTrue(hit)

    def test_top_keys(self):
        self.cw.set("popular", "val")
        for _ in range(3): self.cw.get("popular")
        self.cw.get("popular")  # one more
        keys = self.cw.top_keys(10)
        self.assertIsInstance(keys, list)

    def test_stats(self):
        self.cw.set("s1","v1"); self.cw.get("s1"); self.cw.get("missing")
        s = self.cw.stats()
        for k in ["cached_entries","total_accesses","cache_hits","hit_rate"]: self.assertIn(k, s)

    def test_hit_rate_calculation(self):
        self.cw.set("hr","val")
        self.cw.get("hr"); self.cw.get("hr"); self.cw.get("miss1")
        s = self.cw.stats()
        self.assertGreater(s["hit_rate"], 0.0)

    def test_async_generator_fn(self):
        from agent.cache_warmer import CacheWarmer
        td = tempfile.mkdtemp()
        async def agen(k): await asyncio.sleep(0.001); return f"async:{k}"
        cw = CacheWarmer(generator_fn=agen, db_path=os.path.join(td,"cw4.db"))
        val = _run(cw.get_or_generate("async_key"))
        self.assertEqual(val, "async:async_key")

    def test_warming_job_to_dict(self):
        from agent.cache_warmer import WarmingJob
        job = WarmingJob(id="j1", prompts=["a","b","c"], priority=5)
        d = job.to_dict()
        for k in ["id","prompt_count","priority","completed","hits_generated"]: self.assertIn(k, d)

    def test_cache_entry_to_dict(self):
        from agent.cache_warmer import CacheEntry
        e = CacheEntry(key="k", value="v", ttl=3600)
        d = e.to_dict()
        for k in ["key","ttl","hits","priority","age_s","expired"]: self.assertIn(k, d)

    def test_persistence(self):
        from agent.cache_warmer import CacheWarmer
        td = tempfile.mkdtemp(); db = os.path.join(td,"cw.db")
        cw1 = CacheWarmer(db_path=db, default_ttl=3600)
        cw1.set("persist_key", "persist_value")
        cw2 = CacheWarmer(db_path=db, default_ttl=3600)
        val, hit = cw2.get("persist_key")
        self.assertTrue(hit); self.assertEqual(val, "persist_value")

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v19: {total-failed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  FAIL: {t}\n    {tb.strip().splitlines()[-1]}")
    else: print(f"  ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
