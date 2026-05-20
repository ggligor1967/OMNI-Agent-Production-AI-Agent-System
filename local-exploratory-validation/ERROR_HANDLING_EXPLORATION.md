# Error Handling / Edge Case Exploration

## Status

FAIL

## Cases Exercised

| Case | Probe | Observed | Classification |
| ---- | ----- | -------- | -------------- |
| Unknown route without auth | `GET /does-not-exist` | `401 Unauthorized` with bounded JSON body: `{"error":"unauthorized","detail":"No credentials provided"}` | PASS (auth middleware intercepts protected unknown paths before routing) |
| Unknown route with valid auth | `GET /does-not-exist` + admin key | `404 Not Found` | PASS |
| Wrong method on `/chat` without auth | `GET /chat` | `401 Unauthorized` with bounded JSON body | PASS (auth middleware intercepts before method routing) |
| Wrong method on `/chat` with valid auth | `GET /chat` + admin key | `405 Method Not Allowed` | PASS |
| Malformed JSON on `/chat` without auth | `POST /chat` malformed JSON, no auth | `401 Unauthorized` with bounded JSON body | PASS (auth middleware intercepts before body parsing) |
| Malformed JSON on `/chat` with valid auth | `POST /chat` malformed JSON + admin key | `500 Internal Server Error` plain-text response | BUG |
| Wrong content type on bootstrap | `POST /auth/bootstrap` with `Content-Type: text/plain` | `400 Bad Request` with bounded JSON body: `{"error":"invalid_request","detail":"Content-Type must be application/json"}` | PASS |

## Sanitization

- Response sanitization scan: PASS
- No API key echo found in any X.6 response body
- No `SECRET_KEY` or default-secret marker found in any X.6 response body
- No traceback leaked in HTTP responses
- Server-side stdout did log the internal exception stack for the confirmed `/chat` malformed JSON bug

## Confirmed Bug

### BUG-X6-CHAT-MALFORMED-JSON-500

- Route: `POST /chat`
- Preconditions: valid authentication, `Content-Type: application/json`, malformed JSON body
- Expected: bounded client error such as `400 Bad Request` with JSON error body
- Observed: `500 Internal Server Error` with generic plain-text body
- Evidence:
  - `local-exploratory-validation/evidence/x6_malformed_chat_json_authenticated.log`
  - `local-exploratory-validation/evidence/x1_server_stdout.log`
- Server evidence summary:
  - `main.py` `chat_endpoint` performs `data = await request.json()` before any bounded parse handling
  - server stdout records `json.decoder.JSONDecodeError: Expecting value: line 1 column 12 (char 11)`
- Root-cause hypothesis: `/chat` does not catch JSON parse failures and therefore promotes invalid client input to an internal server error

## Notes

- For protected routes, unauthenticated negative probes can be masked by auth middleware. Authenticated equivalents were run to distinguish auth-policy behavior from route-level error handling.
- This pass is exploratory only. The confirmed `/chat` malformed JSON defect should be fixed in a separate scoped bug-fix lane with a minimal bounded-parse change and regression tests.
