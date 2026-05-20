# Error and Edge-Case Validation

## Status

PASS (targeted bugfix rerun)

## Cases Exercised

| Method | Route | Input Condition | Result | Assessment |
| ------ | ----- | --------------- | ------ | ---------- |
| `POST` | `/status` | wrong HTTP method | `405 Method Not Allowed` | PASS — public route rejected the wrong method cleanly. |
| `GET` | `/definitely-missing-route` | unknown route, no auth | `401 Unauthorized` | OBSERVED — auth middleware still rejects the request before a `404` surfaces under `AUTH_ENFORCE=true`. |
| `POST` | `/auth/bootstrap` | invalid bootstrap token | `403 Forbidden` | PASS — public bootstrap route still rejects invalid credentials cleanly. |
| `POST` | `/auth/bootstrap` | malformed JSON body | `400 Bad Request` | PASS — bounded client error returned with sanitized JSON body. |

## Fix Verification

- Live rerun result for malformed JSON:
  - status: `400`
  - body: `{"error":"invalid_json","detail":"Malformed JSON request body"}`
- No traceback or parser-internal error text was exposed in the response.
- Automated regression coverage now also verifies:
  - malformed JSON -> `400`
  - wrong content type -> `400`
  - non-object JSON body -> `400`

## Assessment

- The previously reproduced `500 Internal Server Error` on malformed bootstrap JSON is closed.
- The public bootstrap endpoint now fails in a controlled way for malformed client input.

## Evidence

- `local-browser-validation/evidence/bugfix-browser/f4_browser_rerun_observations.md`
- `tests/test_auth_bootstrap_json_errors.py`
