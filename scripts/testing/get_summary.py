"""Run pytest and write only the summary line to logs/summary.txt"""
import subprocess, sys, os
from pathlib import Path

# Project root is 2 levels up from scripts/testing/
PROJECT_ROOT = Path(__file__).parent.parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

os.chdir(str(PROJECT_ROOT))
result = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "--tb=no", "-q", "--no-header"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
)
lines = result.stdout.splitlines()
# Write last 10 lines to logs/summary.txt
with open(str(LOGS_DIR / "summary.txt"), "w") as f:
    for line in lines[-10:]:
        f.write(line + "\n")
    f.write(f"\nRETURNCODE: {result.returncode}\n")
    # Also count PASSED/FAILED
    passed = sum(1 for l in lines if " PASSED" in l)
    failed = sum(1 for l in lines if " FAILED" in l)
    errored = sum(1 for l in lines if " ERROR" in l)
    f.write(f"PASSED_LINES: {passed}\nFAILED_LINES: {failed}\nERROR_LINES: {errored}\n")

print(f"Done! See {LOGS_DIR / 'summary.txt'}")
