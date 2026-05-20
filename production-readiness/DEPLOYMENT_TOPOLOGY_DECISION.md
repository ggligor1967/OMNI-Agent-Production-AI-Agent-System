# Deployment Topology Decision

## Status

PENDING DECISION

## Candidate Topologies

| Topology | Fit | Risks | Status |
| ------ | ----- | ------- | ------ |
| Single VM + reverse proxy | Simple operational model and a natural fit for one initial runtime node behind TLS termination | Manual process supervision, backup/restore discipline, secret injection, and OS hardening all remain operator responsibilities | PENDING |
| Docker Compose on VPS | Strongest direct repository fit because `Dockerfile` and `docker-compose.yml` already exist | Current compose file is still local-leaning and would require production secret injection, externalized persistent services, and reverse-proxy/TLS decisions | PENDING |
| Managed container platform | Good long-term operational fit if secret injection, logs, metrics, and external Postgres/Redis are provided by the platform | No platform manifests, IaC, or provider-specific deployment contract is committed in this scope | PENDING |
| Kubernetes | Technically possible for future scale-out and isolation work | Operational overhead is high relative to current evidence, and no cluster manifests or SRE operating model are committed | PENDING |

## Required Decisions

- target hosting provider
- runtime process manager
- reverse proxy / TLS
- environment variable injection
- log retention
- persistent storage
- deployment rollback mechanism

## Recommended Initial Topology

Recommendation only, not an approval: start with **Docker Compose on a single VPS behind a reverse proxy** if the deployment owner wants the shortest path aligned to committed artifacts. This recommendation is evidence-based because the repository already ships `Dockerfile` and `docker-compose.yml`, but it remains `PENDING DECISION` until the hosting provider, reverse proxy/TLS layer, secret injection model, persistence plan, and rollback mechanism are explicitly selected.
