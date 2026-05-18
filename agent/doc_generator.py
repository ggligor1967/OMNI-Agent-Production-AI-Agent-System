"""
OMNI AGENT - Documentation Generator
Auto-generates Markdown docs from source code, agent state, skills, and memory.
"""
import ast
import inspect
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE CODE PARSER
# ══════════════════════════════════════════════════════════════════════════════

def extract_module_docs(file_path: str) -> Dict:
    """Parse a Python file and extract classes, functions, and docstrings."""
    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}"}

    source = path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"error": f"SyntaxError: {e}"}

    module_doc = ast.get_docstring(tree) or ""
    classes = []
    functions = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = []
            for item in ast.walk(node):
                if isinstance(item, ast.FunctionDef) and item.col_offset > node.col_offset:
                    methods.append({
                        "name": item.name,
                        "doc": ast.get_docstring(item) or "",
                        "args": [a.arg for a in item.args.args if a.arg != "self"],
                        "is_async": isinstance(item, ast.AsyncFunctionDef),
                    })
            classes.append({
                "name": node.name,
                "doc": ast.get_docstring(node) or "",
                "methods": methods,
                "line": node.lineno,
            })
        elif isinstance(node, ast.FunctionDef) and node.col_offset == 0:
            functions.append({
                "name": node.name,
                "doc": ast.get_docstring(node) or "",
                "args": [a.arg for a in node.args.args],
                "is_async": False,
                "line": node.lineno,
            })
        elif isinstance(node, ast.AsyncFunctionDef) and node.col_offset == 0:
            functions.append({
                "name": node.name,
                "doc": ast.get_docstring(node) or "",
                "args": [a.arg for a in node.args.args],
                "is_async": True,
                "line": node.lineno,
            })

    return {
        "file": str(path),
        "module_doc": module_doc,
        "classes": classes,
        "functions": functions,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MARKDOWN RENDERER
# ══════════════════════════════════════════════════════════════════════════════

def render_module_md(doc: Dict) -> str:
    """Convert extracted module docs into Markdown."""
    if "error" in doc:
        return f"⚠️ Error: {doc['error']}\n"

    lines = []
    fname = Path(doc["file"]).name
    lines.append(f"## `{fname}`\n")

    if doc["module_doc"]:
        lines.append(f"{doc['module_doc']}\n")

    if doc["classes"]:
        lines.append("### Classes\n")
        for cls in doc["classes"]:
            lines.append(f"#### `class {cls['name']}` *(line {cls['line']})*\n")
            if cls["doc"]:
                lines.append(f"{cls['doc']}\n")
            if cls["methods"]:
                lines.append("**Methods:**\n")
                for m in cls["methods"]:
                    prefix = "async " if m["is_async"] else ""
                    args = ", ".join(m["args"])
                    lines.append(f"- `{prefix}{m['name']}({args})`")
                    if m["doc"]:
                        lines.append(f"  — {m['doc'].splitlines()[0]}")
                    lines.append("")

    if doc["functions"]:
        lines.append("### Functions\n")
        for fn in doc["functions"]:
            prefix = "async " if fn["is_async"] else ""
            args = ", ".join(fn["args"])
            lines.append(f"#### `{prefix}{fn['name']}({args})` *(line {fn['line']})*\n")
            if fn["doc"]:
                lines.append(f"{fn['doc']}\n")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# FULL PROJECT DOCUMENTATION GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

class DocGenerator:
    """Generates comprehensive Markdown documentation for the OMNI Agent project."""

    def __init__(self, root_dir: str = "."):
        self.root = Path(root_dir)

    def generate_api_docs(self, output_path: str = "docs/API.md") -> str:
        """Scan all Python source files and generate API reference docs."""
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        sections = [
            "# OMNI Agent — API Reference\n",
            f"*Auto-generated on {time.strftime('%Y-%m-%d %H:%M:%S')}*\n",
            "---\n",
        ]

        py_files = sorted(self.root.rglob("*.py"))
        for path in py_files:
            # Skip test files and __pycache__
            if any(skip in str(path) for skip in ["__pycache__", "test_", ".git"]):
                continue
            doc = extract_module_docs(str(path))
            sections.append(render_module_md(doc))
            sections.append("\n---\n")

        content = "\n".join(sections)
        out.write_text(content, encoding='utf-8')
        logger.info(f"API docs written: {output_path} ({len(content)} chars)")
        return content

    def generate_skills_doc(self, skills_list: List[Dict],
                            output_path: str = "docs/SKILLS.md") -> str:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# OMNI Agent — Skills Reference\n",
            f"*Auto-generated on {time.strftime('%Y-%m-%d %H:%M:%S')}*\n",
            f"Total skills: **{len(skills_list)}**\n",
            "---\n",
        ]

        for skill in skills_list:
            lines.append(f"## `{skill['name']}` v{skill.get('version','?')}\n")
            lines.append(f"{skill.get('description','No description.')}\n")
            if skill.get("triggers"):
                triggers = ", ".join(f"`{t}`" for t in skill["triggers"])
                lines.append(f"**Triggers:** {triggers}\n")
            lines.append(f"**Status:** {'✅ Enabled' if skill.get('enabled') else '❌ Disabled'} | "
                        f"**Calls:** {skill.get('call_count', 0)}\n")
            lines.append("---\n")

        content = "\n".join(lines)
        out.write_text(content, encoding='utf-8')
        logger.info(f"Skills docs written: {output_path}")
        return content

    def generate_hooks_doc(self, hooks_map: Dict[str, List[str]],
                           output_path: str = "docs/HOOKS.md") -> str:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# OMNI Agent — Events & Hooks Reference\n",
            f"*Auto-generated on {time.strftime('%Y-%m-%d %H:%M:%S')}*\n",
            "---\n",
            "## Registered Hooks\n",
        ]

        if not hooks_map:
            lines.append("No hooks currently registered.\n")
        else:
            for event, handlers in hooks_map.items():
                lines.append(f"### `{event}`\n")
                for h in handlers:
                    lines.append(f"- `{h}`")
                lines.append("")

        lines.extend([
            "---\n",
            "## All Available Event Types\n",
            "| Event | Description |\n",
            "|-------|-------------|\n",
            "| `agent.start` | Agent initialized and ready |\n",
            "| `agent.stop` | Agent shutting down |\n",
            "| `agent.error` | Unhandled agent error |\n",
            "| `message.received` | Incoming user message |\n",
            "| `message.sent` | Outgoing agent response |\n",
            "| `tool.called` | A tool was invoked |\n",
            "| `tool.result` | Tool returned successfully |\n",
            "| `tool.error` | Tool execution failed |\n",
            "| `memory.saved` | Data written to memory |\n",
            "| `memory.retrieved` | Data read from memory |\n",
            "| `skill.loaded` | A skill was registered |\n",
            "| `skill.executed` | A skill was triggered |\n",
            "| `job.started` | Scheduled job began |\n",
            "| `job.completed` | Scheduled job succeeded |\n",
            "| `job.failed` | Scheduled job errored |\n",
            "| `security.alert` | Security threat detected |\n",
            "| `rate_limit.hit` | User exceeded rate limit |\n",
        ])

        content = "\n".join(lines)
        out.write_text(content, encoding='utf-8')
        logger.info(f"Hooks docs written: {output_path}")
        return content

    def generate_all(self, agent_state: Dict = None,
                     skills_list: List[Dict] = None,
                     hooks_map: Dict = None) -> Dict[str, str]:
        """Generate all documentation files."""
        results = {}
        results["api"] = self.generate_api_docs()
        if skills_list is not None:
            results["skills"] = self.generate_skills_doc(skills_list)
        if hooks_map is not None:
            results["hooks"] = self.generate_hooks_doc(hooks_map)

        # Generate index
        index_path = Path("docs/INDEX.md")
        index_path.write_text(
            "# OMNI Agent Documentation\n\n"
            f"*Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}*\n\n"
            "## Files\n"
            "- [API Reference](API.md)\n"
            "- [Skills Reference](SKILLS.md)\n"
            "- [Hooks & Events](HOOKS.md)\n"
            "- [Main README](../README.md)\n",
            encoding='utf-8'
        )
        results["index"] = str(index_path)
        logger.info("Full documentation generated in docs/")
        return results
