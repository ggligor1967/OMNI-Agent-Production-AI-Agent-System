import pytest

from agent.model_registry import get_model
from agent.model_router import ModelRouter, RouteDecision, TaskType
from agent.multi_model_client import MultiModelClient
from agent.tracing import tracer


class _SuccessfulBackend:
    def __init__(self, content="ok"):
        self.content = content
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": self.content}


class _FailingBackend:
    def __init__(self, error="boom"):
        self.error = error
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError(self.error)


@pytest.fixture(autouse=True)
def clear_global_tracer():
    tracer.clear()
    yield
    tracer.clear()


def test_model_router_route_emits_safe_tracing_span(tmp_path):
    router = ModelRouter(db_path=str(tmp_path / "router.db"), default_model="qwen3-next:80b-cloud")
    prompt = "Write a Python function to sort a list"

    decision = router.route(prompt, session_id="sess-route")

    route_span = next(span for span in tracer.get_spans() if span.name == "router.route")
    assert route_span.attributes["task_type"] == "code"
    assert route_span.attributes["selected_model"] == decision.model_id
    assert route_span.attributes["candidate_count"] > 0
    assert route_span.attributes["fallback_count"] == len(decision.fallback_chain)

    attribute_values = [str(value) for value in route_span.attributes.values()]
    assert prompt not in attribute_values
    assert "sess-route" not in attribute_values


@pytest.mark.asyncio
async def test_multi_model_client_chat_traces_route_request_and_model_attempt(monkeypatch):
    client = MultiModelClient()
    prompt = "Write a Python function to sort a list"
    routed_decision = client.router.route(prompt)
    tracer.clear()

    backend = _SuccessfulBackend(content="hello from routed model")

    async def fake_available_registered_models(cache_seconds=30):
        return [routed_decision.model_id]

    monkeypatch.setattr(client, "available_registered_models", fake_available_registered_models)
    monkeypatch.setattr(client, "_get_client", lambda spec: backend)

    response = await client.chat(
        messages=[{"role": "user", "content": prompt}],
        session_id="sess-auto",
        auto_route=True,
    )

    span_names = {span.name for span in tracer.get_spans()}
    assert "llm.route_request" in span_names
    assert "router.route" in span_names
    assert "llm.execute_with_fallback" in span_names
    assert "llm.model_attempt" in span_names

    route_span = next(span for span in tracer.get_spans() if span.name == "llm.route_request")
    attempt_span = next(span for span in tracer.get_spans() if span.name == "llm.model_attempt")

    assert route_span.attributes["selected_model"] == routed_decision.model_id
    assert route_span.attributes["task_type"] == "code"
    assert attempt_span.attributes["model"] == routed_decision.model_id
    assert attempt_span.attributes["success"] is True
    assert response["_routed_to"] == routed_decision.model_id

    all_attribute_values = [str(value) for span in tracer.get_spans() for value in span.attributes.values()]
    assert prompt not in all_attribute_values
    assert "sess-auto" not in all_attribute_values


@pytest.mark.asyncio
async def test_execute_with_fallback_traces_failed_primary_and_successful_secondary(monkeypatch):
    client = MultiModelClient()
    primary = "qwen3-coder-next:cloud"
    secondary = "qwen3-next:80b-cloud"
    decision = RouteDecision(
        model_id=primary,
        model_spec=get_model(primary),
        task_type=TaskType.CODE,
        confidence=0.95,
        reason="test fallback path",
        fallback_chain=[secondary],
    )

    backends = {
        primary: _FailingBackend(error="primary down"),
        secondary: _SuccessfulBackend(content="fallback ok"),
    }

    async def fake_available_registered_models(cache_seconds=30):
        return [primary, secondary]

    monkeypatch.setattr(client, "available_registered_models", fake_available_registered_models)
    monkeypatch.setattr(client, "_get_client", lambda spec: backends[spec.id])

    response = await client._execute_with_fallback(
        decision=decision,
        messages=[{"role": "user", "content": "sort list"}],
        temperature=0.0,
        system=None,
        tools=None,
    )

    attempt_spans = [span for span in tracer.get_spans() if span.name == "llm.model_attempt"]
    assert len(attempt_spans) == 2
    assert attempt_spans[0].attributes["model"] == primary
    assert attempt_spans[0].attributes["success"] is False
    assert attempt_spans[1].attributes["model"] == secondary
    assert attempt_spans[1].attributes["success"] is True

    fallback_span = next(
        span for span in tracer.get_spans() if span.name == "llm.execute_with_fallback"
    )
    assert fallback_span.attributes["fallback_used"] is True
    assert fallback_span.attributes["selected_model"] == secondary
    assert response["_routed_to"] == secondary
