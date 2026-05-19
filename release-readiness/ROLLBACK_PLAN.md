# Rollback Plan

## Scope

Rollback plan for a future production deployment.

## Current Release Reference

- handoff tag: `release-handoff-phase-0-3.8`
- latest phase tag: `phase-3.8-complete`

## Rollback Strategy

- revert deployment to previous image/commit
- restore previous environment config
- restore database backup if schema/data changed
- disable unsafe integrations if needed

## Not Yet Tested

No production rollback drill has been performed in this handoff scope.
