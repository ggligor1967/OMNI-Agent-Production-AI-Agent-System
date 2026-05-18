import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import CONFIG
from agent.auth import AuthManager
from agent.multimodal import VisionPipeline
from agent.streaming import BusMessage, EventBusEvent, bus
from main import run_api

PNG_1X1_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5WvuoAAAAASUVORK5CYII="
)


class DummyMemory:
    def __init__(self):
        self._history = {}

    def get_history(self, session_id, limit=20):
        return self._history.get(session_id, [])[-limit:]

    def add_message(self, session_id, role, content):
        self._history.setdefault(session_id, []).append({"role": role, "content": content})

    def get_audit_log(self, limit=30):
        return [{"event": "probe", "limit": limit}]

    def get_state(self, key):
        return "ok"


class DummyRouter:
    def __init__(self):
        self._session_models = {}

    def set_session_model(self, session_id, model):
        self._session_models[session_id] = model
        return True

    def clear_session_model(self, session_id):
        self._session_models.pop(session_id, None)

    def get_session_model(self, session_id):
        return self._session_models.get(session_id)


class DummyLLM:
    def __init__(self):
        self.router = DummyRouter()

    def get_router_summary(self):
        return {"total_models": 0}

    def get_stats(self):
        return []

    async def _list_ollama_models(self, cache_seconds=0):
        return set()

    async def chat_parallel(self, messages, model_ids=None):
        return {}

    async def chat(self, messages, model=None, session_id=None, auto_route=False):
        return {"content": "vision-ok"}

    async def chat_stream(self, messages, model=None, session_id=None):
        for token in ["hello", " world"]:
            yield token


class NoopRoutes:
    def register_routes(self, app, prefix=""):
        return None


class DummyAgent:
    def __init__(self, tmpdir):
        self.memory = DummyMemory()
        self.llm = DummyLLM()
        self.vision = VisionPipeline(llm=self.llm)
        self.auth = AuthManager(
            secret="test_secret_32chars_xxxxxxxxxx",
            db_path=os.path.join(tmpdir, "auth.db"),
            enforce_auth=True,
            bootstrap_token="bootstrap-secret",
        )
        self.config_mgr = NoopRoutes()
        self.exporter = NoopRoutes()


async def _bootstrap_key(base_url: str) -> str:
    async with ClientSession() as session:
        resp = await session.post(
            f"{base_url}/auth/bootstrap",
            json={"bootstrap_token": "bootstrap-secret", "user_id": "coverage-admin"},
        )
        resp.raise_for_status()
        data = await resp.json()
        return data["key"]


async def _get_text(session: ClientSession, url: str, **kwargs) -> tuple[int, str]:
    async with session.get(url, **kwargs) as resp:
        return resp.status, await resp.text()


async def _post_json(session: ClientSession, url: str, **kwargs) -> tuple[int, dict]:
    async with session.post(url, **kwargs) as resp:
        return resp.status, await resp.json()


async def main():
    CONFIG.API_HOST = "127.0.0.1"
    CONFIG.API_PORT = 18080
    CONFIG.API_FALLBACK_PORTS = [18081, 18082, 18083]

    tmpdir = tempfile.mkdtemp(prefix="omni_cov_")
    agent = DummyAgent(tmpdir)
    runner, port = await run_api(agent)
    base_url = f"http://127.0.0.1:{port}"

    results = {}
    try:
        admin_key = await _bootstrap_key(base_url)
        headers = {"X-API-Key": admin_key}

        async with ClientSession() as session:
            results["dashboard"] = await _get_text(session, f"{base_url}/dashboard")
            results["vision_models_unauth"] = await _get_text(session, f"{base_url}/vision/models")
            results["vision_models"] = await _get_text(
                session, f"{base_url}/vision/models", headers=headers
            )
            results["vision_analyze"] = await _post_json(
                session,
                f"{base_url}/vision/analyze",
                headers=headers,
                json={"source": PNG_1X1_DATA_URI, "task": "describe"},
            )
            results["stream_chat"] = await _get_text(
                session,
                f"{base_url}/stream/chat?prompt=hello&session_id=cov-stream",
                headers=headers,
            )

            async def publish_probe_event():
                await asyncio.sleep(0.02)
                await bus.publish(BusMessage(EventBusEvent.SYSTEM, "probe-event", session_id="cov-stream"))

            publish_task = asyncio.create_task(publish_probe_event())
            results["stream_events"] = await _get_text(
                session,
                f"{base_url}/stream/events?session_id=cov-stream&timeout=0.05",
                headers=headers,
            )
            await publish_task

            results["stream_traces"] = await _get_text(
                session,
                f"{base_url}/stream/traces?timeout=0.02",
                headers=headers,
            )
            results["stream_stats"] = await _get_text(
                session,
                f"{base_url}/stream/stats",
                headers=headers,
            )

        assert results["dashboard"][0] == 200
        assert results["vision_models_unauth"][0] == 401
        assert results["vision_models"][0] == 200
        assert results["vision_analyze"][0] == 200
        assert "vision-ok" in results["vision_analyze"][1]["response"]
        assert results["stream_chat"][0] == 200
        assert "event: token" in results["stream_chat"][1]
        assert results["stream_events"][0] == 200
        assert "probe-event" in results["stream_events"][1]
        assert results["stream_traces"][0] == 200
        assert results["stream_stats"][0] == 200

        print({
            "port": port,
            "dashboard": results["dashboard"][0],
            "vision_models_unauth": results["vision_models_unauth"][0],
            "vision_models": results["vision_models"][0],
            "vision_analyze": results["vision_analyze"][0],
            "stream_chat": results["stream_chat"][0],
            "stream_events": results["stream_events"][0],
            "stream_traces": results["stream_traces"][0],
            "stream_stats": results["stream_stats"][0],
        })
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
