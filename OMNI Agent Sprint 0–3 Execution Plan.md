# OMNI Agent Sprint 0–3 Execution Plan

**Version:** execution-ready  
**Scope:** stabilization, reproducible source of truth, P0 security remediation, reliable CI, and preparation for controlled refactoring.  
**Primary rule:** Phase 1 must not begin until Phase 0 is fully green.

---

## Execution Environment

All commands in this plan are written for:

- WSL2 Ubuntu
- Git Bash
- Linux runner in GitHub Actions

Do not run directly in PowerShell without adaptation. Commands such as `export`, `unset`, `grep`, `find`, `wc`, `tee`, and `timeout` are POSIX/bash commands.

---

## Non-Negotiable Principles

1. No new features are added during Sprint 0.
2. CI has two lanes:
   - `release-gate` — blocking;
   - `legacy-audit` — non-blocking.
3. Coverage is a baseline in Sprint 0, not a numeric threshold.
4. Every gate produces raw evidence:
   - exact command;
   - relevant output;
   - commit SHA;
   - status: `PASS`, `FAIL`, or `BLOCKED`.
5. No active code is deleted without an import scan, AST graph, or import-linter run.
6. Any exploitable vulnerability on the active code path is a blocker.
7. Triage alone does not close P0 vulnerabilities. Exploit vectors must be remediated or Sprint 0 remains blocked.
8. If an Acceptance Criterion fails, do not advance to the next gate.

---

# Phase 0 — Stabilization & Source of Truth

**Estimated duration:** 5–8 days  
**Objective:** verifiable repository state, active test suite green, reproducible CI, safe startup, P0 exploit vectors remediated, and Bandit findings triaged.

---

## Gate 0.1 — Capture Baseline Snapshot

**Purpose:** capture the raw repository state before any modification.

```bash
mkdir -p snapshot-sprint-0

git status --short > snapshot-sprint-0/git_status.txt
git rev-parse HEAD > snapshot-sprint-0/commit.txt

python -m compileall agent main.py 2>&1 | tee snapshot-sprint-0/compile.log

pytest tests/ -q --tb=line 2>&1 | tee snapshot-sprint-0/pytest_full_raw.log
pytest tests/ -q --ignore=tests/legacy 2>&1 | tee snapshot-sprint-0/pytest_active_raw.log

ruff check . 2>&1 | tee snapshot-sprint-0/ruff.log || true

# Full snapshot before quarantine: no -x agent/_legacy exclusion because the folder does not exist yet.
bandit -r agent 2>&1 | tee snapshot-sprint-0/bandit_full_raw.log || true

timeout 300 pip-audit 2>&1 | tee snapshot-sprint-0/pip-audit.log || true

find agent -name "*.py" | wc -l > snapshot-sprint-0/count_modules.txt
```

If `pip-audit` hangs or produces no useful output, run the OSV fallback if an internal script exists:

```bash
python tools/osv_scan.py requirements.txt > snapshot-sprint-0/osv_fallback.log 2>&1 || true
```

Verify the snapshot:

```bash
ls -la snapshot-sprint-0/
git add snapshot-sprint-0/
git commit -m "chore: sprint-0 baseline snapshot"
```

**Acceptance Criteria:**

- `snapshot-sprint-0/` exists.
- Raw outputs are committed.
- Initial state is reproducible.
- Commit: `chore: sprint-0 baseline snapshot`.

---

## Gate 0.2 — Quarantine Legacy Code and Tests

**Purpose:** separate the active suite from legacy code and tests.

### 0.2.1 Mandatory Import Scan

```bash
grep -RE "(import|from) .*(_v[0-9]+|\.legacy|\._legacy)" agent tests main.py > snapshot-sprint-0/import_scan_legacy.txt 2>&1 || true
cat snapshot-sprint-0/import_scan_legacy.txt
```

**Rule:**

- If legacy imports are found in active code, for example `main.py`, `agent/core.py`, or `agent/model_registry.py`: `BLOCKED`.
- If they appear only in `agent/legacy/`, `tests/legacy/`, or already-archived code: proceed.

### 0.2.2 Physical Move

```bash
mkdir -p tests/_archive/legacy
mkdir -p agent/_legacy

git mv tests/legacy/* tests/_archive/legacy/ || true
git mv agent/legacy/* agent/_legacy/ || true
```

Update `pytest.ini`:

```ini
[pytest]
norecursedirs =
    tests/_archive
    agent/_legacy
    .venv
    .git
```

Verify:

```bash
pytest tests/ -q 2>&1 | tee snapshot-sprint-0/pytest_after_quarantine.log
```

Commit:

```bash
git add .
git commit -m "chore: quarantine legacy modules and tests"
```

**Acceptance Criteria:**

- `pytest tests/` sees only the active suite.
- Legacy tests do not block the release.
- Import scan is saved.
- Commit: `chore: quarantine legacy modules and tests`.

---

## Gate 0.3 — Add CI Release Gate and Legacy Audit

**Purpose:** CI becomes the single source of truth.

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on: [push, pull_request]

jobs:
  release-gate:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.12", "3.13"]
    env:
      SECRET_KEY: "test-secret-key-with-minimum-32-characters"
      AUTH_ENFORCE: "true"
      API_HOST: "127.0.0.1"
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: "pip"

      - run: pip install -r requirements.txt pytest ruff bandit pip-audit coverage pyyaml

      - run: python -m compileall agent main.py
      - run: pytest tests/ -q
      - run: ruff check .

      # Fail the build on MEDIUM and HIGH findings.
      # Exceptions must be justified via # nosec or bandit.yaml.
      - run: bandit -r agent -x agent/_legacy,tests -ll -ii

      - run: pip-audit
        timeout-minutes: 5

      - run: coverage erase && coverage run -m pytest tests/ && coverage report

  legacy-audit:
    runs-on: ubuntu-latest
    continue-on-error: true
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
          cache: "pip"

      - run: pip install -r requirements.txt pytest
      - run: pytest tests/_archive/ -q || true
```

Add `bandit.yaml`:

```yaml
skips: []
```

Bandit rules:

```text
No global skip is accepted for:
- B602 shell=True
- B102 exec
- B307 eval
- B608 SQL injection

Every # nosec must include a clear technical justification.
```

Acceptable example:

```python
hashlib.md5(data).hexdigest()  # nosec B324 - non-cryptographic cache key only
```

Unacceptable example:

```python
subprocess.run(command, shell=True)  # nosec
```

Validate YAML:

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Commit:

```bash
git add .github/workflows/ci.yml bandit.yaml
git commit -m "ci: add release-gate and legacy-audit lanes"
```

**Acceptance Criteria:**

- CI runs on Python 3.12 and 3.13.
- `release-gate` is blocking.
- `legacy-audit` is non-blocking.
- Bandit fails the build on MEDIUM and HIGH findings.
- Bandit exceptions have an explicit policy.
- pip cache is active in `actions/setup-python`.
- Commit: `ci: add release-gate and legacy-audit lanes`.

---

## Gate 0.4 — Repair Broken Contracts

**Purpose:** `pytest tests/` becomes 100% green.

Set test environment:

```bash
export SECRET_KEY="test-secret-key-with-minimum-32-characters"
export AUTH_ENFORCE=true
export API_HOST=127.0.0.1
```

### Mandatory Fix Order

#### 1. Model Registry 24 vs 27

Make an official decision:

```text
Option A: revert to 24 models.
Option B: accept 27 models and update tests, docs, and header.
```

If 27 is kept:

- update header/docstring;
- update README, SUPPORT_MATRIX, and tests;
- validate the 3 extra models:
  - `deepseek-v3.2:cloud`
  - `minimax-m2.7:cloud`
  - `nemotron-3-super:cloud`

Verify provider mismatch:

```text
Model: ministral-3:8b-cloud
Reported issue: provider='MiniMax'
Likely correct provider: Mistral AI
```

Provider acceptance criteria:

```text
Decision owner: tech lead / maintainer.
Source of truth: Ollama registry / official model card / provider documentation.
Required test: test_provider_consistency passes.
```

Create ADR:

```text
docs/adr/ADR-001-model-registry.md
```

#### 2. `TaskType.CHAT` / `TaskType.GENERAL`

- Add `CHAT = "chat"` if it has distinct semantics.
- Otherwise replace with `TaskType.GENERAL`.
- Add tests for:
  - explicit `model=` argument;
  - `auto_route=False`.

#### 3. Asyncio Python 3.13

- Replace problematic `asyncio.get_event_loop()` calls in sync tests.
- Use `asyncio.run()` or `pytest-asyncio` fixtures.

#### 4. DocGenerator / Path

- Add `from pathlib import Path` if missing.
- Or fix the reference.

#### 5. Small-Talk Regression

- Adjust quick-response logic or the test only if the intended behavior is explicitly documented.

After each fix:

```bash
pytest tests/ -q 2>&1 | tee snapshot-sprint-0/pytest_gate04_attempt.log
```

Final commit:

```bash
git add .
git commit -m "fix: resolve active test suite failures and synchronize model registry contract"
```

**Acceptance Criteria:**

- `pytest tests/ -q` shows `0 failed`, `0 error`.
- ADR-001 exists.
- README, tests, and code share the same model count.
- The 3 extra models are either validated and included, or removed, with the decision documented in ADR-001.
- Provider mismatch is resolved or explicitly documented.
- Python 3.13 no longer breaks asyncio tests.
- Commit: `fix: resolve active test suite failures and synchronize model registry contract`.

---

## Gate 0.4.5 — Remediate P0 RCE / SQLi Exploit Vectors

**Purpose:** eliminate confirmed command injection, RCE, and SQL injection vectors before any further hardening.

### 1. `tools/__init__.py:187` — `shell=True`

**Problem:** `subprocess.run(command, shell=True)` allows command injection.

**Acceptable remediation:**

- Replace with `subprocess.run([...], shell=False)`.
- Strictly validate the command and arguments.
- Do not pass a raw user-controlled string to the shell.
- If argument parsing is needed, use `shlex.split()` only after allowlist validation.

**Required negative test:**

```python
payload = '"; rm -rf / #'
```

Expected:

```text
Payload rejected or sanitized.
No shell execution.
No side effect.
```

### 2. `skills_manager.py:93` — `exec()` on DB Code

**Problem:** arbitrary code execution from a persistent source.

**Acceptable remediation:**

- Disable direct `exec()`;
- or route exclusively through `Sandbox`;
- or temporarily restrict to `role=admin` + audit log + feature flag disabled by default.

**Required negative test:**

```python
malicious_skill = "__import__('os').system('id')"
```

Expected:

```text
Malicious skill does not execute in user context.
Execution is blocked, sandboxed, or feature-disabled.
```

### 3. `retry_manager.py:135` — SQL Injection via f-string

**Problem:** query built with direct string interpolation.

**Acceptable remediation:**

```python
cursor.execute("SELECT ... WHERE policy = ?", (policy,))
```

Not acceptable:

```python
f"WHERE policy='{policy}'"
```

**Required negative test:**

```python
payload = "' OR 1=1--"
```

Expected:

```text
0 unauthorized rows returned.
No SQL syntax leak.
No DB error exposed.
```

### 4. Minimum Pattern Sweep

Run:

```bash
grep -rE "shell=True|\bexec\(|\beval\(|f['\"].*WHERE.*\{" agent/ > snapshot-sprint-0/p0_pattern_sweep.txt || true
cat snapshot-sprint-0/p0_pattern_sweep.txt
```

Note:

```text
This grep is a manual triage artifact and may produce false positives, for example on executor/execute.
It is not an automated gate. Any hit on the hot path must be classified.
```

### Verification

```bash
pytest tests/ -q 2>&1 | tee snapshot-sprint-0/pytest_after_p0_security_fixes.log

bandit -r agent -x agent/_legacy,tests -ll -ii 2>&1 | tee snapshot-sprint-0/bandit_after_p0_security_fixes.log || true

grep -rE "shell=True|f['\"].*WHERE.*\{" agent/ > snapshot-sprint-0/post_fix_dangerous_patterns.txt || true
cat snapshot-sprint-0/post_fix_dangerous_patterns.txt
```

Commit:

```bash
git add .
git commit -m "security: eliminate P0 RCE and SQL injection vectors"
```

**Acceptance Criteria:**

- All 3 negative tests pass.
- `tools/__init__.py:187` no longer uses `shell=True`.
- `skills_manager.py:93` no longer executes DB code directly via `exec()`.
- `retry_manager.py:135` uses a parameterized query.
- Pattern sweep is saved.
- Any remaining hit is justified or marked as a blocker.
- Commit: `security: eliminate P0 RCE and SQL injection vectors`.

---

## Gate 0.5 — Enforce Fail-Fast Startup Security

**Purpose:** an unsafe default config must prevent startup.

Required rules:

```python
_DEFAULT_SECRET = "CHANGE_ME_IN_PRODUCTION"

def _validate_security_config():
    secret = getattr(CONFIG, "SECRET_KEY", "")

    if not secret or secret == _DEFAULT_SECRET or len(secret) < 32:
        raise RuntimeError(
            "[SECURITY] SECRET_KEY is missing, default, or shorter than 32 chars. "
            "Set a strong SECRET_KEY before starting."
        )

    if getattr(CONFIG, "API_HOST", "") == "0.0.0.0" and not getattr(CONFIG, "AUTH_ENFORCE", True):
        raise RuntimeError(
            "[SECURITY] Cannot bind to 0.0.0.0 with AUTH_ENFORCE=false. "
            "Use 127.0.0.1 for dev or enable authentication."
        )
```

Required default:

```python
API_HOST: str = "127.0.0.1"
```

Exact call location:

```text
_validate_security_config() is called in main.py at module level, before any OmniAgent
instantiation or runtime subsystem initialization.

The test ( unset SECRET_KEY AUTH_ENFORCE API_HOST; time python -c "import main" ) must
trigger the validation and fail with RuntimeError.
```

Fail-fast verification:

```bash
( unset SECRET_KEY AUTH_ENFORCE API_HOST; time python -c "import main" ) 2>&1 | tee snapshot-sprint-0/failfast_test.log
```

Test environment verification:

```bash
export SECRET_KEY="test-secret-key-with-minimum-32-characters"
export AUTH_ENFORCE=true
export API_HOST=127.0.0.1

pytest tests/ -q
```

Commit:

```bash
git add .
git commit -m "security: fail-fast on default SECRET_KEY and insecure API_HOST/AUTH combination"
```

**Acceptance Criteria:**

- Unsafe default config fails in < 1 second.
- `python -c "import main"` triggers the validation.
- The error message is explicit.
- `0.0.0.0 + AUTH_ENFORCE=false` is rejected.
- Tests pass with the safe test environment.
- Commit: `security: fail-fast on default SECRET_KEY and insecure API_HOST/AUTH combination`.

---

## Gate 0.6 — Remove Duplicate Core Initializations

**Purpose:** eliminate duplicate initializations in `OmniAgent.__init__`.

Add extended AST test:

```python
import ast
from pathlib import Path

def test_omni_agent_init_does_not_assign_duplicate_subsystems():
    tree = ast.parse(Path("agent/core.py").read_text(encoding="utf-8"))

    class InitVisitor(ast.NodeVisitor):
        def __init__(self):
            self.targets = []

        def visit_FunctionDef(self, node):
            if node.name != "__init__":
                return

            for child in ast.walk(node):
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            self.targets.append(target.attr)

    visitor = InitVisitor()
    visitor.visit(tree)

    duplicates = {
        name for name in set(visitor.targets)
        if visitor.targets.count(name) > 1
    }

    assert not duplicates, f"Duplicate self assignments in OmniAgent.__init__: {duplicates}"
```

Run:

```bash
export SECRET_KEY="test-secret-key-with-minimum-32-characters"
export AUTH_ENFORCE=true
export API_HOST=127.0.0.1

pytest tests/test_core_init_sanity.py -v 2>&1 | tee snapshot-sprint-0/core_audit.log
```

Commit:

```bash
git add tests/test_core_init_sanity.py agent/core.py
git commit -m "fix: eliminate duplicate subsystem initializations in OmniAgent"
```

**Acceptance Criteria:**

- No duplicate `self.*` assignment exists in `OmniAgent.__init__`.
- AST test passes.
- Commit: `fix: eliminate duplicate subsystem initializations in OmniAgent`.

---

## Gate 0.7 — Record Coverage Baseline

**Purpose:** establish a stable metric without a numeric threshold.

```bash
export SECRET_KEY="test-secret-key-with-minimum-32-characters"
export AUTH_ENFORCE=true
export API_HOST=127.0.0.1

coverage erase
coverage run -m pytest tests/
coverage report > coverage_baseline_sprint0.txt
coverage html
```

Commit:

```bash
git add coverage_baseline_sprint0.txt
git commit -m "chore: add coverage baseline for sprint 0"
```

**Acceptance Criteria:**

- `coverage_baseline_sprint0.txt` exists.
- No numeric threshold is set in Sprint 0.
- The official coverage command is documented.
- Commit: `chore: add coverage baseline for sprint 0`.

---

## Gate 0.8 — Triage Bandit HIGH and Security-Critical MEDIUM Findings

**Purpose:** serious vulnerabilities do not remain ambiguous.

**Relationship to Gate 0.4.5:**

```text
Gate 0.8 validates that the fixes from Gate 0.4.5 are complete and that no other
active-blockers remain untriaged. Gate 0.8 is a validation audit, not just an inventory.
```

Mandatory scope:

```text
- all HIGH findings
- all B602
- all B102 / B307
- all B608
- all B110 on the hot path
- all B324 where the context is auth / password / crypto
```

Run:

```bash
bandit -r agent -x agent/_legacy,tests -f json -o snapshot-sprint-0/bandit_active.json || true
bandit -r agent -x agent/_legacy,tests -ll -ii 2>&1 | tee snapshot-sprint-0/bandit_active_medium_high.log || true
```

Create:

```text
snapshot-sprint-0/bandit_security_triage.md
```

Required format:

```markdown
# Bandit Security Triage — Sprint 0

| ID | File | Line | Rule | Severity | Classification | Decision | Owner |
|----|------|------|------|----------|----------------|----------|-------|
| 1 | tools/__init__.py | 187 | B602 | HIGH | fixed | fixed in Gate 0.4.5 | backend |
| 2 | skills_manager.py | 93 | B102 | MEDIUM | fixed | fixed in Gate 0.4.5 | backend/security |
| 3 | retry_manager.py | 135 | B608 | MEDIUM | fixed | fixed in Gate 0.4.5 | backend |
```

Allowed classifications:

```text
active-blocker
orphan-candidate
legacy
test-only
false-positive
accepted-risk-temporary
fixed
```

Rules:

```text
active-blocker must be fixed or Sprint 0 is BLOCKED.
accepted-risk-temporary requires written justification and a ticket.
false-positive requires a technical reason.
No global skip is accepted for B602 / B102 / B307 / B608.
```

Commit:

```bash
git add snapshot-sprint-0/bandit_active.json snapshot-sprint-0/bandit_active_medium_high.log snapshot-sprint-0/bandit_security_triage.md
git commit -m "security: triage bandit high and critical medium findings"
```

**Acceptance Criteria:**

- All HIGH findings are classified.
- All B602 / B102 / B307 / B608 findings are classified.
- The 3 confirmed exploitable vectors are marked `fixed`.
- No active-blocker remains untriaged.
- Gate 0.8 confirms the completeness of Gate 0.4.5.
- Commit: `security: triage bandit high and critical medium findings`.

---

## Gate 0.9 — Inventory Orphan Modules

**Purpose:** future refactoring does not begin blind.

Run import-linter:

```bash
lint-imports 2>&1 | tee snapshot-sprint-0/import_linter.log || true
```

If an internal AST graph script exists:

```bash
python tools/build_import_graph.py 2>&1 | tee snapshot-sprint-0/import_graph.log || true
```

If no internal script exists, create the inventory manually:

```text
snapshot-sprint-0/orphan_module_inventory.md
```

Required format:

```markdown
# Orphan Module Inventory — Sprint 0

| Module | Status | Decision |
|--------|--------|----------|
| ab_router | orphan-candidate | inspect in Phase 2 |
| circuit_breaker | standalone-candidate | keep pending wiring audit |
| config_validator | active-support | keep |
```

Recommended classifications:

```text
active
standalone-candidate
orphan-candidate
legacy-in-disguise
deletable-later
unknown
```

Commit:

```bash
git add snapshot-sprint-0/import_linter.log snapshot-sprint-0/import_graph.log snapshot-sprint-0/orphan_module_inventory.md
git commit -m "chore: add orphan module inventory baseline"
```

**Acceptance Criteria:**

- Orphan-candidate modules are inventoried, not deleted.
- `pydeps` is not the primary source of truth.
- import-linter + AST graph take priority.
- Commit: `chore: add orphan module inventory baseline`.

---

## Iteration Policy Within a Gate

If a gate fails:

```text
1. Do not advance to the next gate.
2. Save the raw output.
3. Choose one of two options:
   - clean revert;
   - clean fix-forward.
4. Do not combine revert + new fix in the same commit.
5. Maximum 3 attempts per gate before escalation.
6. If escalation is reached, status = BLOCKED.
```

---

# Final KPI — Phase 0 Exit Checklist

Phase 0 is complete only when all items are checked:

| #    | Verification                                                 | Status |
| ---- | ------------------------------------------------------------ | ------ |
| 1    | `git status` is clean                                        | ☐      |
| 2    | `pytest tests/` = 100% green                                 | ☐      |
| 3    | Legacy tests run only in the `legacy-audit` non-blocking lane | ☐      |
| 4    | `ruff check .` passes                                        | ☐      |
| 5    | CI runs on Python 3.12 and 3.13                              | ☐      |
| 6    | `pip-audit` passes or has documented OSV fallback            | ☐      |
| 7    | Unsafe default config fails in < 1s                          | ☐      |
| 8    | Model registry / tests / docs are synchronized               | ☐      |
| 9    | The 3 extra models are either validated and included, or removed, with the decision documented in ADR-001 | ☐      |
| 10   | ADR-001 model registry exists                                | ☐      |
| 11   | `TaskType.CHAT` / `TaskType.GENERAL` is resolved and tested  | ☐      |
| 12   | Asyncio tests pass on Python 3.13                            | ☐      |
| 13   | Duplicate init in `OmniAgent.__init__` eliminated            | ☐      |
| 14   | AST test verifies all duplicate `self.*` assignments         | ☐      |
| 15   | `coverage_baseline_sprint0.txt` exists                       | ☐      |
| 16   | Import graph / orphan inventory exists                       | ☐      |
| 17   | Bandit triage covers HIGH + security-critical B102/B307/B608 findings | ☐      |
| 18   | `tools/__init__.py:187` no longer uses `shell=True` on the active code path | ☐      |
| 19   | `skills_manager.py:93` no longer executes DB code directly via `exec()` | ☐      |
| 20   | `retry_manager.py:135` uses a parameterized query            | ☐      |
| 21   | Negative tests for command injection / RCE / SQLi pass       | ☐      |
| 22   | Gate 0.8 validates that Gate 0.4.5 is complete               | ☐      |
| 23   | Every HIGH-severity finding on the active code path is fixed, justified, or documented as a blocker | ☐      |
| 24   | Every security-critical MEDIUM-severity finding on the active code path is fixed, justified, or documented as a blocker | ☐      |
| 25   | The release-gate commands are reproducible locally           | ☐      |

---

# Phase 1 — Security Hardening

**Entry condition:** all Phase 0 KPIs are checked.

| #    | Task                                                 | Acceptance Criteria                                          |
| ---- | ---------------------------------------------------- | ------------------------------------------------------------ |
| 1.1  | Auth bootstrap CLI: `python main.py --create-admin`  | Admin created, JWT issued, `/chat` accessible when authenticated |
| 1.2  | Bind `user_id` / `session_id` to JWT context         | Negative IDOR tests pass                                     |
| 1.3  | Tool execution exclusively via `ToolRegistry.call()` | No bypass through `[TOOL: ...]`                              |
| 1.4  | Confirmation policy for mutating tools               | Mutating tool without role/confirmation is rejected          |
| 1.5  | Central anti-SSRF validator                          | Blocks `127.0.0.1`, RFC1918, link-local, metadata endpoints  |
| 1.6  | Dashboard hardening                                  | No `innerHTML` on unsafe input, no persistent token in `localStorage`, CSP active |
| 1.7  | Security event audit                                 | Auth failure, tool exec, sandbox trigger are logged without secrets |
| 1.8  | Systemic B608 SQL injection sweep                    | All hot-path SQL queries are parameterized                   |
| 1.9  | Hot-path B110 silent exception sweep                 | `except/pass` in core / auth / LLM / tooling replaced with logging or handling |
| 1.10 | B324 MD5 sweep                                       | MD5 in auth / password / crypto eliminated; cache-only MD5 marked `# nosec B324` with justification |

---

# Phase 2 — Architecture & Scalability

| #    | Task                                     | Acceptance Criteria                                          |
| ---- | ---------------------------------------- | ------------------------------------------------------------ |
| 2.1  | Classify orphan modules                  | `active / standalone / legacy-in-disguise / deletable`       |
| 2.2  | Deduplicate enterprise modules           | Single canonical module for `governance`, `tool_registry`, `scheduler`, `secret_manager` |
| 2.3  | Reorganize into sub-packages             | `agent/llm/`, `agent/storage/`, `agent/security/`, `agent/observability/` with no broken imports |
| 2.4  | Real Redis                               | Replace `aioredis` with `redis.asyncio`; Redis active in `prod` compose profile |
| 2.5  | DB strategy                              | ADR: SQLite = dev only, Postgres = prod                      |
| 2.6  | RAG benchmark                            | 1k / 10k / 100k chunks, p50 / p95 reported                   |
| 2.7  | Export API repair                        | Real contracts for `MemoryDB` / `KnowledgeGraph`             |
| 2.8  | Refactor hot-path functions with CC > 30 | `classify_task`, `chat`, `SchemaValidator._validate_node` reduced or isolated; equivalence tests pass |

---

# Phase 3 — Maturity

| #    | Task                       | Acceptance Criteria                                          |
| ---- | -------------------------- | ------------------------------------------------------------ |
| 3.1  | Versioned documentation    | README / SUPPORT_MATRIX / CLAUDE.md synchronized in CI       |
| 3.2  | OpenTelemetry tracing      | Full trace: chat → router → LLM → tools                      |
| 3.3  | Performance testing        | Locust baseline for `/chat`, p50 / p95 / p99                 |
| 3.4  | Sandbox v2                 | Evaluate gVisor / Firecracker / no-network container         |
| 3.5  | Mutation testing           | `model_router`, `memory`, `auth` with baseline mutation score |
| 3.6  | Numeric coverage threshold | Threshold introduced only after a stable baseline exists     |

---

# Final Stop Conditions

```text
STATUS: EXECUTABLE PLAN
SCOPE: Sprint 0 gates + Phase 1–3 roadmap

NEXT ACTION:
Execute Phase 0 continuously from Gate 0.1 through Gate 0.9.

CONTINUOUS EXECUTION RULE:
Proceed automatically to the next gate when the current gate satisfies all Acceptance Criteria and raw evidence is committed.

DO NOT WAIT FOR MANUAL APPROVAL BETWEEN GREEN GATES.

STOP CONDITION:
Stop and report BLOCKED only if:
- an Acceptance Criterion fails;
- active code imports legacy unexpectedly;
- the active test suite cannot be made green within the gate iteration policy;
- a confirmed exploit vector remains unfixed;
- Bandit reports an untriaged active-blocker;
- CI cannot reproduce the local release-gate commands;
- a product/security decision is required and cannot be made safely from repository evidence.
```

Sprint 0 cannot be declared green on triage alone. Sprint 0 is green only after confirmed exploit vectors are remediated, the active test suite is green, CI runs cleanly on Python 3.12 and 3.13, and Bandit HIGH + security-critical MEDIUM findings are fully triaged.