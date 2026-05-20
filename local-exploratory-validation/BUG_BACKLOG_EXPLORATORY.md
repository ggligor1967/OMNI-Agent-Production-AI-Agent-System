# Exploratory Validation Bug Backlog

## Open Bugs

- None. No confirmed exploratory bugs remain open after the scoped follow-up fix and loopback retest for `BUG-X6-CHAT-MALFORMED-JSON-500`.

## Resolved Bugs

### BUG-X6-CHAT-MALFORMED-JSON-500

- Status: RESOLVED
- Severity: Medium
- Area: API / error handling
- Route: `POST /chat`
- Original repro:
  1. Send authenticated `POST /chat`
  2. Use `Content-Type: application/json`
  3. Send malformed JSON body such as `{"message":`
- Original expected:
  - bounded client error such as `400 Bad Request`
  - JSON error payload
  - no internal exception escalation for invalid client input
- Original observed:
  - `500 Internal Server Error`
  - plain-text body: `Server got itself in trouble`
  - server stdout contains `json.decoder.JSONDecodeError`
- Resolution summary:
  - `main.py` now routes `/chat` request parsing through a bounded JSON-object helper
  - malformed authenticated JSON now returns structured `400 invalid_json`
  - missing auth and invalid auth behavior remain `401 Unauthorized`
  - regression coverage added in `tests/test_chat_json_errors.py`
- Historical evidence:
  - `local-exploratory-validation/evidence/x6_malformed_chat_json_authenticated.log`
  - `local-exploratory-validation/evidence/x1_server_stdout.log`
  - `local-exploratory-validation/ERROR_HANDLING_EXPLORATION.md`
- Fix evidence:
  - `local-exploratory-validation/evidence/bugfix-chat-json/CHAT_JSON_CONTRACT_ANALYSIS.md`
  - `local-exploratory-validation/evidence/bugfix-chat-json/c4_chat_authenticated_malformed_json.log`
  - `local-exploratory-validation/evidence/bugfix-chat-json/c4_chat_missing_auth.log`
  - `local-exploratory-validation/evidence/bugfix-chat-json/c4_chat_invalid_auth.log`
  - `local-exploratory-validation/evidence/bugfix-chat-json/c4_server_log_leak_scan.log`
  - `tests/test_chat_json_errors.py`

## Non-Bug Observations

### OBS-X5-ANALYZE-TEXT-SLOW-SUCCESS

- Status: OBSERVATION
- Area: workflow performance
- Surface: `POST /workflows/analyze_text/run`
- Summary:
  - initial 20-second client timeout produced a local timeout signal
  - longer retest completed successfully in about `55.8s`
- Classification:
  - not a confirmed bug in this pass
  - reachable, functional, but slow enough to require realistic client timeouts
- Evidence:
  - `local-exploratory-validation/evidence/x5_workflow_analyze_text.log`
  - `local-exploratory-validation/WORKFLOW_EXPLORATION.md`
