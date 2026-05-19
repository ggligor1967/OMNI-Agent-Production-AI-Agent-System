# ollama_client.py Quality Ratchet

## Starting Coverage

`33.73%`

## Target

`>= 55%`

## Final Coverage

`100.00%`

## Tests Added

- `tests/test_ollama_client_quality_ratchet.py`
- existing guards retained: `tests/test_models.py`, `tests/test_model_routing_tracing.py`, `tests/test_silent_exception_sweep.py`

## Operational Behaviors Covered

- client session creation, reuse, close, and recreation after closure
- `/api/tags` health probe and model listing behavior
- `/api/pull` streaming update parsing, including blank-line skipping
- `/api/chat` payload construction with explicit model, temperature, system prompt, and tool specs
- non-200 chat responses raise sanitized runtime errors
- streaming chat yields only non-empty content chunks and stops on `done`
- `/api/generate` payload maps `max_tokens` to `num_predict`
- `/api/embed` returns the first embedding and safely handles empty embedding payloads
- `embed_batch()` delegates across inputs without network access
- `build_tool_spec()` emits the expected Ollama-compatible function schema

## Notes

- real defects fixed: none
- product code changes: none
