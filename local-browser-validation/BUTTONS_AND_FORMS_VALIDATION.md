# Buttons and Forms Validation

## Status

PARTIAL

## Browser Routes Exercised During B.3

| Route | Browser Result | Notes |
| ----- | -------------- | ----- |
| `/` | `401 Unauthorized` | Public browser navigation confirmed auth rejection with JSON body `{"error":"unauthorized","detail":"No credentials provided"}`. |
| `/status` | `200 OK` | Raw JSON rendered in browser. |
| `/health` | `200 OK` | Raw JSON rendered in browser and matched the `/status` payload observed during this pass. |
| `/dashboard` | Loaded with defects | Dashboard title rendered, but multiple UI defects were reproduced in the live integrated browser. |

## Buttons / Controls Tested

| Control | Test Method | Result | Evidence |
| ------- | ----------- | ------ | -------- |
| `💬 Chat` tab | Normal browser click | BUG | Clicks did not switch away from Overview during normal interaction. DOM inspection showed inline `onclick="showTab('chat')"`, consistent with CSP-blocked inline handlers. |
| `Save` API key button | Type dummy value `dummy-local-key` then normal browser click | BUG | Browser console emitted `Executing inline event handler violates ... Content Security Policy directive 'script-src ...'`; `sessionStorage.omni_api_key` remained empty and `#key-ok` stayed blank. |
| `Refresh` audit button | Normal browser click | BLOCKED BY SAME DEFECT | Dashboard remained noisy with CSP violations; this control uses inline `onclick="loadAudit()"` in `agent/dashboard.py`, so it is affected by the same browser-blocked event-handler pattern. |
| `Send` chat button | Script-assisted tab switch to Chat, then normal browser click after typing synthetic message | BUG | Browser console emitted the same inline-event CSP violation; no response metadata or assistant reply was produced. |
| `Load History` / `Clear History` | Visible after script-assisted tab switch only | NOT ATTEMPTED | Left unclicked once root cause was confirmed on multiple live controls. |

## Inputs / Forms Tested

| Input / Form | Test Method | Result | Evidence |
| ------------ | ----------- | ------ | -------- |
| API key input | Typed dummy value only | PASS | Field accepted `dummy-local-key`; no real secret used. |
| Chat message input | Script-assisted Chat panel, then typed synthetic message `local browser validation ping` | PASS | Field accepted text locally. |
| API key save workflow | Dummy input + Save click | BUG | Save action blocked by CSP inline-event violation; no client-side persistence occurred. |
| Chat send workflow | Synthetic message + Send click | BUG | Send action blocked by CSP inline-event violation before request dispatch. |

## Visual / Rendering Defects Reproduced

| Surface | Observed Result | Likely Cause |
| ------- | --------------- | ------------ |
| Dashboard header status | `● [object Object]` | `/status` now returns structured `status`; `initOverview()` still treats it as a scalar string. |
| Overview `Status -> State` | `[object Object]` | `agent/dashboard.py` uses `d.status` directly in card rendering. |
| Overview `Agent -> Skills` | `"[object Object],[object Object],[object Object],[object Object]"` | `agent/dashboard.py` uses `d.skills` directly even though the payload is structured. |
| Dashboard load / refresh | Repeated CSP `style-src` violations in console | HTML and generated markup still rely on inline `style=` attributes under a nonce-only CSP. |

## Diagnostic Workaround Used

| Workaround | Purpose | Result | Scope |
| ---------- | ------- | ------ | ----- |
| Direct page-script invocation of `showTab('chat')` | Determine whether the Chat panel exists behind the broken click path | PASS | Chat panel became visible, proving the underlying tab function exists while the normal click path remains broken. This does **not** count as a successful user click path. |

## Assessment

- Normal browser interaction with the dashboard is currently **not reliable** under the active nonce-based CSP.
- Multiple live controls depend on inline event handlers such as `onclick="..."`, which the browser blocked during this validation pass.
- Additional dashboard button/form coverage through standard click interaction is blocked until the dashboard stops depending on inline handlers/styles or the CSP policy is changed.
- This file records only controls actually exercised during B.3; untested controls remain for later coverage and must not be counted as pass results.
