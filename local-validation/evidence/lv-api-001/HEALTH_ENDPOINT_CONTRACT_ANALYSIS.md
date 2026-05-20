# LV-API-001 Health Endpoint Contract Analysis

## Status

IMPLEMENT_HEALTH

## Observed Runtime Behavior

- `/status`: 200 OK
- `/`: 401 Unauthorized
- `/health`: 404 Not Found

## Evidence References

- `main.py` configures `public_paths=["/status", "/health", "/auth/bootstrap", "/dashboard", "/favicon.ico", "/cache/stats", "/audit"]` for the active aiohttp API middleware, but only registers `app.router.add_get("/status", status_endpoint)` in the core route block.
- `agent/auth.py` uses `{"/status", "/health"}` as the middleware default public-path set when no override is supplied.
- `agent/gateway.py` documents and implements `/health` as a public health-check bypass surface in the gateway path model.
- `agent/observability/__init__.py` documents `/health` as a JSON health report endpoint and `/ready` as the readiness surface.
- `local-validation/API_VALIDATION.md` recorded that `/health` was treated as a public endpoint expectation during loopback validation.
- `local-validation/evidence/l2_route_scan.log` and `local-validation/evidence/lv-api-001/health_contract_scan.log` show `/health` in the public/auth-exempt surface.
- `local-validation/evidence/l2_health_curl.log` and `local-validation/evidence/l2_server_stdout.log` show the live loopback runtime returning `404 Not Found` for `GET /health`.

## Decision

Implement `/health` as a local/public health endpoint.

## Rationale

Repository evidence consistently treats `/health` as part of the public health/auth-exempt contract rather than as a stale documentation-only reference. The active runtime already exempts `/health` from authentication, the auth middleware default model includes it, and adjacent gateway/observability modules use `/health` as the canonical public health-check surface. Because the live aiohttp API in `main.py` simply omitted the route registration, the smallest evidence-backed fix is to implement `/health` on the active loopback API surface instead of removing the expectation.

## Post-Fix Validation

- `tests/test_health_endpoint_contract.py` passed after the fix.
- `local-validation/evidence/lv-api-001/server_stdout.log` shows `GET /status` → `200`, `GET /health` → `200`, and `GET /` → `401` on loopback.
- `local-validation/evidence/lv-api-001/server_shutdown_check.log` records `PROCESS_EXIT_CONFIRMED` after the live probe sequence.
