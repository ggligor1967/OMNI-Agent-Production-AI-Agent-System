# Local API Validation

## Status

PARTIAL

## Route Discovery

Source scanning in `local-validation/evidence/l2_route_scan.log` confirms that `main.py` builds an `aiohttp` API server with public-path handling for `/status`, `/health`, `/auth/bootstrap`, `/dashboard`, `/favicon.ico`, `/cache/stats`, and `/audit`. The same route registration block explicitly adds `/status`, `/chat`, `/models`, `/route`, `/sandbox/run`, `/tracing/summary`, `/dashboard`, and many other loopback-safe API routes. No explicit root `/` route is registered in `main.py`.

## Endpoint Checks

| Endpoint | Expected | Observed | Status |
|----------|----------|----------|--------|
| /status | public status endpoint should respond on loopback | `HTTP/1.1 200 OK` with JSON status, health, jobs, skills, router, and model stats payload | PASS |
| /health | public health endpoint should exist if listed as a public path | `HTTP/1.1 404 Not Found` | FAIL |
| / | no public root route is registered, so rejection or not-found is acceptable | `HTTP/1.1 401 Unauthorized` with `{"error": "unauthorized", "detail": "No credentials provided"}` | PASS |

## Runtime Safety

- bound to loopback only
- no public tunnel
- server process stopped after validation
