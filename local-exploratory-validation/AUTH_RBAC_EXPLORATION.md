# Auth and RBAC Exploration

## Status

PASS

## Cases

| Case | Request / Action | Expected | Observed | Status |
| ---- | ---------------- | -------- | -------- | ------ |
| Unauthenticated protected route | `GET /models` without credentials | Protected route should reject anonymous access | `401 Unauthorized` with `No credentials provided` | PASS |
| Invalid token | `GET /models` with invalid bearer token | Invalid JWT should be rejected | `401 Unauthorized` with `Invalid or expired JWT` | PASS |
| Malformed Authorization header | `GET /models` with malformed `Authorization` scheme | Malformed auth should not bypass protection | `401 Unauthorized` with `No credentials provided` | PASS |
| Bootstrap malformed JSON | `POST /auth/bootstrap` with malformed JSON body | Malformed bootstrap JSON should remain a bounded client error | `400 Bad Request` with `invalid_json` / `Malformed JSON request body` | PASS |
| Admin-only route as admin | `GET /auth/keys` with synthetic local admin API key | Admin should access admin-only route | `200 OK` with key metadata list (no raw keys returned) | PASS |
| Admin-only route as developer | `GET /auth/keys` with synthetic local developer API key | Developer should be denied admin-only route | `403 Forbidden` with role-based denial detail | PASS |
| Developer route as developer | `GET /models` with synthetic local developer API key | Developer should access allowed protected route | `200 OK` with model catalog | PASS |
| Read-only route as readonly | `GET /models` with synthetic local readonly API key | Read-only identity should access allowed read route | `200 OK` with model catalog | PASS |
| Admin token creation | `POST /auth/token` with synthetic local admin API key for a synthetic developer identity | Local token flow should mint a usable JWT without exposing it in evidence | `200 OK`; redacted summary recorded with user `local_exploratory_developer`, role `developer`, expires_in `600` | PASS |
| Valid JWT route access | `GET /models` with the synthetic developer JWT | Valid JWT should authorize protected route access | `200 OK` with model catalog | PASS |
| Token revocation | `POST /auth/token/revoke` with synthetic local admin API key | Revocation should invalidate the created JWT | `200 OK` with `revoked: true` | PASS |
| Revoked JWT route access | `GET /models` with the revoked JWT | Revoked token should be rejected | `401 Unauthorized` with `Token has been revoked` | PASS |

## Token / Secret Safety

- Synthetic local API keys were used for this phase; no production credentials were used.
- Evidence artifacts were recorded with token values redacted.
- Response bodies captured for this phase did not expose raw API keys or raw JWTs.
- Secret leak scan result: `leak_detected = false` in `local-exploratory-validation/evidence/x4_secret_leak_scan.json`.

## Bugs

none
