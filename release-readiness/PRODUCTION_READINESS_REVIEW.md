# Production Readiness Review

## Status

NOT PRODUCTION APPROVED YET

## Purpose

This document identifies what must be reviewed before OMNI Agent is deployed to production.

## Current Verified State

- Local handoff: complete
- Remote branch/tags: pushed
- Remote CI: PASS
- Production deployment: not performed

## Review Areas

### 1. Secrets and Configuration

- SECRET_KEY policy
- AUTH_ENFORCE policy
- API_HOST binding
- environment variable handling
- CI/CD secret storage

### 2. Authentication and Authorization

- bootstrap admin
- JWT/session ownership
- IDOR regression tests
- admin boundaries

### 3. Database and Storage

- SQLite dev-only policy
- Postgres production target
- migration strategy
- backup/restore
- data export contracts

### 4. Sandbox

- current SandboxPolicy
- local proof-of-isolation
- production backend decision:
  - Docker no-network
  - gVisor
  - Firecracker
  - other

### 5. Observability

- OpenTelemetry tracing
- exporter configuration
- log sanitization
- security audit events

### 6. Performance

- local baseline only
- no production SLO yet
- required production load test before deployment

### 7. Dependency and Supply Chain

- pip-audit status
- GitHub Actions dependency warnings
- pinned dependencies / constraints policy

### 8. Rollback and Recovery

- rollback plan
- database recovery
- config rollback
- release tag rollback

## Production Approval Criteria

- [ ] Deployment target selected.
- [ ] Secrets management reviewed.
- [ ] Production DB strategy implemented or explicitly accepted.
- [ ] Sandbox production backend decision made.
- [ ] Production observability backend configured.
- [ ] Production load test completed.
- [ ] Rollback drill completed.
- [ ] GitHub Actions green on release branch/main.
