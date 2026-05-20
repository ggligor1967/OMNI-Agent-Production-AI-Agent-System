# Browser Bug Backlog

Canonical source: `BUG_BACKLOG_LOCAL_VALIDATION.md`

## Current Status

All three confirmed defects from the targeted local browser bugfix pass are now **closed / verified**:

- `LBV-001` — dashboard CSP-safe interaction wiring
- `LBV-002` — structured dashboard rendering (`[object Object]` removal)
- `LBV-003` — malformed `POST /auth/bootstrap` now returns bounded `400`

## Evidence

- `BUG_BACKLOG_LOCAL_VALIDATION.md`
- `local-browser-validation/evidence/bugfix-browser/f4_browser_rerun_observations.md`
- `local-browser-validation/evidence/bugfix-browser/f6_pytest.log`

## Note

This alias exists so the browser-validation artifact set has the exact filename requested during the targeted bugfix rerun. The detailed backlog remains maintained in `BUG_BACKLOG_LOCAL_VALIDATION.md`.
