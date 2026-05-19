import json
import os
import tempfile
from pathlib import Path

import pytest

from agent.memory import MemoryDB
from agent.sandbox import Sandbox, SandboxPolicy
from agent.security_audit import build_memory_audit_callback


@pytest.mark.asyncio
async def test_default_policy_denies_network():
    sandbox = Sandbox()

    assert sandbox.policy.allow_network is False
    assert sandbox.allows_network(exec_id="policy-network") is False

    result = await sandbox.run_python("import socket\nprint('blocked')")

    assert result.success is False
    assert any("blocked import 'socket'" in violation for violation in result.security_violations)


def test_default_policy_denies_unrestricted_env_access():
    policy = SandboxPolicy.default()

    filtered = policy.filter_env({"PATH": "safe-path", "OMNI_SECRET_TOKEN": "top-secret"})

    assert filtered["PATH"] == "safe-path"
    assert "OMNI_SECRET_TOKEN" not in filtered

    sandbox = Sandbox(policy=policy)
    assert sandbox.allows_env_key("OMNI_SECRET_TOKEN", exec_id="policy-env") is False


def test_write_outside_allowed_path_is_denied():
    with tempfile.TemporaryDirectory() as tmpdir:
        sandbox = Sandbox(policy=SandboxPolicy(
            allowed_write_paths=(tmpdir,),
            timeout_seconds=1.0,
            max_output_bytes=256,
        ))
        outside_path = str(Path(tmpdir).parent / "blocked.txt")

        assert sandbox.can_write_path(outside_path, exec_id="policy-write") is False


def test_allowed_temp_write_path_is_permitted_by_policy():
    with tempfile.TemporaryDirectory() as tmpdir:
        sandbox = Sandbox(policy=SandboxPolicy(
            allowed_write_paths=(tmpdir,),
            timeout_seconds=1.0,
            max_output_bytes=256,
        ))
        allowed_path = str(Path(tmpdir) / "allowed.txt")

        assert sandbox.can_write_path(allowed_path, exec_id="policy-write") is True


def test_timeout_is_represented_in_policy():
    policy = SandboxPolicy(timeout_seconds=0.25, max_output_bytes=321, backend="subprocess")
    sandbox = Sandbox(policy=policy)

    assert sandbox.policy.timeout_seconds == pytest.approx(0.25)
    assert sandbox.max_seconds == pytest.approx(0.25)
    assert sandbox.policy.max_output_bytes == 321
    assert sandbox.max_output_chars == 321


def test_denial_emits_sanitized_security_audit_event():
    tmpdir = tempfile.mkdtemp()
    memory = MemoryDB(os.path.join(tmpdir, "memory.db"))
    sandbox = Sandbox(audit_callback=build_memory_audit_callback(memory))

    assert sandbox.allows_env_key("OMNI_TEST_SECRET", exec_id="policy-env-audit") is False

    entry = next(
        row for row in memory.get_audit_log(limit=10)
        if row["action"] == "security.sandbox_denied"
    )
    details = json.loads(entry["details"])

    assert details["capability"] == "environment"
    assert details["requested_env_key"] == "OMNI_TEST_SECRET"
    assert details["decision"] == "deny"
    assert "top-secret" not in entry["details"]


def test_policy_logs_do_not_include_secret_values():
    tmpdir = tempfile.mkdtemp()
    memory = MemoryDB(os.path.join(tmpdir, "memory.db"))
    sandbox = Sandbox(audit_callback=build_memory_audit_callback(memory))
    secret_value = "omni_policy_secret_value_abcdefghijklmnopqrstuvwxyz"

    filtered = sandbox.build_subprocess_env(
        {"PATH": "safe-path", "OMNI_POLICY_SECRET": secret_value},
        exec_id="policy-env-filter",
    )

    assert filtered["PATH"] == "safe-path"
    assert "OMNI_POLICY_SECRET" not in filtered

    details_blob = "\n".join(row["details"] for row in memory.get_audit_log(limit=10))
    assert secret_value not in details_blob
