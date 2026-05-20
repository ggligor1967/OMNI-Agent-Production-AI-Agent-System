# F.4 Local browser/API rerun observations

- Date: 2026-05-20
- Runtime: local-only `main.py --mode api`
- Bind: `http://127.0.0.1:8765`
- Auth mode: enabled
- Validation key: local temporary key used for browser auth (value intentionally omitted)

## API checks

- `GET /status` -> `200`
  - Returned structured payload with nested `status`, `health`, `jobs`, `skills`, `router`, and `model_stats`.
- `GET /health` -> `200`
  - Returned healthy runtime state.
- `POST /auth/bootstrap` with malformed JSON body (`{"bootstrap_token":`) -> `400`
  - Response body: `{"error":"invalid_json","detail":"Malformed JSON request body"}`
  - No traceback or parser internals were exposed.

## Browser checks

- Opened `/dashboard` in the integrated browser.
- Initial Overview render showed:
  - status line `● running`
  - `Health` rendered as `healthy`
  - `Skills` rendered as `summarize, word_count, reverse_text, translate_mock`
  - `Providers` rendered as a readable comma-separated list
- Explicit text probe confirmed page body **does not contain** `[object Object]`.

## UI interaction checks

- `Save` button click on API key input succeeded.
  - UI feedback changed to `✓ saved for this tab`.
- `Chat` tab click succeeded.
  - Chat panel became visible and the model dropdown populated.
- `Send` button click succeeded.
  - User message appended immediately in the chat thread.
  - Assistant response rendered successfully.
- Enter-to-send also succeeded via delegated key handling.
  - Message was sent from the chat input with Enter.

## CSP / console observations

- During a focused console capture around:
  - saving API key
  - switching to Chat tab
  - sending a chat message
- Filtered CSP violation signatures returned an empty list:
  - no `Content Security Policy`
  - no `unsafe-inline`
  - no `Refused to execute`
  - no `Refused to load`
- One expected browser console error was observed when intentionally posting malformed JSON to `/auth/bootstrap` and receiving the correct `400 Bad Request` response. This was not a CSP violation.

## Verdict

- `LBV-001` verified fixed locally: dashboard interaction worked under the existing strong nonce-based CSP.
- `LBV-002` verified fixed locally: structured overview values rendered as readable text; no `[object Object]` observed.
- `LBV-003` verified fixed locally: malformed bootstrap JSON returned controlled `400` instead of `500`.
