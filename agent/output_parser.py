"""OMNI AGENT - Output Parser
Structured LLM output extraction: parse JSON, XML, YAML-like, markdown
lists, key-value blocks, regex patterns, and auto-repair malformed output.

Features:
- JSON parser: extract valid JSON objects/arrays, fix common LLM errors
- JSON schema validation: validate parsed object against a simple schema
- XML/HTML extractor: extract tag content via regex, no external deps
- Markdown list parser: ordered and unordered lists → Python lists
- Key-value parser: "Key: Value" or "Key = Value" text blocks
- Regex extractor: user-supplied patterns with named groups
- Code block extractor: pull fenced code blocks with optional language filter
- Table parser: markdown tables → list of dicts
- Auto-repair: strip leading prose/backticks, fix trailing commas, add braces
- Retry/fix pipeline: attempt N parse strategies in order until one succeeds
- Typed coercion: cast values to int/float/bool after extraction
- Parse result: structured output with raw_text, parsed value, strategy used
- REST API: parse, validate, extract-code, extract-table
"""
import re, json, logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
logger = logging.getLogger(__name__)

# ── Repair helpers ─────────────────────────────────────────────────────────────
def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers."""
    m = re.search(r'```(?:\w+)?\s*\n?([\s\S]*?)\n?```', text)
    return m.group(1).strip() if m else text.strip()

def _fix_trailing_commas(text: str) -> str:
    return re.sub(r',\s*([}\]])', r'\1', text)

def _fix_unquoted_keys(text: str) -> str:
    return re.sub(r'(\{|\,)\s*(\w+)\s*:', r'\1 "\2":', text)

def _add_missing_braces(text: str) -> str:
    text = text.strip()
    if text and text[0] not in '{[':
        text = '{' + text + '}'
    return text

def _repair_json(text: str) -> str:
    text = _strip_markdown_fences(text)
    text = _fix_trailing_commas(text)
    text = _fix_unquoted_keys(text)
    # Extract first JSON object or array
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = text.find(start_char)
        if start == -1: continue
        depth = 0; end = start
        for i, ch in enumerate(text[start:], start):
            if ch == start_char: depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0: end = i; break
        if end > start:
            return text[start:end+1]
    return text

# ── Parse strategies ───────────────────────────────────────────────────────────
def _parse_json(text: str) -> Any:
    cleaned = _repair_json(text)
    return json.loads(cleaned)

def _parse_xml_tag(text: str, tag: str) -> List[str]:
    pattern = re.compile(rf'<{re.escape(tag)}[^>]*>([\s\S]*?)</{re.escape(tag)}>', re.I)
    return [m.group(1).strip() for m in pattern.finditer(text)]

def _parse_md_list(text: str) -> List[str]:
    items = []
    for line in text.splitlines():
        m = re.match(r'^\s*(?:[-*+]|\d+\.)\s+(.+)', line)
        if m: items.append(m.group(1).strip())
    return items

def _parse_kv(text: str, sep: str = r'[:=]') -> Dict[str, str]:
    result = {}
    for line in text.splitlines():
        m = re.match(rf'^\s*([A-Za-z_][\w\s]*)' + sep + r'\s*(.+)', line)
        if m:
            key = m.group(1).strip().lower().replace(' ', '_')
            result[key] = m.group(2).strip()
    return result

def _parse_code_blocks(text: str, lang: str = None) -> List[Dict[str, str]]:
    pattern = re.compile(r'```(\w*)\s*\n?([\s\S]*?)\n?```')
    blocks = []
    for m in pattern.finditer(text):
        block_lang = m.group(1).strip()
        code = m.group(2).strip()
        if lang is None or block_lang.lower() == lang.lower():
            blocks.append({"language": block_lang, "code": code})
    return blocks

def _parse_table(text: str) -> List[Dict[str, str]]:
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith('|')]
    if len(lines) < 2: return []
    headers = [h.strip() for h in lines[0].split('|') if h.strip()]
    rows = []
    for line in lines[2:]:  # skip separator line
        # Filter out empty strings from leading/trailing pipes
        cells = [c.strip() for c in line.split('|')]
        cells = [c for c in cells if c or True]  # keep all, including empty
        # Remove first and last if empty (from leading/trailing |)
        if cells and cells[0] == '': cells = cells[1:]
        if cells and cells[-1] == '': cells = cells[:-1]
        row = {}
        for i, h in enumerate(headers):
            row[h] = cells[i].strip() if i < len(cells) else ""
        rows.append(row)
    return rows

def _coerce(value: str, type_hint: str) -> Any:
    if type_hint == "int":
        try: return int(value)
        except: return value
    if type_hint == "float":
        try: return float(value)
        except: return value
    if type_hint == "bool":
        return value.lower() in ("true", "yes", "1", "on")
    return value

# ── Schema validation (simple) ─────────────────────────────────────────────────
def _validate_schema(data: Any, schema: Dict) -> List[str]:
    errors = []
    if not isinstance(data, dict): return [f"Expected dict, got {type(data).__name__}"]
    for key, spec in schema.items():
        required = spec.get("required", False)
        type_name = spec.get("type", "any")
        if key not in data:
            if required: errors.append(f"Missing required field: {key!r}")
            continue
        val = data[key]
        type_map = {"string":str,"str":str,"int":int,"integer":int,
                     "float":float,"number":(int,float),"bool":bool,"list":list,"dict":dict}
        expected = type_map.get(type_name)
        if expected and not isinstance(val, expected):
            errors.append(f"Field {key!r}: expected {type_name}, got {type(val).__name__}")
    return errors

# ── Result dataclass ───────────────────────────────────────────────────────────
@dataclass
class ParseResult:
    success: bool
    value: Any = None
    strategy: str = ""
    raw_text: str = ""
    error: str = ""
    schema_errors: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"success": self.success, "strategy": self.strategy,
                "value": self.value, "error": self.error,
                "schema_errors": self.schema_errors,
                "raw_text": self.raw_text[:200]}

class OutputParser:
    """
    Multi-strategy LLM output parser with auto-repair and schema validation.

    Usage:
        parser = OutputParser()

        # Parse JSON with auto-repair
        result = parser.parse_json('Sure! {"name": "Alice", "age": 30}')
        print(result.value)    # {"name": "Alice", "age": 30}

        # Extract code blocks
        blocks = parser.extract_code(llm_output, lang="python")
        for b in blocks:
            exec(b["code"])

        # Try multiple strategies in order
        result = parser.parse(llm_output,
                               strategies=["json","kv","list"],
                               schema={"name": {"type":"str","required":True}})
    """
    def __init__(self):
        self._custom_patterns: Dict[str, re.Pattern] = {}

    def add_pattern(self, name: str, pattern: str, flags: int = 0):
        self._custom_patterns[name] = re.compile(pattern, flags)

    def parse_json(self, text: str,
                   schema: Dict = None) -> ParseResult:
        try:
            value = _parse_json(text)
            errs = _validate_schema(value, schema) if schema and isinstance(value, dict) else []
            return ParseResult(success=True, value=value, strategy="json",
                                raw_text=text, schema_errors=errs)
        except Exception as e:
            return ParseResult(success=False, strategy="json",
                                raw_text=text, error=str(e))

    def parse_xml(self, text: str, tag: str) -> ParseResult:
        values = _parse_xml_tag(text, tag)
        if values:
            return ParseResult(success=True, value=values, strategy="xml", raw_text=text)
        return ParseResult(success=False, strategy="xml", raw_text=text,
                            error=f"Tag <{tag}> not found")

    def parse_list(self, text: str) -> ParseResult:
        items = _parse_md_list(text)
        if items:
            return ParseResult(success=True, value=items, strategy="list", raw_text=text)
        return ParseResult(success=False, strategy="list", raw_text=text,
                            error="No list items found")

    def parse_kv(self, text: str, sep: str = r'[:=]',
                  type_hints: Dict[str, str] = None) -> ParseResult:
        data = _parse_kv(text, sep)
        if data:
            if type_hints:
                data = {k: _coerce(v, type_hints.get(k, "str")) for k, v in data.items()}
            return ParseResult(success=True, value=data, strategy="kv", raw_text=text)
        return ParseResult(success=False, strategy="kv", raw_text=text,
                            error="No key-value pairs found")

    def parse_regex(self, text: str, pattern: str,
                     flags: int = 0) -> ParseResult:
        try:
            compiled = re.compile(pattern, flags)
            matches = [m.groupdict() if m.groupdict() else m.groups()
                       for m in compiled.finditer(text)]
            if matches:
                return ParseResult(success=True, value=matches,
                                    strategy="regex", raw_text=text)
            return ParseResult(success=False, strategy="regex", raw_text=text,
                                error="Pattern not found")
        except Exception as e:
            return ParseResult(success=False, strategy="regex",
                                raw_text=text, error=str(e))

    def extract_code(self, text: str, lang: str = None) -> List[Dict[str, str]]:
        return _parse_code_blocks(text, lang)

    def extract_table(self, text: str) -> List[Dict[str, str]]:
        return _parse_table(text)

    def parse(self, text: str,
               strategies: List[str] = None,
               schema: Dict = None,
               xml_tag: str = None,
               regex_pattern: str = None) -> ParseResult:
        """Try strategies in order; return first success."""
        strats = strategies or ["json", "kv", "list"]
        for strat in strats:
            if strat == "json":
                r = self.parse_json(text, schema)
            elif strat == "xml" and xml_tag:
                r = self.parse_xml(text, xml_tag)
            elif strat == "list":
                r = self.parse_list(text)
            elif strat == "kv":
                r = self.parse_kv(text)
            elif strat == "regex" and regex_pattern:
                r = self.parse_regex(text, regex_pattern)
            else:
                continue
            if r.success:
                return r
        return ParseResult(success=False, strategy="none", raw_text=text,
                            error=f"All strategies failed: {strats}")

    def parse_bool(self, text: str) -> bool:
        return text.strip().lower() in ("yes","true","1","on","correct","affirmative")

    def parse_number(self, text: str) -> Optional[float]:
        m = re.search(r'-?\d+\.?\d*', text)
        return float(m.group()) if m else None

    def extract_emails(self, text: str) -> List[str]:
        return re.findall(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', text)

    def extract_urls(self, text: str) -> List[str]:
        return re.findall(r'https?://[^\s<>"\']+', text)

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def parse_ep(req):
            d = await req.json()
            r = self.parse(d["text"], d.get("strategies"), d.get("schema"),
                            d.get("xml_tag"), d.get("regex_pattern"))
            return web.json_response(r.to_dict())
        async def json_ep(req):
            d = await req.json()
            r = self.parse_json(d["text"], d.get("schema"))
            return web.json_response(r.to_dict())
        async def code_ep(req):
            d = await req.json()
            blocks = self.extract_code(d["text"], d.get("lang"))
            return web.json_response({"blocks": blocks})
        async def table_ep(req):
            d = await req.json()
            rows = self.extract_table(d["text"])
            return web.json_response({"rows": rows})
        p = f"{prefix}/parse"
        app.router.add_post(f"{p}/parse",  parse_ep)
        app.router.add_post(f"{p}/json",   json_ep)
        app.router.add_post(f"{p}/code",   code_ep)
        app.router.add_post(f"{p}/table",  table_ep)
        logger.info(f"Output parser API at {prefix}/parse/")
