"""OMNI AGENT - Output Formatter
Structure, validate, and render LLM outputs: JSON schema enforcement,
Markdown rendering, table/code/list builders, and multi-format export.

Features:
- JSON schema validation: enforce structure with type checks and required fields
- Auto-repair: attempt to fix truncated or malformed JSON
- Markdown rendering: headings, bold/italic, lists, code blocks, tables
- Table builder: dict-list → aligned ASCII or Markdown table
- Code block: syntax-highlighted fenced blocks with language tag
- List builder: ordered and unordered lists with nesting
- Template rendering: mustache-style {{variable}} substitution
- Multi-format export: plain text, HTML, Markdown, JSON, CSV
- Output truncation: smart truncation at sentence boundary
- Schema registry: register named schemas for reuse
- REST API: format, validate, render, convert
"""
import json, re, csv, io, logging
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass, field
logger = logging.getLogger(__name__)

# ── JSON helpers ──────────────────────────────────────────────────────────────

def _validate_schema(data: Any, schema: Dict) -> List[str]:
    """Minimal JSON-schema-like validation; returns list of error messages."""
    errors = []
    stype = schema.get("type")
    if stype:
        type_map = {"string": str, "number": (int, float), "integer": int,
                     "boolean": bool, "array": list, "object": dict, "null": type(None)}
        expected = type_map.get(stype)
        if expected and not isinstance(data, expected):
            errors.append(f"Expected type {stype!r}, got {type(data).__name__!r}")
    if isinstance(data, dict):
        for req in schema.get("required", []):
            if req not in data:
                errors.append(f"Missing required field: {req!r}")
        for key, sub_schema in schema.get("properties", {}).items():
            if key in data:
                errors.extend(_validate_schema(data[key], sub_schema))
    if isinstance(data, list):
        items_schema = schema.get("items", {})
        for i, item in enumerate(data):
            sub_errors = _validate_schema(item, items_schema)
            errors.extend(f"[{i}] {e}" for e in sub_errors)
    min_len = schema.get("minLength") or schema.get("minItems")
    if min_len and hasattr(data, "__len__") and len(data) < min_len:
        errors.append(f"Length {len(data)} < minLength {min_len}")
    max_len = schema.get("maxLength") or schema.get("maxItems")
    if max_len and hasattr(data, "__len__") and len(data) > max_len:
        errors.append(f"Length {len(data)} > maxLength {max_len}")
    enum = schema.get("enum")
    if enum and data not in enum:
        errors.append(f"Value {data!r} not in enum {enum}")
    return errors

def _repair_json(text: str) -> Optional[str]:
    """Try to fix common JSON issues: trailing commas, missing brackets."""
    t = text.strip()
    # Extract JSON from markdown code block if present
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', t)
    if m: t = m.group(1).strip()
    # Find JSON object or array
    m = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', t)
    if not m: return None
    t = m.group(1)
    # Remove trailing commas before closing brackets
    t = re.sub(r',\s*([}\]])', r'\1', t)
    try:
        json.loads(t); return t
    except:
        # Try adding missing closing braces
        opens = t.count('{') - t.count('}')
        closes = t.count('[') - t.count(']')
        t += '}' * max(0, opens) + ']' * max(0, closes)
        try: json.loads(t); return t
        except: return None

# ── Table builder ─────────────────────────────────────────────────────────────

def _build_table(rows: List[Dict], fmt: str = "markdown") -> str:
    if not rows: return ""
    headers = list(rows[0].keys())
    col_widths = {h: max(len(h), max(len(str(r.get(h,""))) for r in rows)) for h in headers}
    def row_line(r): return "| " + " | ".join(str(r.get(h,"")).ljust(col_widths[h]) for h in headers) + " |"
    sep = "| " + " | ".join("-" * col_widths[h] for h in headers) + " |"
    header_line = "| " + " | ".join(h.ljust(col_widths[h]) for h in headers) + " |"
    if fmt == "markdown":
        return "\n".join([header_line, sep] + [row_line(r) for r in rows])
    else:  # ascii
        border = "+" + "+".join("-" * (col_widths[h]+2) for h in headers) + "+"
        return "\n".join([border, header_line, border] + [row_line(r) for r in rows] + [border])

def _build_list(items: List, ordered: bool = False, indent: int = 0) -> str:
    lines = []
    for i, item in enumerate(items):
        prefix = "  " * indent + (f"{i+1}. " if ordered else "- ")
        if isinstance(item, list):
            lines.append(prefix + (item[0] if item else ""))
            if len(item) > 1 and isinstance(item[1], list):
                lines.append(_build_list(item[1], ordered, indent+1))
        else:
            lines.append(prefix + str(item))
    return "\n".join(lines)

def _render_template(template: str, variables: Dict) -> str:
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", str(value))
        result = result.replace("{{ " + key + " }}", str(value))
    return result

def _smart_truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars: return text
    # Try sentence boundary
    truncated = text[:max_chars]
    last_sent = max(truncated.rfind(". "), truncated.rfind("! "), truncated.rfind("? "))
    if last_sent > max_chars * 0.7:
        return truncated[:last_sent+1] + "…"
    # Try word boundary
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.8:
        return truncated[:last_space] + "…"
    return truncated + "…"

@dataclass
class FormatResult:
    original: Any; formatted: str; format_used: str
    valid: bool = True; errors: List[str] = field(default_factory=list)
    repaired: bool = False
    def to_dict(self):
        return {"formatted": self.formatted[:2000], "format_used": self.format_used,
                "valid": self.valid, "errors": self.errors, "repaired": self.repaired}

class OutputFormatter:
    """
    Structure, validate, and render LLM outputs in multiple formats.

    Usage:
        fmt = OutputFormatter()
        fmt.register_schema("user", {"type":"object","required":["name","email"],
                                       "properties":{"name":{"type":"string"},"email":{"type":"string"}}})

        result = fmt.format_json('{"name":"Alice","email":"alice@x.com"}', schema="user")
        table  = fmt.table([{"name":"Alice","score":95},{"name":"Bob","score":87}])
        code   = fmt.code_block("print('hello')", language="python")
        md     = fmt.markdown_section("Results", table)
    """
    def __init__(self):
        self._schemas: Dict[str, Dict] = {}

    def register_schema(self, name: str, schema: Dict):
        self._schemas[name] = schema

    # ── JSON formatting ───────────────────────────────────────────────────────

    def format_json(self, text: Union[str, Any], schema: str = None,
                     indent: int = 2, repair: bool = True) -> FormatResult:
        errors = []; repaired = False
        if isinstance(text, (dict, list)):
            data = text
        else:
            try:
                data = json.loads(str(text))
            except json.JSONDecodeError:
                if repair:
                    fixed = _repair_json(str(text))
                    if fixed:
                        data = json.loads(fixed); repaired = True
                    else:
                        return FormatResult(original=text, formatted=str(text),
                                             format_used="json", valid=False,
                                             errors=["Invalid JSON; repair failed"])
                else:
                    return FormatResult(original=text, formatted=str(text),
                                         format_used="json", valid=False,
                                         errors=["Invalid JSON"])
        if schema and schema in self._schemas:
            errors = _validate_schema(data, self._schemas[schema])
        formatted = json.dumps(data, indent=indent, ensure_ascii=False)
        return FormatResult(original=text, formatted=formatted, format_used="json",
                             valid=len(errors) == 0, errors=errors, repaired=repaired)

    # ── Markdown helpers ──────────────────────────────────────────────────────

    def markdown_section(self, title: str, content: str, level: int = 2) -> str:
        return f"{'#' * level} {title}\n\n{content}"

    def code_block(self, code: str, language: str = "") -> str:
        return f"```{language}\n{code}\n```"

    def bold(self, text: str) -> str: return f"**{text}**"
    def italic(self, text: str) -> str: return f"*{text}*"
    def inline_code(self, text: str) -> str: return f"`{text}`"

    def table(self, rows: List[Dict], fmt: str = "markdown") -> str:
        return _build_table(rows, fmt)

    def list_block(self, items: List, ordered: bool = False) -> str:
        return _build_list(items, ordered)

    # ── Template rendering ────────────────────────────────────────────────────

    def render_template(self, template: str, variables: Dict) -> str:
        return _render_template(template, variables)

    # ── Multi-format conversion ───────────────────────────────────────────────

    def to_html(self, markdown_text: str) -> str:
        """Convert basic Markdown to HTML."""
        h = markdown_text
        h = re.sub(r'^#{3} (.+)$', r'<h3>\1</h3>', h, flags=re.M)
        h = re.sub(r'^#{2} (.+)$', r'<h2>\1</h2>', h, flags=re.M)
        h = re.sub(r'^#{1} (.+)$', r'<h1>\1</h1>', h, flags=re.M)
        h = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', h)
        h = re.sub(r'\*(.+?)\*', r'<em>\1</em>', h)
        h = re.sub(r'`(.+?)`', r'<code>\1</code>', h)
        h = re.sub(r'^- (.+)$', r'<li>\1</li>', h, flags=re.M)
        h = re.sub(r'(<li>.*</li>)', r'<ul>\1</ul>', h, flags=re.S)
        h = re.sub(r'\n\n', r'</p><p>', h)
        return f"<p>{h}</p>"

    def to_csv(self, rows: List[Dict]) -> str:
        if not rows: return ""
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader(); writer.writerows(rows)
        return buf.getvalue()

    def truncate(self, text: str, max_chars: int) -> str:
        return _smart_truncate(text, max_chars)

    def strip_markdown(self, text: str) -> str:
        t = re.sub(r'#{1,6}\s', '', text)
        t = re.sub(r'\*+(.+?)\*+', r'\1', t)
        t = re.sub(r'`+(.+?)`+', r'\1', t)
        t = re.sub(r'!\[.*?\]\(.*?\)', '', t)
        t = re.sub(r'\[(.+?)\]\(.*?\)', r'\1', t)
        return t.strip()

    # ── Schema validation standalone ─────────────────────────────────────────

    def validate(self, data: Any, schema: Union[str, Dict]) -> List[str]:
        if isinstance(schema, str):
            schema = self._schemas.get(schema, {})
        return _validate_schema(data, schema)

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def format_ep(req):
            d = await req.json()
            result = self.format_json(d.get("text",""), d.get("schema"), repair=bool(d.get("repair",True)))
            return web.json_response(result.to_dict())
        async def table_ep(req):
            d = await req.json()
            t = self.table(d.get("rows",[]), d.get("format","markdown"))
            return web.json_response({"table": t})
        async def template_ep(req):
            d = await req.json()
            out = self.render_template(d.get("template",""), d.get("variables",{}))
            return web.json_response({"rendered": out})
        async def validate_ep(req):
            d = await req.json()
            errors = self.validate(d.get("data"), d.get("schema",{}))
            return web.json_response({"valid": len(errors)==0, "errors": errors})
        p = f"{prefix}/format"
        app.router.add_post(f"{p}/json", format_ep)
        app.router.add_post(f"{p}/table", table_ep)
        app.router.add_post(f"{p}/template", template_ep)
        app.router.add_post(f"{p}/validate", validate_ep)
        logger.info(f"Output formatter API at {prefix}/format/")
