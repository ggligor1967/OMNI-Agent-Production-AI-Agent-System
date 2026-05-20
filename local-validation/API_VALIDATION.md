# Local API Validation

## Status

PASS

## Route Discovery

Source scanning in `main.py` confirms that the active `aiohttp` API server uses public-path handling for `/status`, `/health`, `/auth/bootstrap`, `/dashboard`, `/favicon.ico`, `/cache/stats`, and `/audit`. The core route registration block now explicitly adds both `/status` and `/health` alongside the other loopback-safe API routes. `agent/dashboard.py` also registers `/dashboard` and `/`, with `/` remaining protected because it is not part of the public-path allowlist.

Loopback probing captured in `local-validation/evidence/lv-api-001/status_curl.log`, `local-validation/evidence/lv-api-001/health_curl.log`, `local-validation/evidence/lv-api-001/root_curl.log`, and `local-validation/evidence/lv-api-001/server_stdout.log` confirms the live API surface on `127.0.0.1:8765`.

## Endpoint Checks

| Endpoint | Expected | Observed | Status |
| -------- | -------- | -------- | ------ |
| /status | public status endpoint should respond on loopback | `HTTP/1.1 200 OK` with JSON status, health, jobs, skills, router, and model stats payload | PASS |
| /health | public health endpoint should exist if listed as a public path | `HTTP/1.1 200 OK` with the same JSON status/health payload currently exposed by `/status` | PASS |
| / | protected dashboard root may exist, but it must not be public | `HTTP/1.1 401 Unauthorized` with `{"error": "unauthorized", "detail": "No credentials provided"}` | PASS |

## Regression Coverage

- `tests/test_health_endpoint_contract.py` verifies that `/health` is registered in the app/router surface, stays aligned with the public-path policy, returns `200 OK`, matches `/status`, leaks no obvious secrets, and leaves protected `/` behavior unchanged.

## Runtime Safety

- bound to loopback only
- no public tunnel
- server process stopped after validation
- shutdown confirmed in `local-validation/evidence/lv-api-001/server_shutdown_check.log`
