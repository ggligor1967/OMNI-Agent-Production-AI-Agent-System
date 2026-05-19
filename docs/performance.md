# Performance Baseline

## Scope

Phase 3.3 records a **local baseline only** for the OMNI Agent runtime surface.
It does **not** define production SLOs, deployment targets, or alert thresholds.
The goal is reproducible local measurement of a safe HTTP workload that can run on a developer machine and in a CI-safe smoke mode.

## Workload

The Phase 3.3 baseline uses a deterministic local workload with two HTTP scenarios:

- `GET /status` — public health/status route aligned with `main.py`
- `POST /chat` — a mock-safe local chat route that preserves the request/response contract shape while bypassing real LLM providers

Default Phase 3.3 execution intentionally excludes heavier routes such as `/rag/query`, `/tools/call`, or mutating workflow endpoints because they introduce more local state variance and are not required to establish the initial latency baseline.

## Safety Rules

- run against loopback only (`127.0.0.1` / `localhost`)
- no real external LLM calls
- no production or remotely hosted endpoints
- no destructive operations
- no prompts, auth tokens, API keys, cookies, headers, or response bodies in performance summaries
- no baseline command should require Ollama, cloud providers, Redis, Postgres, or Telegram connectivity
- smoke mode must stay short and low-volume

## Metrics

Each recorded baseline summary must include:

- request count
- failure count
- error rate
- p50 latency
- p95 latency
- p99 latency
- max latency

When useful, reports may also include per-route breakdowns, run duration, target URL, and local execution notes.

## Local Execution Model

The performance harness is expected to run in one of these safe local modes:

1. **Preferred:** an in-process or loopback-only fixture app that reuses OMNI middleware where practical (`auth`, tracing) but replaces the LLM path with deterministic local responses.
2. **Allowed:** a loopback-only local OMNI API process started specifically for the benchmark.

Phase 3.3 uses an equivalent local `asyncio` + `aiohttp` harness instead of adding Locust as a new dependency to the blocking local baseline. That keeps the harness reproducible on the existing toolchain, avoids expanding the release-gate dependency surface, and still produces the required latency percentiles and failure metrics.

The harness defaults should remain conservative:

- users: `5`
- spawn rate: `1/sec`
- duration: `30s` or less
- smoke duration: `15s` or less

## Non-Goals

- no production SLOs
- no load testing against deployed environments
- no optimization refactor in this phase
- no tracing feature expansion beyond completed Phase 3.2 work
- no Sandbox v2 work
- no mutation testing
- no numeric coverage threshold changes
