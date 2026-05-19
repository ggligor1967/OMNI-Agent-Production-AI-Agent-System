import os
import tempfile
from pathlib import Path

import pytest

from agent.sandbox import Sandbox, SandboxPolicy


@pytest.mark.asyncio
async def test_environment_secret_access_is_denied_or_redacted():
    previous = os.environ.get("OMNI_TEST_SECRET")
    os.environ["OMNI_TEST_SECRET"] = "super-secret-value"
    try:
        sandbox = Sandbox(allow_shell=True)
        result = await sandbox.run_bash('printf "%s" "${OMNI_TEST_SECRET:-missing}"')

        assert result.success is True
        assert result.stdout.strip() == "missing"
        assert "super-secret-value" not in result.output
    finally:
        if previous is None:
            os.environ.pop("OMNI_TEST_SECRET", None)
        else:
            os.environ["OMNI_TEST_SECRET"] = previous


def test_filesystem_write_outside_allowlist_is_denied():
    with tempfile.TemporaryDirectory() as allowed_dir:
        sandbox = Sandbox(policy=SandboxPolicy(
            allowed_write_paths=(allowed_dir,),
            timeout_seconds=1.0,
            max_output_bytes=256,
        ))
        blocked_path = str(Path(allowed_dir).parent / "blocked.txt")

        assert sandbox.can_write_path(blocked_path, exec_id="proof-write-outside") is False


def test_filesystem_write_inside_allowed_temp_directory_is_permitted_if_supported():
    with tempfile.TemporaryDirectory() as allowed_dir:
        sandbox = Sandbox(policy=SandboxPolicy(
            allowed_write_paths=(allowed_dir,),
            timeout_seconds=1.0,
            max_output_bytes=256,
        ))
        allowed_path = str(Path(allowed_dir) / "allowed.txt")

        assert sandbox.can_write_path(allowed_path, exec_id="proof-write-inside") is True


@pytest.mark.asyncio
async def test_network_access_is_denied_by_default():
    sandbox = Sandbox()
    result = await sandbox.run_python("import socket\nprint('nope')")

    assert result.success is False
    assert any("blocked import 'socket'" in violation for violation in result.security_violations)


@pytest.mark.asyncio
async def test_subprocess_execution_is_denied_unless_explicitly_allowed():
    denied = await Sandbox().run_bash("printf denied")
    assert denied.success is False
    assert "disabled" in denied.error.lower()

    allowed = await Sandbox(allow_shell=True).run_bash("printf allowed")
    assert allowed.success is True
    assert allowed.stdout == "allowed"


@pytest.mark.asyncio
async def test_large_output_is_truncated_or_rejected_according_to_policy():
    sandbox = Sandbox(policy=SandboxPolicy(timeout_seconds=1.0, max_output_bytes=40))
    result = await sandbox.run_python("print('x' * 200)")

    assert result.success is True
    assert "[truncated at 40 chars]" in result.stdout


@pytest.mark.asyncio
async def test_timeout_policy_is_represented_and_enforced_where_supported():
    sandbox = Sandbox(policy=SandboxPolicy(timeout_seconds=0.1, max_output_bytes=256))
    result = await sandbox.run_python("import time\ntime.sleep(0.5)")

    assert sandbox.policy.timeout_seconds == pytest.approx(0.1)
    assert result.timed_out is True
    assert result.success is False
