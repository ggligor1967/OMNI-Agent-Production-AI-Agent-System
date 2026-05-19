# Sandbox v2 Threat Model

## Scope

Sandbox v2 covers execution of untrusted or semi-trusted code/tool actions initiated by:

- LLM tool calls
- user-defined skills
- workflow steps
- plugin-like execution paths
- any runtime path that can execute commands, code, scripts, or network operations

In the current OMNI runtime, the most immediate hot-path entry is the `execute_python` tool in `agent/tools_registry.py`, which delegates to the active sandbox implementation in `agent/sandbox.py`.

## Assets to Protect

- host filesystem
- environment variables and secrets
- API keys and tokens
- local network and metadata services
- database files
- project source code
- user data and memory/RAG data
- logs and audit trails

## Threats

- command injection
- arbitrary code execution
- filesystem escape
- path traversal
- network exfiltration
- localhost/metadata SSRF
- secret/environment leakage
- privilege escalation
- resource exhaustion
- persistence via generated files
- audit/log tampering

## Required Controls

- deny-by-default execution policy
- explicit capability declaration
- filesystem allowlist
- network disabled by default
- environment variable filtering
- execution timeout
- CPU/memory/process limits where feasible
- structured audit event
- sanitized logs
- no raw secret leakage
- safe failure mode

## Current Baseline Observations

- `agent/sandbox.py` already enforces AST scanning, subprocess isolation for Python and shell execution, timeout handling, and output truncation.
- `agent/tools_registry.py` already forces `execute_python` through the sandbox and blocks unsafe in-process execution for tool calls.
- `agent/security_audit.py` already sanitizes sensitive keys and redacts tokens from security audit details.
- The current sandbox does **not** yet provide an explicit capability model for filesystem/network/environment access.
- The current subprocess path inherits most of the parent environment, which is a key gap Sandbox v2 must narrow.
- The current implementation blocks many dangerous imports statically, but Phase 3.4 should not treat static blocking alone as sufficient isolation.

## Non-Goals

- production Firecracker deployment in Phase 3.4
- mandatory gVisor installation in Phase 3.4
- full multi-tenant isolation proof in Phase 3.4
- performance optimization

## Open Questions

- which workloads require network?
- which tools require write access?
- whether production deployment will prefer container isolation, gVisor, or microVM isolation
