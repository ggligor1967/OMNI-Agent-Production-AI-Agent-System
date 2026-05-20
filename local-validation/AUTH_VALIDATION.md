# Local Auth Validation

## Status

PASS

## Evidence

- `local-validation/evidence/l3_auth_scan.log` confirms `AUTH_ENFORCE` guards, `/auth/bootstrap`, `/auth/token`, and authorization handling are implemented in source.
- `local-validation/evidence/l3_auth_tests.log` passed (`22 passed`) across bootstrap CLI, ownership binding, protected-route enforcement, and confirmation-policy checks.
- `local-validation/evidence/l3_chat_missing_auth.log` shows `POST /chat` returns `401 Unauthorized` with `{"detail": "No credentials provided"}` when no credentials are sent.
- `local-validation/evidence/l3_chat_invalid_token.log` shows `POST /chat` returns `401 Unauthorized` with `{"detail": "Invalid or expired JWT"}` for a synthetic invalid bearer token.
- The synthetic invalid token string used during probing did not appear in any `local-validation/evidence/l3_*` logs.

## Behaviors Checked

- AUTH_ENFORCE=true
- invalid or missing auth
- bootstrap/test-only path if available
- secret redaction

## Notes

No real production secrets or tokens were used. Bootstrap behavior was validated through existing local tests and source evidence rather than mutating the runtime state.
