import importlib

import pytest

from agent import tracing as tracing_mod


def test_tracing_config_defaults_safe():
    settings = tracing_mod.TracingConfig.from_mapping({})

    assert settings.enabled is False
    assert settings.service_name == "omni-agent"
    assert settings.exporter == "none"
    assert settings.endpoint == ""
    assert settings.sample_rate == 1.0


@pytest.mark.parametrize(
    ("raw_exporter", "expected_exporter"),
    [
        ("console", "console"),
        ("OTLP", "otlp"),
        ("bogus", "none"),
    ],
)
def test_tracing_config_normalizes_exporter_and_sample_rate(raw_exporter, expected_exporter):
    settings = tracing_mod.TracingConfig.from_mapping(
        {
            "OTEL_ENABLED": "true",
            "OTEL_EXPORTER": raw_exporter,
            "OTEL_SAMPLE_RATE": "7.5",
        }
    )

    assert settings.enabled is True
    assert settings.exporter == expected_exporter
    assert settings.sample_rate == 1.0


def test_disabled_tracer_uses_noop_provider_and_keeps_in_memory_spans():
    tracer = tracing_mod.Tracer(settings=tracing_mod.TracingConfig())

    with tracer.span("demo", tracing_mod.SpanKind.INTERNAL) as span:
        span.set("model", "demo-model")

    status = tracer.telemetry_status()
    assert status["provider"] == "noop"
    assert status["active"] is False
    assert len(tracer) == 1
    assert tracer.summary()["telemetry"]["provider"] == "noop"


@pytest.mark.asyncio
async def test_llm_span_hashes_session_id_and_omits_prompt_text():
    tracer = tracing_mod.Tracer(settings=tracing_mod.TracingConfig())

    async with tracer.llm_span(
        "demo-model",
        session_id="session-123",
        prompt_text="super secret prompt text",
    ) as span:
        span.set("output_tokens", 42)

    assert "session_id" not in span.attributes
    assert "session_id_hash" in span.attributes
    assert span.attributes["session_id_hash"].startswith("sha256:")
    assert "prompt_text" not in span.attributes
    assert span.attributes["input_tokens_est"] > 0


def test_span_redacts_sensitive_attributes_and_events():
    tracer = tracing_mod.Tracer(settings=tracing_mod.TracingConfig())
    span = tracer.start_span("secure", tracing_mod.SpanKind.INTERNAL)

    span.set("authorization", "Bearer abc123")
    span.set("api_key", "sk-super-secret")
    span.set("model", "demo-model")
    span.add_event(
        "tool.call",
        {
            "password": "dont-store-me",
            "result_count": 2,
        },
    )
    span.end(tracing_mod.SpanStatus.OK)

    assert span.attributes["authorization"] == "[REDACTED]"
    assert span.attributes["api_key"] == "[REDACTED]"
    assert span.attributes["model"] == "demo-model"
    assert span.events[0]["attrs"]["password"] == "[REDACTED]"
    assert span.events[0]["attrs"]["result_count"] == 2


def test_tracer_degrades_when_opentelemetry_sdk_unavailable(monkeypatch):
    real_import_module = importlib.import_module

    def broken_import(name, package=None):
        if name.startswith("opentelemetry"):
            raise ImportError("opentelemetry unavailable")
        return real_import_module(name, package)

    monkeypatch.setattr(tracing_mod.importlib, "import_module", broken_import)

    tracer = tracing_mod.Tracer(
        settings=tracing_mod.TracingConfig.from_mapping(
            {
                "OTEL_ENABLED": "true",
                "OTEL_EXPORTER": "console",
            }
        )
    )

    status = tracer.telemetry_status()
    assert status["provider"] == "noop"
    assert status["active"] is False
    assert "unavailable" in status["reason"].lower()

    span = tracer.start_span("degraded", tracing_mod.SpanKind.INTERNAL)
    span.end(tracing_mod.SpanStatus.OK)
    assert len(tracer) == 1


def test_console_exporter_activates_when_sdk_available():
    pytest.importorskip("opentelemetry.sdk.trace")

    tracer = tracing_mod.Tracer(
        settings=tracing_mod.TracingConfig.from_mapping(
            {
                "OTEL_ENABLED": "true",
                "OTEL_EXPORTER": "console",
                "OTEL_SERVICE_NAME": "test-service",
                "OTEL_SAMPLE_RATE": "0.5",
            }
        )
    )

    status = tracer.telemetry_status()
    assert status["provider"] == "opentelemetry"
    assert status["active"] is True
    assert status["service_name"] == "test-service"
    assert status["sample_rate"] == 0.5

    tracer.shutdown()
