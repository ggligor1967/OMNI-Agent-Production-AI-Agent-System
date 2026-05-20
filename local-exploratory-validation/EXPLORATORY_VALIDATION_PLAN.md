# Local-Only Exploratory Manual Validation Plan

## Status

PLANNED

## Scope

Manual local-only exploration of currently reachable runtime, dashboard, API, auth/RBAC, workflow, error-handling, and logging behavior.

## Non-Goals

- no production deployment
- no public exposure
- no GitHub Release publication
- no production GO
- no Phase 3.9

## Validation Areas

- runtime startup and shutdown
- dashboard navigation
- buttons and forms
- API workflows
- auth and RBAC
- workflow/template/persona/tool surfaces
- error and edge cases
- logs and secret redaction
- browser console observations
- user-facing behavior quality

## Success Criteria

- all reachable surfaces are manually exercised or explicitly marked not testable
- no confirmed bugs remain untriaged
- final automated verification remains green
- production status remains NO-GO
