# Local Startup Validation

## Status

PASS

## Safe Startup

A safe local API startup succeeded with `python main.py --mode api` using `API_HOST=127.0.0.1`, `AUTH_ENFORCE=true`, and a non-default `SECRET_KEY`, with `API_PORT=8765`. The runtime reached `OMNI Agent ready.`, registered `/config/`, `/auth/*`, `/export/*`, streaming routes, and `/dashboard`, then reported `API server running on http://127.0.0.1:8765`. During startup, Redis was unavailable locally and the cache fell back to the in-memory backend, but the application still reached a usable loopback-only API state. The process was terminated immediately after capture so no long-running local server was left behind.

## Fail-Fast Cases

| Case | Expected | Observed | Status |
| ---- | -------- | -------- | ------ |
| default `SECRET_KEY` | reject startup | startup aborted with `[SECURITY] SECRET_KEY is missing, default, or shorter than 32 chars` | PASS |
| short `SECRET_KEY` | reject startup | startup aborted with `[SECURITY] SECRET_KEY is missing, default, or shorter than 32 chars` | PASS |
| `AUTH_ENFORCE=false` + public bind | reject startup | startup aborted with `[SECURITY] Cannot bind to 0.0.0.0 with AUTH_ENFORCE=false` | PASS |

## Notes

No public bind was used.
