import json
import os
import sys
from collections import deque

import pytest


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _async_return(value):
    async def _inner():
        return value

    return _inner


class _FakeStream:
    def __init__(self, lines):
        self._lines = [line if isinstance(line, bytes) else line.encode("utf-8") for line in lines]

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for line in self._lines:
            yield line


class _FakeResponse:
    def __init__(self, *, status=200, json_data=None, text_data="", lines=None):
        self.status = status
        self._json_data = {} if json_data is None else json_data
        self._text_data = text_data
        self.content = _FakeStream(lines or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data


class _FakeSession:
    def __init__(self, *, get_responses=None, post_responses=None, timeout=None):
        self.get_calls = []
        self.post_calls = []
        self.closed = False
        self.timeout = timeout
        self._get_responses = deque(get_responses or [])
        self._post_responses = deque(post_responses or [])

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        if not self._get_responses:
            raise AssertionError(f"Unexpected GET request for {url}")
        return self._get_responses.popleft()

    def post(self, url, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        if not self._post_responses:
            raise AssertionError(f"Unexpected POST request for {url}")
        return self._post_responses.popleft()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_get_session_reuses_open_client_session_and_close_recreates_when_needed(monkeypatch):
    from agent.ollama_client import OllamaClient
    import agent.ollama_client as ollama_module

    created_sessions = []

    def fake_client_session(*, timeout):
        session = _FakeSession(timeout=timeout)
        created_sessions.append(session)
        return session

    monkeypatch.setattr(ollama_module.aiohttp, "ClientSession", fake_client_session)

    client = OllamaClient(base_url="http://ollama.local", model="demo-model")

    first = await client._get_session()
    second = await client._get_session()

    assert first is second
    assert len(created_sessions) == 1
    assert getattr(first.timeout, "total", None) == 120

    await client.close()
    assert first.closed is True

    third = await client._get_session()
    assert third is not first
    assert len(created_sessions) == 2


@pytest.mark.asyncio
async def test_health_and_model_listing_use_tags_endpoint(monkeypatch):
    from agent.ollama_client import OllamaClient

    fake_session = _FakeSession(
        get_responses=[
            _FakeResponse(status=200, json_data={"models": []}),
            _FakeResponse(status=200, json_data={"models": [{"name": "llama3"}, {"name": "qwen3"}]}),
        ]
    )

    client = OllamaClient(base_url="http://ollama.local", model="demo-model")
    monkeypatch.setattr(client, "_get_session", _async_return(fake_session))

    assert await client.is_available() is True
    assert await client.list_models() == ["llama3", "qwen3"]
    assert [call["url"] for call in fake_session.get_calls] == [
        "http://ollama.local/api/tags",
        "http://ollama.local/api/tags",
    ]


@pytest.mark.asyncio
async def test_pull_model_streams_json_status_updates(monkeypatch):
    from agent.ollama_client import OllamaClient

    fake_session = _FakeSession(
        post_responses=[
            _FakeResponse(
                lines=[
                    json.dumps({"status": "pulling manifest"}) + "\n",
                    b"\n",
                    json.dumps({"status": "success"}) + "\n",
                ]
            )
        ]
    )

    client = OllamaClient(base_url="http://ollama.local", model="demo-model")
    monkeypatch.setattr(client, "_get_session", _async_return(fake_session))

    updates = [item async for item in client.pull_model("phi4")]

    assert updates == [{"status": "pulling manifest"}, {"status": "success"}]
    assert fake_session.post_calls[0]["url"] == "http://ollama.local/api/pull"
    assert fake_session.post_calls[0]["json"] == {"name": "phi4", "stream": True}


@pytest.mark.asyncio
async def test_chat_normalizes_response_and_sends_optional_fields(monkeypatch):
    from agent.ollama_client import OllamaClient

    fake_session = _FakeSession(
        post_responses=[
            _FakeResponse(
                status=200,
                json_data={
                    "message": {
                        "role": "assistant",
                        "content": "hello from ollama",
                        "tool_calls": [{"id": "tool-1"}],
                    },
                    "model": "phi4",
                    "done": False,
                    "prompt_eval_count": 12,
                    "eval_count": 34,
                },
            )
        ]
    )

    client = OllamaClient(base_url="http://ollama.local", model="demo-model")
    monkeypatch.setattr(client, "_get_session", _async_return(fake_session))

    response = await client.chat(
        messages=[{"role": "user", "content": "hello"}],
        model="phi4",
        temperature=0.25,
        system="You are helpful.",
        tools=[{"type": "function", "function": {"name": "lookup"}}],
    )

    assert response == {
        "role": "assistant",
        "content": "hello from ollama",
        "tool_calls": [{"id": "tool-1"}],
        "model": "phi4",
        "done": False,
        "prompt_eval_count": 12,
        "eval_count": 34,
    }

    payload = fake_session.post_calls[0]["json"]
    assert payload == {
        "model": "phi4",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "options": {"temperature": 0.25},
        "system": "You are helpful.",
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }


@pytest.mark.asyncio
async def test_chat_raises_runtime_error_on_non_200_response(monkeypatch):
    from agent.ollama_client import OllamaClient

    fake_session = _FakeSession(
        post_responses=[_FakeResponse(status=503, text_data="service unavailable")]
    )

    client = OllamaClient(base_url="http://ollama.local", model="demo-model")
    monkeypatch.setattr(client, "_get_session", _async_return(fake_session))

    with pytest.raises(RuntimeError, match="Ollama error 503: service unavailable"):
        await client.chat(messages=[{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_chat_stream_yields_only_non_empty_content_until_done(monkeypatch):
    from agent.ollama_client import OllamaClient

    fake_session = _FakeSession(
        post_responses=[
            _FakeResponse(
                lines=[
                    json.dumps({"message": {"content": "Hel"}, "done": False}) + "\n",
                    json.dumps({"message": {"content": ""}, "done": False}) + "\n",
                    json.dumps({"message": {"content": "lo"}, "done": False}) + "\n",
                    json.dumps({"message": {"content": ""}, "done": True}) + "\n",
                ]
            )
        ]
    )

    client = OllamaClient(base_url="http://ollama.local", model="demo-model")
    monkeypatch.setattr(client, "_get_session", _async_return(fake_session))

    chunks = [
        chunk
        async for chunk in client.chat_stream(
            messages=[{"role": "user", "content": "stream this"}],
            model="phi4",
            temperature=0.1,
            system="stream system",
        )
    ]

    assert chunks == ["Hel", "lo"]
    payload = fake_session.post_calls[0]["json"]
    assert payload["stream"] is True
    assert payload["system"] == "stream system"
    assert payload["options"] == {"temperature": 0.1}


@pytest.mark.asyncio
async def test_generate_and_embed_helpers_return_payload_data(monkeypatch):
    from agent.ollama_client import OllamaClient

    fake_session = _FakeSession(
        post_responses=[
            _FakeResponse(status=200, json_data={"response": "generated text"}),
            _FakeResponse(status=200, json_data={"embeddings": [[0.1, 0.2, 0.3]]}),
            _FakeResponse(status=200, json_data={"embeddings": []}),
        ]
    )

    client = OllamaClient(base_url="http://ollama.local", model="demo-model")
    monkeypatch.setattr(client, "_get_session", _async_return(fake_session))

    generated = await client.generate(
        "Write a haiku",
        model="phi4",
        temperature=0.4,
        max_tokens=77,
    )
    embedded = await client.embed("semantic text", model="embed-small")
    empty_embed = await client.embed("semantic text", model="embed-small")

    assert generated == "generated text"
    assert embedded == [0.1, 0.2, 0.3]
    assert empty_embed == []

    generate_payload = fake_session.post_calls[0]["json"]
    embed_payload = fake_session.post_calls[1]["json"]
    assert generate_payload == {
        "model": "phi4",
        "prompt": "Write a haiku",
        "stream": False,
        "options": {"temperature": 0.4, "num_predict": 77},
    }
    assert embed_payload == {"model": "embed-small", "input": "semantic text"}


@pytest.mark.asyncio
async def test_embed_batch_and_tool_spec_cover_helper_paths(monkeypatch):
    from agent.ollama_client import OllamaClient

    client = OllamaClient(base_url="http://ollama.local", model="demo-model")

    async def fake_embed(text, model=None):
        return [len(text), len(model or "")]

    monkeypatch.setattr(client, "embed", fake_embed)

    batch = await client.embed_batch(["a", "four"], model="embed-small")
    spec = client.build_tool_spec(
        "lookup",
        "Look up a record by id",
        {
            "record_id": {"type": "string", "description": "ID to fetch"},
            "limit": {"type": "integer", "description": "Max results"},
        },
    )

    assert batch == [[1, 11], [4, 11]]
    assert spec == {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look up a record by id",
            "parameters": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string", "description": "ID to fetch"},
                    "limit": {"type": "integer", "description": "Max results"},
                },
                "required": ["record_id", "limit"],
            },
        },
    }
