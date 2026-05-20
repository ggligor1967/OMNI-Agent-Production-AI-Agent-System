# Production Environment Decision

## Status

PENDING DECISION

## Purpose

Select or defer the target production environment for OMNI Agent.

## Candidate Environments

| Candidate | Fit | Required Inputs | Risks | Status |
|----------|-----|-----------------|-------|--------|
| Single VPS + Docker Compose + reverse proxy | Strong initial fit with current repository artifacts | VPS provider, domain/subdomain, TLS, secret injection, backup path | Operator-managed hardening, backups, monitoring, rollback discipline | PENDING |
| Managed container platform | Good operational fit if secrets/logging/Postgres are managed | Provider, build/deploy contract, env vars, external DB, observability | Platform lock-in, config drift, cost | PENDING |
| Kubernetes | Future scale option | Cluster, manifests, ingress, secret manager, observability, SRE owner | Too much overhead for current state | DEFERRED unless explicitly selected |
| Local-only / no production | Safe if project is not ready for public service | None | No production availability | PENDING DECISION |

## Recommended Environment

Repository evidence supports one default recommendation only:

`Single VPS + Docker Compose + reverse proxy`

This is a recommendation, not an approval.

## Required User / Operator Inputs

- hosting provider
- domain/subdomain
- TLS strategy
- reverse proxy choice
- server owner
- backup owner
- observability owner
- deployment approver

## Decision

PENDING DECISION

## Rationale

The repository already contains `Dockerfile`, `docker-compose.yml`, and an existing topology recommendation in `production-readiness/DEPLOYMENT_TOPOLOGY_DECISION.md`, so a single VPS running Docker Compose behind a reverse proxy is the strongest evidence-backed initial environment. That said, no hosting provider, domain, TLS strategy, reverse proxy, or accountable operator owner has been approved in repository evidence or user instructions. Until those explicit decision inputs exist, the production environment remains `PENDING DECISION`.
