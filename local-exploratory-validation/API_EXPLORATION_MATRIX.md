# API Exploration Matrix

## Status

PASS

## Endpoints Tested

| Endpoint | Method | Auth Case | Expected | Observed | Status |
| -------- | ------ | --------- | -------- | -------- | ------ |
| `/status` | GET | Public | Runtime status should be reachable on loopback without auth | `200 OK` with JSON status/health/jobs/router payload | PASS |
| `/health` | GET | Public | Health endpoint should be reachable on loopback without auth | `200 OK` with JSON health payload | PASS |
| `/dashboard` | GET | Public | Dashboard HTML should be publicly reachable on loopback | `200 OK` with dashboard HTML and CSP header | PASS |
| `/chat` | POST | Missing auth | Protected chat should reject anonymous access | `401 Unauthorized` with `No credentials provided` | PASS |
| `/chat` | POST | Invalid bearer token | Protected chat should reject invalid JWT | `401 Unauthorized` with `Invalid or expired JWT` | PASS |
| `/auth/bootstrap` | POST | Malformed JSON | Bootstrap should reject malformed JSON with bounded client error | `400 Bad Request` with `invalid_json` / `Malformed JSON request body` | PASS |
| `/cache/stats` | GET | Synthetic admin API key | Safe stats endpoint should respond without secret leakage | `200 OK` with memory-backend stats | PASS |
| `/audit?limit=5` | GET | Synthetic admin API key | Audit log should be readable through authenticated safe access | `200 OK` with recent audit entries | PASS |
| `/models` | GET | Synthetic admin API key | Protected model catalog should load under valid local auth | `200 OK` with model catalog (`37` total / `27` available) | PASS |
| `/pipelines` | GET | Synthetic admin API key | Pipeline list should load | `200 OK` with `research`, `code_gen`, and `job_search_tank_adr_improved` | PASS |
| `/workflows` | GET | Synthetic admin API key | Workflow list should load | `200 OK` with `research`, `analyze_text`, and `code_review` | PASS |
| `/templates` | GET | Synthetic admin API key | Template list should load | `200 OK` with prompt template metadata | PASS |
| `/tools` | GET | Synthetic admin API key | Tool registry list should load | `200 OK` with registered tool metadata | PASS |
| `/personas` | GET | Synthetic admin API key | Persona registry should load | `200 OK` with persona metadata | PASS |
| `/tracing/summary` | GET | Synthetic admin API key | Tracing summary should load | `200 OK` with span/error/cost counters | PASS |
| `/sandbox/history` | GET | Synthetic admin API key | Sandbox history should load safely | `200 OK` with empty history and stats | PASS |
| `/vision/models` | GET | Synthetic admin API key | Vision model inventory should load | `200 OK` with available vision-model list | PASS |
| `/kg/stats` | GET | Synthetic admin API key | Knowledge-graph stats should load | `200 OK` with zero-node graph stats | PASS |
| `/eval/suites` | GET | Synthetic admin API key | Evaluation suites should load | `200 OK` with suite metadata | PASS |
| `/notifications/channels` | GET | Synthetic admin API key | Notification channel metadata should load | `200 OK` with console channel listed | PASS |
| `/notifications/stats` | GET | Synthetic admin API key | Notification stats should load | `200 OK` with empty stats/recent arrays | PASS |
| `/memories` | GET | Synthetic admin API key | Memories endpoint should respond safely | `200 OK` with empty memory list | PASS |

## Workflow Observations

- Public loopback probes behaved consistently: `/status`, `/health`, and `/dashboard` were reachable without authentication.
- Protected write behavior for `/chat` rejected both missing credentials and invalid bearer tokens with bounded `401` responses.
- `/auth/bootstrap` malformed JSON handling remained bounded at `400`, matching the earlier targeted bug-fix expectations.
- The authenticated safe-GET sweep returned `200 OK` for all `16` explored endpoints using a synthetic local admin API key.
- No tested endpoint leaked raw API keys, JWTs, or internal secret values in the response bodies captured for this phase.

## Bugs

none
