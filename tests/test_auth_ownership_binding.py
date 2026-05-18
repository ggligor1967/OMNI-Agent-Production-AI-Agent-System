import io
import os
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestAuthOwnershipHelpers(unittest.TestCase):
    def test_effective_user_id_prefers_authenticated_context(self):
        from agent.auth import AuthContext, Role, effective_user_id

        ctx = AuthContext(
            authenticated=True,
            user_id="alice",
            role=Role.USER,
            auth_method="jwt",
        )

        self.assertEqual(
            effective_user_id(ctx, requested_user_id="mallory", default_user_id="api_user"),
            "alice",
        )

    def test_scoped_session_id_namespaces_authenticated_users(self):
        from agent.auth import AuthContext, Role, scoped_session_id

        ctx = AuthContext(
            authenticated=True,
            user_id="alice",
            role=Role.USER,
            auth_method="jwt",
        )

        self.assertEqual(
            scoped_session_id(ctx, requested_session_id="shared", default_session_id="api"),
            "user:alice:shared",
        )

    def test_scoped_session_id_rejects_foreign_namespace(self):
        from agent.auth import AuthContext, Role, scoped_session_id

        ctx = AuthContext(
            authenticated=True,
            user_id="alice",
            role=Role.USER,
            auth_method="jwt",
        )

        with self.assertRaises(PermissionError):
            scoped_session_id(
                ctx,
                requested_session_id="user:bob:shared",
                default_session_id="api",
            )

    def test_visible_session_ids_filters_to_authenticated_owner(self):
        from agent.auth import AuthContext, Role, visible_session_ids

        ctx = AuthContext(
            authenticated=True,
            user_id="alice",
            role=Role.ADMIN,
            auth_method="jwt",
        )

        self.assertEqual(
            visible_session_ids(
                ctx,
                [
                    "user:alice:shared",
                    "user:bob:shared",
                    "user:alice:project",
                    "user:alice:shared",
                ],
            ),
            ["user:alice:shared", "user:alice:project"],
        )


class TestAuthOwnershipBindingRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agent.auth import AuthManager, Role, auth_context_from_request, effective_user_id, scoped_session_id
        from agent.export import Exporter
        from agent.memory import MemoryDB

        self._auth_context_from_request = auth_context_from_request
        self._effective_user_id = effective_user_id
        self._scoped_session_id = scoped_session_id

        self.tmpdir = tempfile.mkdtemp()
        self.auth = AuthManager(
            secret="test_secret_32chars_xxxxxxxxxx",
            db_path=os.path.join(self.tmpdir, "auth.db"),
            enforce_auth=True,
            bootstrap_token="bootstrap-secret",
        )
        self.memory = MemoryDB(db_path=os.path.join(self.tmpdir, "memory.db"))
        self.personas = {}

        self.alice_user_headers = {
            "Authorization": f"Bearer {self.auth.create_token('alice', Role.USER)}"
        }
        self.bob_user_headers = {
            "Authorization": f"Bearer {self.auth.create_token('bob', Role.USER)}"
        }
        self.alice_admin_headers = {
            "Authorization": f"Bearer {self.auth.create_token('alice', Role.ADMIN)}"
        }
        self.bob_admin_headers = {
            "Authorization": f"Bearer {self.auth.create_token('bob', Role.ADMIN)}"
        }

        app = web.Application(middlewares=[self.auth.middleware()])

        async def chat(request):
            data = await request.json()
            ctx = self._auth_context_from_request(request)
            user_id = self._effective_user_id(
                ctx,
                requested_user_id=data.get("user_id", "api_user"),
                default_user_id="api_user",
            )
            default_session_id = f"api:{user_id}" if not ctx.authenticated else "api"
            try:
                session_id = self._scoped_session_id(
                    ctx,
                    requested_session_id=data.get("session_id", ""),
                    default_session_id=default_session_id,
                )
            except PermissionError as exc:
                return web.json_response({"error": "forbidden", "detail": str(exc)}, status=403)

            self.memory.add_message(session_id, "user", data.get("message", ""))
            return web.json_response({"user_id": user_id, "session_id": session_id})

        async def persona_set(request):
            ctx = self._auth_context_from_request(request)
            try:
                session_id = self._scoped_session_id(
                    ctx,
                    requested_session_id=request.match_info.get("session_id", ""),
                    default_session_id="persona",
                )
            except PermissionError as exc:
                return web.json_response({"error": "forbidden", "detail": str(exc)}, status=403)

            data = await request.json()
            self.personas[session_id] = data.get("persona", "assistant")
            return web.json_response({"session_id": session_id, "persona": self.personas[session_id]})

        async def persona_get(request):
            ctx = self._auth_context_from_request(request)
            try:
                session_id = self._scoped_session_id(
                    ctx,
                    requested_session_id=request.match_info.get("session_id", ""),
                    default_session_id="persona",
                )
            except PermissionError as exc:
                return web.json_response({"error": "forbidden", "detail": str(exc)}, status=403)

            return web.json_response({
                "session_id": session_id,
                "persona": self.personas.get(session_id, "assistant"),
            })

        app.router.add_post("/chat", chat)
        app.router.add_post("/personas/session/{session_id}", persona_set)
        app.router.add_get("/personas/session/{session_id}", persona_get)

        exporter_agent = SimpleNamespace(memory=self.memory)
        Exporter(exporter_agent).register_routes(app, prefix="")

        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_chat_ignores_spoofed_user_id_and_scopes_session(self):
        response = await self.client.post(
            "/chat",
            headers=self.bob_user_headers,
            json={
                "user_id": "alice",
                "session_id": "shared",
                "message": "hello from bob",
            },
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["user_id"], "bob")
        self.assertEqual(payload["session_id"], "user:bob:shared")

        bob_history = self.memory.get_history("user:bob:shared")
        alice_history = self.memory.get_history("user:alice:shared")
        self.assertEqual(len(bob_history), 1)
        self.assertEqual(bob_history[0]["content"], "hello from bob")
        self.assertEqual(alice_history, [])

    async def test_persona_routes_isolate_same_session_label_per_user(self):
        alice_set = await self.client.post(
            "/personas/session/shared",
            headers=self.alice_user_headers,
            json={"persona": "pirate"},
        )
        self.assertEqual(alice_set.status, 200)

        bob_set = await self.client.post(
            "/personas/session/shared",
            headers=self.bob_user_headers,
            json={"persona": "scientist"},
        )
        self.assertEqual(bob_set.status, 200)

        alice_info = await self.client.get(
            "/personas/session/shared",
            headers=self.alice_user_headers,
        )
        bob_info = await self.client.get(
            "/personas/session/shared",
            headers=self.bob_user_headers,
        )

        self.assertEqual((await alice_info.json())["persona"], "pirate")
        self.assertEqual((await bob_info.json())["persona"], "scientist")

    async def test_persona_route_rejects_explicit_foreign_namespace(self):
        await self.client.post(
            "/personas/session/shared",
            headers=self.alice_user_headers,
            json={"persona": "pirate"},
        )

        response = await self.client.get(
            "/personas/session/user:alice:shared",
            headers=self.bob_user_headers,
        )
        self.assertEqual(response.status, 403)

    async def test_export_conversation_rejects_explicit_foreign_namespace(self):
        self.memory.add_message("user:alice:shared", "assistant", "alice secret")

        response = await self.client.get(
            "/export/conversation/user:alice:shared?format=json",
            headers=self.bob_admin_headers,
        )
        self.assertEqual(response.status, 403)

    async def test_export_dump_filters_sessions_to_authenticated_owner(self):
        self.memory.add_message("user:alice:shared", "assistant", "alice secret")
        self.memory.add_message("user:bob:shared", "assistant", "bob secret")

        response = await self.client.post(
            "/export/dump",
            headers=self.bob_admin_headers,
            json={},
        )
        self.assertEqual(response.status, 200)

        archive_bytes = await response.read()
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = archive.namelist()
            self.assertTrue(any("user_bob_shared" in name for name in names))
            self.assertFalse(any("user_alice_shared" in name for name in names))
