# Production Readiness Decision Matrix

| Gate | File | Status | Current Evidence | Required to Unblock |
| ------ | ---- | ------ | ---------------- | ------------------- |
| P.1 | `production-readiness/SECRETS_CONFIG_APPROVAL.md` | PENDING DECISION | Safe defaults and fail-fast checks are implemented for `SECRET_KEY`, `AUTH_ENFORCE`, and `API_HOST` | Approve production secret source, bind strategy, TLS owner, bootstrap-token policy, and CI/CD secret storage |
| P.2 | `production-readiness/PRODUCTION_DB_DECISION.md` | PENDING DECISION | Accepted ADR says Postgres is the production target, but active runtime remains SQLite-first | Approve migration sequencing, backup strategy, restore procedure, and export contracts |
| P.3 | `production-readiness/DEPLOYMENT_TOPOLOGY_DECISION.md` | PENDING DECISION | Docker artifacts exist and support an initial recommendation, but no hosting provider is selected | Select hosting provider, reverse proxy/TLS strategy, persistence model, and rollback mechanism |
| P.4 | `production-readiness/SANDBOX_RUNTIME_DECISION.md` | PENDING DECISION | Sandbox policy and local isolation tests exist; Docker is available locally | Select and approve a production isolation backend plus its hardening policy |
| P.5 | `production-readiness/OBSERVABILITY_EXPORTER_DECISION.md` | PENDING DECISION | OTEL-compatible tracing exists with safe defaults and privacy-preserving tests | Choose exporter/backend, endpoint, retention policy, sampling policy, and alert owner |
| P.6 | `production-readiness/LOAD_SLO_PLAN.md` | PENDING EXECUTION | Phase 3.3 local smoke and baseline metrics are recorded | Run a production-like load test in the chosen topology and approve SLOs |
| P.7 | `production-readiness/ROLLBACK_DRILL_PLAN.md` | PENDING EXECUTION | Rollback drill plan exists with references and success criteria | Select target environment, prepare rollback artifact and backups, then execute and document the drill |
| P.8 | `production-readiness/GITHUB_RELEASE_PUBLICATION_DECISION.md` | KEEP_DRAFT | Draft release exists and remains unpublished | Obtain explicit user approval after release-note review and after confirming publication is not production approval |
