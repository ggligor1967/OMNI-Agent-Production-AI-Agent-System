# Error Handling / Edge Case Exploration

## Status

PASS

## Cases Exercised

| Case | Probe | Observed | Classification |
| ---- | ----- | -------- | -------------- |
| Unknown route without auth | `GET /does-not-exist` | `401 Unauthorized` with bounded JSON body: `{"error":"unauthorized","detail":"No credentials provided"}` | PASS (auth middleware intercepts protected unknown paths before routing) |
| Unknown route with valid auth | `GET /does-not-exist` + admin key | `404 Not Found` | PASS |
| Wrong method on `/chat` without auth | `GET /chat` | `401 Unauthorized` with bounded JSON body | PASS (auth middleware intercepts before method routing) |
| Wrong method on `/chat` with valid auth | `GET /chat` + admin key | `405 Method Not Allowed` | PASS |
| Malformed JSON on `/chat` without auth | `POST /chat` malformed JSON, no auth | `401 Unauthorized` with bounded JSON body | PASS (auth middleware intercepts before body parsing) |
| Malformed JSON on `/chat` with valid auth | Scoped follow-up live retest on isolated loopback runtime with synthetic auth | `400 Bad Request` with bounded JSON body: `{"error":"invalid_json","detail":"Malformed JSON request body"}` | PASS (original FAIL preserved below as resolved bug evidence) |
| Wrong content type on bootstrap | `POST /auth/bootstrap` with `Content-Type: text/plain` | `400 Bad Request` with bounded JSON body: `{"error":"invalid_request","detail":"Content-Type must be application/json"}` | PASS |

## Sanitization

- Response sanitization scan: PASS
- No API key echo found in any X.6 response body
- No `SECRET_KEY` or default-secret marker found in any X.6 response body
- No traceback leaked in HTTP responses
- Follow-up fix verification log scan returned `NO_MATCH` for `Traceback`, `JSONDecodeError`, `Internal Server Error`, `SECRET_KEY`, and the local synthetic secret marker
- Historical original-pass evidence still preserves the prior server-side stdout exception stack for the now-resolved `/chat` malformed JSON bug

## Historical Confirmed Bug (Resolved)

### BUG-X6-CHAT-MALFORMED-JSON-500

- Route: `POST /chat`
- Preconditions: valid authentication, `Content-Type: application/json`, malformed JSON body
- Expected: bounded client error such as `400 Bad Request` with JSON error body
- Original observed result: `500 Internal Server Error` with generic plain-text body
- Evidence:
  - `local-exploratory-validation/evidence/x6_malformed_chat_json_authenticated.log`
  - `local-exploratory-validation/evidence/x1_server_stdout.log`
- Server evidence summary:
  - `main.py` `chat_endpoint` performs `data = await request.json()` before any bounded parse handling
  - server stdout records `json.decoder.JSONDecodeError: Expecting value: line 1 column 12 (char 11)`
- Root-cause hypothesis: `/chat` does not catch JSON parse failures and therefore promotes invalid client input to an internal server error
- Follow-up fix verification:
  - authenticated malformed `/chat` now returns `400 Bad Request`
  - bounded JSON body is `{"error":"invalid_json","detail":"Malformed JSON request body"}`
  - missing auth remains `401 Unauthorized`
  - invalid auth remains `401 Unauthorized`
  - no traceback or secret leakage was found in the isolated server log scan
- Fix evidence:
  - `local-exploratory-validation/evidence/bugfix-chat-json/CHAT_JSON_CONTRACT_ANALYSIS.md`
  - `local-exploratory-validation/evidence/bugfix-chat-json/c4_chat_authenticated_malformed_json.log`
  - `local-exploratory-validation/evidence/bugfix-chat-json/c4_chat_missing_auth.log`
  - `local-exploratory-validation/evidence/bugfix-chat-json/c4_chat_invalid_auth.log`
  - `local-exploratory-validation/evidence/bugfix-chat-json/c4_server_log_leak_scan.log`

## Notes

- For protected routes, unauthenticated negative probes can be masked by auth middleware. Authenticated equivalents were run to distinguish auth-policy behavior from route-level error handling.
- The scoped bug-fix lane added bounded `/chat` JSON parsing in `main.py` and regression coverage in `tests/test_chat_json_errors.py`.
- Wrong-content-type handling for authenticated `/chat` requests is covered in the scoped fix regression suite and returns a bounded `400` response aligned with the existing auth-route contract.
