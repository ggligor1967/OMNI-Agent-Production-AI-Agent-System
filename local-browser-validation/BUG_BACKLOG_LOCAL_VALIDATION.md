# Bug Backlog — Local Validation Pass

## Confirmed Bugs

| ID | Severity | Area | Summary | Evidence | Likely Fix Area |
| -- | -------- | ---- | ------- | -------- | --------------- |
| `LBV-UI-001` | High | Dashboard UI / CSP | Dashboard tabs and buttons rely on inline event handlers (`onclick`, `onkeydown`) that are blocked by the active nonce-only CSP, breaking normal browser interaction. | `BUTTONS_AND_FORMS_VALIDATION.md`, `evidence/b3_browser_observations.md` | `agent/dashboard.py` event binding model and dashboard CSP contract |
| `LBV-UI-002` | Medium | Dashboard Overview | Overview renders structured `/status` payload fields as `[object Object]`, producing incorrect status and skills display. | `BUTTONS_AND_FORMS_VALIDATION.md`, `evidence/b3_browser_observations.md` | `agent/dashboard.py:initOverview()` display mapping |
| `LBV-API-001` | Medium | Auth bootstrap endpoint | `POST /auth/bootstrap` with malformed JSON returns `500 Internal Server Error` due an unhandled `JSONDecodeError` instead of a bounded client error. | `ERROR_EDGE_CASE_VALIDATION.md`, `local-browser-validation/evidence/b2_server_stdout.log` | `agent/auth.py` bootstrap request parsing |

## Important Observed Behaviors (Not Automatically Bugs)

| ID | Area | Observation | Notes |
| -- | ---- | ----------- | ----- |
| `LBV-BEH-001` | Routing + auth middleware | Unknown unauthenticated route `/definitely-missing-route` returned `401` instead of surfacing `404`. | This may be acceptable under current middleware ordering, but it should be explicitly documented as the intended contract if kept. |
| `LBV-ENV-001` | Validation environment | The repo `.env` bootstrap token did not match the active runtime's synthetic bootstrap token during this pass. | Environment nuance, not a product bug by itself. It affected how valid-auth setup had to be performed. |

## Recommended Follow-Up Order

1. Fix `LBV-UI-001` first, because it blocks most dashboard click-path validation.
2. Fix `LBV-UI-002` next, because it distorts operator-visible status data even when the page loads.
3. Fix `LBV-API-001` to harden the public bootstrap error path and prevent avoidable `500` responses.
4. Re-run the local browser validation pass after the above fixes before considering any wider readiness conclusion.
