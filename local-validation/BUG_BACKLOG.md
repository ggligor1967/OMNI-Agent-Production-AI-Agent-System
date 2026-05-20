# Local Validation Bug Backlog

## Status

RESOLVED

## Confirmed Bugs

No open confirmed local-validation bugs remain.

## Resolved Bugs

| ID | Severity | Area | Resolution Evidence | Outcome |
| -- | -------- | ---- | ------------------- | ------- |
| LV-API-001 | Medium | Local API health endpoint | `local-validation/evidence/lv-api-001/HEALTH_ENDPOINT_CONTRACT_ANALYSIS.md`, `local-validation/evidence/lv-api-001/server_stdout.log`, `local-validation/evidence/lv-api-001/server_shutdown_check.log`, `tests/test_health_endpoint_contract.py` | RESOLVED — `main.py` now registers `GET /health`, loopback probing returned `HTTP/1.1 200 OK` for both `/status` and `/health`, and protected `/` still returned `HTTP/1.1 401 Unauthorized` |

## Observations / Follow-Ups

| ID | Area | Observation | Evidence | Recommendation |
| -- | ---- | ----------- | -------- | -------------- |
| LV-OBS-001 | Local test invocation | Targeting the sandbox tests via `pytest.exe` directly on Windows produced `ModuleNotFoundError: agent`, while `python -m pytest` passed the same local sandbox validation | `local-validation/evidence/l4_sandbox_tests.log`, `local-validation/evidence/l4_sandbox_tests_resolved.log` | Standardize local validation docs/scripts on `python -m pytest` for targeted runs or adjust package/import-path handling so both invocation styles behave consistently |
| LV-OBS-002 | Local cache runtime | Local API startup repeatedly fell back from Redis to the in-memory cache backend but still reached ready state on loopback | `local-validation/evidence/l1_safe_startup.log`, `local-validation/evidence/l2_server_stdout.log`, `local-validation/evidence/l3_server_stdout.log` | Decide whether local validation should provision Redis explicitly or treat in-memory fallback as the accepted local-only behavior |

## Rules

- Only list reproduced defects as confirmed bugs.
- Mark ambiguous issues as observations.
- Do not invent bugs.
