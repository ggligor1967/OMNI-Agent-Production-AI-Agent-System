# Local Validation Bug Backlog

## Status

OPEN

## Confirmed Bugs

| ID | Severity | Area | Evidence | Recommended Fix |
| -- | -------- | ---- | -------- | --------------- |
| LV-API-001 | Medium | Local API health endpoint | `local-validation/evidence/l2_route_scan.log` shows `/health` is listed in auth public paths, while `local-validation/evidence/l2_health_curl.log` shows `HTTP/1.1 404 Not Found` | Implement a real `GET /health` route or remove `/health` from the declared public-path set and related documentation/tests so route declarations match runtime behavior |

## Observations / Follow-Ups

| ID | Area | Observation | Evidence | Recommendation |
| -- | ---- | ----------- | -------- | -------------- |
| LV-OBS-001 | Local test invocation | Targeting the sandbox tests via `pytest.exe` directly on Windows produced `ModuleNotFoundError: agent`, while `python -m pytest` passed the same local sandbox validation | `local-validation/evidence/l4_sandbox_tests.log`, `local-validation/evidence/l4_sandbox_tests_resolved.log` | Standardize local validation docs/scripts on `python -m pytest` for targeted runs or adjust package/import-path handling so both invocation styles behave consistently |
| LV-OBS-002 | Local cache runtime | Local API startup repeatedly fell back from Redis to the in-memory cache backend but still reached ready state on loopback | `local-validation/evidence/l1_safe_startup.log`, `local-validation/evidence/l2_server_stdout.log`, `local-validation/evidence/l3_server_stdout.log` | Decide whether local validation should provision Redis explicitly or treat in-memory fallback as the accepted local-only behavior |

## Rules

- Only list reproduced defects as confirmed bugs.
- Mark ambiguous issues as observations.
- Do not invent bugs.
