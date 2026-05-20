# Local Observability Validation

## Status

PASS

## Current Mode

Local/no-op unless explicitly enabled.

## Behaviors Checked

- tracing config defaults
- prompt/token redaction
- route masking
- no production exporter configured

## Notes

Source and config evidence in `local-validation/evidence/l5_observability_config_scan.log` confirms `.env.example` defaults to `OTEL_ENABLED=false`, `OTEL_EXPORTER=none`, and an empty `OTEL_ENDPOINT`, with standard local log settings in `LOG_LEVEL` and `LOG_FILE`.

Focused tracing tests in `local-validation/evidence/l5_tracing_tests.log` passed (`15 passed`) and verified that:

- disabled tracing uses the no-op provider by default
- prompt text is omitted from span attributes
- session and user identifiers are hashed rather than stored raw
- authorization and other sensitive attributes are redacted
- dynamic HTTP paths are masked to route templates or `/unknown`
- tracing degrades safely when OpenTelemetry SDK components are unavailable

This does not approve production observability backend.
