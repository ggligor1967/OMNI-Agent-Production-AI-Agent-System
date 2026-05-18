import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestAuthBootstrapAndEnforcement(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agent.auth import AuthManager

        self.tmpdir = tempfile.mkdtemp()
        self.auth = AuthManager(
            secret="test_secret_32chars_xxxxxxxxxx",
            db_path=os.path.join(self.tmpdir, "auth.db"),
            enforce_auth=True,
            bootstrap_token="bootstrap-secret",
        )

        app = web.Application(middlewares=[
            self.auth.middleware(public_paths=["/status", "/auth/bootstrap"])
        ])

        async def status(request):
            return web.json_response({"ok": True})

        async def chat(request):
            return web.json_response({"ok": True})

        app.router.add_get("/status", status)
        app.router.add_get("/chat", chat)
        self.auth.register_routes(app)

        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def _bootstrap_admin(self):
        resp = await self.client.post("/auth/bootstrap", json={
            "bootstrap_token": "bootstrap-secret",
            "user_id": "bootstrap-admin",
        })
        self.assertEqual(resp.status, 201)
        return await resp.json()

    async def test_public_status_and_protected_chat(self):
        status_resp = await self.client.get("/status")
        self.assertEqual(status_resp.status, 200)

        chat_resp = await self.client.get("/chat")
        self.assertEqual(chat_resp.status, 401)

    async def test_bootstrap_only_works_once(self):
        first = await self._bootstrap_admin()
        self.assertTrue(first["key"].startswith("omni_"))
        self.assertEqual(first["role"], "admin")

        second = await self.client.post("/auth/bootstrap", json={
            "bootstrap_token": "bootstrap-secret",
            "user_id": "another-admin",
        })
        self.assertEqual(second.status, 409)

    async def test_admin_routes_require_admin_credentials(self):
        unauth = await self.client.get("/auth/keys")
        self.assertEqual(unauth.status, 401)

        admin = await self._bootstrap_admin()
        headers = {"X-API-Key": admin["key"]}

        allowed = await self.client.get("/auth/keys", headers=headers)
        self.assertEqual(allowed.status, 200)

        created = await self.client.post("/auth/keys", headers=headers, json={
            "user_id": "regular-user",
            "role": "user",
            "name": "User key",
        })
        self.assertEqual(created.status, 201)
        user_key = (await created.json())["key"]

        denied = await self.client.get("/auth/keys", headers={"X-API-Key": user_key})
        self.assertEqual(denied.status, 403)


class TestToolConfirmationGuards(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_required_tools_fail_closed(self):
        from agent.tools_registry import ToolCall, ToolRegistry

        tools = ToolRegistry()

        @tools.register(
            description="Dangerous tool",
            params=[],
            requires_confirmation=True,
        )
        async def dangerous_tool():
            return {"ran": True}

        blocked = await tools.call(ToolCall(tool_name="dangerous_tool", arguments={}))
        self.assertFalse(blocked.success)
        self.assertIn("confirmation", blocked.error.lower())

        allowed = await tools.call(
            ToolCall(tool_name="dangerous_tool", arguments={}),
            allow_confirmed_tools=True,
        )
        self.assertTrue(allowed.success)
        self.assertEqual(allowed.output, {"ran": True})

    async def test_builtin_execute_python_uses_sandbox_when_explicitly_allowed(self):
        from agent.tools_registry import ToolCall, build_default_tools

        class DummyResult:
            def to_dict(self):
                return {"success": True, "stdout": "sandboxed"}

        class DummySandbox:
            def __init__(self):
                self.last_code = None

            async def run_python(self, code):
                self.last_code = code
                return DummyResult()

        class DummyExecutor:
            def execute_python(self, *args, **kwargs):
                raise AssertionError("execute_python should not use the in-process executor")

        class DummyAgent:
            def __init__(self):
                self.sandbox = DummySandbox()
                self.executor = DummyExecutor()

        agent = DummyAgent()
        tools = build_default_tools(agent)

        result = await tools.call(
            ToolCall(tool_name="execute_python", arguments={
                "code": "print('hello')",
                "safe_mode": True,
            }),
            allow_confirmed_tools=True,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.output["stdout"], "sandboxed")
        self.assertEqual(agent.sandbox.last_code, "print('hello')")

    async def test_builtin_job_search_tool_returns_summary(self):
        from agent.tools_registry import ToolCall, build_default_tools

        expected = {
            "search_date": "2026-05-18",
            "total_results": 11,
            "report_files": {"json": "C:/tmp/report.json", "html": "C:/tmp/report.html"},
        }

        class DummyAgent:
            pass

        tools = build_default_tools(DummyAgent())

        with patch(
            "job_search_tank_adr_improved.run_search_with_summary",
            new=AsyncMock(return_value=expected),
        ):
            result = await tools.call(
                ToolCall(
                    tool_name="run_job_search_tank_adr_improved",
                    arguments={"export_format": "html", "verbose": False},
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.output, expected)
