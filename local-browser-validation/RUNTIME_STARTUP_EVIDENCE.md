# Runtime Startup Evidence

## Status

PASS

## Startup Command

`./.venv-1/Scripts/python.exe main.py --mode api`

Environment overrides used for this local-only run:

- `SECRET_KEY=[synthetic local test value]`
- `AUTH_ENFORCE=true`
- `AUTH_BOOTSTRAP_TOKEN=[redacted synthetic local token]`
- `API_HOST=127.0.0.1`
- `API_PORT=8765`
- `API_FALLBACK_PORTS=8766`
- `PYTHONIOENCODING=utf-8`
- `PYTHONUTF8=1`

## Bind Address

127.0.0.1 only.

## Endpoint Probe Results

| Endpoint | Result |
| -------- | ------ |
| `/status` | `200 OK` with JSON status/health payload |
| `/health` | `200 OK` with JSON payload matching `/status` |
| `/` | `401 Unauthorized` with JSON auth error body |

## Notes

- Local runtime process identified as PID `17608` during the B.2 capture.
- Socket probe confirmed `127.0.0.1:8765` was open.
- `b2_server_stdout.log` captured startup/runtime logs; `b2_server_stderr.log` remained empty during startup capture.
- No public exposure, tunnel, or non-loopback bind was used.
