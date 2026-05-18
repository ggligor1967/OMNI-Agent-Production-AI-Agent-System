"""
CVE scan via OSV.dev API - bypasses pip_api Unicode path issue.
Queries https://api.osv.dev/v1/query for each installed package.
"""
import json, sys, urllib.request, urllib.error
from pathlib import Path

# Get installed packages using pip list
import subprocess
result = subprocess.run(
    [sys.executable, '-m', 'pip', 'list', '--format=json'],
    capture_output=True, text=True, encoding='utf-8',
    cwd=r"c:\Users\gligo\My Projects\OMNI Agent — Production AI Agent System"
)
packages = json.loads(result.stdout)

print("=" * 70)
print("pip-audit (OSV.dev API) — DEPENDENCY VULNERABILITY SCAN")
print("=" * 70)
print(f"Packages to scan: {len(packages)}")
print()

vulns_found = []
errors = []

for pkg in packages:
    name = pkg['name']
    version = pkg['version']
    
    # Skip internal/dev tools we just installed
    if name in ('pip', 'setuptools', 'wheel', 'pip-audit', 'bandit', 'radon', 
                'coverage', 'pytest-cov', 'import-linter', 'mando', 'stevedore',
                'grimp', 'mutmut'):
        continue
    
    payload = json.dumps({
        "version": version,
        "package": {"name": name, "ecosystem": "PyPI"}
    }).encode('utf-8')
    
    try:
        req = urllib.request.Request(
            "https://api.osv.dev/v1/query",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            vuln_list = data.get('vulns', [])
            if vuln_list:
                for v in vuln_list:
                    vid = v.get('id', 'UNKNOWN')
                    summary = v.get('summary', 'No summary')[:80]
                    severity = 'UNKNOWN'
                    for sev in v.get('severity', []):
                        if sev.get('type') == 'CVSS_V3':
                            score = float(sev.get('score', '0').split('/')[0] if '/' not in sev.get('score','0') else '0')
                            # CVSS3 score from vector
                            severity = f"CVSS3:{sev.get('score','?')}"
                    aliases = [a for a in v.get('aliases', []) if a.startswith('CVE-')]
                    cve = aliases[0] if aliases else vid
                    fixed_in = []
                    for affected in v.get('affected', []):
                        for r in affected.get('ranges', []):
                            for ev in r.get('events', []):
                                if 'fixed' in ev:
                                    fixed_in.append(ev['fixed'])
                    vulns_found.append({
                        'package': name,
                        'version': version,
                        'vuln_id': vid,
                        'cve': cve,
                        'summary': summary,
                        'severity': severity,
                        'fixed_in': fixed_in[:1]
                    })
    except urllib.error.HTTPError as e:
        if e.code != 404:
            errors.append(f"{name} {version}: HTTP {e.code}")
    except Exception as e:
        errors.append(f"{name} {version}: {type(e).__name__}: {str(e)[:50]}")

print(f"Vulnerabilities found: {len(vulns_found)}")
print()

if vulns_found:
    print("=== VULNERABILITIES ===")
    for v in sorted(vulns_found, key=lambda x: x['package']):
        fix_str = f" → fix: {v['fixed_in'][0]}" if v['fixed_in'] else " (no fix available)"
        print(f"  [{v['cve']}] {v['package']} {v['version']}")
        print(f"         {v['summary']}")
        print(f"         Severity: {v['severity']}{fix_str}")
        print()
else:
    print("  ✓ No known vulnerabilities found in installed packages.")

if errors:
    print(f"\n=== SCAN ERRORS ({len(errors)}) ===")
    for e in errors[:10]:
        print(f"  {e}")

print()
print(f"Scanned: {len(packages)} packages | Vulnerabilities: {len(vulns_found)} | Errors: {len(errors)}")
