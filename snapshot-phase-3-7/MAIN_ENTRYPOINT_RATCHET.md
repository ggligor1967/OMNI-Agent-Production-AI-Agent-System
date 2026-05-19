# main.py Quality Ratchet

## Starting Coverage

`18.73%`

## Target

`>= 30%`

## Final Coverage

`86.93%`

## Tests Added

- `tests/test_main_entrypoint_quality_ratchet.py`
- existing startup coverage retained: `tests/test_startup_security.py`
- existing tracing coverage retained: `tests/test_http_tracing.py`

## Runtime Behaviors Covered

- display-host normalization for safe dashboard URLs
- API port selection with duplicate elimination, SEARXNG conflict avoidance, and no-usable-port failure
- prompt/default and bootstrap token helper behavior
- API app wiring via `run_api()` without long-running server side effects
- public-path guard wiring for `/status`, `/dashboard`, `/cache/stats`, `/audit`, and `/favicon.ico`
- chat endpoint validation, session ownership enforcement, and model override persistence
- route preview, cache, audit, structured-output, and persona session endpoints
- models, compare, RAG, pipelines, templates, workflows, tools, tracing, evaluation, knowledge-graph, sandbox, vision, and notifications surfaces
- fallback-port behavior when the preferred API port is unavailable
- create-admin failure path exits with status `1` without leaking secret values
- CLI mode dispatch starts, runs, and stops the agent cleanly without leaving background services behind

## Notes

- real defects fixed: none
- production helper extraction was not required; the API surface was exercised by capturing the constructed aiohttp app and testing it directly
