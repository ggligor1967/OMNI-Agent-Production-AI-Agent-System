import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]

MODULE_SYMBOL_PAIRS = [
    ("agent.model_registry", "agent.llm.model_registry", "ModelRegistry"),
    ("agent.model_router", "agent.llm.model_router", "ModelRouter"),
    ("agent.multi_model_client", "agent.llm.multi_model_client", "MultiModelClient"),
    ("agent.ollama_client", "agent.llm.ollama_client", "OllamaClient"),
    ("agent.memory", "agent.storage.memory", "MemoryDB"),
    ("agent.cache", "agent.storage.cache", "CacheClient"),
    ("agent.rag", "agent.storage.rag", "RAGPipeline"),
    ("agent.auth", "agent.security.auth", "AuthManager"),
    ("agent.sandbox", "agent.security.sandbox", "Sandbox"),
    (
        "agent.security_audit",
        "agent.security.security_audit",
        "sanitize_audit_details",
    ),
    ("agent.tools", "agent.security.toolkit", "SecurityToolkit"),
    ("agent.observability", "agent.observability.metrics", "MetricsRegistry"),
    ("agent.tracing", "agent.observability.tracing", "Tracer"),
    ("agent.streaming", "agent.observability.streaming", "EventBus"),
    ("agent.dashboard", "agent.observability.dashboard", "register_dashboard"),
    ("agent.multimodal", "agent.integrations.multimodal", "VisionPipeline"),
    ("agent.notifications", "agent.integrations.notifications", "Notifier"),
    ("agent.telegram_bot", "agent.integrations.telegram_bot", "TelegramBot"),
]


@pytest.mark.parametrize(("legacy_module", "modern_module", "symbol"), MODULE_SYMBOL_PAIRS)
def test_old_and_new_import_paths_resolve_same_symbol(legacy_module, modern_module, symbol):
    legacy = importlib.import_module(legacy_module)
    modern = importlib.import_module(modern_module)

    assert getattr(legacy, symbol) is getattr(modern, symbol)


@pytest.mark.parametrize(
    "package_name",
    [
        "agent.llm",
        "agent.storage",
        "agent.security",
        "agent.observability",
        "agent.integrations",
    ],
)
def test_phase2_subpackages_import(package_name):
    module = importlib.import_module(package_name)
    assert module is not None


def test_omni_agent_init_succeeds_with_phase2_subpackages(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    import config

    monkeypatch.setattr(
        config.CONFIG,
        "DB_PATH",
        str(tmp_path / "data" / "omni_agent.db"),
        raising=False,
    )
    monkeypatch.setattr(
        config.CONFIG,
        "LOG_FILE",
        str(tmp_path / "logs" / "omni_agent.log"),
        raising=False,
    )
    monkeypatch.setattr(
        config.CONFIG,
        "SECRET_KEY",
        "test-secret-key-with-minimum-32-characters",
        raising=False,
    )
    monkeypatch.setattr(config.CONFIG, "AUTH_ENFORCE", True, raising=False)
    monkeypatch.setattr(config.CONFIG, "API_HOST", "127.0.0.1", raising=False)

    from agent.core import OmniAgent

    agent = OmniAgent()

    assert agent is not None
    assert hasattr(agent, "scheduler")
    assert hasattr(agent, "tool_registry")



def _import_main(env_overrides=None):
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )



def test_main_import_succeeds_with_phase2_subpackages():
    proc = _import_main(
        {
            "SECRET_KEY": "test-secret-key-with-minimum-32-characters",
            "AUTH_ENFORCE": "true",
            "API_HOST": "127.0.0.1",
        }
    )

    assert proc.returncode == 0, f"import main failed: {proc.stdout}\n{proc.stderr}"
