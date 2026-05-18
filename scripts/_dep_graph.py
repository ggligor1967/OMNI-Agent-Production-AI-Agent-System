"""
Module dependency graph + dead code detection via AST import analysis.
Builds an import graph for agent/ modules and identifies:
1. Import graph (who imports what)
2. Modules never imported by any peer (candidates for dead/unused)
3. Circular import cycles
4. Hub modules (high in-degree) and leaf modules (no imports)
5. Architectural layer violations
"""
import ast, os, sys
from pathlib import Path
from collections import defaultdict, deque

ROOT = Path(r"c:\Users\gligo\My Projects\OMNI Agent — Production AI Agent System")
AGENT_DIR = ROOT / "agent"
SKIP_DIRS = {'legacy', '__pycache__', '.venv-1', '.venv', '.git'}

# ── Step 1: Collect all agent module names ────────────────────────────────
agent_modules = {}  # short_name -> path
for fpath in AGENT_DIR.rglob("*.py"):
    if any(part in SKIP_DIRS for part in fpath.parts):
        continue
    rel = fpath.relative_to(AGENT_DIR)
    parts = list(rel.parts)
    parts[-1] = parts[-1][:-3]  # strip .py
    short = ".".join(parts)
    if short == "__init__":
        short = "(init)"
    agent_modules[short] = fpath

# ── Step 2: Parse imports from each module ────────────────────────────────
def extract_imports(fpath: Path) -> list:
    """Return list of short module names imported from agent/."""
    try:
        src = fpath.read_text(encoding='utf-8', errors='replace')
        tree = ast.parse(src)
    except Exception:
        return []
    
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name.startswith("agent."):
                    imports.append(name[len("agent."):])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("agent."):
                imports.append(module[len("agent."):])
            elif module and not module.startswith('.'):
                # relative? check if it's a known agent module
                pass
            # handle relative imports
            if node.level and node.level > 0:
                # relative import from same package
                base_parts = list(fpath.relative_to(AGENT_DIR).parent.parts)
                if node.level > 1:
                    base_parts = base_parts[:-(node.level-1)]
                if module:
                    rel_mod = ".".join(base_parts + [module]) if base_parts else module
                else:
                    rel_mod = ".".join(base_parts) if base_parts else ""
                if rel_mod:
                    imports.append(rel_mod)
    return list(set(imports))

# Build adjacency: importer -> {imported modules}
graph = {}  # module -> set of modules it imports (from agent/)
for mod, fpath in agent_modules.items():
    raw_imports = extract_imports(fpath)
    # Filter to only agent modules
    resolved = set()
    for imp in raw_imports:
        if imp in agent_modules:
            resolved.add(imp)
        else:
            # try prefix match (e.g. "model_registry" matches "model_registry")
            for known in agent_modules:
                if imp == known or imp.startswith(known + "."):
                    resolved.add(known)
                    break
    graph[mod] = resolved

# ── Step 3: Build reverse graph (who imports me) ─────────────────────────
reverse_graph = defaultdict(set)
for mod, imports in graph.items():
    for imp in imports:
        reverse_graph[imp].add(mod)

# ── Step 4: Find unreferenced modules (never imported) ────────────────────
# Exclude known entry points
ENTRY_POINTS = {'core', 'cli', 'dashboard', 'telegram_bot', 'api_gateway', 
                'streaming', 'main', '__init__'}

unreferenced = []
for mod in sorted(agent_modules.keys()):
    if mod in ENTRY_POINTS:
        continue
    if not reverse_graph[mod]:
        unreferenced.append(mod)

# ── Step 5: Detect circular imports ──────────────────────────────────────
def find_cycles(graph):
    visited = set()
    path = []
    path_set = set()
    cycles = []
    
    def dfs(node):
        if node in path_set:
            cycle_start = path.index(node)
            cycles.append(path[cycle_start:] + [node])
            return
        if node in visited:
            return
        visited.add(node)
        path.append(node)
        path_set.add(node)
        for neighbor in graph.get(node, set()):
            dfs(neighbor)
        path.pop()
        path_set.discard(node)
    
    for node in graph:
        if node not in visited:
            dfs(node)
    return cycles

cycles = find_cycles(graph)

# ── Step 6: Hub analysis ──────────────────────────────────────────────────
in_degree = {mod: len(reverse_graph[mod]) for mod in agent_modules}
out_degree = {mod: len(graph.get(mod, set())) for mod in agent_modules}

# ── Step 7: Architectural layer classification ────────────────────────────
LAYERS = {
    "NUCLEUS":      {"model_registry", "model_router", "multi_model_client", "ollama_client"},
    "CORE":         {"core", "config_manager"},
    "PERSISTENCE":  {"memory", "cache", "database", "datastore", "vector_store"},
    "TRANSPORT":    {"dashboard", "api_gateway", "gateway", "streaming", "telegram_bot"},
    "SECURITY":     {"auth", "crypto_utils", "secrets_manager", "sandbox", "sandbox_executor", "audit_logger"},
    "TOOLS":        {"tools_registry", "tools", "skills", "skills_manager"},
    "LLM_SUPPORT":  {"rag", "embeddings", "embedding_pipeline", "prompt_templates", "summarizer"},
    "INFRA":        {"hooks", "event_bus", "scheduler", "agent_scheduler", "metrics_collector"},
}
def classify(mod):
    for layer, members in LAYERS.items():
        if mod in members or any(mod.startswith(m) for m in members):
            return layer
    return "OTHER"

# ── Print results ─────────────────────────────────────────────────────────
print("=" * 70)
print("MODULE DEPENDENCY GRAPH ANALYSIS")
print("=" * 70)
print(f"\nTotal agent modules: {len(agent_modules)}")

print(f"\n[1] TOP 15 HUB MODULES (most imported by others)")
top_hubs = sorted(in_degree.items(), key=lambda x: -x[1])[:15]
for mod, deg in top_hubs:
    layer = classify(mod)
    print(f"    {deg:3d} imports ← {mod} [{layer}]")

print(f"\n[2] TOP 10 MODULES WITH MOST OUTGOING IMPORTS")
top_out = sorted(out_degree.items(), key=lambda x: -x[1])[:10]
for mod, deg in top_out:
    layer = classify(mod)
    deps = sorted(graph.get(mod, set()))[:5]
    print(f"    {deg:3d} imports → {mod} [{layer}]: {', '.join(deps[:3])}{'...' if len(deps)>3 else ''}")

print(f"\n[3] CIRCULAR IMPORT CYCLES ({len(cycles)} found)")
if cycles:
    seen = set()
    for cycle in cycles[:10]:
        key = frozenset(cycle)
        if key not in seen:
            seen.add(key)
            print(f"    CYCLE: {' → '.join(cycle)}")
else:
    print("    ✓ No circular imports detected.")

print(f"\n[4] UNREFERENCED MODULES — never imported by any peer ({len(unreferenced)} modules)")
print("    (These are candidates for dead code or standalone entry points)")
for mod in unreferenced:
    lines = 0
    try:
        lines = sum(1 for _ in agent_modules[mod].open(encoding='utf-8', errors='replace'))
    except Exception:
        pass
    layer = classify(mod)
    print(f"    ✗ {mod} ({lines} lines) [{layer}]")

print(f"\n[5] NUCLEUS MODULE IMPORT HEALTH")
for nucleus_mod in sorted(LAYERS["NUCLEUS"]):
    if nucleus_mod in agent_modules:
        importers = sorted(reverse_graph[nucleus_mod])
        print(f"    {nucleus_mod}: imported by {len(importers)} modules")
        for imp in importers[:5]:
            print(f"       ← {imp}")
        if len(importers) > 5:
            print(f"       ... and {len(importers)-5} more")

print(f"\n[6] MODULES WITH ZERO IMPORTS (no dependencies on agent/)")
zero_out = [mod for mod, deg in out_degree.items() if deg == 0 and mod not in ENTRY_POINTS]
for mod in sorted(zero_out)[:20]:
    lines = 0
    try:
        lines = sum(1 for _ in agent_modules[mod].open(encoding='utf-8', errors='replace'))
    except Exception:
        pass
    print(f"    {mod} ({lines} lines)")

print("\n" + "=" * 70)
print("DEPENDENCY ANALYSIS COMPLETE")
print("=" * 70)
