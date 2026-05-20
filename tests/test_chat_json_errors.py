import json
import os
import shutil
import sys
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("SECRET_KEY", "test-secret-key-with-minimum-32-characters")
os.environ.setdefault("AUTH_ENFORCE", "true")
os.environ.setdefault("API_HOST", "127.0.0.1")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main
from agent.auth import AuthManager, Role, auth_context_from_request, effective_user_id, scoped_session_id


class TestChatJsonErrors(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.auth = AuthManager(
            secret="test_secret_32chars_xxxxxxxxxx",
            db_path=os.path.join(self.tmpdir, "auth.db"),
            enforce_auth=True,
            bootstrap_token="bootstrap-secret",
        )
        self.admin_token = self.auth.create_token("alice", Role.ADMIN)
        self.admin_headers = {"Authorization": f"Bearer {self.admin_token}"}

        app = web.Application(middlewares=[
            self.auth.middleware(public_paths=["/status", "/health"])
        ])

        async def status(_request):
            return web.json_response({"ok": True})

        async def health(_request):
            return web.json_response({"ok": True})

        async def chat(request):
            data, error_response = await main._parse_json_object_request(request)
            if error_response:
                return error_response

            ctx = auth_context_from_request(request)
            user_id = effective_user_id(
                ctx,
                requested_user_id=data.get("user_id", "api_user"),
                default_user_id="api_user",
            )
            default_session_id = f"api:{user_id}" if not ctx.authenticated else "api"
            try:
                session_id = scoped_session_id(
                    ctx,
                    requested_session_id=data.get("session_id", ""),
                    default_session_id=default_session_id,
                )
            except PermissionError as exc:
                return web.json_response(
                    {"error": "forbidden", "detail": str(exc)},
                    status=403,
                )

            text = data.get("message", "")
            if not text:
                return web.json_response({"error": "message required"}, status=400)

            return web.json_response(
                {
                    "response": f"reply:{user_id}:{session_id}:{text}",
                    "session_id": session_id,
                    "model": "auto",
                }
            )

        app.router.add_get("/status", status)
        app.router.add_get("/health", health)
        app.router.add_post("/chat", chat)

        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_authenticated_malformed_json_returns_sanitized_400(self):
        response = await self.client.post(
            "/chat",
            data='{"message":',
            headers={**self.admin_headers, "Content-Type": "application/json"},
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")

        payload = await response.json()
        self.assertEqual(
            payload,
            {"error": "invalid_json", "detail": "Malformed JSON request body"},
        )

        body_text = json.dumps(payload)
        self.assertNotIn("traceback", body_text.lower())
        self.assertNotIn("JSONDecodeError", body_text)
        self.assertNotIn("SECRET_KEY", body_text)
        self.assertNotIn("test_secret_32chars_xxxxxxxxxx", body_text)
        self.assertNotIn(self.admin_token, body_text)
        self.assertNotIn('{"message":', body_text)

    async def test_authenticated_wrong_content_type_returns_bounded_400(self):
        response = await self.client.post(
            "/chat",
            data='{"message":"hello"}',
            headers={**self.admin_headers, "Content-Type": "text/plain"},
        )

        self.assertEqual(response.status, 400)
        payload = await response.json()
        self.assertEqual(
            payload,
            {
                "error": "invalid_request",
                "detail": "Content-Type must be application/json",
            },
        )

    async def test_missing_auth_malformed_json_remains_401(self):
        response = await self.client.post(
            "/chat",
            data='{"message":',
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status, 401)
        payload = await response.json()
        self.assertEqual(payload["error"], "unauthorized")

    async def test_invalid_auth_malformed_json_remains_401(self):
        response = await self.client.post(
            "/chat",
            data='{"message":',
            headers={
                "Authorization": "Bearer invalid-token-for-json-regression",
                "Content-Type": "application/json",
            },
        )

        self.assertEqual(response.status, 401)
        payload = await response.json()
        self.assertEqual(payload["error"], "unauthorized")

    async def test_status_and_health_remain_public(self):
        status_response = await self.client.get("/status")
        health_response = await self.client.get("/health")

        self.assertEqual(status_response.status, 200)
        self.assertEqual(health_response.status, 200)
        self.assertEqual(await status_response.json(), {"ok": True})
        self.assertEqual(await health_response.json(), {"ok": True})

    async def test_valid_authenticated_chat_behavior_remains_unchanged(self):
        response = await self.client.post(
            "/chat",
            json={"message": "hello", "session_id": "chat"},
            headers=self.admin_headers,
        )

        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["response"], "reply:alice:user:alice:chat:hello")
        self.assertEqual(payload["session_id"], "user:alice:chat")
        self.assertEqual(payload["model"], "auto")
