# streaming.py Quality Ratchet

## Starting Coverage

`41.43%`

## Target

`>= 60%`

## Final Coverage

`90.00%`

## Tests Added

- `tests/test_streaming_quality_ratchet.py`
- existing guard retained: `tests/test_advanced_modules.py`

## Operational Behaviors Covered

- `stream_chat_tokens()` replays user/assistant history only, appends the new prompt, streams token SSE frames, and persists the assembled assistant response
- `stream_chat_tokens()` emits sanitized error SSE frames when the LLM stream fails
- `/stream/chat` validates the required prompt and streams SSE frames from the token generator
- `/stream/chat` rejects forbidden scoped sessions with a JSON `403`
- `/stream/events` honors explicit event filters and requested `session_id` filtering
- `/stream/events` applies authenticated owner-prefix filtering when no explicit `session_id` is provided
- `/stream/stats` exposes current subscriber/history snapshots from the event bus
- `/stream/traces` replays completed recent spans, writes heartbeat comments, and streams live trace-span events

## Notes

- real defects fixed: none
- product code changes: none
- observed warning: aiohttp deprecates `response.drain()`; current behavior remains correct and non-blocking for this gate
