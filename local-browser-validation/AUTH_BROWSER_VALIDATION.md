# Auth Browser Validation

Canonical source: `AUTH_VALIDATION.md`

## Status

PARTIAL (rerun clean)

## Summary

The targeted browser rerun confirmed that auth behavior remained intact while the dashboard defects were fixed:

- auth stayed enabled during the live rerun
- saving a temporary local validation key through the dashboard succeeded
- protected dashboard-backed `/models` and `/chat` requests succeeded after saving the key
- malformed `POST /auth/bootstrap` returned controlled `400` instead of `500`
- the temporary validation key used for rerun was revoked during cleanup

## Evidence

- `AUTH_VALIDATION.md`
- `local-browser-validation/evidence/bugfix-browser/f4_browser_rerun_observations.md`
- `tests/test_security_auth_tools.py`
- `tests/test_auth_bootstrap_json_errors.py`

## Note

This alias exists to provide the exact auth browser validation filename requested during the bugfix pass. The maintained detailed auth report remains `AUTH_VALIDATION.md`.
