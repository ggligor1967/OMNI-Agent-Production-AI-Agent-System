# Local Runtime Environment

## Status

PASS

## Host Mode

Loopback only.

## Required Environment Variables

- `SECRET_KEY`
- `AUTH_ENFORCE`
- `API_HOST`
- `API_PORT`
- `API_FALLBACK_PORTS`
- `AUTH_BOOTSTRAP_TOKEN`
- `DB_BACKEND`
- `DB_PATH`
- `OLLAMA_BASE_URL`
- `SEARXNG_URL`
- `LOG_LEVEL`
- `LOG_FILE`
- `OTEL_ENABLED`
- `OTEL_EXPORTER`
- `OTEL_ENDPOINT`
- `OTEL_SAMPLE_RATE`

## Local Defaults

- API_HOST=127.0.0.1
- AUTH_ENFORCE=true
- SECRET_KEY must be non-default and >= 32 characters

## Safety Boundary

No public tunnel, VPS, external bind, or production deployment is used.
