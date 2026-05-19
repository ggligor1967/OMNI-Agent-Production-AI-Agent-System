# Phase 3.3 Performance Workload Contract

## Selected Routes

- `GET /status`
  - public route
  - used to capture low-cost HTTP + middleware latency
- `POST /chat`
  - exercised through a deterministic local fixture route
  - preserves the expected OMNI request shape (`message`, `session_id`) and JSON response form without contacting a real provider

## Auth Strategy for Local Tests

- `/status` remains unauthenticated/public
- `/chat` uses a short-lived local test credential generated inside the local fixture process
- credentials stay in memory only and are never written to raw performance logs, JSON summaries, or Markdown summaries
- summaries record only route names, counts, status codes, and latency metrics

## LLM Path Handling

- the Phase 3.3 default workload **does not call real external LLM providers**
- the `/chat` benchmark path uses a deterministic local responder
- the deterministic responder may reuse OMNI auth/tracing middleware to preserve realistic request overhead, but it bypasses cloud/Ollama model execution

## Expected Local-Only Execution Mode

- loopback target only (`http://127.0.0.1:<port>`)
- safe default concurrency: `5` users, spawn rate `1/sec`
- safe default baseline duration: `30s` or less
- safe default smoke duration: `15s` or less
- no production endpoints, no remote infrastructure, no destructive routes

## Reporting Requirements

Every smoke/baseline run must produce:

- raw log output
- machine-readable summary (`.json`)
- human-readable summary (`.md`)

Every summary must include:

- request count
- failure count
- error rate
- p50
- p95
- p99
- max latency
- target
- timestamp
