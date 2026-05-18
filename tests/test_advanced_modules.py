"""
OMNI AGENT - Tests v3
Covers: StructuredOutputParser, ToolRegistry, Tracer, WorkflowManager, EventBus
Run: pytest tests/test_advanced_modules.py -v
"""
import sys
import os
import json
import time
import asyncio
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ══════════════════════════════════════════════════════════════════════════════
# STRUCTURED OUTPUT TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestOutputField:

    def test_schema_fragment_string(self):
        from agent.structured_output import OutputField, FieldType
        f = OutputField("name", FieldType.STRING, "Person name")
        s = f.schema_fragment()
        assert s["type"] == "string"
        assert s["description"] == "Person name"

    def test_schema_fragment_enum(self):
        from agent.structured_output import OutputField, FieldType
        f = OutputField("mood", FieldType.ENUM, "Mood",
                       enum_values=["happy","sad","neutral"])
        s = f.schema_fragment()
        assert s["type"] == "string"
        assert s["enum"] == ["happy","sad","neutral"]

    def test_schema_fragment_array(self):
        from agent.structured_output import OutputField, FieldType
        f = OutputField("tags", FieldType.ARRAY, "Tags",
                       item_type=FieldType.STRING)
        s = f.schema_fragment()
        assert s["type"] == "array"
        assert s["items"]["type"] == "string"

    def test_schema_fragment_float(self):
        from agent.structured_output import OutputField, FieldType
        f = OutputField("score", FieldType.FLOAT, "Score 0-1")
        s = f.schema_fragment()
        assert s["type"] == "number"


class TestOutputSchema:

    def test_to_json_schema(self):
        from agent.structured_output import OutputSchema, OutputField, FieldType
        s = OutputSchema("test", [
            OutputField("a", FieldType.STRING, "Field A"),
            OutputField("b", FieldType.INTEGER, "Field B", required=False, default=0),
        ])
        js = s.to_json_schema()
        assert js["type"] == "object"
        assert "a" in js["properties"]
        assert "b" in js["properties"]
        assert "a" in js["required"]
        assert "b" not in js["required"]

    def test_prompt_description(self):
        from agent.structured_output import OutputSchema, OutputField, FieldType
        s = OutputSchema("test", [
            OutputField("answer", FieldType.STRING, "The answer", required=True),
        ])
        desc = s.prompt_description()
        assert "answer" in desc
        assert "string" in desc
        assert "required" in desc

    def test_defaults(self):
        from agent.structured_output import OutputSchema, OutputField, FieldType
        s = OutputSchema("test", [
            OutputField("x", FieldType.STRING, "Required"),
            OutputField("y", FieldType.INTEGER, "Optional", required=False, default=42),
        ])
        d = s.defaults()
        assert "y" in d and d["y"] == 42
        assert "x" not in d


class TestSchemaValidator:

    def test_valid_data(self):
        from agent.structured_output import (SchemaValidator, OutputSchema,
                                              OutputField, FieldType)
        v = SchemaValidator()
        schema = OutputSchema("t", [
            OutputField("name", FieldType.STRING, "Name"),
            OutputField("age", FieldType.INTEGER, "Age"),
        ])
        result, warnings = v.validate({"name": "Alice", "age": "30"}, schema)
        assert result["name"] == "Alice"
        assert result["age"] == 30
        assert not warnings

    def test_missing_required_raises(self):
        from agent.structured_output import (SchemaValidator, OutputSchema,
                                              OutputField, FieldType, ValidationError)
        v = SchemaValidator()
        schema = OutputSchema("t", [OutputField("x", FieldType.STRING, "X")])
        with pytest.raises(ValidationError, match="missing"):
            v.validate({}, schema)

    def test_uses_default_for_optional(self):
        from agent.structured_output import (SchemaValidator, OutputSchema,
                                              OutputField, FieldType)
        v = SchemaValidator()
        schema = OutputSchema("t", [
            OutputField("y", FieldType.INTEGER, "Y", required=False, default=99)
        ])
        result, _ = v.validate({}, schema)
        assert result["y"] == 99

    def test_coerce_bool_from_string(self):
        from agent.structured_output import (SchemaValidator, OutputSchema,
                                              OutputField, FieldType)
        v = SchemaValidator()
        schema = OutputSchema("t", [OutputField("flag", FieldType.BOOLEAN, "Flag")])
        r, _ = v.validate({"flag": "true"}, schema)
        assert r["flag"] is True
        r2, _ = v.validate({"flag": "false"}, schema)
        assert r2["flag"] is False

    def test_coerce_float(self):
        from agent.structured_output import (SchemaValidator, OutputSchema,
                                              OutputField, FieldType)
        v = SchemaValidator()
        schema = OutputSchema("t", [OutputField("score", FieldType.FLOAT, "Score")])
        r, _ = v.validate({"score": "0.85"}, schema)
        assert abs(r["score"] - 0.85) < 1e-6

    def test_enum_case_insensitive(self):
        from agent.structured_output import (SchemaValidator, OutputSchema,
                                              OutputField, FieldType)
        v = SchemaValidator()
        schema = OutputSchema("t", [
            OutputField("mood", FieldType.ENUM, "Mood",
                       enum_values=["Happy","Sad","Neutral"])
        ])
        r, _ = v.validate({"mood": "happy"}, schema)
        assert r["mood"] == "Happy"

    def test_enum_invalid_raises(self):
        from agent.structured_output import (SchemaValidator, OutputSchema,
                                              OutputField, FieldType, ValidationError)
        v = SchemaValidator()
        schema = OutputSchema("t", [
            OutputField("x", FieldType.ENUM, "X", enum_values=["a","b"])
        ])
        with pytest.raises(ValidationError):
            v.validate({"x": "c"}, schema)

    def test_array_from_csv_string(self):
        from agent.structured_output import (SchemaValidator, OutputSchema,
                                              OutputField, FieldType)
        v = SchemaValidator()
        schema = OutputSchema("t", [
            OutputField("tags", FieldType.ARRAY, "Tags", item_type=FieldType.STRING)
        ])
        r, _ = v.validate({"tags": "python, ai, llm"}, schema)
        assert "python" in r["tags"]
        assert len(r["tags"]) == 3


class TestJSONExtractor:

    def test_bare_json(self):
        from agent.structured_output import JSONExtractor
        e = JSONExtractor()
        result = e.extract('{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_json_in_prose(self):
        from agent.structured_output import JSONExtractor
        e = JSONExtractor()
        text = 'Here is the result: {"answer": "yes", "score": 0.9} as requested.'
        result = e.extract(text)
        assert result is not None
        assert result.get("answer") == "yes"

    def test_json_in_code_fence(self):
        from agent.structured_output import JSONExtractor
        e = JSONExtractor()
        text = 'Sure!\n```json\n{"label": "positive", "score": 0.8}\n```'
        result = e.extract(text)
        assert result is not None
        assert result.get("label") == "positive"

    def test_trailing_comma_cleaned(self):
        from agent.structured_output import JSONExtractor
        e = JSONExtractor()
        text = '{"a": 1, "b": 2,}'
        result = e.extract(text)
        assert result is not None

    def test_no_json_returns_none(self):
        from agent.structured_output import JSONExtractor
        e = JSONExtractor()
        result = e.extract("This is just plain text with no JSON at all.")
        assert result is None


class TestCommonSchemas:

    def test_sentiment_schema_fields(self):
        from agent.structured_output import SENTIMENT_SCHEMA
        names = [f.name for f in SENTIMENT_SCHEMA.output_fields]
        assert "label" in names
        assert "score" in names
        assert "summary" in names

    def test_entity_schema_fields(self):
        from agent.structured_output import ENTITY_SCHEMA
        names = [f.name for f in ENTITY_SCHEMA.output_fields]
        assert "people" in names
        assert "organizations" in names
        assert "locations" in names

    def test_plan_schema_fields(self):
        from agent.structured_output import PLAN_SCHEMA
        names = [f.name for f in PLAN_SCHEMA.output_fields]
        assert "title" in names
        assert "steps" in names

    def test_code_review_schema_fields(self):
        from agent.structured_output import CODE_REVIEW_SCHEMA
        names = [f.name for f in CODE_REVIEW_SCHEMA.output_fields]
        assert "severity" in names
        assert "issues" in names
        assert "score" in names


# ══════════════════════════════════════════════════════════════════════════════
# TOOL REGISTRY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestToolParam:

    def test_to_schema_string(self):
        from agent.tools_registry import ToolParam, ParamType
        p = ToolParam("query", ParamType.STRING, "Search query")
        s = p.to_schema()
        assert s["type"] == "string"
        assert s["description"] == "Search query"

    def test_to_schema_enum(self):
        from agent.tools_registry import ToolParam, ParamType
        p = ToolParam("mood", ParamType.STRING, "Mood",
                     enum_values=["happy","sad"])
        s = p.to_schema()
        assert s["enum"] == ["happy","sad"]

    def test_to_schema_integer(self):
        from agent.tools_registry import ToolParam, ParamType
        p = ToolParam("count", ParamType.INTEGER, "Count")
        assert p.to_schema()["type"] == "integer"


class TestRegisteredTool:

    def _make_tool(self):
        from agent.tools_registry import RegisteredTool, ToolParam, ParamType
        async def fn(query: str, limit: int = 5):
            return []
        return RegisteredTool(
            name="search", description="Search", fn=fn,
            params=[
                ToolParam("query", ParamType.STRING, "Query"),
                ToolParam("limit", ParamType.INTEGER, "Limit",
                         required=False, default=5),
            ],
            category="research",
        )

    def test_to_openai_schema(self):
        tool = self._make_tool()
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "search"
        assert "query" in schema["function"]["parameters"]["properties"]
        assert "query" in schema["function"]["parameters"]["required"]
        assert "limit" not in schema["function"]["parameters"]["required"]

    def test_to_anthropic_schema(self):
        tool = self._make_tool()
        schema = tool.to_anthropic_schema()
        assert schema["name"] == "search"
        assert "input_schema" in schema
        assert "query" in schema["input_schema"]["properties"]

    def test_to_dict(self):
        tool = self._make_tool()
        d = tool.to_dict()
        assert d["name"] == "search"
        assert d["category"] == "research"
        assert d["call_count"] == 0

    def test_success_rate_no_calls(self):
        tool = self._make_tool()
        assert tool.success_rate == 1.0

    def test_success_rate_with_errors(self):
        tool = self._make_tool()
        tool.call_count = 10
        tool.error_count = 2
        assert abs(tool.success_rate - 0.8) < 1e-6


class TestToolRegistry:

    @pytest.fixture
    def registry(self):
        from agent.tools_registry import ToolRegistry
        return ToolRegistry()

    @pytest.mark.asyncio
    async def test_register_and_call_async(self, registry):
        from agent.tools_registry import ToolParam, ParamType, ToolCall

        @registry.register(
            description="Add two numbers",
            params=[
                ToolParam("a", ParamType.NUMBER, "First number"),
                ToolParam("b", ParamType.NUMBER, "Second number"),
            ],
            category="math",
        )
        async def add(a: float, b: float) -> float:
            return a + b

        result = await registry.call(ToolCall("add", {"a": 3, "b": 4}))
        assert result.success
        assert result.output == 7.0

    @pytest.mark.asyncio
    async def test_register_sync_tool(self, registry):
        from agent.tools_registry import ToolParam, ParamType, ToolCall

        @registry.register(
            description="Multiply",
            params=[
                ToolParam("x", ParamType.INTEGER, "x"),
                ToolParam("y", ParamType.INTEGER, "y"),
            ],
        )
        def multiply(x: int, y: int) -> int:
            return x * y

        result = await registry.call(ToolCall("multiply", {"x": 6, "y": 7}))
        assert result.success
        assert result.output == 42

    @pytest.mark.asyncio
    async def test_missing_tool_returns_error(self, registry):
        from agent.tools_registry import ToolCall
        result = await registry.call(ToolCall("nonexistent_tool", {}))
        assert not result.success
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_disabled_tool_returns_error(self, registry):
        from agent.tools_registry import ToolParam, ParamType, ToolCall

        @registry.register(description="A tool", params=[])
        async def my_tool() -> str:
            return "ok"

        registry.disable("my_tool")
        result = await registry.call(ToolCall("my_tool", {}))
        assert not result.success
        assert "disabled" in result.error

    @pytest.mark.asyncio
    async def test_param_validation_and_coercion(self, registry):
        from agent.tools_registry import ToolParam, ParamType, ToolCall

        @registry.register(
            description="Negate",
            params=[ToolParam("x", ParamType.INTEGER, "value")],
        )
        async def negate(x: int) -> int:
            return -x

        # Pass as string "5" — should be coerced to int
        result = await registry.call(ToolCall("negate", {"x": "5"}))
        assert result.success
        assert result.output == -5

    @pytest.mark.asyncio
    async def test_missing_required_param(self, registry):
        from agent.tools_registry import ToolParam, ParamType, ToolCall

        @registry.register(
            description="Requires query",
            params=[ToolParam("query", ParamType.STRING, "query")],
        )
        async def needs_query(query: str) -> str:
            return query

        result = await registry.call(ToolCall("needs_query", {}))
        assert not result.success
        assert "Validation" in result.error or "Required" in result.error

    @pytest.mark.asyncio
    async def test_timeout_enforced(self, registry):
        from agent.tools_registry import ToolParam, ParamType, ToolCall, RegisteredTool

        async def slow_fn() -> str:
            await asyncio.sleep(10)
            return "done"

        tool = RegisteredTool(
            name="slow", description="slow", fn=slow_fn,
            params=[], timeout=0.05
        )
        registry.add(tool)
        result = await registry.call(ToolCall("slow", {}))
        assert not result.success
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_tool_exception_captured(self, registry):
        from agent.tools_registry import ToolCall

        @registry.register(description="Explodes", params=[])
        async def exploder() -> str:
            raise ValueError("intentional error")

        result = await registry.call(ToolCall("exploder", {}))
        assert not result.success
        assert "intentional error" in result.error

    def test_openai_schemas(self, registry):
        from agent.tools_registry import ToolParam, ParamType

        @registry.register(description="Test tool", params=[
            ToolParam("x", ParamType.STRING, "x")
        ], category="test")
        async def test_fn(x: str) -> str:
            return x

        schemas = registry.openai_schemas()
        assert any(s["function"]["name"] == "test_fn" for s in schemas)

    def test_anthropic_schemas(self, registry):
        from agent.tools_registry import ToolParam, ParamType

        @registry.register(description="Anthropic tool", params=[
            ToolParam("q", ParamType.STRING, "q")
        ])
        async def anthro_fn(q: str) -> str:
            return q

        schemas = registry.anthropic_schemas()
        assert any(s["name"] == "anthro_fn" for s in schemas)

    @pytest.mark.asyncio
    async def test_call_batch_parallel(self, registry):
        from agent.tools_registry import ToolParam, ParamType, ToolCall

        @registry.register(description="Double", params=[
            ToolParam("n", ParamType.INTEGER, "n")
        ])
        async def double(n: int) -> int:
            return n * 2

        calls = [ToolCall("double", {"n": i}) for i in range(5)]
        results = await registry.call_batch(calls, parallel=True)
        assert len(results) == 5
        assert all(r.success for r in results)
        outputs = {r.output for r in results}
        assert 0 in outputs and 8 in outputs

    def test_list_tools_by_category(self, registry):
        from agent.tools_registry import ToolParam, ParamType

        @registry.register(description="Cat A", params=[], category="catA")
        async def tool_a() -> str: return "a"

        @registry.register(description="Cat B", params=[], category="catB")
        async def tool_b() -> str: return "b"

        tools_a = registry.list_tools(category="catA")
        assert all(t["category"] == "catA" for t in tools_a)
        assert len(tools_a) == 1

    def test_middleware_can_block(self, registry):
        from agent.tools_registry import ToolCall

        @registry.register(description="Blocked", params=[])
        async def blocked_tool() -> str: return "ok"

        def block_all(call, reg):
            raise PermissionError("blocked by middleware")

        registry.use(block_all)
        result = asyncio.get_event_loop().run_until_complete(
            registry.call(ToolCall("blocked_tool", {}))
        )
        assert not result.success
        assert "Middleware" in result.error

    def test_search(self, registry):
        from agent.tools_registry import ToolParam, ParamType

        @registry.register(description="Search the database for records",
                           params=[], tags=["database"])
        async def db_search() -> list: return []

        results = registry.search("database")
        assert any(t.name == "db_search" for t in results)


# ══════════════════════════════════════════════════════════════════════════════
# TRACING TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestTracer:

    @pytest.fixture
    def tracer(self):
        from agent.tracing import Tracer
        return Tracer()

    def test_new_trace_returns_id(self, tracer):
        tid = tracer.new_trace()
        assert len(tid) > 0

    def test_start_span(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        s = tracer.start_span("test.op", SpanKind.INTERNAL)
        assert s.span_id
        assert s.trace_id
        assert s.status == SpanStatus.PENDING
        assert s.name == "test.op"

    def test_span_duration(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        s = tracer.start_span("timed", SpanKind.INTERNAL)
        time.sleep(0.05)
        s.end(SpanStatus.OK)
        assert s.duration_ms >= 40

    def test_span_context_manager(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        with tracer.span("ctx.op", SpanKind.TOOL, tool="web_search") as s:
            s.set("result_count", 5)
        assert s.status == SpanStatus.OK
        assert s.attributes["result_count"] == 5
        assert s.attributes["tool"] == "web_search"

    def test_span_context_manager_on_error(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        s = None
        try:
            with tracer.span("failing.op", SpanKind.INTERNAL) as span:
                s = span
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert s is not None
        assert s.status == SpanStatus.ERROR
        assert "boom" in s.error

    @pytest.mark.asyncio
    async def test_async_span(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        async with tracer.async_span("async.op", SpanKind.LLM) as s:
            s.set("model", "test-model")
            await asyncio.sleep(0.01)
        assert s.status == SpanStatus.OK
        assert s.attributes["model"] == "test-model"

    def test_add_event(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        s = tracer.start_span("events", SpanKind.PIPELINE)
        s.add_event("step_start", {"step": "search"})
        s.add_event("step_done", {"step": "search"})
        s.end(SpanStatus.OK)
        assert len(s.events) == 2
        assert s.events[0]["name"] == "step_start"

    def test_get_spans_by_kind(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        s1 = tracer.start_span("llm.call", SpanKind.LLM)
        s1.end(SpanStatus.OK)
        s2 = tracer.start_span("tool.call", SpanKind.TOOL)
        s2.end(SpanStatus.OK)

        llm_spans = tracer.get_spans(kind=SpanKind.LLM)
        assert all(s.kind == SpanKind.LLM for s in llm_spans)

    def test_get_trace(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        tid = tracer.new_trace()
        s1 = tracer.start_span("s1", SpanKind.INTERNAL, trace_id=tid)
        s1.end(SpanStatus.OK)
        s2 = tracer.start_span("s2", SpanKind.INTERNAL, trace_id=tid)
        s2.end(SpanStatus.OK)

        trace_spans = tracer.get_trace(tid)
        assert len(trace_spans) == 2

    def test_summary_empty(self, tracer):
        s = tracer.summary()
        assert s["total_spans"] == 0

    def test_summary_with_spans(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        s = tracer.start_span("llm.test", SpanKind.LLM)
        s.set("model", "test-model")
        s.set("input_tokens_est", 100)
        s.set("output_tokens", 50)
        s.end(SpanStatus.OK)

        summary = tracer.summary()
        assert summary["completed_spans"] == 1
        assert summary["llm_calls"] == 1
        assert summary["total_input_tokens"] == 100
        assert summary["total_output_tokens"] == 50

    def test_recent_errors(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        s = tracer.start_span("bad.op", SpanKind.INTERNAL)
        s.end(SpanStatus.ERROR, error="test error")
        errors = tracer.recent_errors()
        assert len(errors) >= 1
        assert any(e["status"] == "error" for e in errors)

    def test_len(self, tracer):
        from agent.tracing import SpanKind
        assert len(tracer) == 0
        tracer.start_span("x", SpanKind.INTERNAL)
        assert len(tracer) == 1

    def test_clear(self, tracer):
        from agent.tracing import SpanKind
        tracer.start_span("x", SpanKind.INTERNAL)
        tracer.clear()
        assert len(tracer) == 0

    def test_cost_estimation(self):
        from agent.tracing import estimate_cost, estimate_tokens
        cost = estimate_cost("qwen3-next:80b-cloud", 1000, 500)
        assert cost > 0
        assert cost < 0.01  # sanity check

        tokens = estimate_tokens("Hello world!")
        assert tokens > 0

    def test_model_leaderboard(self, tracer):
        from agent.tracing import SpanKind, SpanStatus
        for model in ["model-a", "model-b", "model-a"]:
            s = tracer.start_span(f"llm.{model}", SpanKind.LLM)
            s.set("model", model)
            s.end(SpanStatus.OK)
        lb = tracer.model_leaderboard()
        assert lb[0]["model"] == "model-a"
        assert lb[0]["calls"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# WORKFLOW DSL TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkflowParser:

    def test_parse_dict_basic(self):
        from agent.workflow import WorkflowParser
        p = WorkflowParser()
        spec = p.parse_dict({
            "name": "test_wf",
            "description": "A test",
            "steps": [
                {"name": "step1", "action": "echo", "message": "Hello"},
                {"name": "step2", "action": "echo", "message": "World",
                 "condition": "step1"},
            ]
        })
        assert spec.name == "test_wf"
        assert len(spec.steps) == 2
        assert spec.steps[0].action == "echo"
        assert spec.steps[1].condition == "step1"

    def test_parse_dict_defaults(self):
        from agent.workflow import WorkflowParser
        p = WorkflowParser()
        spec = p.parse_dict({"name": "minimal", "steps": []})
        assert spec.description == ""
        assert spec.model_hint == ""
        assert spec.variables == {}

    def test_parse_dict_tool_step(self):
        from agent.workflow import WorkflowParser
        p = WorkflowParser()
        spec = p.parse_dict({
            "name": "wf",
            "steps": [{
                "name": "search",
                "action": "tool",
                "tool": "web_search",
                "params": {"query": "{{q}}"},
                "output": "results",
                "on_error": "skip",
            }]
        })
        step = spec.steps[0]
        assert step.tool == "web_search"
        assert step.params == {"query": "{{q}}"}
        assert step.on_error == "skip"


class TestTemplateEngine:

    def test_render_template_simple(self):
        from agent.workflow import _render_template
        result = _render_template("Hello {{name}}!", {"name": "Alice"})
        assert result == "Hello Alice!"

    def test_render_template_missing_key(self):
        from agent.workflow import _render_template
        result = _render_template("Hello {{name}}!", {})
        assert "{{name}}" in result  # left as-is

    def test_render_template_dict_value(self):
        from agent.workflow import _render_template
        ctx = {"data": {"key": "value"}}
        result = _render_template("Data: {{data}}", ctx)
        assert "key" in result

    def test_render_params_recursive(self):
        from agent.workflow import _render_params
        params = {
            "query": "{{topic}} research",
            "filters": ["{{category}}", "latest"],
            "nested": {"key": "{{value}}"},
        }
        ctx = {"topic": "AI", "category": "tech", "value": "42"}
        result = _render_params(params, ctx)
        assert result["query"] == "AI research"
        assert result["filters"][0] == "tech"
        assert result["nested"]["key"] == "42"

    def test_render_non_string_passthrough(self):
        from agent.workflow import _render_params
        result = _render_params(42, {})
        assert result == 42
        result2 = _render_params(None, {})
        assert result2 is None


class TestWorkflowBuiltins:
    """Test that built-in workflows are registered and have correct structure."""

    @pytest.fixture
    def mock_agent(self):
        """Minimal mock agent for workflow registration."""
        class MockMemory:
            def save_memory(self, *a, **kw): pass
            def get_history(self, *a, **kw): return []

        class MockLLM:
            async def chat(self, *a, **kw): return {"content": "ok"}

        class MockRAG:
            async def retrieve(self, *a, **kw): return []
            async def ingest_text(self, *a, **kw):
                from agent.rag import Document
                return Document(id="d1", title="t", source="", doc_type="raw", total_chunks=1)

        class MockPipelineExecutor:
            def register(self, p): pass
            async def run(self, p, ctx=None):
                from agent.pipeline import PipelineRun, StepStatus
                run = PipelineRun(run_id="test", pipeline_name=p.name)
                run.status = StepStatus.SUCCESS
                run.context = dict(ctx or {})
                run.finished_at = time.time()
                return run
            async def run_by_name(self, name, ctx=None):
                return None
            def list_pipelines(self): return []

        class MockStructuredParser:
            async def parse(self, text, schema, **kw):
                from agent.structured_output import ParseResult
                return ParseResult(success=True, data={"label":"neutral","score":0.5,"summary":"ok"},
                                   warnings=[], attempts=1, raw_response="{}", latency_ms=10)

        class Agent:
            memory = MockMemory()
            llm = MockLLM()
            rag = MockRAG()
            pipeline_executor = MockPipelineExecutor()
            structured_parser = MockStructuredParser()

        return Agent()

    def test_workflow_manager_registers_builtins(self, mock_agent):
        from agent.workflow import WorkflowManager
        wm = WorkflowManager(mock_agent)
        workflows = wm.list_workflows()
        names = [w["name"] for w in workflows]
        assert "research" in names
        assert "analyze_text" in names
        assert "code_review" in names

    def test_workflow_spec_structure(self, mock_agent):
        from agent.workflow import WorkflowManager
        wm = WorkflowManager(mock_agent)
        spec = wm.get_spec("research")
        assert spec is not None
        assert spec.description
        assert len(spec.steps) >= 3

    def test_register_custom_workflow(self, mock_agent):
        from agent.workflow import WorkflowManager
        wm = WorkflowManager(mock_agent)
        wm.register({
            "name": "custom_wf",
            "description": "My workflow",
            "steps": [
                {"name": "greet", "action": "echo",
                 "message": "Hello {{name}}!", "output": "greeting"},
            ]
        })
        assert wm.get_spec("custom_wf") is not None
        wfs = wm.list_workflows()
        assert any(w["name"] == "custom_wf" for w in wfs)

    @pytest.mark.asyncio
    async def test_run_echo_workflow(self, mock_agent):
        from agent.workflow import WorkflowManager
        wm = WorkflowManager(mock_agent)
        wm.register({
            "name": "echo_test",
            "steps": [
                {"name": "say", "action": "echo",
                 "message": "Hello {{name}}!", "output": "msg"},
            ]
        })
        run = await wm.run("echo_test", {"name": "World"})
        assert run is not None

    def test_run_unknown_raises(self, mock_agent):
        from agent.workflow import WorkflowManager
        wm = WorkflowManager(mock_agent)
        with pytest.raises(KeyError, match="ghost_workflow"):
            asyncio.get_event_loop().run_until_complete(
                wm.run("ghost_workflow", {})
            )


# ══════════════════════════════════════════════════════════════════════════════
# EVENT BUS / STREAMING TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestEventBus:

    @pytest.fixture
    def bus(self):
        from agent.streaming import EventBus
        return EventBus(max_queue=32, max_history=100)

    @pytest.mark.asyncio
    async def test_publish_and_receive(self, bus):
        from agent.streaming import BusMessage, EventBusEvent
        sid = bus.subscribe()
        await bus.publish(BusMessage(EventBusEvent.SYSTEM, "hello"))
        q = bus._subscribers[sid]
        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        assert msg.data == "hello"
        assert msg.event == EventBusEvent.SYSTEM

    @pytest.mark.asyncio
    async def test_multiple_subscribers_receive(self, bus):
        from agent.streaming import BusMessage, EventBusEvent
        sid1 = bus.subscribe()
        sid2 = bus.subscribe()
        await bus.publish(BusMessage(EventBusEvent.TOKEN, "tok"))
        q1 = bus._subscribers[sid1]
        q2 = bus._subscribers[sid2]
        m1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        m2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert m1.data == m2.data == "tok"

    @pytest.mark.asyncio
    async def test_event_filter(self, bus):
        from agent.streaming import BusMessage, EventBusEvent
        sid = bus.subscribe(events=[EventBusEvent.TOKEN])
        await bus.publish(BusMessage(EventBusEvent.SYSTEM, "sys"))
        await bus.publish(BusMessage(EventBusEvent.TOKEN, "tok"))
        q = bus._subscribers[sid]
        assert q.qsize() == 1
        msg = q.get_nowait()
        assert msg.event == EventBusEvent.TOKEN

    @pytest.mark.asyncio
    async def test_unsubscribe(self, bus):
        from agent.streaming import BusMessage, EventBusEvent
        sid = bus.subscribe()
        bus.unsubscribe(sid)
        await bus.publish(BusMessage(EventBusEvent.SYSTEM, "x"))
        assert sid not in bus._subscribers

    @pytest.mark.asyncio
    async def test_history_stored(self, bus):
        from agent.streaming import BusMessage, EventBusEvent
        await bus.publish(BusMessage(EventBusEvent.TOOL_CALL, {"tool":"search"}))
        await bus.publish(BusMessage(EventBusEvent.TOOL_RESULT, {"result":"ok"}))
        recent = bus.recent(limit=10)
        assert len(recent) == 2

    @pytest.mark.asyncio
    async def test_history_filter_by_event(self, bus):
        from agent.streaming import BusMessage, EventBusEvent
        await bus.publish(BusMessage(EventBusEvent.TOKEN, "t1"))
        await bus.publish(BusMessage(EventBusEvent.SYSTEM, "s1"))
        await bus.publish(BusMessage(EventBusEvent.TOKEN, "t2"))
        tokens = bus.recent(event=EventBusEvent.TOKEN)
        assert all(m.event == EventBusEvent.TOKEN for m in tokens)
        assert len(tokens) == 2

    def test_subscriber_count(self, bus):
        from agent.streaming import EventBusEvent
        assert bus.subscriber_count == 0
        sid1 = bus.subscribe()
        sid2 = bus.subscribe()
        assert bus.subscriber_count == 2
        bus.unsubscribe(sid1)
        assert bus.subscriber_count == 1

    @pytest.mark.asyncio
    async def test_listen_yields_messages(self, bus):
        from agent.streaming import BusMessage, EventBusEvent
        sid = bus.subscribe()
        received = []

        async def publisher():
            await asyncio.sleep(0.02)
            await bus.publish(BusMessage(EventBusEvent.SYSTEM, "msg1"))
            await bus.publish(BusMessage(EventBusEvent.SYSTEM, "msg2"))

        async def consumer():
            count = 0
            async for msg in bus.listen(sid, timeout=0.5):
                if msg.data == "__heartbeat__":
                    continue
                received.append(msg)
                count += 1
                if count >= 2:
                    break

        await asyncio.gather(publisher(), consumer())
        assert len(received) == 2

    def test_session_id_in_message(self, bus):
        from agent.streaming import BusMessage, EventBusEvent
        msg = BusMessage(EventBusEvent.ROUTE, {"model": "x"}, session_id="sess123")
        assert msg.session_id == "sess123"

    def test_sse_format(self):
        from agent.streaming import sse_format
        frame = sse_format({"key": "val"}, event="token", id="abc")
        assert "event: token" in frame
        assert "id: abc" in frame
        assert '"key": "val"' in frame
        assert frame.endswith("\n\n")

    def test_sse_heartbeat(self):
        from agent.streaming import sse_heartbeat
        hb = sse_heartbeat()
        assert hb.startswith(": heartbeat")
        assert hb.endswith("\n\n")

    def test_bus_message_to_sse(self):
        from agent.streaming import BusMessage, EventBusEvent
        msg = BusMessage(EventBusEvent.TOKEN, "hello", session_id="s1")
        sse = msg.to_sse()
        assert "event: token" in sse
        assert "hello" in sse
