import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestToolRegistryEnforcement(unittest.IsolatedAsyncioTestCase):
    async def test_tool_directives_use_tool_registry_for_named_arguments(self):
        from agent.core import OmniAgent
        from agent.tools_registry import ParamType, ToolParam, ToolRegistry

        registry = ToolRegistry()

        @registry.register(
            description="Echo a query",
            params=[ToolParam("query", ParamType.STRING, "Query text")],
        )
        async def echo(query: str):
            return {"echo": query}

        original_call = registry.call
        registry.call = AsyncMock(side_effect=original_call)

        agent = OmniAgent.__new__(OmniAgent)
        agent.tool_registry = registry

        rendered = await OmniAgent._process_tool_calls(
            agent,
            "Before [TOOL: echo(query='hello world')] After",
            "sess-named",
        )

        self.assertIn("hello world", rendered)
        registry.call.assert_awaited_once()

    async def test_tool_directives_map_positional_argument_to_registered_param(self):
        from agent.core import OmniAgent
        from agent.tools_registry import ParamType, ToolParam, ToolRegistry

        registry = ToolRegistry()

        @registry.register(
            description="Echo a query",
            params=[ToolParam("query", ParamType.STRING, "Query text")],
        )
        async def echo(query: str):
            return {"echo": query}

        original_call = registry.call
        registry.call = AsyncMock(side_effect=original_call)

        agent = OmniAgent.__new__(OmniAgent)
        agent.tool_registry = registry

        rendered = await OmniAgent._process_tool_calls(
            agent,
            "Before [TOOL: echo(hello world)] After",
            "sess-positional",
        )

        self.assertIn("hello world", rendered)
        registry.call.assert_awaited_once()

    async def test_tool_directives_cannot_bypass_confirmation_policy(self):
        from agent.core import OmniAgent
        from agent.tools_registry import ParamType, ToolParam, ToolRegistry

        registry = ToolRegistry()

        @registry.register(
            description="Dangerous tool",
            params=[ToolParam("code", ParamType.STRING, "Code to execute")],
            requires_confirmation=True,
        )
        async def dangerous(code: str):
            return {"ran": True, "code": code}

        original_call = registry.call
        registry.call = AsyncMock(side_effect=original_call)

        agent = OmniAgent.__new__(OmniAgent)
        agent.tool_registry = registry

        result = await OmniAgent._execute_tool(
            agent,
            "dangerous",
            "print('hello')",
            "sess-confirm",
        )

        self.assertIn("confirmation", result.lower())
        registry.call.assert_awaited_once()

    async def test_workflow_tool_steps_require_tool_registry(self):
        from agent.workflow import WorkflowCompiler, WorkflowSpec, WorkflowStepSpec

        fallback = AsyncMock()
        agent = SimpleNamespace(llm=SimpleNamespace(), _execute_tool=fallback)
        compiler = WorkflowCompiler(agent)
        spec = WorkflowSpec(
            name="tool-only",
            steps=[
                WorkflowStepSpec(
                    name="call_tool",
                    action="tool",
                    tool="echo",
                    params={"query": "hello"},
                )
            ],
        )

        handler = compiler._build_handler(spec.steps[0], spec)

        with self.assertRaises(RuntimeError) as raised:
            await handler({})

        self.assertIn("Tool registry is required", str(raised.exception))
        fallback.assert_not_awaited()
