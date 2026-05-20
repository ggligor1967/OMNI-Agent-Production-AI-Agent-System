# Local Browser Validation Plan

## Status

FAIL

## Scope

Validate all locally reachable browser/UI/API behavior through VS Code integrated browser or equivalent local browser surface.

## Non-Goals

- no production deployment
- no public exposure
- no GitHub Release publication
- no Phase 3.9
- no production GO

## Validation Areas

- local startup
- route discovery
- browser navigation
- visible UI pages
- buttons
- forms
- auth behavior
- API workflows
- error states
- logs and secret redaction
- shutdown behavior

## Success Criteria

- local runtime starts on loopback
- all discovered browser routes are visited
- all visible buttons/forms are exercised where safe
- protected routes reject unauthenticated access
- status/health routes work
- no secret leakage observed
- confirmed bugs are recorded with evidence
- final automated checks remain green
