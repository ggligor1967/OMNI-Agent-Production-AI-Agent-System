import json
import os
import sys
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestAuthBootstrapJsonErrors(unittest.IsolatedAsyncioTestCase):
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
            self.auth.middleware(public_paths=["/auth/bootstrap"])
        ])
        self.auth.register_routes(app)

        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_bootstrap_malformed_json_returns_sanitized_400(self):
        response = await self.client.post(
            "/auth/bootstrap",
            data='{"bootstrap_token":',
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status, 400)
        payload = await response.json()
        self.assertEqual(payload["error"], "invalid_json")
        self.assertEqual(payload["detail"], "Malformed JSON request body")

        body_text = json.dumps(payload)
        self.assertNotIn("JSONDecodeError", body_text)
        self.assertNotIn("Expecting value", body_text)
        self.assertNotIn("traceback", body_text.lower())

    async def test_bootstrap_rejects_non_json_content_type_with_400(self):
        response = await self.client.post(
            "/auth/bootstrap",
            data="bootstrap_token=bootstrap-secret",
            headers={"Content-Type": "text/plain"},
        )

        self.assertEqual(response.status, 400)
        payload = await response.json()
        self.assertEqual(payload, {
            "error": "invalid_request",
            "detail": "Content-Type must be application/json",
        })

    async def test_bootstrap_rejects_non_object_json_body(self):
        response = await self.client.post(
            "/auth/bootstrap",
            data='["bootstrap-secret"]',
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status, 400)
        payload = await response.json()
        self.assertEqual(payload, {
            "error": "invalid_request",
            "detail": "JSON body must be an object",
        })
