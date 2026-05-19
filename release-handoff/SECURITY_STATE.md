# Security State

## Security Gates

- active-path Bandit: PASS (`release-handoff/evidence/final_bandit_active_path.log`)
- pip-audit: PASS (`release-handoff/evidence/final_pip_audit.log`) using UTF-8 environment overrides plus a writable temp cache directory in the Windows Unicode-path workspace
- full-agent Bandit audit policy: separate audit lane `full-agent-bandit-audit` remains documented in `tests/SUPPORT_MATRIX.md`
- security audit events: active regression coverage present in `tests/test_security_event_audit.py`; latest release-gate pytest passed in `release-handoff/evidence/final_pytest.log`
- anti-SSRF: active regression coverage present in `tests/test_ssrf_validator.py`; latest release-gate pytest passed in `release-handoff/evidence/final_pytest.log`
- sandbox policy: evaluated in `snapshot-phase-3-4/SANDBOX_V2_EVALUATION.md` and covered by `tests/test_sandbox_policy.py` plus `tests/test_sandbox_isolation_proofs.py`

## Known Security Caveats

- Windows Unicode-path local execution can still break tools that assume UTF-8 subprocess output; `pip-audit` needed `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`, and a writable temp cache directory during final handoff verification.
- Full-agent Bandit audit remains separate from the blocking active-path gate.
