"""Run bandit and parse results - avoids Windows console encoding issues."""
import subprocess, sys, json, os

env = os.environ.copy()
env['PYTHONUTF8'] = '1'
env['PYTHONIOENCODING'] = 'utf-8'

root = r"c:\Users\gligo\My Projects\OMNI Agent — Production AI Agent System"

result = subprocess.run(
    [sys.executable, '-m', 'bandit', '-r', 'agent/', '-x', 'agent/legacy', '-ll', '-f', 'json'],
    capture_output=True, text=True, encoding='utf-8', env=env, cwd=root
)

print(f"Bandit return code: {result.returncode}")

# stdout may have warning lines before the JSON; find the first '{'
stdout = result.stdout
json_start = stdout.find('{')
if json_start == -1:
    print("ERROR: No JSON found in bandit output")
    print("First 500 chars of stdout:", repr(stdout[:500]))
    sys.exit(1)

stdout_json = stdout[json_start:]

data = json.loads(stdout_json)
metrics = data.get('metrics', {}).get('_totals', {})
results = data.get('results', [])

print("=" * 70)
print("BANDIT SECURITY SCAN RESULTS")
print("=" * 70)
print(f"LOC scanned:            {metrics.get('loc', 0)}")
print(f"NOSEC comments:         {metrics.get('nosec', 0)}")
print(f"HIGH severity issues:   {int(metrics.get('SEVERITY.HIGH', 0))}")
print(f"MEDIUM severity issues: {int(metrics.get('SEVERITY.MEDIUM', 0))}")
print(f"LOW severity issues:    {int(metrics.get('SEVERITY.LOW', 0))}")
print(f"HIGH confidence:        {int(metrics.get('CONFIDENCE.HIGH', 0))}")
print(f"MEDIUM confidence:      {int(metrics.get('CONFIDENCE.MEDIUM', 0))}")
print()

# Group by severity
high_issues = [r for r in results if r['issue_severity'] == 'HIGH']
med_issues = [r for r in results if r['issue_severity'] == 'MEDIUM']

print(f"=== HIGH SEVERITY ({len(high_issues)} issues) ===")
for r in high_issues:
    fname = r['filename'].replace(root + '\\', '').replace(root + '/', '')
    code = r.get('code', '').strip()[:100]
    print(f"  [{r['test_id']}] {r['issue_text']}")
    print(f"         File: {fname}:{r['line_number']}")
    print(f"         Code: {code}")
    print()

print(f"=== MEDIUM SEVERITY ({len(med_issues)} issues) ===")
for r in med_issues:
    fname = r['filename'].replace(root + '\\', '').replace(root + '/', '')
    code = r.get('code', '').strip()[:100]
    print(f"  [{r['test_id']}] {r['issue_text']}")
    print(f"         File: {fname}:{r['line_number']}")
    print(f"         Code: {code}")
    print()

# Breakdown by test ID
from collections import Counter
test_counts = Counter(r['test_id'] for r in results)
print(f"=== ISSUES BY TEST ID ===")
for test_id, count in sorted(test_counts.items(), key=lambda x: -x[1]):
    # find description
    desc = next((r['issue_text'] for r in results if r['test_id'] == test_id), "")[:60]
    print(f"  {test_id}: {count} — {desc}")
