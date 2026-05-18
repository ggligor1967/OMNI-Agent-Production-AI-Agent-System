import json
import os
import sys
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSecurityEventAudit(unittest.IsolatedAsyncioTestCase):
    async def test_auth_failure_logged_without_credentials(self):
        from agent.auth import AuthManager
        from agent.memory import MemoryDB
        from agent.security_audit import build_memory_audit_callback

        tmpdir = tempfile.mkdtemp()
        memory = MemoryDB(os.path.join(tmpdir, "memory.db"))
        auth = AuthManager(
            secret="test_secret_32chars_xxxxxxxxxx",
            db_path=os.path.join(tmpdir, "auth.db"),
            enforce_auth=True,
        )

        app = web.Application(middlewares=[
            auth.middleware(
                public_paths=["/status"],
                audit_callback=build_memory_audit_callback(memory),
            )
        ])

        async def chat(request):
            return web.json_response({"ok": True})

        app.router.add_get("/chat", chat)

        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            response = await client.get(
                "/chat",
                headers={"X-API-Key": "omni_super_secret_api_key_value_abcdefghijklmnopqrstuvwxyz"},
            )
            self.assertEqual(response.status, 401)
        finally:
            await client.close()

        entry = next(
            row for row in memory.get_audit_log(limit=10)
            if row["action"] == "security.auth_failure"
        )
        details = json.loads(entry["details"])

        self.assertEqual(details["auth_method"], "api_key")
        self.assertEqual(details["path"], "/chat")
        self.assertNotIn("omni_super_secret_api_key_value", entry["details"])

    async def test_tool_execution_logged_without_argument_values(self):
        from agent.memory import MemoryDB
        from agent.security_audit import build_memory_audit_callback
        from agent.tools_registry import ParamType, ToolCall, ToolParam, ToolRegistry

        tmpdir = tempfile.mkdtemp()
        memory = MemoryDB(os.path.join(tmpdir, "memory.db"))
        tools = ToolRegistry(audit_callback=build_memory_audit_callback(memory))

        @tools.register(
            description="Audit test tool",
            params=[
                ToolParam("query", ParamType.STRING, "Query"),
                ToolParam("secret", ParamType.STRING, "Secret", required=False, default=""),
            ],
        )
        async def audit_test_tool(query: str, secret: str = ""):
            return {"query": query, "seen_secret": bool(secret)}

        result = await tools.call(
            ToolCall(
                tool_name="audit_test_tool",
                arguments={
                    "query": "hello world",
                    "secret": "omni_highly_sensitive_token_value_abcdefghijklmnopqrstuvwxyz",
                },
                session_id="session-audit",
            )
        )
        self.assertTrue(result.success)

        entry = next(
            row for row in memory.get_audit_log(limit=10)
            if row["action"] == "security.tool_execution"
        )
        details = json.loads(entry["details"])

        self.assertEqual(details["tool"], "audit_test_tool")
        self.assertEqual(details["session_id"], "session-audit")
        self.assertEqual(details["arg_keys"], ["query", "secret"])
        self.assertNotIn("hello world", entry["details"])
        self.assertNotIn("omni_highly_sensitive_token_value", entry["details"])

    async def test_sandbox_trigger_logged_without_code_contents(self):
        from agent.memory import MemoryDB
        from agent.sandbox import Sandbox
        from agent.security_audit import build_memory_audit_callback

        tmpdir = tempfile.mkdtemp()
        memory = MemoryDB(os.path.join(tmpdir, "memory.db"))
        sandbox = Sandbox(audit_callback=build_memory_audit_callback(memory))

        code = "print('super-secret-token'); value = 2 + 2"
        result = await sandbox.run_python(code)
        self.assertTrue(result.success)

        trigger_entry = next(
            row for row in memory.get_audit_log(limit=10)
            if row["action"] == "security.sandbox_trigger"
        )
        trigger_details = json.loads(trigger_entry["details"])

        self.assertEqual(trigger_details["language"], "python")
        self.assertEqual(trigger_details["code_chars"], len(code))
        self.assertIn("code_sha256", trigger_details)
        self.assertNotIn("super-secret-token", trigger_entry["details"])
        self.assertNotIn("print(", trigger_entry["details"])
