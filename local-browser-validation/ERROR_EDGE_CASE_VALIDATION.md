# Error and Edge-Case Validation

## Status

FAIL

## Cases Exercised

| Method | Route | Input Condition | Result | Assessment |
| ------ | ----- | --------------- | ------ | ---------- |
| `POST` | `/status` | wrong HTTP method | `405 Method Not Allowed` | PASS — public route rejected the wrong method cleanly. |
| `GET` | `/definitely-missing-route` | unknown route, no auth | `401 Unauthorized` | OBSERVED — auth middleware rejected the request before a `404` could surface. This is current runtime behavior and should be considered when defining unknown-route expectations under `AUTH_ENFORCE=true`. |
| `POST` | `/auth/bootstrap` | invalid bootstrap token | `403 Forbidden` | PASS — public bootstrap route rejected invalid credentials cleanly. |
| `POST` | `/auth/bootstrap` | malformed JSON body | `500 Internal Server Error` | FAIL — malformed JSON was not handled gracefully. |

## Root-Cause Evidence for the `500`

Server log evidence from the active runtime showed:

- traceback emitted in `local-browser-validation/evidence/b2_server_stdout.log`
- exception frame at `agent/auth.py:713` inside `bootstrap`
- unhandled `json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)`

Relevant code path:

- `agent/auth.py:713` currently executes `data = await request.json() if request.content_length else {}` without guarding JSON parse failures

## Assessment

- Most exercised error paths returned bounded, intentional HTTP errors.
- One real bug was reproduced: malformed JSON to the public bootstrap endpoint causes an unhandled exception and `500` instead of a client error such as `400 Bad Request`.
- This bug is independent of the dashboard CSP issues and should be tracked separately if a follow-up fix pass is started.
