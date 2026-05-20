# Auth Validation

## Status

PARTIAL

## Missing / Invalid Credential Checks

| Method | Route | Credential State | Result | Observation |
| ------ | ----- | ---------------- | ------ | ----------- |
| `GET` | `/` | none | `401 Unauthorized` | Confirmed in browser with JSON body `{"error":"unauthorized","detail":"No credentials provided"}`. |
| `POST` | `/chat` | none | `401 Unauthorized` | JSON body reported `No credentials provided`. |
| `POST` | `/chat` | invalid JWT | `401 Unauthorized` | JSON body reported `Invalid or expired JWT`. |
| `GET` | `/models` | none | `401 Unauthorized` | JSON body reported `No credentials provided`. |
| `GET` | `/models` | invalid API key | `401 Unauthorized` | JSON body reported `Invalid API key`. |
| `POST` | `/auth/bootstrap` | token from repo `.env` | `403 Forbidden` | Active runtime was started with a different local synthetic bootstrap token than the token stored in `.env`; repo `.env` token was therefore invalid for this running process. |
| `POST` | `/auth/bootstrap` | no token | `403 Forbidden` | JSON body reported `Invalid bootstrap token`. |

## Valid Auth / RBAC Checks

A local in-memory admin JWT was generated against the active loopback runtime. No token values were printed or stored in repo files.

| Method | Route | Credential State | Result | Observation |
| ------ | ----- | ---------------- | ------ | ----------- |
| `GET` | `/auth/keys` | valid admin JWT | `200 OK` | Runtime reported `1` active key in the auth store. |
| `POST` | `/auth/token` | valid admin JWT | `200 OK` | Successfully minted a developer JWT in memory only. |
| `POST` | `/auth/token/verify` | valid admin JWT + freshly minted developer token | `200 OK` | Verification returned `valid=true`, `blacklisted=false`. |
| `GET` | `/models` | valid developer JWT | `200 OK` | Developer role could access standard protected model catalog endpoint. |
| `GET` | `/auth/keys` | valid developer JWT | `403 Forbidden` | JSON body confirmed RBAC: `Role 'developer' cannot access '/auth/keys'`. |

## Security Handling Notes

- No production credentials were used.
- No token or raw API key value was printed in terminal output artifacts or written to repo files.
- Authenticated checks used in-memory synthetic local credentials only.

## Assessment

- Missing credentials, invalid JWTs, and invalid API keys were rejected correctly.
- Admin-only versus developer-level RBAC was enforced correctly on the tested endpoints.
- The repo `.env` bootstrap token did not match the active runtime token for this validation run, so bootstrap behavior had to be interpreted relative to the running process rather than the checked-in environment file.
- Auth coverage is meaningful, but not exhaustive; additional role and revocation tests remain optional follow-up work.
