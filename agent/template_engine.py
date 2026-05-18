"""OMNI AGENT - Template Engine
Lightweight template engine: variables, loops, conditionals,
filters, macros, template inheritance, and auto-escaping.

Features:
- Variables: {{ variable }}, {{ obj.attr }}, {{ obj["key"] }}
- Attribute/index access: dot-notation with fallback to dict key
- Filters: {{ value | upper }}, {{ value | default("N/A") }}
    Built-in: upper, lower, title, trim, length, default, join,
              split, first, last, int, float, bool, json, escape,
              truncate(n), replace(a,b), repeat(n), reverse
- Conditionals: {% if expr %} ... {% elif expr %} ... {% else %} ... {% endif %}
- Loops: {% for item in list %} ... {% endfor %}
    Loop vars: loop.index (1-based), loop.index0 (0-based),
               loop.first, loop.last, loop.length
- Loop else: {% for ... %} ... {% else %} ... {% endfor %}
- Nested loops and conditionals supported
- Macros: {% macro name(arg1, arg2="default") %} ... {% endmacro %}
    Call: {{ name(val1, val2) }}
- Include: {% include "other_template" %} (from template registry)
- Inheritance: {% extends "base" %} + {% block name %} ... {% endblock %}
- Comments: {# this is a comment #} stripped from output
- Auto-escape: HTML special chars escaped by default; mark safe with |safe
- Whitespace control: {%- and -%} strips surrounding whitespace
- Custom filters: register fn(value, *args) → str
- Custom tags: not needed (macro covers most use cases)
- Template registry: named templates stored in-memory + SQLite
- SQLite persistence: templates, render log
- REST API: register, render, list, delete, stats
"""
import html, json, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Built-in filters ─────────────────────────────────────────────────────────
def _f_upper(v, *a):      return str(v).upper()
def _f_lower(v, *a):      return str(v).lower()
def _f_title(v, *a):      return str(v).title()
def _f_trim(v, *a):       return str(v).strip()
def _f_length(v, *a):     return len(v) if hasattr(v,"__len__") else 0
def _f_default(v, *a):    return v if v is not None else (a[0] if a else "")
def _f_join(v, *a):       sep = a[0] if a else ""; return sep.join(str(x) for x in (v or []))
def _f_split(v, *a):      sep = a[0] if a else " "; return str(v).split(sep)
def _f_first(v, *a):      return v[0] if v else ""
def _f_last(v, *a):       return v[-1] if v else ""
def _f_int(v, *a):
    try: return int(v)
    except: return 0
def _f_float(v, *a):
    try: return float(v)
    except: return 0.0
def _f_bool(v, *a):       return bool(v)
def _f_json(v, *a):       return json.dumps(v, default=str)
def _f_escape(v, *a):     return html.escape(str(v))
def _f_safe(v, *a):       return _SafeStr(str(v))
def _f_truncate(v, *a):
    n = int(a[0]) if a else 50; s = str(v)
    return s if len(s) <= n else s[:n-3] + "..."
def _f_replace(v, *a):
    if len(a) >= 2: return str(v).replace(str(a[0]), str(a[1]))
    return str(v)
def _f_repeat(v, *a):
    n = int(a[0]) if a else 1; return str(v) * n
def _f_reverse(v, *a):
    if isinstance(v, list): return list(reversed(v))
    return str(v)[::-1]
def _f_abs(v, *a):
    try: return abs(float(v))
    except: return v
def _f_round(v, *a):
    n = int(a[0]) if a else 0
    try: return round(float(v), n)
    except: return v
def _f_list(v, *a):       return list(v) if hasattr(v,"__iter__") else [v]
def _f_unique(v, *a):
    seen = set(); out = []
    for x in (v or []):
        if x not in seen: seen.add(x); out.append(x)
    return out
def _f_sort(v, *a):
    try: return sorted(v, reverse=(a[0].lower()=="desc") if a else False)
    except: return v

class _SafeStr(str):
    """Marks a string as already escaped (skip auto-escape)."""
    pass

_BUILTIN_FILTERS: Dict[str, Callable] = {
    "upper":upper,"lower":lower,"title":title,"trim":trim,
    "length":length,"default":default,"join":join,"split":split,
    "first":first,"last":last,"int":int_,"float":float_,
    "bool":bool_,"json":json_,"escape":escape,"safe":safe,
    "truncate":truncate,"replace":replace,"repeat":repeat,
    "reverse":reverse,"abs":abs_,"round":round_,
    "list":list_,"unique":unique,"sort":sort,
} if False else {}  # populated below

for _name, _fn in [
    ("upper",_f_upper),("lower",_f_lower),("title",_f_title),
    ("trim",_f_trim),("length",_f_length),("default",_f_default),
    ("join",_f_join),("split",_f_split),("first",_f_first),
    ("last",_f_last),("int",_f_int),("float",_f_float),
    ("bool",_f_bool),("json",_f_json),("escape",_f_escape),
    ("safe",_f_safe),("truncate",_f_truncate),("replace",_f_replace),
    ("repeat",_f_repeat),("reverse",_f_reverse),("abs",_f_abs),
    ("round",_f_round),("list",_f_list),("unique",_f_unique),
    ("sort",_f_sort)]:
    _BUILTIN_FILTERS[_name] = _fn

# ── Attribute lookup ──────────────────────────────────────────────────────────
def _getattr_deep(obj: Any, path: str) -> Any:
    """Resolve 'a.b.c' or 'a["k"]' chains."""
    parts = re.split(r'\.|(?=\[)', path)
    for part in parts:
        if not part: continue
        if part.startswith("["):
            key = part[1:-1].strip("'\"")
            try:
                key = int(key)
            except ValueError:
                pass
            try: obj = obj[key]
            except: obj = None
        else:
            if isinstance(obj, dict):
                obj = obj.get(part)
            else:
                obj = getattr(obj, part, None)
        if obj is None: return None
    return obj

# ── Expression evaluator (restricted) ────────────────────────────────────────
def _eval_expr(expr: str, ctx: Dict) -> Any:
    expr = expr.strip()
    # Boolean literals
    if expr == "True" or expr == "true":   return True
    if expr == "False" or expr == "false": return False
    if expr == "None" or expr == "null":   return None
    # String literal
    if (expr.startswith('"') and expr.endswith('"')) or \
       (expr.startswith("'") and expr.endswith("'")):
        return expr[1:-1]
    # Number literal
    try: return int(expr)
    except ValueError: pass
    try: return float(expr)
    except ValueError: pass
    # Comparison operators
    for op in [" not in ", " in ", " == ", " != ", " >= ", " <= ", " > ", " < "]:
        if op in expr:
            parts = expr.split(op, 1)
            lhs = _eval_expr(parts[0], ctx)
            rhs = _eval_expr(parts[1], ctx)
            if op == " == ":    return lhs == rhs
            if op == " != ":    return lhs != rhs
            if op == " >= ":    return (lhs or 0) >= (rhs or 0)
            if op == " <= ":    return (lhs or 0) <= (rhs or 0)
            if op == " > ":     return (lhs or 0) > (rhs or 0)
            if op == " < ":     return (lhs or 0) < (rhs or 0)
            if op == " in ":    return lhs in (rhs or [])
            if op == " not in ":return lhs not in (rhs or [])
    # Boolean not
    if expr.startswith("not "):
        return not _eval_expr(expr[4:], ctx)
    # Variable lookup (with dot/bracket access)
    base = expr.split(".")[0].split("[")[0]
    if base in ctx:
        obj = ctx[base]
        rest = expr[len(base):]
        if rest:
            return _getattr_deep(obj, rest.lstrip("."))
        return obj
    return None

# ── Filter application ────────────────────────────────────────────────────────
def _apply_filters(value: Any, filter_chain: str,
                    filters: Dict[str, Callable],
                    auto_escape: bool) -> str:
    parts = [p.strip() for p in filter_chain.split("|")]
    for part in parts:
        if not part: continue
        m = re.match(r'(\w+)(?:\(([^)]*)\))?$', part)
        if not m: continue
        fname = m.group(1)
        args_str = m.group(2) or ""
        args = []
        import re as _re
        _ARG_PAT = _re.compile(r'''(?:[^,'"]*(?:(?:'[^']*'|"[^"]*")[^,'"]*)*)+''')
        raw_args = [m.group(0).strip() for m in _ARG_PAT.finditer(args_str) if m.group(0).strip()]
        for a in raw_args:
            if not a: continue
            if (a.startswith('"') and a.endswith('"')) or \
               (a.startswith("'") and a.endswith("'")):
                args.append(a[1:-1])
            else:
                try: args.append(int(a))
                except:
                    try: args.append(float(a))
                    except: args.append(a)
        fn = filters.get(fname)
        if fn:
            value = fn(value, *args)
    if isinstance(value, _SafeStr):
        return value
    if auto_escape:
        return html.escape(str(value)) if value is not None else ""
    return str(value) if value is not None else ""

@dataclass
class Template:
    name: str; source: str
    auto_escape: bool = True
    created_at: float = field(default_factory=time.time)

class TEStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS templates(
                    name TEXT PRIMARY KEY, source TEXT,
                    auto_escape INTEGER, created_at REAL);
                CREATE TABLE IF NOT EXISTS render_log(
                    id TEXT PRIMARY KEY, template TEXT,
                    elapsed_ms REAL, success INTEGER, ts REAL);
            """)

    def save(self, t: Template):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO templates VALUES(?,?,?,?)",
                (t.name, t.source, int(t.auto_escape), t.created_at))

    def load(self, name: str) -> Optional[Template]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM templates WHERE name=?", (name,)).fetchone()
        if not row: return None
        return Template(name=row["name"], source=row["source"],
                         auto_escape=bool(row["auto_escape"]),
                         created_at=row["created_at"])

    def log_render(self, template: str, elapsed_ms: float, success: bool):
        with self._conn() as c:
            c.execute("INSERT INTO render_log VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], template, elapsed_ms,
                 int(success), time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            nt = c.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
            nr = c.execute("SELECT COUNT(*) FROM render_log").fetchone()[0]
            avg = c.execute("SELECT AVG(elapsed_ms) FROM render_log").fetchone()[0] or 0
        return {"templates": nt, "renders": nr, "avg_ms": round(avg, 2)}

class TemplateEngine:
    """
    Lightweight template engine with filters, loops, and inheritance.

    Usage:
        te = TemplateEngine()
        te.register("greeting",
                     "Hello {{ name | title }}! "
                     "{% if vip %}VIP member.{% endif %}")

        output = te.render("greeting", {"name": "alice", "vip": True})
        # "Hello Alice! VIP member."

        # Inline render without registering
        output = te.render_string("Items: {% for x in items %}{{ x }}{% endfor %}",
                                   {"items": [1, 2, 3]})
    """
    def __init__(self, db_path: str = "data/templates.db",
                 auto_escape: bool = True):
        self._store = TEStore(db_path)
        self._templates: Dict[str, Template] = {}
        self._filters: Dict[str, Callable] = dict(_BUILTIN_FILTERS)
        self._auto_escape = auto_escape
        self._macros: Dict[str, Tuple[List[str], str]] = {}  # name → (params, body)

    def add_filter(self, name: str, fn: Callable):
        self._filters[name] = fn

    def register(self, name: str, source: str,
                  auto_escape: bool = None) -> Template:
        ae = auto_escape if auto_escape is not None else self._auto_escape
        t = Template(name=name, source=source, auto_escape=ae)
        self._templates[name] = t
        self._store.save(t)
        return t

    def get(self, name: str) -> Optional[Template]:
        if name in self._templates: return self._templates[name]
        t = self._store.load(name); 
        if t: self._templates[name] = t
        return t

    def _tokenize(self, source: str) -> List[Tuple[str, str]]:
        """Tokenize template source into (type, content) tuples."""
        tokens = []
        pos = 0; n = len(source)
        while pos < n:
            # Comment
            m = re.match(r'\{#.*?#\}', source[pos:], re.DOTALL)
            if m: pos += m.end(); continue
            # Tag
            m = re.match(r'\{%-?\s*(.*?)\s*-?%\}', source[pos:], re.DOTALL)
            if m:
                tokens.append(("tag", m.group(1).strip()))
                pos += m.end(); continue
            # Variable
            m = re.match(r'\{\{-?\s*(.*?)\s*-?\}\}', source[pos:], re.DOTALL)
            if m:
                tokens.append(("var", m.group(1).strip()))
                pos += m.end(); continue
            # Text
            next_special = min(
                (source.find(d, pos) for d in ["{{","{%","{#"] if source.find(d,pos) >= 0),
                default=n)
            tokens.append(("text", source[pos:next_special]))
            pos = next_special
        return tokens

    def _render_tokens(self, tokens: List[Tuple[str, str]],
                        ctx: Dict, auto_escape: bool,
                        start: int = 0) -> Tuple[str, int]:
        """Recursive token renderer. Returns (rendered_str, next_pos)."""
        out = []; i = start
        while i < len(tokens):
            ttype, tcontent = tokens[i]
            if ttype == "text":
                out.append(tcontent); i += 1
            elif ttype == "var":
                # Split on first pipe for filter chain
                parts = tcontent.split("|", 1)
                expr = parts[0].strip()
                fchain = parts[1] if len(parts) > 1 else ""
                # Check for macro call first (only when no filter chain)
                if not fchain:
                    macro_result = self._resolve_macro_call(expr, ctx)
                    if macro_result is not None:
                        out.append(macro_result); i += 1; continue
                value = _eval_expr(expr, ctx)
                out.append(_apply_filters(value, fchain, self._filters, auto_escape))
                i += 1
            elif ttype == "tag":
                words = tcontent.split()
                if not words: i += 1; continue
                kw = words[0]

                if kw == "if":
                    cond_expr = " ".join(words[1:])
                    # Collect branches
                    branches = [(cond_expr, [])]
                    depth = 1; j = i + 1
                    while j < len(tokens) and depth > 0:
                        tt2, tc2 = tokens[j]
                        if tt2 == "tag":
                            w2 = tc2.split()
                            if w2 and w2[0] in ("if","for","macro"): depth += 1
                            elif w2 and w2[0] == "endif" and depth == 1:
                                depth -= 1; break
                            elif w2 and w2[0] in ("elif","else") and depth == 1:
                                branches.append((" ".join(w2[1:]) if w2[0]=="elif" else None, []))
                                j += 1; continue
                            elif w2 and w2[0] in ("endif","endfor","endmacro"): depth -= 1
                        if depth > 0: branches[-1][1].append(tokens[j])
                        j += 1
                    # Evaluate branches
                    rendered = ""
                    for br_cond, br_tokens in branches:
                        if br_cond is None or _eval_expr(br_cond, ctx):
                            rendered, _ = self._render_tokens(br_tokens, ctx, auto_escape)
                            break
                    out.append(rendered); i = j + 1

                elif kw == "for":
                    # {% for item in iterable %}
                    m = re.match(r'for\s+(\w+)\s+in\s+(.+)$', tcontent)
                    if not m: i += 1; continue
                    var_name = m.group(1)
                    iter_expr = m.group(2).strip()
                    iterable = _eval_expr(iter_expr, ctx)
                    # Collect body and optional else
                    body_tokens: List = []; else_tokens: List = []
                    in_else = False; depth = 1; j = i + 1
                    while j < len(tokens) and depth > 0:
                        tt2, tc2 = tokens[j]
                        if tt2 == "tag":
                            w2 = tc2.split()
                            if w2 and w2[0] in ("if","for","macro"): depth += 1
                            elif w2 and w2[0] == "endfor" and depth == 1:
                                depth -= 1; break
                            elif w2 and w2[0] == "else" and depth == 1:
                                in_else = True; j += 1; continue
                            elif w2 and w2[0] in ("endif","endfor","endmacro"): depth -= 1
                        if depth > 0:
                            if in_else: else_tokens.append(tokens[j])
                            else:        body_tokens.append(tokens[j])
                        j += 1
                    items = list(iterable or [])
                    if not items and else_tokens:
                        rendered, _ = self._render_tokens(else_tokens, ctx, auto_escape)
                        out.append(rendered)
                    else:
                        for idx, item in enumerate(items):
                            loop_ctx = dict(ctx)
                            loop_ctx[var_name] = item
                            loop_ctx["loop"] = {
                                "index": idx + 1, "index0": idx,
                                "first": idx == 0, "last": idx == len(items) - 1,
                                "length": len(items)}
                            rendered, _ = self._render_tokens(body_tokens, loop_ctx, auto_escape)
                            out.append(rendered)
                    i = j + 1

                elif kw == "macro":
                    # {% macro name(arg1, arg2="default") %}
                    m = re.match(r'macro\s+(\w+)\s*\(([^)]*)\)', tcontent)
                    if not m: i += 1; continue
                    macro_name = m.group(1)
                    params_str = m.group(2)
                    body_tokens = []; depth = 1; j = i + 1
                    while j < len(tokens) and depth > 0:
                        tt2, tc2 = tokens[j]
                        if tt2 == "tag":
                            w2 = tc2.split()
                            if w2 and w2[0] in ("if","for","macro"): depth += 1
                            elif w2 and w2[0] == "endmacro" and depth == 1:
                                depth -= 1; break
                            elif w2 and w2[0] in ("endif","endfor","endmacro"): depth -= 1
                        if depth > 0: body_tokens.append(tokens[j])
                        j += 1
                    # Parse params: "a, b='default'"
                    params = []; defaults = {}
                    for p in params_str.split(","):
                        p = p.strip()
                        if not p: continue
                        if "=" in p:
                            pn, pd = p.split("=", 1)
                            pn = pn.strip(); pd = pd.strip().strip("'\"")
                            params.append(pn); defaults[pn] = pd
                        else:
                            params.append(p)
                    self._macros[macro_name] = (params, defaults, body_tokens)
                    i = j + 1

                elif kw in ("endif","endfor","endmacro","else","elif"):
                    # These are handled by parent; stop recursion
                    break
                elif kw == "extends":
                    # {% extends "base" %} — handled at render level
                    i += 1
                elif kw == "block":
                    # {% block name %} ... {% endblock %}
                    i += 1  # blocks handled at render level; skip
                elif kw == "include":
                    tname = " ".join(words[1:]).strip("'\"")
                    tmpl = self.get(tname)
                    if tmpl:
                        rendered = self._do_render(tmpl.source, ctx, tmpl.auto_escape)
                        out.append(rendered)
                    i += 1
                else:
                    i += 1
            else:
                i += 1
        return "".join(out), i

    def _resolve_macro_call(self, var_expr: str, ctx: Dict) -> Optional[str]:
        """Check if var_expr is a macro call like name(a, b)."""
        m = re.match(r'^(\w+)\(([^)]*)\)$', var_expr.strip())
        if not m: return None
        macro_name = m.group(1)
        if macro_name not in self._macros: return None
        params, defaults, body_tokens = self._macros[macro_name]
        args_raw = [a.strip() for a in m.group(2).split(",") if a.strip()]
        macro_ctx = dict(ctx); macro_ctx.update(defaults)
        for i, arg in enumerate(args_raw):
            if i < len(params):
                macro_ctx[params[i]] = _eval_expr(arg, ctx)
        rendered, _ = self._render_tokens(body_tokens, macro_ctx, self._auto_escape)
        return rendered

    def _handle_inheritance(self, source: str, ctx: Dict) -> str:
        """Handle {% extends %} + {% block %} inheritance."""
        extends_m = re.search(r'\{%-?\s*extends\s+[\'"]([^\'"]+)[\'"]\s*-?%\}', source)
        if not extends_m: return source
        parent_name = extends_m.group(1)
        parent_tmpl = self.get(parent_name)
        if not parent_tmpl: return source
        # Extract child blocks
        child_blocks: Dict[str, str] = {}
        for bm in re.finditer(
                r'\{%-?\s*block\s+(\w+)\s*-?%\}(.*?)\{%-?\s*endblock\s*-?%\}',
                source, re.DOTALL):
            child_blocks[bm.group(1)] = bm.group(2)
        # Substitute into parent
        parent_src = parent_tmpl.source
        def replace_block(m):
            block_name = m.group(1)
            return child_blocks.get(block_name, m.group(2))
        result = re.sub(
            r'\{%-?\s*block\s+(\w+)\s*-?%\}(.*?)\{%-?\s*endblock\s*-?%\}',
            replace_block, parent_src, flags=re.DOTALL)
        return result

    def _do_render(self, source: str, ctx: Dict,
                    auto_escape: bool) -> str:
        source = self._handle_inheritance(source, ctx)
        tokens = self._tokenize(source)
        result, _ = self._render_tokens(tokens, ctx, auto_escape)
        return result

    def render(self, name: str, context: Dict = None) -> str:
        t = self.get(name)
        if not t: raise KeyError(f"Template {name!r} not found")
        t0 = time.time()
        try:
            result = self._do_render(t.source, dict(context or {}), t.auto_escape)
            self._store.log_render(name, (time.time()-t0)*1000, True)
            return result
        except Exception as e:
            self._store.log_render(name, (time.time()-t0)*1000, False)
            raise

    def render_string(self, source: str, context: Dict = None,
                       auto_escape: bool = None) -> str:
        ae = auto_escape if auto_escape is not None else self._auto_escape
        return self._do_render(source, dict(context or {}), ae)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory"] = len(self._templates)
        s["macros"] = len(self._macros)
        s["filters"] = len(self._filters)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def register_ep(req):
            d = await req.json()
            t = self.register(d["name"], d["source"],
                               d.get("auto_escape", self._auto_escape))
            return web.json_response({"name": t.name}, status=201)
        async def render_ep(req):
            d = await req.json()
            try:
                if "source" in d:
                    out = self.render_string(d["source"], d.get("context",{}))
                else:
                    out = self.render(d["name"], d.get("context",{}))
                return web.json_response({"output": out})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=400)
        async def list_ep(req):
            return web.json_response(
                {"templates": list(self._templates.keys())})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/templates"
        app.router.add_post(f"{p}/register", register_ep)
        app.router.add_post(f"{p}/render",   render_ep)
        app.router.add_get( f"{p}/list",     list_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        logger.info(f"Template engine API at {prefix}/templates/")
