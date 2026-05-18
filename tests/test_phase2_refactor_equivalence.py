import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class _MemoryStub:
    def __init__(self):
        self.messages = []
        self.saved_memories = []
        self.audits = []

    def add_message(self, session_id, role, content, metadata=None):
        self.messages.append({
            "session_id": session_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
        })

    def get_history(self, session_id, limit=50):
        history = [
            {"role": item["role"], "content": item["content"], "timestamp": None}
            for item in self.messages
            if item["session_id"] == session_id
        ]
        return history[-limit:]

    def save_memory(self, key, value, category="general", importance=5):
        self.saved_memories.append({
            "key": key,
            "value": value,
            "category": category,
            "importance": importance,
        })

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


@pytest.mark.parametrize(
    ("text", "has_image", "expected_task"),
    [
        ("Write a Python function to sort a list", False, "code"),
        ("Solve the integral of x squared", False, "math"),
        ("Translate hello from English to French", False, "translation"),
        ("What is the capital of France", False, "general"),
        ("Make me a haiku about the sea", False, "creative"),
        ("Compare pros and cons of React vs Vue", False, "reasoning"),
        ("hi", False, "fast"),
        ("describe this", True, "vision"),
    ],
)
def test_classify_task_equivalence_examples(text, has_image, expected_task):
    from agent.model_router import classify_task

    task, confidence = classify_task(text, has_image=has_image)

    assert task.value == expected_task
    assert 0.5 <= confidence <= 1.0


def test_schema_validator_object_rules_equivalence(tmp_path):
    from agent.schema_validator import SchemaValidator

    validator = SchemaValidator(db_path=str(tmp_path / "schema.db"), strict=True)
    schema = {
        "type": "object",
        "required": ["name", "age"],
        "minProperties": 2,
        "additionalProperties": False,
        "dependencies": {"credit_card": ["billing_address"]},
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "age": {"type": "integer", "minimum": 0, "maximum": 150},
            "tags": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string"},
            },
            "credit_card": {"type": "string"},
            "billing_address": {"type": "string"},
        },
    }

    errors = validator.validate(
        {
            "name": "",
            "age": 200,
            "tags": ["dup", "dup"],
            "credit_card": "1234",
            "extra": True,
        },
        schema,
    )

    messages = [error.message for error in errors]
    assert any("String too short" in message for message in messages)
    assert any("Violates maximum 150" in message for message in messages)
    assert any("Array items not unique" in message for message in messages)
    assert any("Required when 'credit_card' is present" in message for message in messages)
    assert any("Additional property 'extra' not allowed" in message for message in messages)


def test_schema_validator_combiners_and_conditionals_equivalence(tmp_path):
    from agent.schema_validator import SchemaValidator

    validator = SchemaValidator(db_path=str(tmp_path / "schema.db"))

    any_of_schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    assert validator.validate("hello", any_of_schema) == []
    assert validator.validate(7, any_of_schema) == []
    assert any("Fails all anyOf schemas" in error.message for error in validator.validate([], any_of_schema))

    one_of_schema = {"oneOf": [{"type": "integer"}, {"type": "number"}]}
    one_of_errors = validator.validate(3, one_of_schema)
    assert any("Must match exactly one of oneOf" in error.message for error in one_of_errors)

    conditional_schema = {
        "if": {"type": "object", "properties": {"kind": {"const": "email"}}},
        "then": {
            "type": "object",
            "properties": {"value": {"type": "string", "format": "email"}},
        },
        "else": {
            "type": "object",
            "properties": {"value": {"type": "string", "minLength": 3}},
        },
    }
    assert validator.validate({"kind": "email", "value": "user@example.com"}, conditional_schema) == []
    assert any(
        "Invalid format: email" in error.message
        for error in validator.validate({"kind": "email", "value": "not-an-email"}, conditional_schema)
    )
    assert any(
        "String too short" in error.message
        for error in validator.validate({"kind": "note", "value": "x"}, conditional_schema)
    )


@pytest.mark.asyncio
async def test_omniagent_chat_quick_response_equivalence():
    from agent.core import OmniAgent

    agent = OmniAgent.__new__(OmniAgent)
    agent.security = _SecurityStub()
    agent.memory = _MemoryStub()
    agent.conversations = SimpleNamespace(
        process=lambda session_id, user_id, text: {"quick_response": "Quick hello!"}
    )
    agent.skills = SimpleNamespace(find_by_trigger=lambda text: [])

    response = await OmniAgent.chat(agent, "alice", "sess-quick", "hello")

    assert response == "Quick hello!"
    assert [message["role"] for message in agent.memory.messages] == ["user", "assistant"]
    assert agent.memory.messages[1]["content"] == "Quick hello!"


@pytest.mark.asyncio
async def test_omniagent_chat_cached_response_equivalence():
    from agent.core import OmniAgent

    agent = OmniAgent.__new__(OmniAgent)
    agent.security = _SecurityStub()
    agent.memory = _MemoryStub()
    agent.conversations = SimpleNamespace(
        process=lambda session_id, user_id, text: {"quick_response": ""}
    )
    agent.skills = SimpleNamespace(find_by_trigger=lambda text: [])
    agent.summarizer = _SummarizerStub()
    agent.rag = SimpleNamespace()
    agent.cache = _CacheStub(cached_response={"response": {"content": "cached answer"}})
    agent.router = SimpleNamespace(get_session_model=lambda session_id: None)

    response = await OmniAgent.chat(agent, "alice", "sess-cache", "hello world")

    assert response == "cached answer"
    assert agent.cache.last_key[0] == "auto"
    assert [message["role"] for message in agent.memory.messages] == ["user", "assistant"]
    assert agent.memory.messages[1]["metadata"] == {"cached": True}
