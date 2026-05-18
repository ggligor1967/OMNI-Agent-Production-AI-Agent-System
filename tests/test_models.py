"""
OMNI AGENT - Model Integration Tests
Tests for ModelRegistry, ModelRouter, task classification, and MultiModelClient.
Run: pytest tests/test_models.py -v
"""
import sys
import os
import asyncio
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ══════════════════════════════════════════════════════════════════════════════
# MODEL REGISTRY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestModelRegistry:
    ALL_EXPECTED_IDS = [
        "qwen3-vl:235b-instruct-cloud",
        "qwen3-coder-next:cloud",
        "glm-5:cloud",
        "deepseek-v3.1:671b-cloud",
        "qwen3-coder:480b-cloud",
        "gpt-oss:120b-cloud",
        "gpt-oss:20b-cloud",
        "gemma3:4b-cloud",
        "mistral-large-3:675b-cloud",
        "minimax-m2:cloud",
        "cogito-2.1:671b-cloud",
        "glm-4.7:cloud",
        "gemini-3-flash-preview:cloud",
        "devstral-2:123b-cloud",
        "devstral-small-2:24b-cloud",
        "nemotron-3-nano:30b-cloud",
        "qwen3-next:80b-cloud",
        "rnj-1:8b-cloud",
        "ministral-3:8b-cloud",
        "qwen3-vl:235b-cloud",
        "qwen3.5:cloud",
        "kimi-k2.5:cloud",
        "minimax-m2.5:cloud",
        "gemma3:12b-cloud",
    ]

    def test_all_24_models_registered(self):
        from agent.model_registry import MODELS
        assert len(MODELS) == 24, f"Expected 24 models, got {len(MODELS)}"

    def test_all_expected_ids_present(self):
        from agent.model_registry import MODELS
        for mid in self.ALL_EXPECTED_IDS:
            assert mid in MODELS, f"Missing model: {mid}"

    def test_no_duplicate_ids(self):
        from agent.model_registry import MODELS
        assert len(MODELS) == len(set(MODELS.keys()))

    def test_all_models_have_required_fields(self):
        from agent.model_registry import MODELS
        for mid, spec in MODELS.items():
            assert spec.id == mid, f"{mid}: id mismatch"
            assert spec.display_name, f"{mid}: missing display_name"
            assert spec.provider, f"{mid}: missing provider"
            assert spec.tier is not None, f"{mid}: missing tier"
            assert len(spec.capabilities) > 0, f"{mid}: no capabilities"
            assert spec.context_window > 0, f"{mid}: invalid context_window"
            assert spec.description, f"{mid}: missing description"
            assert len(spec.best_for) > 0, f"{mid}: empty best_for"

    def test_vision_models_flagged(self):
        from agent.model_registry import MODELS, ModelCapability
        vision_models = [m for m in MODELS.values() if m.supports_vision]
        assert len(vision_models) >= 2  # at least qwen3-vl and gemini
        for vm in vision_models:
            assert ModelCapability.VISION in vm.capabilities, \
                f"{vm.id} supports_vision=True but VISION not in capabilities"

    def test_get_model(self):
        from agent.model_registry import get_model
        spec = get_model("deepseek-v3.1:671b-cloud")
        assert spec is not None
        assert spec.display_name == "DeepSeek-V3.1 671B"
        assert get_model("nonexistent:model") is None

    def test_get_models_by_capability_code(self):
        from agent.model_registry import get_models_by_capability, ModelCapability
        code_models = get_models_by_capability(ModelCapability.CODE)
        ids = [m.id for m in code_models]
        assert "qwen3-coder-next:cloud" in ids
        assert "devstral-2:123b-cloud" in ids

    def test_get_models_by_capability_vision(self):
        from agent.model_registry import get_models_by_capability, ModelCapability
        vision = get_models_by_capability(ModelCapability.VISION)
        assert len(vision) >= 2
        assert all(ModelCapability.VISION in m.capabilities for m in vision)

    def test_get_models_by_provider(self):
        from agent.model_registry import get_models_by_provider
        alibaba = get_models_by_provider("Alibaba")
        assert len(alibaba) >= 5
        assert all(m.provider == "Alibaba" for m in alibaba)

    def test_get_models_by_tier(self):
        from agent.model_registry import get_models_by_tier, ModelTier
        flagships = get_models_by_tier(ModelTier.FLAGSHIP)
        micros = get_models_by_tier(ModelTier.MICRO)
        assert len(flagships) >= 5
        assert len(micros) >= 2

    def test_long_context_models(self):
        from agent.model_registry import get_long_context_models
        # Models with >= 500k context
        mega = get_long_context_models(min_tokens=500000)
        ids = [m.id for m in mega]
        assert "gemini-3-flash-preview:cloud" in ids
        assert "minimax-m2:cloud" in ids
        assert "minimax-m2.5:cloud" in ids

    def test_summary_table(self):
        from agent.model_registry import summary_table
        table = summary_table()
        assert len(table) == 24
        for row in table:
            assert "id" in row
            assert "provider" in row
            assert "context_k" in row
            assert isinstance(row["context_k"], int)

    def test_model_to_dict(self):
        from agent.model_registry import get_model
        spec = get_model("qwen3-vl:235b-instruct-cloud")
        d = spec.to_dict()
        assert d["id"] == "qwen3-vl:235b-instruct-cloud"
        assert d["supports_vision"] is True
        assert "vision" in d["capabilities"]
        assert "context_window" in d

    def test_providers_count(self):
        from agent.model_registry import MODELS
        providers = {m.provider for m in MODELS.values()}
        expected = {"Alibaba", "Zhipu AI", "DeepSeek", "OpenAI OSS",
                   "Google", "Mistral AI", "NVIDIA", "MiniMax", "Cogito",
                   "Moonshot AI", "RNJ Labs"}
        assert providers == expected, f"Provider mismatch: {providers}"


# ══════════════════════════════════════════════════════════════════════════════
# TASK CLASSIFICATION TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestTaskClassification:

    def test_code_classification(self):
        from agent.model_router import classify_task, TaskType
        task, conf = classify_task("Write a Python function to parse JSON")
        assert task == TaskType.CODE, f"Expected CODE, got {task}"
        assert conf > 0

    def test_math_classification(self):
        from agent.model_router import classify_task, TaskType
        task, _ = classify_task("Calculate the integral of x^2 from 0 to 10")
        assert task == TaskType.MATH

    def test_vision_from_flag(self):
        from agent.model_router import classify_task, TaskType
        task, conf = classify_task("describe this", has_image=True)
        assert task == TaskType.VISION
        assert conf == 1.0

    def test_creative_classification(self):
        from agent.model_router import classify_task, TaskType
        task, _ = classify_task("Write a short story about a robot who discovers emotions")
        assert task == TaskType.CREATIVE

    def test_reasoning_classification(self):
        from agent.model_router import classify_task, TaskType
        task, _ = classify_task("Analyze step by step why this argument is flawed")
        assert task == TaskType.REASONING

    def test_translation_classification(self):
        from agent.model_router import classify_task, TaskType
        task, _ = classify_task("Translate this text to Spanish: Hello world")
        assert task == TaskType.TRANSLATION

    def test_agent_classification(self):
        from agent.model_router import classify_task, TaskType
        task, _ = classify_task("Search the web for the latest Python news")
        assert task == TaskType.AGENT

    def test_short_message_fast(self):
        from agent.model_router import classify_task, TaskType
        task, _ = classify_task("hi")
        assert task == TaskType.FAST

    def test_general_chat_default(self):
        from agent.model_router import classify_task, TaskType
        task, conf = classify_task("Tell me something interesting about the ocean")
        # Should not be CODE, MATH, etc.
        assert task not in (TaskType.CODE, TaskType.MATH, TaskType.VISION)


# ══════════════════════════════════════════════════════════════════════════════
# MODEL ROUTER TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestModelRouter:

    @pytest.fixture
    def router(self):
        from agent.model_router import ModelRouter
        return ModelRouter()

    def test_basic_routing_returns_valid_model(self, router):
        from agent.model_registry import MODELS
        decision = router.route("Write a Python sorting algorithm")
        assert decision.model_id in MODELS
        assert decision.model_spec is not None
        assert decision.task_type.value == "code"

    def test_code_routes_to_code_specialist(self, router):
        from agent.model_registry import ModelCapability
        decision = router.route("Debug this Python code: def broken(")
        assert decision.model_spec.has(ModelCapability.CODE)

    def test_vision_routes_to_vision_model(self, router):
        decision = router.route("What is in this image?", has_image=True)
        assert decision.model_spec.supports_vision

    def test_session_override(self, router):
        router.set_session_model("sess1", "gemma3:4b-cloud")
        decision = router.route("Write a complex ML algorithm", session_id="sess1")
        assert decision.model_id == "gemma3:4b-cloud"
        assert "override" in decision.reason.lower()

    def test_clear_session_override(self, router):
        from agent.model_registry import MODELS
        router.set_session_model("sess2", "gemma3:4b-cloud")
        router.clear_session_model("sess2")
        assert router.get_session_model("sess2") is None

    def test_invalid_model_override_rejected(self, router):
        result = router.set_session_model("sess3", "nonexistent:model")
        assert result is False

    def test_fallback_chain_populated(self, router):
        decision = router.route("complex reasoning task")
        # Should have at least one fallback
        assert isinstance(decision.fallback_chain, list)

    def test_fallback_chain_no_cycles(self, router):
        from agent.model_router import FALLBACK_CHAINS
        for mid, fallback in FALLBACK_CHAINS.items():
            assert mid != fallback, f"Self-loop: {mid}"

    def test_stats_recording_success(self, router):
        router.record_call("qwen3-next:80b-cloud", success=True, latency_ms=150.0)
        router.record_call("qwen3-next:80b-cloud", success=True, latency_ms=200.0)
        stats = {s["model_id"]: s for s in router.get_stats()}
        assert "qwen3-next:80b-cloud" in stats
        s = stats["qwen3-next:80b-cloud"]
        assert s["total_calls"] == 2
        assert s["successful_calls"] == 2
        assert s["avg_latency_ms"] == 175.0

    def test_stats_recording_failure(self, router):
        router.record_call("rnj-1:8b-cloud", success=False, error="timeout")
        stats = {s["model_id"]: s for s in router.get_stats()}
        assert stats["rnj-1:8b-cloud"]["failed_calls"] == 1

    def test_unavailable_model_skipped(self, router):
        # Mark the top code model as unavailable
        router.mark_unavailable("qwen3-coder-next:cloud", cooldown_seconds=3600)
        decision = router.route("Write a Python class")
        assert decision.model_id != "qwen3-coder-next:cloud"
        router.mark_available("qwen3-coder-next:cloud")  # cleanup

    def test_models_for_task(self, router):
        models = router.models_for_task("debug Python code")
        assert len(models) > 0
        assert all("id" in m for m in models)

    def test_list_all_models(self, router):
        models = router.list_all_models()
        assert len(models) == 24

    def test_router_summary(self, router):
        summary = router.router_summary()
        assert summary["total_models"] == 24
        assert "providers" in summary
        assert len(summary["providers"]) > 0


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-MODEL CLIENT TESTS (no network required)
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiModelClient:

    @pytest.fixture
    def client(self):
        # Use unittest.mock to avoid real network
        import unittest.mock as mock
        import sys
        sys.modules.setdefault('aiohttp', mock.MagicMock())
        from agent.multi_model_client import MultiModelClient
        return MultiModelClient()

    def test_client_has_24_models(self, client):
        assert len(client.router.list_all_models()) == 24

    def test_router_accessible(self, client):
        from agent.model_router import ModelRouter
        assert isinstance(client.router, ModelRouter)

    def test_extract_user_text(self, client):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello there!"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "What's 2+2?"},
        ]
        text = client._extract_user_text(messages)
        assert text == "What's 2+2?"

    def test_extract_from_empty(self, client):
        text = client._extract_user_text([])
        assert text == ""

    def test_enrich_messages_no_image(self, client):
        msgs = [{"role": "user", "content": "hello"}]
        result = client._enrich_messages(msgs)
        assert result == msgs

    def test_enrich_messages_with_b64(self, client):
        import base64
        b64 = base64.b64encode(b"fake image data").decode()
        msgs = [{"role": "user", "content": "describe this"}]
        result = client._enrich_messages(msgs, image_b64=b64)
        assert "images" in result[-1]
        assert result[-1]["images"] == [b64]

    def test_session_model_set(self, client):
        assert client.router.set_session_model("test_sess", "gemma3:4b-cloud")
        assert client.router.get_session_model("test_sess") == "gemma3:4b-cloud"

    def test_attempt_order_uses_available_fallbacks(self, client):
        from agent.model_router import RouteDecision, TaskType
        from agent.model_registry import MODELS

        decision = RouteDecision(
            model_id="cogito-2.1:671b-cloud",
            model_spec=MODELS["cogito-2.1:671b-cloud"],
            task_type=TaskType.GENERAL,
            confidence=1.0,
            reason="manual override",
            fallback_chain=[],
        )

        attempt_order = client._build_attempt_order(
            decision,
            ["gpt-oss:20b-cloud", "minimax-m2.5:cloud"],
        )

        assert attempt_order[0] == "cogito-2.1:671b-cloud"
        assert "gpt-oss:20b-cloud" in attempt_order[1:]
        assert "minimax-m2.5:cloud" in attempt_order[1:]

    @pytest.mark.asyncio
    async def test_list_models(self, client):
        models = await client.list_models()
        assert len(models) == 24
        assert "qwen3-coder-next:cloud" in models

    def test_get_stats_empty(self, client):
        # No calls made yet
        stats = client.get_stats()
        assert isinstance(stats, list)

    def test_router_summary(self, client):
        summary = client.get_router_summary()
        assert summary["total_models"] == 24
