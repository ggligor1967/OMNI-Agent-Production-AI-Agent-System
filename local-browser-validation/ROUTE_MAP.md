# Local Route Map

## Status

PASS

## Discovered Runtime Routes

| Route | Source File | Expected Auth | Expected Behavior | Test Method |
| ----- | ----------- | ------------- | ----------------- | ----------- |
| `GET /status` | `main.py` | Public | JSON status/health summary for loopback runtime | browser + curl |
| `GET /health` | `main.py` | Public | JSON health alias matching `/status` payload | browser + curl |
| `GET /dashboard` | `agent/dashboard.py` | Public | Control panel HTML shell with client-side tabs and API calls | browser |
| `GET /` | `agent/dashboard.py` + auth middleware | Protected | Dashboard handler exists but should return `401` when unauthenticated with `AUTH_ENFORCE=true` | browser + curl |
| `GET /favicon.ico` | `main.py` | Public | Empty `204` favicon response | browser incidental |
| `GET /audit` | `main.py` / `agent/dashboard.py` | Public | JSON audit feed used by dashboard overview | browser + curl |
| `GET /cache/stats` | `main.py` | Public | JSON cache statistics | browser + curl |
| `POST /auth/bootstrap` | `agent/auth.py` via `main.py` | Public | One-time bootstrap endpoint for local synthetic admin identity if bootstrap token is configured | curl / scripted local call |
| `GET/POST/DELETE /auth/keys{/{key_id}}` | `agent/auth.py` | Admin-only | Key management after authentication | curl |
| `POST /auth/token{,/revoke,/verify}` | `agent/auth.py` | Admin-only | JWT issue / revoke / verify flows | curl |
| `POST /chat` | `main.py` | Authenticated | Chat request endpoint; should reject missing/invalid auth and accept safe synthetic payloads with valid auth | dashboard + curl |
| `GET /memories` | `main.py` | Authenticated | Memory search/list JSON used by dashboard | dashboard + curl |
| `GET /models` | `main.py` | Authenticated | Model catalog JSON used by dashboard chat/compare/models views | dashboard + curl |
| `GET /models/{model_id}` | `main.py` | Authenticated | Single model detail JSON | curl |
| `POST /route` | `main.py` | Authenticated | Routing preview JSON | curl |
| `POST /compare` | `main.py` | Authenticated | Multi-model compare workflow for dashboard compare tab | dashboard + curl |
| `POST /rag/ingest` | `main.py` | Authenticated | Ingest synthetic text into local RAG store | dashboard + curl |
| `POST /rag/query` | `main.py` | Authenticated | Query local RAG chunks | dashboard + curl |
| `GET /rag/docs` / `DELETE /rag/docs/{doc_id}` | `main.py` | Authenticated | Inspect and delete ingested RAG docs | dashboard + curl |
| `GET /pipelines` / `GET /pipelines/runs` | `main.py` | Authenticated | List pipelines and recent runs | dashboard + curl |
| `POST /pipelines/{name}/run` | `main.py` | Authenticated | Execute named pipeline with synthetic JSON context | dashboard + curl |
| `GET /workflows` / `POST /workflows/{name}/run` | `main.py` | Authenticated | List and run workflows | dashboard + curl |
| `GET /templates` / `POST /templates/{name}/render` | `main.py` | Authenticated | List templates and render with supplied variables | dashboard + curl |
| `GET /tools` / `POST /tools/call` | `main.py` | Developer/Admin | Tool listing and confirmed tool invocation flow | dashboard + curl |
| `GET /tracing/{summary,spans,errors}` | `main.py` | Developer/Admin | Observability JSON for dashboard tools area | dashboard + curl |
| `POST /structured` | `main.py` | Authenticated | Structured output parsing endpoint | dashboard + curl |
| `GET /personas` / `GET or POST /personas/session/{session_id}` | `main.py` | Authenticated | Persona listing and per-session set/get operations | dashboard + curl |
| `GET /eval/{suites,history/{suite},compare/{suite}}` | `main.py` | Developer/Admin | Evaluation metadata endpoints | curl |
| `GET /kg/{stats,search,path,export}` / `POST /kg/extract` | `main.py` | Developer/Admin | Knowledge graph stats/search/export and extract workflow | dashboard + curl |
| `POST /sandbox/run` / `GET /sandbox/history` | `main.py` | Developer/Admin | Sandbox execution and history endpoints | dashboard + curl |
| `POST /vision/analyze` / `GET /vision/models` | `main.py` | Admin-only | Vision analysis and model list | dashboard + curl |
| `GET /notifications/channels` / `GET /notifications/stats` / `POST /notifications/send` | `main.py` | Admin-only | Notification inspection/send workflows | curl |
| `GET /stream/{chat,events,traces,stats}` | `agent/streaming.py` | Authenticated | SSE streaming/chat/events/tracing endpoints | browser/curl where safe |
| `GET /config/{key,snapshot,validate,stats}` / `POST /config/set` | `agent/config_manager.py` | Admin-only | Live config read/validate/set surface | curl |
| `GET /export/{conversation/{session_id},memories,traces,kg}` / `POST /export/dump` | `agent/export.py` | Admin-only | Export APIs for conversation, memory, traces, KG, and dump | curl |

## Discovered Browser/UI Routes

| Route | Source File | Expected UI | Test Method |
| ----- | ----------- | ----------- | ----------- |
| `GET /dashboard` | `agent/dashboard.py` | Main control panel with tabs: Overview, Chat, Compare, RAG, Pipelines, Sandbox, KG, Tools, Models | browser navigation + tab clicks |
| `GET /` | `agent/dashboard.py` + auth middleware | Same handler as dashboard, but protected when unauthenticated | browser navigation |
| `GET /status` | `main.py` | Raw JSON visible in browser | browser navigation |
| `GET /health` | `main.py` | Raw JSON visible in browser | browser navigation |

## Notes

- `_legacy` modules appeared in the broad code scan but are intentionally excluded from the active route map because they are not on the runtime hot path.
- Auth expectations are derived from the active middleware allowlist in `main.py` plus role permissions in `agent/auth.py`.
- The browser UI is a single dashboard document at `/dashboard`; the tab surfaces are client-side views backed by API calls rather than separate server-rendered routes.
- Unknowns remain for environment-dependent actions that require local provider availability or privileged auth until Gate B.2–B.6 exercise them live.
