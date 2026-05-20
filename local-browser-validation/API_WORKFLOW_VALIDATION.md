# API Workflow Validation

## Status

PASS (targeted bugfix rerun)

## Public Runtime Checks Re-run in F.4

| Method | Route | Result | Notes |
| ------ | ----- | ------ | ----- |
| `GET` | `/status` | `200 OK` | Returned structured `status`, `health`, `jobs`, `skills`, `router`, and `model_stats`. |
| `GET` | `/health` | `200 OK` | Returned healthy loopback runtime state. |
| `POST` | `/auth/bootstrap` | `400 Bad Request` on malformed JSON | Returned bounded `{"error":"invalid_json","detail":"Malformed JSON request body"}`. |

## Authenticated Workflow Checks Re-run in F.4

| Method / Flow | Result | Notes |
| ------------- | ------ | ----- |
| Dashboard `Save` API key flow | PASS | Saved a temporary local validation key in session-scoped storage while auth remained enabled. |
| Dashboard-backed `/models` load | PASS | Chat tab model dropdown populated after saving the validation key. |
| Dashboard-backed `/chat` send | PASS | Sending `ping` and `enter-send` produced assistant responses in the live runtime. |

## Previously Verified Read-Only Coverage Retained

Earlier local validation already confirmed successful read coverage for:

- `/audit`
- `/cache/stats`
- `/models`
- `/tools`
- `/pipelines`
- `/pipelines/runs`
- `/workflows`
- `/templates`
- `/personas`
- `/kg/stats`
- `/sandbox/history`
- `/tracing/summary`
- `/memories`

## Assessment

- The confirmed API-facing regression (`POST /auth/bootstrap` malformed JSON -> `500`) is closed.
- The dashboard is once again able to drive authenticated API workflows without disabling auth.
- No deploy, release, or production promotion work was performed as part of this rerun.

## Evidence

- `local-browser-validation/evidence/bugfix-browser/f4_browser_rerun_observations.md`
- `local-browser-validation/evidence/bugfix-browser/f6_pytest.log`
