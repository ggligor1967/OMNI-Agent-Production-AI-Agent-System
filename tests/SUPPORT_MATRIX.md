# OMNI Agent Test Support Matrix

Generated: 2026-05-18
Active Suite Status: **PASSING** (321/321 tests)
Model Catalog Contract: **27 cloud models**

---

## Active Suite Tests (`tests/`)

| File | Pass | Fail | Category | Action |
| ---- | ---: | ---: | -------- | ------ |
| `test_models.py` | 51 | 0 | Runtime contract | KEEP |
| `test_advanced_modules.py` | 87 | 0 | Runtime modules | KEEP |
| `test_new_modules.py` | 93 | 0 | Runtime modules | KEEP |
| `test_suite.py` | 73 | 0 | Core modules | KEEP |
| `test_security_auth_tools.py` | 6 | 0 | Security gates | KEEP |
| `test_dashboard.py` | 2 | 0 | Dashboard UI | KEEP |
| `test_job_search_tank_adr_improved.py` | 9 | 0 | Job search | KEEP |
| **Active subtotal** | **321** | **0** | | **PASSING** |

---

## Archived Legacy Suite

Legacy versioned tests are quarantined from the blocking release gate. They remain available for reference and non-blocking audit work.

| Path | Status | Action |
| ---- | ------ | ------ |
| `tests/_archive/legacy/` | archived | Run only in `legacy-audit` |
| `agent/_legacy/` | archived | Excluded from release-gate tooling |

---

## Current Contract Notes

- The runtime model registry source of truth is **27 models**.
- The catalog includes `deepseek-v3.2:cloud`, `minimax-m2.7:cloud`, and `nemotron-3-super:cloud`.
- `ministral-3:8b-cloud`, `devstral-2:123b-cloud`, and `devstral-small-2:24b-cloud` are normalized under **Mistral AI**.
- `TaskType.CHAT` call sites were resolved to `TaskType.GENERAL` for explicit-model and `auto_route=False` paths.
- Python 3.13-sensitive sync tests now use `asyncio.run(...)`.

---

## Release-Gate Summary

The active release-gate test command is:

```bash
pytest tests/ -q
```

Current result:

- **321 passed**
- **0 failed**
- **0 errors**

---

## Recommendations

1. Keep `tests/_archive/legacy/` and `agent/_legacy/` out of the blocking CI lane.
2. Use `docs/adr/ADR-001-model-registry.md` as the source of truth for the 27-model decision.
3. Treat Bandit HIGH findings and security-critical MEDIUM findings as follow-on Sprint 0 blockers until remediated.
