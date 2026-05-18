"""
Sprint 2 validation script.
Run: python sprint2_validate.py
Writes sprint2_report.txt
"""
import subprocess
import sys
import os
import importlib
import pathlib

# Project root is 2 levels up from scripts/testing/
REPO = pathlib.Path(__file__).parent.parent.parent
REPORT = REPO / "logs" / "sprint2_report.txt"
(REPO / "logs").mkdir(parents=True, exist_ok=True)

lines = []

def log(s=""):
    lines.append(s)
    print(s)

# ── 1. Full pytest run ────────────────────────────────────────────────────────
log("=" * 70)
log("SECTION 1: FULL TEST SUITE")
log("=" * 70)

result = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "-v",
     "--tb=line", "--no-header", "-q"],
    capture_output=True, text=True, cwd=str(REPO)
)
log(result.stdout[-8000:] if len(result.stdout) > 8000 else result.stdout)
if result.stderr:
    log("STDERR (last 2000):")
    log(result.stderr[-2000:])
log(f"Return code: {result.returncode}")

# ── 2. Per-file pass/fail tally ───────────────────────────────────────────────
log()
log("=" * 70)
log("SECTION 2: PER-FILE SUMMARY")
log("=" * 70)

result2 = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "--tb=no", "-q",
     "--no-header"],
    capture_output=True, text=True, cwd=str(REPO)
)
log(result2.stdout)

# detailed per-file
result3 = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "--tb=no",
     "--no-header", "-v", "--co", "-q"],
    capture_output=True, text=True, cwd=str(REPO)
)
# Count by file
import re
test_files = {}
for line in result3.stdout.splitlines():
    m = re.match(r"(tests/[\w/]+\.py)", line)
    if m:
        f = m.group(1)
        test_files[f] = test_files.get(f, 0) + 1
log("Discovered test files:")
for f, count in sorted(test_files.items()):
    log(f"  {f:55s} {count:3d} tests")

# ── 3. Nucleus tests specifically ────────────────────────────────────────────
log()
log("=" * 70)
log("SECTION 3: NUCLEUS TESTS ONLY")
log("=" * 70)
result_nuc = subprocess.run(
    [sys.executable, "-m", "pytest",
     "tests/test_models.py", "tests/test_advanced_modules.py",
     "-v", "--tb=short", "--no-header"],
    capture_output=True, text=True, cwd=str(REPO)
)
# Just summary line
for line in result_nuc.stdout.splitlines():
    if "passed" in line or "failed" in line or "error" in line.lower():
        log(line)

# ── 4. Startup path checks ────────────────────────────────────────────────────
log()
log("=" * 70)
log("SECTION 4: STARTUP PATH CHECKS")
log("=" * 70)

checks = [
    ("core import",
     "from agent.core import OmniAgent; print('OK')"),
    ("model_registry",
     "from agent.model_registry import MODELS; print(f'OK — {len(MODELS)} models')"),
    ("model_router",
     "from agent.model_router import classify_task; r=classify_task('write python code'); print(f'OK — {r}')"),
    ("scheduler import",
     "from agent.scheduler import Scheduler, HeartbeatMonitor; s=Scheduler(); print('OK')"),
    ("cache import",
     "from agent.cache import CacheClient; c=CacheClient(); print('OK')"),
    ("pipeline import",
     "from agent.pipeline import PipelineExecutor; print('OK')"),
    ("hooks import",
     "from agent.hooks import hooks, EventType; print('OK')"),
    ("auth import",
     "from agent.auth import AuthManager, Role; print('OK')"),
    ("dashboard import",
     "import ast, pathlib; src=pathlib.Path('agent/dashboard.py').read_text(); ast.parse(src); print('OK — parses')"),
    ("cli import",
     "import ast, pathlib; src=pathlib.Path('agent/cli.py').read_text(); ast.parse(src); print('OK — parses')"),
    ("telegram guard",
     "import ast, pathlib; src=pathlib.Path('main.py').read_text(); "
     "ok='TELEGRAM_TOKEN' in src and 'telegram_bot' in src; print('OK — guarded' if ok else 'MISSING GUARD')"),
    ("config",
     "from config import CONFIG; print(f'OK — DB={CONFIG.DB_PATH}')"),
]

for name, code in checks:
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(REPO)
    )
    status = "PASS" if r.returncode == 0 else "FAIL"
    out = (r.stdout + r.stderr).strip().replace("\n", " ")
    log(f"  [{status}] {name:30s} {out[:120]}")

# ── 5. Root artifact inventory ────────────────────────────────────────────────
log()
log("=" * 70)
log("SECTION 5: ROOT ARTIFACT INVENTORY")
log("=" * 70)

known_artifacts = [
    "abort_test", "async_test", "chain", "ckpt_test", "cond_test",
    "fallback_test", "filter_test", "reg_pipe", "retry_test", "skip_test", "test"
]

log("Artifacts gitignored but physically present:")
for name in known_artifacts:
    p = REPO / name
    if p.exists():
        size = p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        kind = "dir" if p.is_dir() else "file"
        log(f"  PRESENT  [{kind:4s}] {name:20s}  {size:6d} bytes")
    else:
        log(f"  ABSENT         {name}")

# db / sqlite in root
log()
log("Root .db/.sqlite files:")
for p in REPO.glob("*.db"):
    log(f"  PRESENT  [file] {p.name:30s}  {p.stat().st_size:6d} bytes")
for p in REPO.glob("*.sqlite"):
    log(f"  PRESENT  [file] {p.name:30s}  {p.stat().st_size:6d} bytes")
for p in REPO.glob("*.sqlite3"):
    log(f"  PRESENT  [file] {p.name:30s}  {p.stat().st_size:6d} bytes")

# ── Write report ──────────────────────────────────────────────────────────────
REPORT.write_text("\n".join(lines), encoding="utf-8")
print(f"\nReport written to: {REPORT}")
