"""
Model Registry Audit Script
Compares registry entries against config.py references and model_router.py routing.
"""
import sys, os
sys.path.insert(0, r"c:\Users\gligo\My Projects\OMNI Agent — Production AI Agent System")

# Load registry directly
from agent.model_registry import MODELS, ModelTier, ModelCapability

print("=" * 70)
print("MODEL REGISTRY AUDIT")
print("=" * 70)
print(f"\n[1] REGISTRY SIZE")
print(f"    Docstring claims: 24 models")
print(f"    Actual MODELS dict entries: {len(MODELS)}")
delta = len(MODELS) - 24
print(f"    Delta: +{delta} (extra entries beyond claimed 24)" if delta > 0 else f"    Delta: {delta}")

print(f"\n[2] ALL {len(MODELS)} REGISTRY ENTRIES")
for i, (k, v) in enumerate(MODELS.items(), 1):
    caps = [c.value if hasattr(c, 'value') else str(c) for c in v.capabilities]
    print(f"    {i:2d}. {k:<45} [{v.provider}] tier={v.tier.value} ctx={v.context_window//1000}K caps={caps}")

# Check for duplicates (same display_name or near-duplicate IDs)
print(f"\n[3] DUPLICATE / NEAR-DUPLICATE ANALYSIS")
by_display = {}
for k, v in MODELS.items():
    base = v.display_name.lower().strip()
    by_display.setdefault(base, []).append(k)

dups = {k: v for k, v in by_display.items() if len(v) > 1}
if dups:
    for name, ids in dups.items():
        print(f"    DUPLICATE display_name '{name}':")
        for mid in ids:
            print(f"      → {mid}")
else:
    print("    No exact display_name duplicates.")

# Check model IDs for near-duplicates (same base model, different suffix)
print(f"\n[4] NEAR-DUPLICATE MODEL ID PAIRS (same base model)")
ids = list(MODELS.keys())
from collections import defaultdict
base_groups = defaultdict(list)
for mid in ids:
    # extract base (remove version suffixes like :235b-instruct-cloud vs :235b-cloud)
    parts = mid.split(":")
    base_groups[parts[0]].append(mid)
near_dups = {k: v for k, v in base_groups.items() if len(v) > 1}
if near_dups:
    for base, variants in near_dups.items():
        print(f"    BASE '{base}' has {len(variants)} variants:")
        for mid in variants:
            m = MODELS[mid]
            caps = [c.value if hasattr(c, 'value') else str(c) for c in m.capabilities]
            print(f"      → {mid}")
            print(f"         caps={caps} ctx={m.context_window//1000}K")
else:
    print("    No near-duplicate IDs found.")

# Config.py cross-reference
print(f"\n[5] CONFIG.PY MODEL REFERENCES → REGISTRY LOOKUP")
config_refs = {
    "MODEL_CODE":       "qwen3-coder-next:cloud",
    "MODEL_MATH":       "deepseek-v3.1:671b-cloud",
    "MODEL_VISION":     "qwen3-vl:235b-instruct-cloud",
    "MODEL_REASON":     "cogito-2.1:671b-cloud",
    "MODEL_FAST":       "gemma3:4b-cloud",
    "MODEL_CREATIVE":   "gpt-oss:120b-cloud",
    "MODEL_AGENT":      "devstral-2:123b-cloud",
    "MODEL_MULTILANG":  "mistral-large-3:675b-cloud",
    "MODEL_LONGCTX":    "minimax-m2.5:cloud",
    "OLLAMA_MODEL":     "qwen3-next:80b-cloud",
}
orphan_configs = []
for cfg_key, model_id in config_refs.items():
    in_registry = model_id in MODELS
    status = "✓" if in_registry else "✗ ORPHAN"
    print(f"    {cfg_key:<20} → {model_id:<45} {status}")
    if not in_registry:
        orphan_configs.append((cfg_key, model_id))

# model_router.py fallback chains cross-reference
print(f"\n[6] FALLBACK CHAIN REFERENCES → REGISTRY LOOKUP")
fallback_chains = {
    "qwen3-coder-next:cloud": ["qwen3-next:80b-cloud", "gpt-oss:120b-cloud", "cogito-2.1:671b-cloud"],
    "deepseek-v3.1:671b-cloud": ["qwen3-coder-next:cloud", "gpt-oss:120b-cloud", "glm-4.7:cloud"],
    "gpt-oss:120b-cloud": ["qwen3-coder-next:cloud", "deepseek-v3.1:671b-cloud", "glm-4.7:cloud"],
    "gemini-3-flash-preview:cloud": ["qwen3-vl:235b-instruct-cloud", "qwen3-vl:235b-cloud"],
}
for primary, fallbacks in fallback_chains.items():
    p_ok = "✓" if primary in MODELS else "✗"
    print(f"    PRIMARY {p_ok} {primary}")
    for fb in fallbacks:
        fb_ok = "✓" if fb in MODELS else "✗ MISSING"
        print(f"            → {fb_ok} {fb}")

# Provider correctness check
print(f"\n[7] PROVIDER CORRECTNESS CHECK (known mismatches)")
known_issues = {
    "ministral-3:8b-cloud": {"expected": "Mistral AI", "actual": MODELS.get("ministral-3:8b-cloud").provider if "ministral-3:8b-cloud" in MODELS else "N/A"},
}
for mid, info in known_issues.items():
    if mid in MODELS:
        actual = MODELS[mid].provider
        expected = info["expected"]
        if actual != expected:
            print(f"    MISMATCH {mid}: provider='{actual}' but should be '{expected}'")
        else:
            print(f"    OK {mid}: provider='{actual}'")
    else:
        print(f"    NOT FOUND: {mid}")

# Models NOT referenced anywhere (potential orphans)
print(f"\n[8] MODELS NOT REFERENCED IN CONFIG.PY OR ROUTING")
referenced = set(config_refs.values())
for chains in fallback_chains.values():
    referenced.update(chains)
referenced.update(fallback_chains.keys())
unreferenced = [mid for mid in MODELS if mid not in referenced]
if unreferenced:
    for mid in unreferenced:
        m = MODELS[mid]
        caps = [c.value if hasattr(c, 'value') else str(c) for c in m.capabilities]
        print(f"    UNREFERENCED: {mid} [{m.provider}] caps={caps}")
else:
    print("    All models referenced.")

# Identify the 3 extra entries beyond the claimed 24
print(f"\n[9] THE +3 EXTRA ENTRIES (added after docstring was updated)")
# Original 24 appear to be all except the last 3 added:
# deepseek-v3.2:cloud, minimax-m2.7:cloud, nemotron-3-super:cloud
extra_entries = ["deepseek-v3.2:cloud", "minimax-m2.7:cloud", "nemotron-3-super:cloud"]
for mid in extra_entries:
    if mid in MODELS:
        m = MODELS[mid]
        in_config = mid in referenced
        caps = [c.value if hasattr(c, 'value') else str(c) for c in m.capabilities]
        print(f"    EXTRA: {mid}")
        print(f"           Provider: {m.provider}, Tier: {m.tier.value}")
        print(f"           Caps: {caps}")
        print(f"           Referenced in config/routing: {'YES' if in_config else 'NO — ORPHAN'}")

print("\n" + "=" * 70)
print("AUDIT COMPLETE")
print("=" * 70)
