# Secrets and Configuration Approval Review

## Status

PENDING DECISION

## Current Verified Security Defaults

- `SECRET_KEY` must not be default.
- `SECRET_KEY` must meet minimum length.
- `AUTH_ENFORCE=false` must not be allowed on non-loopback.
- `API_HOST` defaults to loopback.

Evidence:

- `config.py` defaults `SECRET_KEY` to a placeholder, `AUTH_ENFORCE` to `true`, and `API_HOST` to `127.0.0.1`.
- `main.py` raises a runtime error when `SECRET_KEY` is missing, default, or shorter than 32 characters.
- `main.py` rejects `API_HOST=0.0.0.0` when `AUTH_ENFORCE=false`.
- `.env.example` documents `SECRET_KEY`, `AUTH_ENFORCE`, `AUTH_BOOTSTRAP_TOKEN`, and `API_HOST` with safe-by-default values.
- `tests/test_main_entrypoint_quality_ratchet.py` covers the fail-fast helpers and bind-safety behavior.

## Production Required Decisions

| Item | Required Decision | Current Evidence | Status |
| ------ | ------------------- | ------------------ | ------ |
| `SECRET_KEY` source | Secret manager / CI secret / env injection | `config.py` and `main.py` enforce non-default, minimum-length behavior, but no committed production secret source is configured | PENDING |
| `AUTH_ENFORCE` | Must be true in production | `.env.example` sets `AUTH_ENFORCE=true`; `config.py` defaults to `true`; `main.py` rejects public bind with auth disabled | PENDING |
| `API_HOST` | Bind strategy behind reverse proxy | `.env.example` and `config.py` default to loopback; no committed production bind/reverse-proxy topology exists | PENDING |
| TLS termination | Reverse proxy / platform-managed | No committed production reverse-proxy or TLS termination configuration exists in scope | PENDING |
| CI/CD secrets | GitHub Actions secrets / external vault | CI evidence uses test-only environment variables for verification; no production secret storage approval is committed | PENDING |

## Required Runtime Environment Variables

- `SECRET_KEY`
- `AUTH_ENFORCE`
- `AUTH_BOOTSTRAP_TOKEN`
- `API_HOST`
- `API_PORT`
- `API_FALLBACK_PORTS`
- `DB_BACKEND`
- `POSTGRES_DSN`
- `REDIS_URL`
- `OLLAMA_BASE_URL`
- `OTEL_ENABLED`
- `OTEL_EXPORTER`
- `OTEL_ENDPOINT`
- `OTEL_SAMPLE_RATE`
- `LOG_LEVEL`
- `LOG_FILE`
- `TELEGRAM_TOKEN` (only if Telegram mode is enabled)

## Blockers

- No committed production secret manager, vault, or CI secret-injection decision.
- No production reverse proxy / TLS termination decision.
- No approved production bind strategy for non-loopback exposure.
- No operator-approved policy for bootstrap token issuance, storage, rotation, and revocation.
- No production CI/CD secret ownership or rotation policy is recorded in repository evidence.
