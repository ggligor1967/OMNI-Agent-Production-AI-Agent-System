# Local Runtime Session Log

## Status

PASS

## Startup

- command: `.venv-1\Scripts\python.exe main.py --mode api`
- PID: `21124`
- bind: `127.0.0.1`
- port: `8765`

## Core Endpoint Results

| Endpoint | Result |
|----------|--------|
| `/status` | `200 OK` — JSON status payload returned |
| `/health` | `200 OK` — JSON health payload returned |
| `/` | `401 Unauthorized` — route exists but anonymous access is denied under auth enforcement |
| `/dashboard` | `200 OK` — dashboard HTML returned with nonce-based CSP headers |

## Safety

- loopback only
- no public exposure
- no production secrets
