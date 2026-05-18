import logging
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TARGET_FILES = [
    Path("agent/core.py"),
    Path("agent/auth.py"),
    Path("agent/multi_model_client.py"),
    Path("agent/ollama_client.py"),
    Path("agent/sandbox.py"),
    Path("agent/tools_registry.py"),
    Path("agent/tools/__init__.py"),
]

EXCEPT_PASS_PATTERN = re.compile(
    r"except[^\n]*:\n(?:\s*#.*\n)*\s*pass\b",
    re.MULTILINE,
)


def test_hot_path_modules_do_not_silently_swallow_exceptions_with_pass():
    offenders = []
    for path in TARGET_FILES:
        text = path.read_text(encoding="utf-8")
        if EXCEPT_PASS_PATTERN.search(text):
            offenders.append(str(path))

    assert offenders == [], f"Found silent except/pass blocks in: {offenders}"


def test_verify_jwt_logs_decode_failures_without_leaking_token(caplog, monkeypatch):
    from agent.auth import verify_jwt

    bad_token = "abc.def.ghi"

    def boom(_value):
        raise ValueError("decode failure")

    monkeypatch.setattr("agent.auth._b64url_decode", boom)

    with caplog.at_level(logging.DEBUG, logger="agent.auth"):
        payload = verify_jwt(bad_token, "test-secret-key-with-minimum-32-characters")

    assert payload is None
    assert "JWT verification failed" in caplog.text
    assert bad_token not in caplog.text


@pytest.mark.asyncio
async def test_ollama_availability_logs_probe_failures(caplog):
    from agent.ollama_client import OllamaClient

    client = OllamaClient(base_url="http://example.invalid")

    async def failing_session():
        raise RuntimeError("network down")

    client._get_session = failing_session

    with caplog.at_level(logging.DEBUG, logger="agent.ollama_client"):
        available = await client.is_available()

    assert available is False
    assert "Ollama availability check failed" in caplog.text
    assert "http://example.invalid" in caplog.text
    assert "network down" in caplog.text
