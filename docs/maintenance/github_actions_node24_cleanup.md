# GitHub Actions Node 24 Cleanup

## Status

PASS

## Scope

This maintenance pass updates the active CI workflow to remove the Node.js 20 deprecation warnings reported by GitHub Actions for the `CI` workflow.

## Why this change exists

GitHub announced the deprecation of Node.js 20 on GitHub Actions runners. As of June 16, 2026, runners begin using Node 24 by default, and Node 20 is removed later in the fall of 2026. The run for commit `dd88d34` (`fix: resolve local health endpoint validation mismatch`) completed successfully but reported non-blocking warnings for JavaScript actions still running on Node.js 20.

## Official compatibility evidence

- GitHub changelog: `https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/`
- `actions/checkout` `v6.0.2` action metadata declares `runs.using: node24`
- `actions/setup-python` `v6.2.0` action metadata declares `runs.using: node24`
- `actions/upload-artifact` `v7.0.1` action metadata declares `runs.using: node24`
- `actions/setup-python` `v6.2.0` release notes explicitly mention dependency upgrades to Node 24-compatible versions

## Repository changes

Updated `.github/workflows/ci.yml` only where required:

- `actions/checkout@v4` → `actions/checkout@v6`
- `actions/setup-python@v5` → `actions/setup-python@v6`
- `actions/upload-artifact@v4` → `actions/upload-artifact@v7`

Preserved behavior intentionally:

- `fetch-depth: 0` remains on the `release-gate` checkout step so phase tags remain visible to documentation consistency checks
- release-gate commands, matrix versions, and audit lanes remain unchanged

## Local verification

The following checks passed after the workflow update:

- workflow YAML parse via `yaml.safe_load`
- `python tools/check_documentation_consistency.py --root .`
- `python -m compileall agent main.py`
- `pytest tests/ -q` → `511 passed, 5 warnings`
- `ruff check .`

## Intended outcome

The next push should keep the existing CI behavior green while eliminating the Node.js 20 deprecation warnings emitted for the official GitHub JavaScript actions used by this repository.
