"""Sprint 2 test analysis script - writes results to logs/sprint2_results.txt"""
import subprocess, re, sys, pathlib

# Project root is 2 levels up from scripts/testing/
_PROJECT_ROOT = str(pathlib.Path(__file__).parent.parent.parent)

result = subprocess.run(
    [sys.executable, '-m', 'pytest', 'tests/', '--tb=line', '--no-header', '-q'],
    capture_output=True, text=True, cwd=_PROJECT_ROOT
)
output = result.stdout + result.stderr
lines = output.splitlines()

# Find FAILED lines
failed_lines = [l for l in lines if 'FAILED' in l and '::' in l]
passed_lines = [l for l in lines if 'passed' in l.lower() and '::' not in l]

# Error lines (collection errors)
error_lines = [l for l in lines if 'ERROR' in l and '::' in l]

# Group by file
files_failed = {}
for l in failed_lines:
    m = re.search(r'tests/(test_[^:]+)::(\w+)::(\w+)', l)
    if m:
        fname, cls, tname = m.groups()
        files_failed.setdefault(fname, set()).add(cls)

# Get first failure reason per bad class (from --tb=line output)
first_errors = {}
current_test = None
for l in lines:
    m = re.match(r'FAILED tests/([^:]+)::(\w+)::(\w+) - (.+)', l)
    if m:
        fname, cls, tname, reason = m.groups()
        key = f"{fname}::{cls}"
        if key not in first_errors:
            first_errors[key] = reason[:120]

out = []
out.append("=== FAILURES BY FILE ===")
for fname in sorted(files_failed):
    classes = sorted(files_failed[fname])
    out.append(f"\n  FILE: {fname}")
    for cls in classes:
        key = f"{fname}::{cls}"
        reason = first_errors.get(key, "unknown")
        out.append(f"    CLASS: {cls}")
        out.append(f"    REASON: {reason}")

fail_count = len(failed_lines)
pass_count = sum(
    int(m.group(1)) for l in passed_lines
    for m in [re.search(r'(\d+) passed', l)] if m
)

out.append("")
out.append("=== TOTALS ===")
out.append(f"  FAILED lines: {fail_count}")
out.append(f"  PASSED (from summary): {pass_count}")
if error_lines:
    out.append(f"  COLLECTION ERRORS: {len(error_lines)}")
    for e in error_lines[:10]:
        out.append(f"    {e}")

out.append("")
out.append("=== LAST 10 OUTPUT LINES ===")
for l in lines[-10:]:
    out.append(f"  {l}")

text = "\n".join(out)
_out_file = pathlib.Path(_PROJECT_ROOT) / "logs" / "sprint2_results.txt"
_out_file.parent.mkdir(parents=True, exist_ok=True)
_out_file.write_text(text, encoding="utf-8")
print(text)
