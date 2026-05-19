# Security State

## Security Gates

- active-path Bandit: PASS (`release-handoff/evidence/bandit_active_path.log`)
- pip-audit: PASS via ASCII mirror workaround (`release-handoff/evidence/pip_audit_ascii.log`)
- full-agent Bandit audit policy: separate audit lane `full-agent-bandit-audit` remains documented in `tests/SUPPORT_MATRIX.md`
- security audit events: active regression coverage present in `tests/test_security_event_audit.py`; latest release-gate pytest passed in `release-handoff/evidence/pytest.log`
- anti-SSRF: active regression coverage present in `tests/test_ssrf_validator.py`; latest release-gate pytest passed in `release-handoff/evidence/pytest.log`
- sandbox policy: evaluated in `snapshot-phase-3-4/SANDBOX_V2_EVALUATION.md` and covered by `tests/test_sandbox_policy.py` plus `tests/test_sandbox_isolation_proofs.py`

## Known Security Caveats

- Windows Unicode-path `pip-audit` workaround remains necessary in this local path context.
- Full-agent Bandit audit remains separate from the blocking active-path gate.
