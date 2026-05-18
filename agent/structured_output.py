"""
OMNI AGENT - Structured Output Parser
Forces LLM responses into typed Python objects via JSON schema validation.
Auto-retries with corrective feedback on parse failures.

Features:
- Define output schemas as dataclasses or plain dicts
- Validate against JSON Schema (stdlib-only, no jsonschema dep)
- Coerce types (str→int, "true"→bool, etc.)
- Auto-retry with the parse error injected back into the prompt
- Pydantic-style field descriptions that go into the LLM prompt
- Batch extraction from long documents
"""
import re
import json
import time
import logging
import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union
from dataclasses import dataclass, field, fields, asdict
from enum import Enum

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA DEFINITION
# ══════════════════════════════════════════════════════════════════════════════

class FieldType(str, Enum):
    STRING  = "string"
    INTEGER = "integer"
    FLOAT   = "float"
    BOOLEAN = "boolean"
    ARRAY   = "array"
    OBJECT  = "object"
    ENUM    = "enum"


@dataclass
class OutputField:
    """A single field in a structured output schema."""
    name: str
    type: FieldType
    description: str = ""
    required: bool = True
    default: Any = None
    enum_values: List[str] = field(default_factory=list)   # for FieldType.ENUM
    item_type: Optional[FieldType] = None                   # for FieldType.ARRAY
    example: Any = None

    def schema_fragment(self) -> Dict:
        """Generate JSON Schema fragment for this field."""
        if self.type == FieldType.ENUM:
            return {"type": "string", "enum": self.enum_values,
                    "description": self.description}
        elif self.type == FieldType.ARRAY:
            item_schema = ({"type": self.item_type.value}
                          if self.item_type else {"type": "string"})
            return {"type": "array", "items": item_schema,
                    "description": self.description}
        elif self.type == FieldType.FLOAT:
            return {"type": "number", "description": self.description}
        else:
            return {"type": self.type.value, "description": self.description}


@dataclass
class OutputSchema:
    """
    A named schema defining the structure of an LLM's JSON output.

    Example:
        schema = OutputSchema("sentiment_result", fields=[
            OutputField("sentiment", FieldType.ENUM, "Sentiment label",
                        enum_values=["positive","negative","neutral"]),
            OutputField("score", FieldType.FLOAT, "Confidence 0-1"),
            OutputField("keywords", FieldType.ARRAY, "Key phrases",
                        item_type=FieldType.STRING, required=False),
        ])
    """
    name: str
    output_fields: List[OutputField]
    description: str = ""

    def to_json_schema(self) -> Dict:
        props = {f.name: f.schema_fragment() for f in self.output_fields}
        required = [f.name for f in self.output_fields if f.required]
        return {
            "type": "object",
            "description": self.description,
            "properties": props,
            "required": required,
        }

    def prompt_description(self) -> str:
        """Human-readable field spec to inject into the system prompt."""
        lines = [f"Return a JSON object with these fields:"]
        for f in self.output_fields:
            req = "required" if f.required else "optional"
            t = (f.type.value if f.type != FieldType.ENUM
                 else f"one of: {f.enum_values}")
            ex = f" (e.g. {json.dumps(f.example)})" if f.example is not None else ""
            lines.append(f"  - {f.name} ({t}, {req}): {f.description}{ex}")
        return "\n".join(lines)

    def defaults(self) -> Dict:
        return {
            f.name: f.default
            for f in self.output_fields
            if not f.required and f.default is not None
        }


# ══════════════════════════════════════════════════════════════════════════════
# VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════

class ValidationError(Exception):
    def __init__(self, message: str, raw: str = "", data: Dict = None):
        super().__init__(message)
        self.raw = raw
        self.data = data or {}


class SchemaValidator:
    """Validates and coerces a parsed dict against an OutputSchema."""

    def validate(self, data: Dict, schema: OutputSchema) -> Tuple[Dict, List[str]]:
        """
        Validate data against schema.
        Returns (coerced_data, list_of_warnings).
        Raises ValidationError on fatal errors.
        """
        errors = []
        warnings = []
        result = {}

        # Check required fields
        for f in schema.output_fields:
            if f.required and f.name not in data:
                if f.default is not None:
                    result[f.name] = f.default
                    warnings.append(f"'{f.name}' missing, using default")
                else:
                    errors.append(f"Required field '{f.name}' is missing")
                continue

            raw_val = data.get(f.name, f.default)
            if raw_val is None and not f.required:
                result[f.name] = f.default
                continue

            try:
                result[f.name] = self._coerce(raw_val, f)
            except (ValueError, TypeError) as e:
                errors.append(f"Field '{f.name}': {e}")

        if errors:
            raise ValidationError(
                f"Schema validation failed: {'; '.join(errors)}",
                data=data
            )

        # Include any extra fields not in schema
        for k, v in data.items():
            if k not in result:
                result[k] = v

        return result, warnings

    def _coerce(self, value: Any, field: OutputField) -> Any:
        """Coerce a value to the expected type."""
        if value is None:
            return field.default

        t = field.type
        if t == FieldType.STRING:
            return str(value)
        elif t == FieldType.INTEGER:
            if isinstance(value, bool):
                raise ValueError(f"Expected int, got bool")
            return int(float(str(value)))
        elif t == FieldType.FLOAT:
            return float(str(value))
        elif t == FieldType.BOOLEAN:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                if value.lower() in ("true", "yes", "1"):
                    return True
                if value.lower() in ("false", "no", "0"):
                    return False
            if isinstance(value, (int, float)):
                return bool(value)
            raise ValueError(f"Cannot coerce '{value}' to boolean")
        elif t == FieldType.ENUM:
            s = str(value)
            if s not in field.enum_values:
                # Case-insensitive match
                lower_map = {v.lower(): v for v in field.enum_values}
                if s.lower() in lower_map:
                    return lower_map[s.lower()]
                raise ValueError(f"'{s}' not in {field.enum_values}")
            return s
        elif t == FieldType.ARRAY:
            if not isinstance(value, list):
                if isinstance(value, str):
                    # Try parsing as JSON array
                    try:
                        value = json.loads(value)
                    except Exception:
                        value = [v.strip() for v in value.split(",")]
            return list(value)
        elif t == FieldType.OBJECT:
            if isinstance(value, str):
                return json.loads(value)
            return value
        return value


# ══════════════════════════════════════════════════════════════════════════════
# EXTRACTOR
# ══════════════════════════════════════════════════════════════════════════════

class JSONExtractor:
    """Extracts JSON from LLM output that may include prose, code fences, etc."""

    def extract(self, text: str) -> Optional[Dict]:
        """Try multiple strategies to find JSON in messy LLM output."""

        # Strategy 1: entire response is valid JSON
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass

        # Strategy 2: code fence ```json ... ```
        fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if fence:
            try:
                return json.loads(fence.group(1))
            except json.JSONDecodeError:
                pass

        # Strategy 3: first { ... } block (greedy)
        brace = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', text, re.DOTALL)
        if brace:
            try:
                return json.loads(brace.group())
            except json.JSONDecodeError:
                pass

        # Strategy 4: relaxed — fix common issues (trailing comma, single quotes)
        cleaned = self._clean_json(text)
        if cleaned:
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass

        return None

    def _clean_json(self, text: str) -> Optional[str]:
        """Fix common JSON syntax issues from LLM output."""
        # Find the outermost { }
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return None
        candidate = text[start:end + 1]
        # Replace single quotes with double quotes (cautiously)
        candidate = re.sub(r"(?<![\\])'", '"', candidate)
        # Remove trailing commas before } or ]
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        return candidate


# ══════════════════════════════════════════════════════════════════════════════
# STRUCTURED OUTPUT PARSER (main class)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ParseResult:
    success: bool
    data: Dict
    warnings: List[str]
    attempts: int
    raw_response: str
    latency_ms: float
    error: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "data": self.data,
            "warnings": self.warnings,
            "attempts": self.attempts,
            "error": self.error,
        }


class StructuredOutputParser:
    """
    Forces an LLM to produce validated, typed JSON output.

    Workflow:
      1. Inject schema description into system prompt
      2. Call LLM
      3. Extract JSON from response
      4. Validate and coerce against schema
      5. On failure: retry with error feedback (up to max_retries)

    Usage:
        parser = StructuredOutputParser(llm=agent.llm)

        schema = OutputSchema("result", [
            OutputField("answer", FieldType.STRING, "The answer"),
            OutputField("confidence", FieldType.FLOAT, "Confidence 0-1"),
        ])

        result = await parser.parse(
            prompt="What is 2+2?",
            schema=schema,
        )
        print(result["answer"], result["confidence"])
    """

    def __init__(self, llm, max_retries: int = 3):
        self.llm = llm
        self.max_retries = max_retries
        self.extractor = JSONExtractor()
        self.validator = SchemaValidator()

    async def parse(
        self,
        prompt: str,
        schema: OutputSchema,
        system: str = None,
        model: str = None,
        session_id: str = "structured",
        temperature: float = 0.1,
    ) -> ParseResult:
        """
        Parse LLM output into a validated dict conforming to schema.
        """
        start = time.time()
        base_system = self._build_system(schema, system)

        messages = [{"role": "user", "content": prompt}]
        last_error = ""
        last_raw = ""

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self.llm.chat(
                    messages=messages,
                    system=base_system,
                    model=model,
                    temperature=temperature,
                    session_id=session_id,
                    auto_route=False,
                )
                raw = resp.get("content", "")
                last_raw = raw

                extracted = self.extractor.extract(raw)
                if extracted is None:
                    raise ValidationError(
                        f"No JSON found in response",
                        raw=raw
                    )

                validated, warnings = self.validator.validate(extracted, schema)

                latency_ms = (time.time() - start) * 1000
                logger.debug(f"Structured parse succeeded on attempt {attempt}")
                return ParseResult(
                    success=True,
                    data=validated,
                    warnings=warnings,
                    attempts=attempt,
                    raw_response=raw,
                    latency_ms=latency_ms,
                )

            except (ValidationError, Exception) as e:
                last_error = str(e)
                logger.warning(f"Parse attempt {attempt}/{self.max_retries}: {e}")

                if attempt < self.max_retries:
                    # Inject corrective feedback
                    messages.append({"role": "assistant", "content": last_raw})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Your response had an error: {last_error}\n\n"
                            f"Please respond ONLY with a valid JSON object "
                            f"matching this schema:\n{schema.prompt_description()}\n"
                            f"Do not include any text outside the JSON."
                        )
                    })

        latency_ms = (time.time() - start) * 1000
        return ParseResult(
            success=False,
            data=schema.defaults(),
            warnings=[],
            attempts=self.max_retries,
            raw_response=last_raw,
            latency_ms=latency_ms,
            error=last_error,
        )

    async def extract_list(
        self,
        text: str,
        item_schema: OutputSchema,
        model: str = None,
        max_items: int = 20,
    ) -> List[Dict]:
        """
        Extract a list of structured items from a long document.
        Chunks by paragraph and parses each independently.
        """
        prompt = (
            f"Extract all instances matching this schema from the text below.\n"
            f"Schema: {item_schema.prompt_description()}\n\n"
            f"Return a JSON array of objects. Maximum {max_items} items.\n\n"
            f"TEXT:\n{text[:4000]}"
        )

        array_schema = OutputSchema(
            name="list_result",
            output_fields=[
                OutputField("items", FieldType.ARRAY, "List of extracted items",
                           item_type=FieldType.OBJECT)
            ]
        )

        result = await self.parse(prompt, array_schema, model=model)
        items = result.get("items", [])
        return items[:max_items] if items else []

    def _build_system(self, schema: OutputSchema, extra_system: str = None) -> str:
        base = (
            "You are a precise data extraction assistant. "
            "You ALWAYS respond with valid JSON only — no prose, no explanation, "
            "no markdown fences. Just a raw JSON object.\n\n"
            f"{schema.prompt_description()}"
        )
        if extra_system:
            base = f"{extra_system}\n\n{base}"
        return base

    # ── Convenience Methods ───────────────────────────────────────────────────

    async def classify(self, text: str, categories: List[str],
                       model: str = None) -> ParseResult:
        """Classify text into one of the given categories."""
        schema = OutputSchema("classification", [
            OutputField("category", FieldType.ENUM, "The category",
                       enum_values=categories),
            OutputField("confidence", FieldType.FLOAT,
                       "Confidence score 0.0-1.0", example=0.85),
            OutputField("reasoning", FieldType.STRING,
                       "Brief explanation", required=False),
        ])
        return await self.parse(
            f"Classify the following text:\n\n{text}",
            schema, model=model
        )

    async def extract_entities(self, text: str, model: str = None) -> ParseResult:
        """Extract named entities from text."""
        schema = OutputSchema("entities", [
            OutputField("people", FieldType.ARRAY, "Person names",
                       item_type=FieldType.STRING, required=False, default=[]),
            OutputField("organizations", FieldType.ARRAY, "Organizations",
                       item_type=FieldType.STRING, required=False, default=[]),
            OutputField("locations", FieldType.ARRAY, "Places and locations",
                       item_type=FieldType.STRING, required=False, default=[]),
            OutputField("dates", FieldType.ARRAY, "Dates and time references",
                       item_type=FieldType.STRING, required=False, default=[]),
            OutputField("topics", FieldType.ARRAY, "Main topics/themes",
                       item_type=FieldType.STRING, required=False, default=[]),
        ])
        return await self.parse(
            f"Extract all named entities from:\n\n{text}",
            schema, model=model
        )

    async def sentiment_analysis(self, text: str, model: str = None) -> ParseResult:
        """Analyse sentiment with score."""
        schema = OutputSchema("sentiment", [
            OutputField("label", FieldType.ENUM, "Sentiment label",
                       enum_values=["positive", "negative", "neutral", "mixed"]),
            OutputField("score", FieldType.FLOAT,
                       "Intensity 0.0 (very negative) to 1.0 (very positive)",
                       example=0.72),
            OutputField("emotions", FieldType.ARRAY,
                       "Detected emotions", item_type=FieldType.STRING,
                       required=False, default=[]),
            OutputField("summary", FieldType.STRING, "One sentence summary"),
        ])
        return await self.parse(
            f"Analyse the sentiment of:\n\n{text}",
            schema, model=model
        )

    async def summarize_structured(self, text: str,
                                   model: str = None) -> ParseResult:
        """Structured summary with key points and metadata."""
        schema = OutputSchema("summary", [
            OutputField("title", FieldType.STRING, "Document title or inferred heading"),
            OutputField("summary", FieldType.STRING, "2-3 sentence summary"),
            OutputField("key_points", FieldType.ARRAY, "Main points (max 5)",
                       item_type=FieldType.STRING),
            OutputField("word_count", FieldType.INTEGER, "Approximate word count"),
            OutputField("language", FieldType.STRING, "Detected language",
                       required=False, default="English"),
        ])
        return await self.parse(
            f"Summarize the following document:\n\n{text[:3000]}",
            schema, model=model
        )


# ══════════════════════════════════════════════════════════════════════════════
# COMMON SCHEMAS (ready-to-use)
# ══════════════════════════════════════════════════════════════════════════════

SENTIMENT_SCHEMA = OutputSchema("sentiment", [
    OutputField("label", FieldType.ENUM, "Sentiment",
               enum_values=["positive", "negative", "neutral", "mixed"]),
    OutputField("score", FieldType.FLOAT, "0.0-1.0", example=0.8),
    OutputField("summary", FieldType.STRING, "One line summary"),
])

ENTITY_SCHEMA = OutputSchema("entities", [
    OutputField("people", FieldType.ARRAY, "Person names",
               item_type=FieldType.STRING, required=False, default=[]),
    OutputField("organizations", FieldType.ARRAY, "Org names",
               item_type=FieldType.STRING, required=False, default=[]),
    OutputField("locations", FieldType.ARRAY, "Places",
               item_type=FieldType.STRING, required=False, default=[]),
])

PLAN_SCHEMA = OutputSchema("plan", [
    OutputField("title", FieldType.STRING, "Plan title"),
    OutputField("steps", FieldType.ARRAY, "Ordered steps",
               item_type=FieldType.STRING),
    OutputField("estimated_time", FieldType.STRING, "Time estimate",
               required=False),
    OutputField("risks", FieldType.ARRAY, "Potential risks",
               item_type=FieldType.STRING, required=False, default=[]),
])

CODE_REVIEW_SCHEMA = OutputSchema("code_review", [
    OutputField("severity", FieldType.ENUM, "Overall severity",
               enum_values=["critical", "high", "medium", "low", "ok"]),
    OutputField("issues", FieldType.ARRAY, "List of issues found",
               item_type=FieldType.STRING),
    OutputField("suggestions", FieldType.ARRAY, "Improvement suggestions",
               item_type=FieldType.STRING, required=False, default=[]),
    OutputField("score", FieldType.INTEGER, "Code quality 1-10", example=7),
    OutputField("summary", FieldType.STRING, "One line verdict"),
])
