# Gate 3.6.3 pip-audit workaround

## Status

PASS for the pip-audit portion of Gate 3.6.3 after rerunning the audit from an ASCII-only mirror path.

## Original Failure

The original local run from the repository working tree at `C:\Users\gligo\My Projects\OMNI Agent — Production AI Agent System` failed before completing the dependency audit.

Observed failure:

- `UnicodeDecodeError: 'utf-8' codec can't decode byte 0x97 ...`
- Failure occurred in the local Windows path context containing the non-ASCII em dash in the repository path.

Original failing evidence:

- `snapshot-phase-3-6/gate_3_6_3_pip_audit.log`

## Workaround

The audit was rerun using the same class of workaround previously proven for Windows path-encoding issues:

1. Mirror the repository into an ASCII-only path.
2. Use a global Python executable from an ASCII-only path.
3. Use an explicit writable cache directory.
4. Run `pip_audit` against `requirements.txt` from the ASCII mirror.
5. Copy the resulting audit log back into `snapshot-phase-3-6`.

## ASCII mirror path

- Mirror root: `C:\omni-phase36`
- Cache directory: `C:\pip-audit-cache-phase36`

## Audited file

- `requirements.txt`

## Python executable/version

- Executable: `C:\Python313\python.exe`
- Version: `Python 3.13.3`

## Result

Clean audit result from the ASCII mirror:

- `No known vulnerabilities found`

## Evidence list

- Original failing log: `snapshot-phase-3-6/gate_3_6_3_pip_audit.log`
- Clean ASCII-path rerun log: `snapshot-phase-3-6/gate_3_6_3_pip_audit_ascii.log`
- YAML rerun: `snapshot-phase-3-6/gate_3_6_3_yaml_validation_rerun.log`
- Documentation consistency rerun: `snapshot-phase-3-6/gate_3_6_3_doc_consistency_rerun.log`
- Compile rerun: `snapshot-phase-3-6/gate_3_6_3_compile_rerun.log`
- Pytest rerun: `snapshot-phase-3-6/gate_3_6_3_pytest_rerun.log`
- Ruff rerun: `snapshot-phase-3-6/gate_3_6_3_ruff_rerun.log`
- Coverage rerun: `snapshot-phase-3-6/gate_3_6_3_coverage_rerun.log`
- Active-path Bandit rerun: `snapshot-phase-3-6/gate_3_6_3_bandit_active_path_rerun.log`
