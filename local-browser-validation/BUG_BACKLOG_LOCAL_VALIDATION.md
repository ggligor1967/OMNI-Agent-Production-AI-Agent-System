# Bug Backlog — Local Validation Pass

## Confirmed Bugs

| ID | Severity | Area | Summary | Current Status | Evidence | Fix Area |
| -- | -------- | ---- | ------- | -------------- | -------- | -------- |
| `LBV-001` | High | Dashboard UI / CSP | Dashboard tabs and buttons previously relied on inline event handlers that were blocked by the active nonce-only CSP. | CLOSED / VERIFIED (2026-05-20) | `BUTTONS_AND_FORMS_VALIDATION.md`, `local-browser-validation/evidence/bugfix-browser/f4_browser_rerun_observations.md` | `agent/dashboard.py` delegated event wiring + CSS class cleanup |
| `LBV-002` | Medium | Dashboard Overview | Overview previously rendered structured `/status` payload fields as `[object Object]`. | CLOSED / VERIFIED (2026-05-20) | `BUTTONS_AND_FORMS_VALIDATION.md`, `local-browser-validation/evidence/bugfix-browser/f4_browser_rerun_observations.md` | `agent/dashboard.py` structured value formatting + overview mapping |
| `LBV-003` | Medium | Auth bootstrap endpoint | `POST /auth/bootstrap` with malformed JSON previously returned `500 Internal Server Error`. | CLOSED / VERIFIED (2026-05-20) | `ERROR_EDGE_CASE_VALIDATION.md`, `local-browser-validation/evidence/bugfix-browser/f4_browser_rerun_observations.md`, `tests/test_auth_bootstrap_json_errors.py` | `agent/auth.py` guarded bootstrap request parsing |

## Important Observed Behaviors (Not Automatically Bugs)

| ID | Area | Observation | Notes |
| -- | ---- | ----------- | ----- |
| `LBV-BEH-001` | Routing + auth middleware | Unknown unauthenticated route `/definitely-missing-route` returned `401` instead of surfacing `404`. | Still observed under `AUTH_ENFORCE=true`; treat as documented runtime behavior unless new requirements say otherwise. |

## Backlog State

- No confirmed browser-validation defects remain open from the targeted minimal-diff bugfix scope.
- Any future dashboard or auth findings should be tracked as new scope rather than silently reopening these closed defects.
