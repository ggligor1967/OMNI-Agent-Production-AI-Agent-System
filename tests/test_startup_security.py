import os
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _import_main(env_overrides=None, removals=()):
    env = os.environ.copy()
    for key in removals:
        env.pop(key, None)
    if env_overrides:
        env.update(env_overrides)

    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, time.perf_counter() - started


class TestStartupSecurity:

    def test_import_main_fails_fast_with_missing_secret(self):
        proc, elapsed = _import_main(removals=["SECRET_KEY", "AUTH_ENFORCE", "API_HOST"])
        combined = f"{proc.stdout}\n{proc.stderr}"

        assert proc.returncode != 0
        assert elapsed < 1.0
        assert "SECRET_KEY is missing, default, or shorter than 32 chars" in combined

    def test_import_main_rejects_public_bind_without_auth(self):
        proc, _ = _import_main({
            "SECRET_KEY": "test-secret-key-with-minimum-32-characters",
            "AUTH_ENFORCE": "false",
            "API_HOST": "0.0.0.0",
        })
        combined = f"{proc.stdout}\n{proc.stderr}"

        assert proc.returncode != 0
        assert "Cannot bind to 0.0.0.0 with AUTH_ENFORCE=false" in combined

    def test_import_main_allows_safe_startup_config(self):
        proc, _ = _import_main({
            "SECRET_KEY": "test-secret-key-with-minimum-32-characters",
            "AUTH_ENFORCE": "true",
            "API_HOST": "127.0.0.1",
        })

        assert proc.returncode == 0
