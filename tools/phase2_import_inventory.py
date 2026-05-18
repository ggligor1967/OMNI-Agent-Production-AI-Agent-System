from __future__ import annotations

"""Read-only import inventory for Phase 2 orphan-module classification.

Parses Python files with AST and reports internal incoming/outgoing import counts.
Skips archived/legacy folders so the output focuses on the active code path.
"""

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {"_legacy", "legacy", "_archive", "__pycache__", ".venv", ".venv-1", "venv"}
INTERNAL_PREFIXES = ("agent", "tests", "main", "config")


@dataclass
class ModuleRecord:
    name: str
    path: Path
    outgoing: set[str] = field(default_factory=set)
    incoming_runtime: set[str] = field(default_factory=set)
    incoming_tests: set[str] = field(default_factory=set)


def iter_python_files(root: Path) -> Iterable[Path]:
    explicit_files = [root / "main.py", root / "config.py"]
    for path in explicit_files:
        if path.exists():
            yield path

    for folder_name in ("agent", "tests", "scripts"):
        folder = root / folder_name
        if not folder.exists():
            continue
        for path in folder.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT)
            if any(part in SKIP_PARTS for part in rel.parts):
                continue
            yield path


def path_to_module(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT)
    if rel.name == "main.py":
        return "main"
    if rel.name == "config.py":
        return "config"
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def is_internal(module_name: str | None) -> bool:
    if not module_name:
        return False
    return module_name == "main" or module_name == "config" or module_name.startswith(INTERNAL_PREFIXES)


def normalize_internal(module_name: str | None, known_modules: set[str]) -> str | None:
    if not is_internal(module_name):
        return None
    if module_name in known_modules:
        return module_name
    while module_name:
        if module_name in known_modules:
            return module_name
        if "." not in module_name:
            break
        module_name = module_name.rsplit(".", 1)[0]
    return module_name if module_name in known_modules else None


def resolve_relative_import(current_module: str, level: int, module: str | None) -> str | None:
    package_parts = current_module.split(".")
    if current_module in {"main", "config"}:
        package_parts = [current_module]
    if level > len(package_parts):
        return module
    base_parts = package_parts[:-level]
    if module:
        return ".".join([*base_parts, module]) if base_parts else module
    return ".".join(base_parts) if base_parts else None


def collect_imports(module_name: str, file_path: Path, known_modules: set[str]) -> set[str]:
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                normalized = normalize_internal(alias.name, known_modules)
                if normalized and normalized != module_name:
                    imports.add(normalized)
        elif isinstance(node, ast.ImportFrom):
            base_module = node.module
            if node.level:
                base_module = resolve_relative_import(module_name, node.level, node.module)
            normalized_base = normalize_internal(base_module, known_modules)
            if normalized_base and normalized_base != module_name:
                imports.add(normalized_base)
            for alias in node.names:
                candidate = f"{base_module}.{alias.name}" if base_module else alias.name
                normalized_child = normalize_internal(candidate, known_modules)
                if normalized_child and normalized_child != module_name:
                    imports.add(normalized_child)
    return imports


def build_inventory() -> dict[str, ModuleRecord]:
    files = list(iter_python_files(REPO_ROOT))
    records = {
        path_to_module(path): ModuleRecord(name=path_to_module(path), path=path)
        for path in files
    }
    known_modules = set(records)
    for module_name, record in records.items():
        record.outgoing = collect_imports(module_name, record.path, known_modules)
    for module_name, record in records.items():
        for imported in record.outgoing:
            target = records.get(imported)
            if target is None:
                continue
            if module_name.startswith("tests"):
                target.incoming_tests.add(module_name)
            else:
                target.incoming_runtime.add(module_name)
    return records


def emit_report(records: dict[str, ModuleRecord]) -> str:
    lines: list[str] = []
    lines.append("# Phase 2 Import Inventory")
    lines.append("")
    lines.append(f"Analyzed modules: {len(records)}")
    lines.append("")
    lines.append("## Active agent modules by inbound runtime imports")
    lines.append("")
    lines.append("module | runtime_in | test_in | outgoing | imported_by | imports")
    lines.append("--- | ---: | ---: | ---: | --- | ---")

    agent_records = [
        record for record in records.values()
        if record.name.startswith("agent.") and "._legacy" not in record.name
    ]
    agent_records.sort(key=lambda item: (len(item.incoming_runtime), len(item.incoming_tests), item.name))

    for record in agent_records:
        imported_by = ", ".join(sorted(record.incoming_runtime | record.incoming_tests)) or "-"
        imports = ", ".join(sorted(record.outgoing)) or "-"
        lines.append(
            f"{record.name} | {len(record.incoming_runtime)} | {len(record.incoming_tests)} | "
            f"{len(record.outgoing)} | {imported_by} | {imports}"
        )

    lines.append("")
    lines.append("## Potential orphan candidates (0 runtime inbound imports)")
    lines.append("")
    for record in agent_records:
        if record.incoming_runtime:
            continue
        lines.append(
            f"- {record.name} (tests={len(record.incoming_tests)}, outgoing={len(record.outgoing)})"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    records = build_inventory()
    sys.stdout.write(emit_report(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
