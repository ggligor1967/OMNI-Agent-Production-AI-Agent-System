# BUG-X6 Chat JSON Contract Analysis

## Status

FIXABLE

## Current Failure

- route: `POST /chat`
- request case: authenticated request with `Content-Type: application/json` and malformed JSON body such as `{"message":`
- observed: `500 Internal Server Error` with a generic plain-text body, plus server-side `JSONDecodeError` evidence
- expected: bounded `400 Bad Request` JSON response with sanitized error content

## Current Implementation

`main.py` defines `chat_endpoint` inside `run_api()`. The handler currently begins with `data = await request.json()` and only then derives auth-scoped `user_id` and `session_id`.

That means:

- auth middleware still runs first, so missing/invalid auth requests are rejected before body parsing
- authenticated malformed JSON reaches `chat_endpoint`
- the unguarded `await request.json()` raises and bubbles into a `500`

A matching safe pattern already exists in `agent/auth.py` via `_parse_optional_json_object()`, which:

- accepts empty bodies when appropriate
- enforces JSON content types for non-empty bodies
- catches malformed JSON parsing failures
- returns sanitized `400` JSON responses without traceback leakage

## Contract Decision

Malformed JSON on `POST /chat` must return:

```text
400 Bad Request
```

The response must be sanitized and must not include traceback, exception class internals, token values, secret values, or raw request content.

For non-empty request bodies with a non-JSON content type, `POST /chat` should also return a bounded `400` JSON client error consistent with the repository's existing `/auth/bootstrap` pattern.

## Valid Behavior To Preserve

- missing auth remains `401`
- invalid auth remains `401`
- valid authenticated chat behavior remains unchanged
- `/status` remains `200`
- `/health` remains `200`

## Minimal Fix Plan

1. Add a small bounded JSON-object parsing helper in `main.py` that mirrors the repository's `/auth/bootstrap` safety pattern.
2. Use the helper only in `chat_endpoint`.
3. Return sanitized `400` JSON responses for malformed JSON and non-JSON content types.
4. Add focused regression tests for authenticated malformed JSON, sanitization, and preserved auth/status/health behavior.
