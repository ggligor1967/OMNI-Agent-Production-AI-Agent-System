"""
OMNI AGENT - Workflow DSL
Define multi-step agentic workflows in YAML or Python dicts.
Auto-compiles to Pipeline objects for execution.

Workflow YAML format:
        name: research_and_summarize
        description: Search, scrape, summarize, store
        model_hint: qwen3-next:80b-cloud
        steps:
            - name: search
                action: tool
                tool: web_search
                params:
                    query: "{{query}}"
                output: search_results
                on_error: skip

            - name: summarize
                action: llm
                prompt: |
                    Summarize these results about {{query}}:
                    {{search_results}}
                model: gpt-oss:120b-cloud
                output: summary
                condition: "search_results"

            - name: store
                action: memory
                key: "research:{{query}}"
                value: "{{summary}}"

            - name: structured
                action: structured
                schema: sentiment
                input: "{{summary}}"
                output: sentiment_result

Supported actions: tool | llm | memory | structured | pipeline | echo | transform
"""
import ast
import re
import time
import json
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Union
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

from agent.pipeline import Pipeline, PipelineStep, PipelineExecutor
from agent.structured_output import (
    StructuredOutputParser, OutputSchema, OutputField, FieldType,
    SENTIMENT_SCHEMA, ENTITY_SCHEMA, PLAN_SCHEMA, CODE_REVIEW_SCHEMA
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# BUILT-IN SCHEMAS (accessible by name in workflow YAML)
# ══════════════════════════════════════════════════════════════════════════════

NAMED_SCHEMAS: Dict[str, OutputSchema] = {
    "sentiment":    SENTIMENT_SCHEMA,
    "entities":     ENTITY_SCHEMA,
    "plan":         PLAN_SCHEMA,
    "code_review":  CODE_REVIEW_SCHEMA,
}


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATE ENGINE ({{variable}} substitution)
# ══════════════════════════════════════════════════════════════════════════════

def _render_template(text: str, context: Dict[str, Any]) -> str:
    """Replace {{variable}} placeholders with context values."""
    if not isinstance(text, str):
        return text

    def replacer(match):
        key = match.group(1).strip()
        val = context.get(key, match.group(0))  # leave placeholder if missing
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)[:2000]
        return str(val)

    return re.sub(r'\{\{(\w+)\}\}', replacer, text)


def _render_params(params: Any, context: Dict) -> Any:
    """Recursively render templates in params dict/list/str."""
    if isinstance(params, str):
        return _render_template(params, context)
    elif isinstance(params, dict):
        return {k: _render_params(v, context) for k, v in params.items()}
    elif isinstance(params, list):
        return [_render_params(v, context) for v in params]
    return params


SAFE_TRANSFORM_FUNCTIONS: Dict[str, Callable[..., Any]] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
}

SAFE_TRANSFORM_METHODS: Dict[type, set[str]] = {
    dict: {"get", "items", "keys", "values"},
    list: {"count", "index"},
    set: {"difference", "intersection", "issubset", "issuperset", "union"},
    str: {
        "endswith",
        "lower",
        "lstrip",
        "replace",
        "rstrip",
        "split",
        "startswith",
        "strip",
        "upper",
    },
    tuple: {"count", "index"},
}


class _SafeTransformEvaluator(ast.NodeVisitor):
    """Evaluate a tightly-scoped expression language for workflow transforms."""

    def __init__(self, context: Dict[str, Any]):
        self.context = dict(context)

    def evaluate(self, expr: str) -> Any:
        tree = ast.parse(expr, mode="eval")
        return self.visit(tree.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in SAFE_TRANSFORM_FUNCTIONS:
            return SAFE_TRANSFORM_FUNCTIONS[node.id]
        if node.id in self.context:
            return self.context[node.id]
        raise ValueError(f"Unknown name '{node.id}'")

    def visit_List(self, node: ast.List) -> List[Any]:
        return [self.visit(item) for item in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        return tuple(self.visit(item) for item in node.elts)

    def visit_Set(self, node: ast.Set) -> set[Any]:
        return {self.visit(item) for item in node.elts}

    def visit_Dict(self, node: ast.Dict) -> Dict[Any, Any]:
        return {
            self.visit(key): self.visit(value)
            for key, value in zip(node.keys, node.values)
        }

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        value = self.visit(node.value)
        index = self._resolve_slice(node.slice)
        return value[index]

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        value = self.visit(node.value)
        attr = self._resolve_attribute(value, node.attr)
        if callable(attr):
            raise ValueError(f"Method '{node.attr}' must be called explicitly")
        return attr

    def visit_Call(self, node: ast.Call) -> Any:
        args = [self.visit(arg) for arg in node.args]
        kwargs = {
            kw.arg: self.visit(kw.value)
            for kw in node.keywords
            if kw.arg is not None
        }

        if isinstance(node.func, ast.Name):
            func = SAFE_TRANSFORM_FUNCTIONS.get(node.func.id)
            if func is None:
                raise ValueError(f"Function '{node.func.id}' is not allowed")
            return func(*args, **kwargs)

        if isinstance(node.func, ast.Attribute):
            target = self.visit(node.func.value)
            method = self._resolve_method(target, node.func.attr)
            return method(*args, **kwargs)

        raise ValueError("Unsupported function call")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)

        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right

        raise ValueError(f"Operator '{type(node.op).__name__}' is not allowed")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)

        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.Not):
            return not operand

        raise ValueError(f"Unary operator '{type(node.op).__name__}' is not allowed")

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        values = [self.visit(value) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        raise ValueError(f"Boolean operator '{type(node.op).__name__}' is not allowed")

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.In):
                ok = left in right
            elif isinstance(op, ast.NotIn):
                ok = left not in right
            elif isinstance(op, ast.Is):
                ok = left is right
            elif isinstance(op, ast.IsNot):
                ok = left is not right
            else:
                raise ValueError(f"Comparison '{type(op).__name__}' is not allowed")

            if not ok:
                return False
            left = right

        return True

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def _resolve_slice(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Slice):
            return slice(
                self.visit(node.lower) if node.lower else None,
                self.visit(node.upper) if node.upper else None,
                self.visit(node.step) if node.step else None,
            )
        return self.visit(node)

    def _resolve_attribute(self, value: Any, attr: str) -> Any:
        if attr.startswith("_"):
            raise ValueError(f"Private attribute access is not allowed: '{attr}'")

        if isinstance(value, dict) and attr in value:
            return value[attr]

        if not hasattr(value, attr):
            raise ValueError(f"Attribute '{attr}' is not available")

        return getattr(value, attr)

    def _resolve_method(self, value: Any, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise ValueError(f"Private method access is not allowed: '{name}'")

        allowed_names: set[str] = set()
        for value_type, names in SAFE_TRANSFORM_METHODS.items():
            if isinstance(value, value_type):
                allowed_names.update(names)

        if name not in allowed_names:
            raise ValueError(f"Method '{name}' is not allowed")

        method = getattr(value, name)
        if not callable(method):
            raise ValueError(f"Attribute '{name}' is not callable")
        return method

    def generic_visit(self, node: ast.AST) -> Any:
        raise ValueError(f"Unsupported expression node '{type(node).__name__}'")


def _safe_eval_transform(expr: str, context: Dict[str, Any]) -> Any:
    try:
        return _SafeTransformEvaluator(context).evaluate(expr)
    except SyntaxError as exc:
        raise ValueError(f"Invalid transform expression '{expr}': {exc.msg}") from exc


# ══════════════════════════════════════════════════════════════════════════════
# WORKFLOW STEP SPEC
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class WorkflowStepSpec:
    """Parsed specification for a single workflow step."""
    name: str
    action: str                           # tool|llm|memory|structured|pipeline|echo|transform
    output: str = ""                      # context key to store result
    on_error: str = "fail"               # fail|skip|retry
    max_retries: int = 0
    timeout: float = 60.0
    condition: str = ""                   # context key that must be truthy
    model: str = ""                       # model override for llm action
    # action-specific fields
    tool: str = ""                        # for action=tool
    params: Dict = field(default_factory=dict)   # for action=tool
    prompt: str = ""                      # for action=llm
    schema: str = ""                      # for action=structured
    input: str = ""                       # for action=structured (context key)
    key: str = ""                         # for action=memory
    value: str = ""                       # for action=memory
    pipeline_name: str = ""              # for action=pipeline
    transform_expr: str = ""              # for action=transform (Python expr)
    echo_message: str = ""               # for action=echo


@dataclass
class WorkflowSpec:
    """A complete parsed workflow definition."""
    name: str
    description: str = ""
    model_hint: str = ""
    steps: List[WorkflowStepSpec] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)  # default values


# ══════════════════════════════════════════════════════════════════════════════
# PARSER
# ══════════════════════════════════════════════════════════════════════════════

class WorkflowParser:
    """Parses YAML strings or Python dicts into WorkflowSpec objects."""

    def parse_dict(self, data: Dict) -> WorkflowSpec:
        steps = []
        for s in data.get("steps", []):
            steps.append(WorkflowStepSpec(
                name=s.get("name", f"step_{len(steps)}"),
                action=s.get("action", "echo"),
                output=s.get("output", s.get("name", "")),
                on_error=s.get("on_error", "fail"),
                max_retries=int(s.get("max_retries", 0)),
                timeout=float(s.get("timeout", 60)),
                condition=s.get("condition", ""),
                model=s.get("model", ""),
                tool=s.get("tool", ""),
                params=s.get("params", {}),
                prompt=s.get("prompt", ""),
                schema=s.get("schema", ""),
                input=s.get("input", ""),
                key=s.get("key", ""),
                value=s.get("value", ""),
                pipeline_name=s.get("pipeline", ""),
                transform_expr=s.get("transform", ""),
                echo_message=s.get("message", ""),
            ))
        return WorkflowSpec(
            name=data.get("name", "unnamed"),
            description=data.get("description", ""),
            model_hint=data.get("model_hint", ""),
            steps=steps,
            variables=data.get("variables", {}),
        )

    def parse_yaml(self, yaml_text: str) -> WorkflowSpec:
        if not YAML_AVAILABLE:
            raise ImportError("PyYAML required: pip install pyyaml")
        data = yaml.safe_load(yaml_text)
        return self.parse_dict(data)

    def parse_file(self, path: str) -> WorkflowSpec:
        content = Path(path).read_text()
        if path.endswith(".json"):
            return self.parse_dict(json.loads(content))
        return self.parse_yaml(content)


# ══════════════════════════════════════════════════════════════════════════════
# COMPILER — WorkflowSpec → Pipeline
# ══════════════════════════════════════════════════════════════════════════════

class WorkflowCompiler:
    """Compiles a WorkflowSpec into an executable Pipeline."""

    def __init__(self, agent):
        self.agent = agent
        self.structured_parser = StructuredOutputParser(agent.llm)

    def compile(self, spec: WorkflowSpec) -> Pipeline:
        """Convert a WorkflowSpec into a Pipeline ready for execution."""
        pipeline = Pipeline(spec.name, spec.description)

        for step_spec in spec.steps:
            handler = self._build_handler(step_spec, spec)
            condition = self._build_condition(step_spec)

            pipeline.step(
                name=step_spec.name,
                handler=handler,
                description=f"{step_spec.action}: {step_spec.name}",
                condition=condition,
                on_error=step_spec.on_error,
                max_retries=step_spec.max_retries,
                timeout=step_spec.timeout,
                output_key=step_spec.output or step_spec.name,
            )

        return pipeline

    def _build_condition(self, spec: WorkflowStepSpec) -> Optional[Callable]:
        if not spec.condition:
            return None
        key = spec.condition

        def _check(ctx: Dict) -> bool:
            val = ctx.get(key)
            if val is None or val == "" or val == [] or val == {}:
                return False
            return True

        return _check

    def _build_handler(self, spec: WorkflowStepSpec,
                       wf: WorkflowSpec) -> Callable:
        """Build an async handler function for a step."""
        agent = self.agent
        parser = self.structured_parser

        if spec.action == "tool":
            async def tool_handler(ctx: Dict) -> Any:
                rendered_params = _render_params(spec.params, ctx)
                from agent.tools_registry import ToolCall
                call = ToolCall(
                    tool_name=spec.tool,
                    arguments=rendered_params,
                    session_id=ctx.get("_session_id", "workflow"),
                )
                if not hasattr(agent, "tool_registry"):
                    raise RuntimeError("Tool registry is required for workflow tool steps")
                result = await agent.tool_registry.call(call)
                if not result.success:
                    raise RuntimeError(result.error)
                return result.output
            return tool_handler

        elif spec.action == "llm":
            async def llm_handler(ctx: Dict) -> str:
                prompt = _render_template(spec.prompt, ctx)
                model = spec.model or wf.model_hint or None
                session_id = ctx.get("_session_id", "workflow")
                resp = await agent.llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model=model,
                    session_id=session_id,
                    auto_route=not bool(spec.model),
                )
                return resp.get("content", "")
            return llm_handler

        elif spec.action == "structured":
            async def structured_handler(ctx: Dict) -> Dict:
                schema = NAMED_SCHEMAS.get(spec.schema)
                if not schema:
                    raise ValueError(f"Unknown schema '{spec.schema}'. "
                                   f"Available: {list(NAMED_SCHEMAS.keys())}")
                input_text = (ctx.get(spec.input, "")
                             if spec.input else _render_template(spec.prompt, ctx))
                if not input_text:
                    raise ValueError(f"No input for structured step '{spec.name}'")
                result = await parser.parse(
                    str(input_text), schema,
                    model=spec.model or None,
                    session_id=ctx.get("_session_id", "workflow"),
                )
                return result.data
            return structured_handler

        elif spec.action == "memory":
            async def memory_handler(ctx: Dict) -> str:
                key = _render_template(spec.key, ctx)
                value = _render_template(spec.value, ctx)
                agent.memory.save_memory(key, value, category="workflow")
                return f"stored:{key}"
            return memory_handler

        elif spec.action == "pipeline":
            async def pipeline_handler(ctx: Dict) -> Dict:
                sub_ctx = dict(ctx)
                run = await agent.pipeline_executor.run_by_name(
                    spec.pipeline_name, sub_ctx
                )
                if not run:
                    raise ValueError(f"Pipeline '{spec.pipeline_name}' not found")
                return run.context
            return pipeline_handler

        elif spec.action == "echo":
            async def echo_handler(ctx: Dict) -> str:
                msg = _render_template(spec.echo_message, ctx)
                logger.info(f"[workflow:{wf.name}] {spec.name}: {msg}")
                return msg
            return echo_handler

        elif spec.action == "transform":
            async def transform_handler(ctx: Dict) -> Any:
                expr = _render_template(spec.transform_expr, ctx)
                try:
                    return _safe_eval_transform(expr, ctx)
                except Exception as e:
                    raise ValueError(f"Transform '{expr}' failed: {e}")
            return transform_handler

        else:
            async def unknown_handler(ctx: Dict) -> str:
                raise ValueError(f"Unknown workflow action: '{spec.action}'")
            return unknown_handler


# ══════════════════════════════════════════════════════════════════════════════
# WORKFLOW MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class WorkflowManager:
    """
    Manages workflow definitions and their compilation/execution.
    Workflows can be registered from dicts, YAML strings, or files.

    Usage:
        wm = WorkflowManager(agent)

        # Register from dict
        wm.register({
            "name": "greet",
            "steps": [
                {"name": "say", "action": "echo", "message": "Hello {{name}}!"}
            ]
        })

        # Or from YAML file
        wm.load_file("workflows/research.yaml")

        # Execute
        run = await wm.run("greet", {"name": "Alice"})
        print(run.context)
    """

    def __init__(self, agent):
        self.agent = agent
        self._parser = WorkflowParser()
        self._compiler = WorkflowCompiler(agent)
        self._specs: Dict[str, WorkflowSpec] = {}
        self._pipelines: Dict[str, Pipeline] = {}
        self._load_builtins()

    def _load_builtins(self):
        """Register built-in workflows."""
        builtins = [
            {
                "name": "research",
                "description": "Web research with summarization",
                "model_hint": "qwen3-next:80b-cloud",
                "steps": [
                    {
                        "name": "search",
                        "action": "tool",
                        "tool": "web_search",
                        "params": {"query": "{{query}}", "num_results": 5},
                        "output": "search_results",
                        "on_error": "skip",
                    },
                    {
                        "name": "summarize",
                        "action": "llm",
                        "prompt": "Summarize these search results about {{query}}:\n\n{{search_results}}",
                        "output": "summary",
                        "condition": "search_results",
                        "model": "gpt-oss:120b-cloud",
                    },
                    {
                        "name": "store",
                        "action": "memory",
                        "key": "research:{{query}}",
                        "value": "{{summary}}",
                        "output": "stored",
                        "condition": "summary",
                        "on_error": "skip",
                    },
                    {
                        "name": "analyze_sentiment",
                        "action": "structured",
                        "schema": "sentiment",
                        "input": "summary",
                        "output": "sentiment",
                        "on_error": "skip",
                    },
                ],
            },
            {
                "name": "analyze_text",
                "description": "Deep text analysis: entities + sentiment + summary",
                "steps": [
                    {
                        "name": "entities",
                        "action": "structured",
                        "schema": "entities",
                        "input": "text",
                        "output": "entities",
                        "on_error": "skip",
                    },
                    {
                        "name": "sentiment",
                        "action": "structured",
                        "schema": "sentiment",
                        "input": "text",
                        "output": "sentiment",
                        "on_error": "skip",
                    },
                    {
                        "name": "echo_done",
                        "action": "echo",
                        "message": "Analysis complete for input text",
                        "output": "status",
                    },
                ],
            },
            {
                "name": "code_review",
                "description": "Review code and return structured findings",
                "model_hint": "devstral-2:123b-cloud",
                "steps": [
                    {
                        "name": "review",
                        "action": "structured",
                        "schema": "code_review",
                        "prompt": "Review this code:\n\n```\n{{code}}\n```",
                        "output": "review_result",
                    },
                    {
                        "name": "plan_fixes",
                        "action": "structured",
                        "schema": "plan",
                        "prompt": "Create a fix plan for: {{review_result}}",
                        "output": "fix_plan",
                        "condition": "review_result",
                        "on_error": "skip",
                    },
                ],
            },
        ]
        for w in builtins:
            try:
                self.register(w)
            except Exception as e:
                logger.warning(f"Built-in workflow '{w['name']}' failed to register: {e}")

    def register(self, data: Union[Dict, str]) -> WorkflowSpec:
        """Register a workflow from a dict or YAML string."""
        if isinstance(data, str):
            spec = self._parser.parse_yaml(data)
        else:
            spec = self._parser.parse_dict(data)

        self._specs[spec.name] = spec
        self._pipelines[spec.name] = self._compiler.compile(spec)
        logger.info(f"Workflow registered: '{spec.name}' ({len(spec.steps)} steps)")
        return spec

    def load_file(self, path: str) -> WorkflowSpec:
        """Load a workflow from a YAML or JSON file."""
        spec = self._parser.parse_file(path)
        self._specs[spec.name] = spec
        self._pipelines[spec.name] = self._compiler.compile(spec)
        logger.info(f"Workflow loaded from file: '{spec.name}'")
        return spec

    def load_directory(self, dir_path: str) -> List[WorkflowSpec]:
        """Load all .yaml and .json workflow files from a directory."""
        loaded = []
        for path in Path(dir_path).glob("*.yaml"):
            try:
                loaded.append(self.load_file(str(path)))
            except Exception as e:
                logger.warning(f"Failed to load workflow {path}: {e}")
        for path in Path(dir_path).glob("*.json"):
            try:
                loaded.append(self.load_file(str(path)))
            except Exception as e:
                logger.warning(f"Failed to load workflow {path}: {e}")
        return loaded

    async def run(self, name: str, context: Dict = None) -> Any:
        """Execute a registered workflow. Returns PipelineRun."""
        pipeline = self._pipelines.get(name)
        if not pipeline:
            raise KeyError(f"Workflow '{name}' not found. "
                          f"Available: {list(self._specs.keys())}")
        # Merge default variables from spec
        spec = self._specs[name]
        ctx = dict(spec.variables)
        if context:
            ctx.update(context)
        ctx.setdefault("_workflow_name", name)

        return await self.agent.pipeline_executor.run(pipeline, ctx)

    def get_spec(self, name: str) -> Optional[WorkflowSpec]:
        return self._specs.get(name)

    def list_workflows(self) -> List[Dict]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "steps": len(s.steps),
                "model_hint": s.model_hint,
                "actions": list({st.action for st in s.steps}),
            }
            for s in self._specs.values()
        ]

    def export_yaml(self, name: str) -> str:
        """Export a workflow back to YAML (requires pyyaml)."""
        if not YAML_AVAILABLE:
            raise ImportError("PyYAML required: pip install pyyaml")
        spec = self._specs.get(name)
        if not spec:
            raise KeyError(f"Workflow '{name}' not found")
        data = {
            "name": spec.name,
            "description": spec.description,
            "model_hint": spec.model_hint,
            "variables": spec.variables,
            "steps": [
                {k: v for k, v in {
                    "name": s.name,
                    "action": s.action,
                    "output": s.output,
                    "on_error": s.on_error if s.on_error != "fail" else None,
                    "condition": s.condition or None,
                    "model": s.model or None,
                    "tool": s.tool or None,
                    "params": s.params or None,
                    "prompt": s.prompt or None,
                    "schema": s.schema or None,
                    "input": s.input or None,
                    "key": s.key or None,
                    "value": s.value or None,
                }.items() if v is not None}
                for s in spec.steps
            ],
        }
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)
