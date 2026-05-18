import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-with-minimum-32-characters")
os.environ.setdefault("AUTH_ENFORCE", "true")
os.environ.setdefault("API_HOST", "127.0.0.1")

from agent.core import OmniAgent
from agent.tools_registry import ParamType, ToolParam, ToolRegistry
from agent.tracing import SpanKind, Tracer, TracingConfig


class _MemoryStub:
    def __init__(self):
        self.messages = []
        self.saved_memories = []
        self.audits = []

    def add_message(self, session_id, role, content, metadata=None):
        self.messages.append(
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                "metadata": metadata or {},
            }
        )

    def get_history(self, session_id, limit=50):
        history = [
            {"role": item["role"], "content": item["content"], "timestamp": None}
            for item in self.messages
            if item["session_id"] == session_id
        ]
        return history[-limit:]

    def save_memory(self, key, value, category="general", importance=5):
        self.saved_memories.append(
            {
                "key": key,
                "value": value,
                "category": category,
                "importance": importance,
            }
        )

    def audit(self, action, actor="system", details=None):
        self.audits.append({"action": action, "actor": actor, "details": details or {}})


class _SecurityStub:
    def sanitize_input(self, text):
        return text

    def check_prompt_injection(self, text):
        return {"safe": True, "threats": []}

    def rate_check(self, user_id):
        return {"allowed": True, "retry_after": 0}


class _SummarizerStub:
    threshold = 999

    async def maybe_compress(self, messages):
        return messages, None


class _CacheStub:
    def __init__(self, cached_response=None):
        self.cached_response = cached_response
        self.last_key = None
        self.stored = None

    def _response_key(self, model_id, messages):
        self.last_key = (model_id, tuple((m["role"], m["content"]) for m in messages))
        return "cache-key"

    async def get(self, key):
        return self.cached_response

    async def set(self, key, value, ttl):
        self.stored = {"key": key, "value": value, "ttl": ttl}


class _LLMStub:
    def __init__(self, response_data=None, available=True):
        self.response_data = response_data or {
            "content": "default response",
            "_routed_to": "demo-model",
            "_task_type": "general",
            "_latency_ms": 12.5,
            "output_tokens": 12,
        }
        self.available = available
        self.calls = []

    async def is_available(self):
        return self.available

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.response_data)


class _BrokenTraceManager:
    async def __aenter__(self):
        raise RuntimeError("trace enter failed")

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _BrokenTracer:
    def async_span(self, *args, **kwargs):
        return _BrokenTraceManager()

    def llm_span(self, *args, **kwargs):
        return _BrokenTraceManager()


def _build_agent(
    *,
    tracer=None,
    quick_response="",
    cached_response=None,
    llm_response=None,
    tool_registry=None,
):
    agent = OmniAgent.__new__(OmniAgent)
    agent.tracer = tracer if tracer is not None else Tracer(settings=TracingConfig())
    agent.security = _SecurityStub()
    agent.memory = _MemoryStub()
    agent.conversations = SimpleNamespace(
        process=lambda session_id, user_id, text: {"quick_response": quick_response}
    )
    agent.skills = SimpleNamespace(find_by_trigger=lambda text: [])
    agent.summarizer = _SummarizerStub()
    agent.rag = SimpleNamespace()
    agent.cache = _CacheStub(cached_response=cached_response)
    agent.router = SimpleNamespace(get_session_model=lambda session_id: None)
    agent.llm = _LLMStub(response_data=llm_response)
    agent.persona_manager = SimpleNamespace(
        build_system_prompt=lambda session_id, user_id, prompt: prompt,
        auto_detect_and_set=lambda session_id, text: None,
    )
    agent.config_mgr = SimpleNamespace(flag=lambda name: False)
    agent.tool_registry = tool_registry or ToolRegistry()
    agent.analyzer = SimpleNamespace(
        analyze=lambda text: {
            "word_count": len(text.split()),
            "sentiment": {"label": "neutral"},
            "keywords": [],
        }
    )
    return agent


@pytest.mark.asyncio
async def test_chat_quick_response_traces_hashed_context_without_raw_values():
    tracer = Tracer(settings=TracingConfig())
    agent = _build_agent(tracer=tracer, quick_response="Quick hello!")

    response = await OmniAgent.chat(agent, "alice", "sess-quick", "hello there")

    assert response == "Quick hello!"
    chat_span = next(span for span in tracer.get_spans() if span.name == "chat.request")
    assert chat_span.kind == SpanKind.PIPELINE
    assert chat_span.attributes["user_id_hash"].startswith("sha256:")
    assert chat_span.attributes["session_id_hash"].startswith("sha256:")
    assert chat_span.attributes["result_path"] == "quick_response"
    assert "user_id" not in chat_span.attributes
    assert "session_id" not in chat_span.attributes

    all_attribute_values = [str(value) for span in tracer.get_spans() for value in span.attributes.values()]
    assert "alice" not in all_attribute_values
    assert "sess-quick" not in all_attribute_values
    assert "hello there" not in all_attribute_values


@pytest.mark.asyncio
async def test_chat_generated_tool_flow_emits_chat_llm_and_tool_spans():
    tracer = Tracer(settings=TracingConfig())
    registry = ToolRegistry()

    @registry.register(
        description="Echo a query",
        params=[ToolParam("query", ParamType.STRING, "Query text")],
    )
    async def echo(query: str):
        return {"echo": query}

    agent = _build_agent(
        tracer=tracer,
        llm_response={
            "content": "Before [TOOL: echo(query='hello tool')] After",
            "_routed_to": "demo-model",
            "_task_type": "general",
            "_latency_ms": 18.4,
            "output_tokens": 21,
        },
        tool_registry=registry,
    )

    response = await OmniAgent.chat(agent, "bob", "sess-tool", "please use a tool")

    assert "hello tool" in response

    spans = tracer.get_spans()
    span_names = {span.name for span in spans}
    assert "chat.request" in span_names
    assert "chat.tool_processing" in span_names
    assert "tool.echo" in span_names
    assert any(span.kind == SpanKind.LLM for span in spans)

    chat_span = next(span for span in spans if span.name == "chat.request")
    llm_span = next(span for span in spans if span.kind == SpanKind.LLM)
    tool_span = next(span for span in spans if span.name == "tool.echo")

    assert chat_span.attributes["result_path"] == "generated"
    assert chat_span.attributes["tool_directive_present"] is True
    assert llm_span.attributes["model"] == "demo-model"
    assert llm_span.attributes["task_type"] == "general"
    assert llm_span.attributes["session_id_hash"].startswith("sha256:")
    assert "prompt_text" not in llm_span.attributes
    assert tool_span.attributes["tool_name"] == "echo"
    assert tool_span.attributes["arg_count"] == 1
    assert tool_span.attributes["success"] is True

    all_attribute_values = [str(value) for span in spans for value in span.attributes.values()]
    assert "bob" not in all_attribute_values
    assert "sess-tool" not in all_attribute_values
    assert "please use a tool" not in all_attribute_values


@pytest.mark.asyncio
async def test_chat_still_returns_quick_response_when_tracing_fails_to_start():
    agent = _build_agent(tracer=_BrokenTracer(), quick_response="Quick hello!")

    response = await OmniAgent.chat(agent, "alice", "sess-broken", "hello there")

    assert response == "Quick hello!"
    assert [message["role"] for message in agent.memory.messages] == ["user", "assistant"]
