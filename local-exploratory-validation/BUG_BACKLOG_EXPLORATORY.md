# Exploratory Validation Bug Backlog

## Open Bugs

### BUG-X6-CHAT-MALFORMED-JSON-500

- Status: OPEN
- Severity: Medium
- Area: API / error handling
- Route: `POST /chat`
- Repro:
  1. Send authenticated `POST /chat`
  2. Use `Content-Type: application/json`
  3. Send malformed JSON body such as `{"message":`
- Expected:
  - bounded client error such as `400 Bad Request`
  - JSON error payload
  - no internal exception escalation for invalid client input
- Observed:
  - `500 Internal Server Error`
  - plain-text body: `Server got itself in trouble`
  - server stdout contains `json.decoder.JSONDecodeError`
- Likely cause:
  - `main.py` `chat_endpoint` calls `await request.json()` without bounded parse handling
- Evidence:
  - `local-exploratory-validation/evidence/x6_malformed_chat_json_authenticated.log`
  - `local-exploratory-validation/evidence/x1_server_stdout.log`
  - `local-exploratory-validation/ERROR_HANDLING_EXPLORATION.md`
- Recommended fix lane:
  - add bounded JSON parse handling in `/chat`
  - return structured `400` response for malformed JSON
  - add regression tests for authenticated malformed JSON requests

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
