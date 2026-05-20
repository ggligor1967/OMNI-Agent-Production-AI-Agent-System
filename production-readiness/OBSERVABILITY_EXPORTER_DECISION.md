# Observability Exporter Decision

## Status

PENDING DECISION

## Current State

- OpenTelemetry-compatible tracing exists.
- Tracing is disabled by default unless configured.
- Sensitive payloads are not recorded.
- No production collector/backend is approved by default.

Evidence:

- `.env.example` documents `OTEL_ENABLED=false`, `OTEL_EXPORTER=none`, and an empty `OTEL_ENDPOINT` as the safe defaults.
- `config.py` exposes `OTEL_ENABLED`, `OTEL_SERVICE_NAME`, `OTEL_EXPORTER`, `OTEL_ENDPOINT`, and `OTEL_SAMPLE_RATE`.
- `agent/core.py` and `main.py` wire tracing spans and tracing summary routes into the active runtime.
- `tests/test_chat_tracing.py` verifies that user identifiers, session identifiers, and prompt text are hashed or omitted from trace attributes.
- `tests/test_http_tracing.py` verifies route-template masking, hashed user IDs, and the absence of raw dynamic-path values in HTTP request traces.

## Candidate Backends

| Backend | Fit | Risks | Status |
| ------ | ----- | ------- | ------ |
| Console / no-op | Good for local debugging and current safe defaults | No production visibility, no durable aggregation, no alerting | dev only |
| OTLP Collector | Best fit for vendor-neutral production telemetry once deployed and owned | Requires collector deployment, endpoint management, retention policy, and dashboard/alert ownership | pending |
| Managed observability backend | Operationally attractive if a platform/vendor is already selected | Cost, vendor lock-in, data-classification approval, and configuration ownership are still undecided | pending |

## Required Production Decisions

- exporter type
- endpoint
- sampling policy
- retention policy
- log / tracing data classification
- dashboard / alerting owner

## Blockers

- No production OTLP collector or managed backend has been selected.
- No approved production endpoint or credential model is committed.
- No production sampling, retention, or alert ownership policy is recorded.
- No repository evidence shows a production dashboard, SLO alert, or incident-routing owner.
