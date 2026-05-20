# UI Surface Inventory

## Status

PASS

## Pages / Views

| Page / Route | Found | Browser Tested | Result |
| ------------ | ----- | -------------- | ------ |
| `/dashboard` shell | Yes | No | Planned for B.3 |
| `/dashboard` Overview tab | Yes | No | Planned for B.3 |
| `/dashboard` Chat tab | Yes | No | Planned for B.3 |
| `/dashboard` Compare tab | Yes | No | Planned for B.3 |
| `/dashboard` RAG tab | Yes | No | Planned for B.3 |
| `/dashboard` Pipelines tab | Yes | No | Planned for B.3 |
| `/dashboard` Sandbox tab | Yes | No | Planned for B.3 |
| `/dashboard` KG tab | Yes | No | Planned for B.3 |
| `/dashboard` Tools tab | Yes | No | Planned for B.3 |
| `/dashboard` Models tab | Yes | No | Planned for B.3 |
| `/` protected root | Yes | No | Planned for auth/browser check |
| `/status` JSON view | Yes | No | Planned for B.3 |
| `/health` JSON view | Yes | No | Planned for B.3 |

## Buttons

| Button / Control | Location | Expected Action | Tested | Result |
| ---------------- | -------- | --------------- | ------ | ------ |
| Save API key | Dashboard header | Save `X-API-Key` into session storage for the current tab | No | Planned |
| Quick Run ADR Job Search | Overview tab | Launch dedicated ADR job-search tool call | No | Planned |
| Audit Refresh | Overview tab | Refresh audit panel from `/audit` | No | Planned |
| Send | Chat tab | POST synthetic message to `/chat` | No | Planned |
| Clear | Chat tab | Clear visible chat transcript | No | Planned |
| Load History | Chat tab | Query memories for current session | No | Planned |
| Clear History | Chat tab | Show local UI note that clear is unsupported | No | Planned |
| Run Compare | Compare tab | POST prompt/models to `/compare` | No | Planned |
| Ingest | RAG tab | POST synthetic text into `/rag/ingest` | No | Planned |
| Query | RAG tab | POST synthetic query to `/rag/query` | No | Planned |
| RAG Refresh | RAG tab | Refresh `/rag/docs` | No | Planned |
| Pipelines Refresh | Pipelines tab | Refresh pipeline list | No | Planned |
| Execute Pipeline | Pipelines tab | POST selected pipeline context | No | Planned |
| Workflows Refresh | Pipelines tab | Refresh workflow list | No | Planned |
| Run ADR Job Search | Pipelines tab | Invoke dedicated ADR search tool call | No | Planned |
| Parse | Pipelines tab | POST structured parsing request | No | Planned |
| Execute Sandbox | Sandbox tab | POST code to `/sandbox/run` | No | Planned |
| Sandbox Refresh | Sandbox tab | Refresh `/sandbox/history` | No | Planned |
| Extract | KG tab | POST text to `/kg/extract` | No | Planned |
| Search | KG tab | Query `/kg/search` | No | Planned |
| Find Path | KG tab | Query `/kg/path` | No | Planned |
| Stats | KG tab | Query `/kg/stats` | No | Planned |
| Export JSON | KG tab | Query `/kg/export` | No | Planned |
| Tools Refresh | Tools tab | Refresh `/tools` | No | Planned |
| OpenAI Schema | Tools tab | Query `/tools?format=openai` | No | Planned |
| Anthropic Schema | Tools tab | Query `/tools?format=anthropic` | No | Planned |
| Call Tool | Tools tab | POST tool invocation JSON | No | Planned |
| Analyze | Tools tab | POST image source to `/vision/analyze` | No | Planned |
| Tracing Refresh | Tools tab | Refresh `/tracing/summary` | No | Planned |
| Models Refresh | Models tab | Refresh `/models` catalog table | No | Planned |
| Personas Refresh | Models tab | Refresh `/personas` | No | Planned |
| Set Persona | Models tab | POST persona selection for session | No | Planned |
| Templates Refresh | Models tab | Refresh `/templates` | No | Planned |
| Memories Refresh | Models tab | Refresh `/memories` | No | Planned |

## Forms / Inputs

| Form / Input | Location | Expected Behavior | Tested | Result |
| ------------- | -------- | ----------------- | ------ | ------ |
| API key password input | Dashboard header | Accept local synthetic API key and keep it client-side via session storage only | No | Planned |
| Overview job-search export/verbose/output fields | Overview tab | Collect optional ADR quick-run parameters | No | Planned |
| Chat model/session/message inputs | Chat tab | Collect chat settings and synthetic prompt | No | Planned |
| Compare prompt/models inputs | Compare tab | Collect prompt and model list for compare request | No | Planned |
| RAG text/doc/source inputs | RAG tab | Collect synthetic ingest content and metadata | No | Planned |
| RAG query/top-k/filter inputs | RAG tab | Collect query parameters | No | Planned |
| Pipeline id/context inputs | Pipelines tab | Collect selected pipeline and JSON context | No | Planned |
| Pipelines ADR job-search export/verbose/output fields | Pipelines tab | Collect tool-call parameters for ADR search shortcut | No | Planned |
| Structured prompt/schema inputs | Pipelines tab | Collect prompt text and JSON schema | No | Planned |
| Sandbox language/timeout/code inputs | Sandbox tab | Collect safe code sample and runtime parameters | No | Planned |
| KG text/search/from/to inputs | KG tab | Collect graph extract/search/path parameters | No | Planned |
| Tool name/JSON params inputs | Tools tab | Collect tool invocation payload | No | Planned |
| Vision image/prompt inputs | Tools tab | Collect image source and prompt | No | Planned |
| Persona activation input | Models tab | Collect persona id for current session | No | Planned |

## Workflows

| Workflow | Entry Point | Steps | Tested | Result |
| -------- | ----------- | ----- | ------ | ------ |
| Dashboard unauthenticated load | `/dashboard` | Load shell → fetch public overview data | No | Planned |
| Protected root rejection | `/` | Load root without auth → expect `401` | No | Planned |
| Chat workflow | Chat tab | Save key → send synthetic message → inspect response/meta/history | No | Planned |
| Model compare workflow | Compare tab | Open tab → submit prompt → inspect result cards | No | Planned |
| RAG workflow | RAG tab | ingest synthetic text → query → refresh docs | No | Planned |
| Pipeline/workflow listing | Pipelines tab | refresh pipeline/workflow lists → optional safe execution | No | Planned |
| Sandbox workflow | Sandbox tab | submit safe code → inspect history | No | Planned |
| KG workflow | KG tab | extract → search/path/stats/export | No | Planned |
| Tool schema/call workflow | Tools tab | refresh tools → inspect schemas → safe call where allowed | No | Planned |
| Model/persona/template/memory workflow | Models tab | refresh catalog/personas/templates/memories → optional set persona | No | Planned |
