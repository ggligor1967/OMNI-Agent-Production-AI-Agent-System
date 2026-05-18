import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_crypto_utils_disable_md5_helpers():
    from agent.crypto_utils import CryptoUtils, hash_md5

    tmpdir = tempfile.mkdtemp()

    with pytest.raises(ValueError, match="MD5 is insecure and unsupported"):
        hash_md5("test")

    with pytest.raises(ValueError, match="MD5 is insecure and unsupported"):
        CryptoUtils(db_path=os.path.join(tmpdir, "crypto.db")).md5("test")


def test_crypto_module_no_longer_uses_hashlib_md5():
    text = Path("agent/crypto_utils.py").read_text(encoding="utf-8")

    assert "hashlib.md5" not in text


def test_active_md5_uses_are_documented_as_non_security_exceptions():
    offenders = []
    agent_root = Path("agent")

    for path in agent_root.rglob("*.py"):
        if "_legacy" in path.parts or path.name == "crypto_utils.py":
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "hashlib.md5" in line and "# nosec B324" not in line:
                offenders.append(f"{path}:{line_no}")

    assert offenders == [], f"Undocumented active MD5 usages: {offenders}"
