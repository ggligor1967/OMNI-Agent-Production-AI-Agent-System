# B.3 Browser Observations

## Status

FAIL

## Browser Surface Under Test

- Local URL: `http://127.0.0.1:8765/dashboard`
- Browser mode: VS Code integrated browser / local-only
- Runtime scope: loopback only (`127.0.0.1`), no tunnel, no public exposure

## Routes Visited Live

| Route | Result | Observation |
| ----- | ------ | ----------- |
| `/` | `401 Unauthorized` | Browser displayed JSON auth error body, confirming protected root behavior remains enforced. |
| `/status` | `200 OK` | Browser rendered raw JSON status payload. |
| `/health` | `200 OK` | Browser rendered raw JSON payload matching `/status`. |
| `/dashboard` | `200 OK` with visible defects | Dashboard shell loaded and pulled public overview data, but console and UI defects were reproduced immediately. |

## Reproduced Browser Defects

### 1. CSP blocks inline event handlers

Observed live in browser console when clicking interactive controls:

- `Save` button click emitted:
  - `Executing inline event handler violates the following Content Security Policy directive 'script-src 'self' 'nonce-...' ... The action has been blocked.`
- `Send` button click in Chat emitted the same error after typing a synthetic message.

Functional consequences observed live:

- `Save` did not persist the dummy API key to `sessionStorage`.
- `Send` did not dispatch the chat action or update response metadata.
- Normal tab click behavior was broken for `💬 Chat` before a diagnostic workaround was used.

Correlated code evidence in `agent/dashboard.py`:

- `line 121`: `onclick="saveKey()"`
- `lines 127-135`: tab switches defined with inline `onclick="showTab('...')"`
- `line 184`: audit refresh uses inline `onclick="loadAudit()"`
- `line 206`: chat input uses inline `onkeydown="...sendChat()"`
- `lines 207-221`: chat actions use inline `onclick="sendChat()"`, `onclick="clearChat()"`, `onclick="loadHistory()"`, `onclick="clearHistory()"`

### 2. CSP blocks inline styles

Observed live in browser console on dashboard load and refresh:

- repeated `Applying inline style violates the following Content Security Policy directive 'style-src 'self' 'nonce-...' ... The action has been blocked.`

Correlated code evidence in `agent/dashboard.py`:

- many inline `style="..."` attributes in the dashboard HTML and generated markup, including `line 122`, `lines 143-184`, `line 558`, `line 615`, and multiple later render helpers.

### 3. Overview renders structured objects as strings

Observed live in browser UI:

- header status rendered as `● [object Object]`
- Overview card `Status -> State` rendered as `[object Object]`
- Overview card `Agent -> Skills` rendered as `"[object Object],[object Object],[object Object],[object Object]"`

Correlated code evidence in `agent/dashboard.py`:

- `initOverview()` at `lines 538-548` uses `d.status`, `d.models`, `d.skills`, and `d.pipelines` as display-ready scalar values.
- Current `/status` payload is structured, so string coercion produces `[object Object]` output.

## Diagnostic Workaround Performed

A direct page-script invocation of `showTab('chat')` was used only to continue observation after the normal click path failed.

Observed result:

- the Chat panel became visible in the integrated browser
- the screenshot captured during this state showed the Chat panel contents and confirmed the page was still live

Interpretation:

- the underlying `showTab()` logic exists
- the browser-click wiring is what failed under CSP, not the mere presence of the Chat panel markup

This workaround is diagnostic evidence only and does **not** convert the normal tab click path into a pass.

## Data Handling Notes

- No production or user secret was entered.
- Dummy local-only text used during testing:
  - API key field: `dummy-local-key`
  - Chat input: `local browser validation ping`
- Post-click verification showed `sessionStorage.omni_api_key` remained empty after the blocked Save action.

## Conclusion

Gate B.3 reproduced real local browser defects in the dashboard:

1. nonce-only CSP conflicts with inline event handlers
2. nonce-only CSP conflicts with inline style attributes
3. Overview rendering is stale relative to the current structured `/status` contract

Because these defects were reproduced in the live integrated browser, dashboard click-driven workflows cannot currently be marked as passed. Standard browser coverage beyond the already-tested controls is blocked until the dashboard event/style model is made CSP-compatible or the CSP contract is changed.
