# Workflow / Template / Tool Surface Exploration

## Status

PASS

## Surfaces Found

| Surface | Route / Entry Point | Safe To Test | Tested | Result |
| ------- | ------------------- | ------------ | ------ | ------ |
| Workflows list | `GET /workflows` | Yes | Yes | `200 OK` |
| Pipelines list | `GET /pipelines` | Yes | Yes | `200 OK` |
| Templates list | `GET /templates` | Yes | Yes | `200 OK` |
| Tools list | `GET /tools` | Yes | Yes | `200 OK` |
| Tool schema export (OpenAI) | `GET /tools?format=openai` | Yes | Yes | `200 OK` |
| Tool schema export (Anthropic) | `GET /tools?format=anthropic` | Yes | Yes | `200 OK` |
| Personas list | `GET /personas` | Yes | Yes | `200 OK` |
| Sandbox history | `GET /sandbox/history` | Yes | Yes | `200 OK` |
| Template render | `POST /templates/agent_plan/render` | Yes | Yes | `200 OK` |
| Safe tool call | `POST /tools/call` with `analyze_text` | Yes | Yes | `200 OK` |
| Safe workflow run | `POST /workflows/analyze_text/run` | Yes | Yes | `200 OK` |

## Workflows Exercised

| Workflow | Steps | Expected | Observed | Status |
| -------- | ----- | -------- | -------- | ------ |
| `analyze_text` workflow | `entities` → `sentiment` → `echo_done` | Should complete safely with synthetic text input and return a successful run object | `200 OK`; run finished successfully with three successful steps and total duration about `55.8s` | PASS |
| `agent_plan` template render | Variable substitution into template message list | Should render prompt messages without mutating system state | `200 OK`; returned rendered `messages` payload | PASS |
| `analyze_text` tool call | Safe analysis tool with synthetic text input | Should return structured text-analysis output without side effects | `200 OK`; returned word count, keywords, and neutral sentiment summary | PASS |

## Not Tested

| Item | Reason |
| ---- | ------ |
| `POST /workflows/research/run` | Uses web-search/tool steps and is not a minimal local-only safe probe for this pass |
| `POST /pipelines/research/run` | Can trigger external research/tool behavior beyond the safe exploratory scope |
| `POST /pipelines/code_gen/run` | May generate or transform code and is outside the minimal safe surface check |
| `POST /pipelines/job_search_tank_adr_improved/run` | Produces export/report side effects and is not required for this local-only safe surface pass |
| `POST /sandbox/run` | Intentionally skipped to avoid executing arbitrary code during this non-destructive exploratory lane |

## Bugs

none
