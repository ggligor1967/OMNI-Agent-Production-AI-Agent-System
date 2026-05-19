from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.performance.local_fixture import start_local_fixture
from tools.performance.reporting import (
    RequestObservation,
    build_summary,
    observations_to_dicts,
    write_summary_outputs,
)

SNAPSHOT_DIR = PROJECT_ROOT / "snapshot-phase-3-3"
DEFAULT_SMOKE_DURATION = 6.0
DEFAULT_BASELINE_DURATION = 12.0
DEFAULT_SMOKE_USERS = 2
DEFAULT_BASELINE_USERS = 5
DEFAULT_SMOKE_SPAWN_RATE = 1.0
DEFAULT_BASELINE_SPAWN_RATE = 1.0
DEFAULT_COMMAND_TARGET = "http://127.0.0.1:8000"


@dataclass(frozen=True)
class RouteScenario:
    name: str
    method: str
    path: str
    requires_auth: bool


@dataclass(frozen=True)
class HarnessConfig:
    mode: str
    users: int
    spawn_rate: float
    duration_seconds: float
    base_url: str | None
    api_key: str | None
    raw_log_path: Path
    summary_json_path: Path
    summary_md_path: Path


SCENARIOS = (
    RouteScenario("status", "GET", "/status", False),
    RouteScenario("chat", "POST", "/chat", True),
)


def default_duration(mode: str) -> float:
    return DEFAULT_SMOKE_DURATION if mode == "smoke" else DEFAULT_BASELINE_DURATION


def default_users(mode: str) -> int:
    return DEFAULT_SMOKE_USERS if mode == "smoke" else DEFAULT_BASELINE_USERS


def default_spawn_rate(mode: str) -> float:
    return DEFAULT_SMOKE_SPAWN_RATE if mode == "smoke" else DEFAULT_BASELINE_SPAWN_RATE


def is_loopback_target(base_url: str) -> bool:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def build_command(mode: str, base_url: str = DEFAULT_COMMAND_TARGET) -> list[str]:
    users = default_users(mode)
    duration = int(default_duration(mode))
    spawn_rate = int(default_spawn_rate(mode))
    flag = "--smoke" if mode == "smoke" else "--baseline"
    return [
        "python",
        "tools/performance/run_local_baseline.py",
        flag,
        "--base-url",
        base_url,
        "--users",
        str(users),
        "--spawn-rate",
        str(spawn_rate),
        "--duration-seconds",
        str(duration),
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local Phase 3.3 performance baseline.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true", help="Run the short smoke workload.")
    mode.add_argument("--baseline", action="store_true", help="Run the full local baseline workload.")
    parser.add_argument("--base-url", help="Optional loopback base URL. When omitted, an in-process fixture is started.")
    parser.add_argument("--api-key", help="Optional API key for an existing loopback target.")
    parser.add_argument("--users", type=int, help="Concurrent user count override.")
    parser.add_argument("--spawn-rate", type=float, help="User spawn rate per second override.")
    parser.add_argument("--duration-seconds", type=float, help="Duration override in seconds.")
    parser.add_argument("--raw-log", type=Path, help="Optional raw log output path.")
    parser.add_argument("--summary-json", type=Path, help="Optional summary JSON output path.")
    parser.add_argument("--summary-md", type=Path, help="Optional summary Markdown output path.")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> HarnessConfig:
    mode = "smoke" if args.smoke else "baseline"
    base_url = args.base_url
    if base_url and not is_loopback_target(base_url):
        raise ValueError("Performance harness only allows loopback targets by default")
    if base_url and not args.api_key:
        raise ValueError("--api-key is required when targeting an existing loopback /chat endpoint")

    raw_log_path = args.raw_log or SNAPSHOT_DIR / f"performance_{mode}_raw.log"
    summary_json_path = args.summary_json or SNAPSHOT_DIR / f"performance_{mode}_summary.json"
    summary_md_path = args.summary_md or SNAPSHOT_DIR / f"performance_{mode}_summary.md"

    return HarnessConfig(
        mode=mode,
        users=args.users or default_users(mode),
        spawn_rate=args.spawn_rate or default_spawn_rate(mode),
        duration_seconds=args.duration_seconds or default_duration(mode),
        base_url=base_url,
        api_key=args.api_key,
        raw_log_path=raw_log_path,
        summary_json_path=summary_json_path,
        summary_md_path=summary_md_path,
    )


def _chat_payload(user_index: int, iteration: int) -> dict[str, Any]:
    return {
        "message": f"perf ping {user_index}:{iteration}",
        "session_id": f"perf-{user_index}",
    }


async def _issue_request(
    session: ClientSession,
    *,
    base_url: str,
    api_key: str,
    scenario: RouteScenario,
    user_index: int,
    iteration: int,
) -> RequestObservation:
    url = f"{base_url}{scenario.path}"
    headers: dict[str, str] = {}
    if scenario.requires_auth:
        headers["X-API-Key"] = api_key

    started = time.perf_counter()
    status_code = 0
    error = ""
    ok = False
    try:
        if scenario.method == "GET":
            async with session.get(url, headers=headers) as response:
                status_code = response.status
                await response.read()
        else:
            async with session.post(url, headers=headers, json=_chat_payload(user_index, iteration)) as response:
                status_code = response.status
                await response.read()
        ok = 200 <= status_code < 400
        if not ok:
            error = f"http_{status_code}"
    except Exception as exc:  # pragma: no cover - exercised by smoke execution if fixture breaks
        error = type(exc).__name__
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    return RequestObservation(
        route=scenario.path,
        method=scenario.method,
        status_code=status_code,
        latency_ms=elapsed_ms,
        ok=ok,
        error=error,
    )


async def _user_worker(
    *,
    user_index: int,
    session: ClientSession,
    base_url: str,
    api_key: str,
    duration_seconds: float,
    spawn_rate: float,
    observations: list[RequestObservation],
) -> None:
    await asyncio.sleep(max(0.0, user_index / max(spawn_rate, 0.001)))
    deadline = time.perf_counter() + duration_seconds
    iteration = 0
    while time.perf_counter() < deadline:
        scenario = SCENARIOS[iteration % len(SCENARIOS)]
        observation = await _issue_request(
            session,
            base_url=base_url,
            api_key=api_key,
            scenario=scenario,
            user_index=user_index,
            iteration=iteration,
        )
        observations.append(observation)
        iteration += 1


def _render_raw_log(
    *,
    config: HarnessConfig,
    target: str,
    observations: list[RequestObservation],
) -> str:
    lines = [
        f"mode={config.mode}",
        f"target={target}",
        f"users={config.users}",
        f"spawn_rate={config.spawn_rate}",
        f"duration_seconds={config.duration_seconds}",
        "events:",
    ]
    for observation in observations:
        lines.append(
            "route={route} method={method} status={status_code} ok={ok} latency_ms={latency_ms} error={error}".format(
                route=observation.route,
                method=observation.method,
                status_code=observation.status_code,
                ok=str(observation.ok).lower(),
                latency_ms=observation.latency_ms,
                error=observation.error or "-",
            )
        )
    return "\n".join(lines) + "\n"


async def run_harness(config: HarnessConfig) -> dict[str, Any]:
    timeout = ClientTimeout(total=5)
    observations: list[RequestObservation] = []

    if config.base_url:
        base_url = config.base_url
        api_key = config.api_key or ""
        async with ClientSession(timeout=timeout) as session:
            await asyncio.gather(
                *[
                    _user_worker(
                        user_index=index,
                        session=session,
                        base_url=base_url,
                        api_key=api_key,
                        duration_seconds=config.duration_seconds,
                        spawn_rate=config.spawn_rate,
                        observations=observations,
                    )
                    for index in range(config.users)
                ]
            )
    else:
        async with start_local_fixture() as fixture:
            base_url = fixture.base_url
            api_key = fixture.api_key
            async with ClientSession(timeout=timeout) as session:
                await asyncio.gather(
                    *[
                        _user_worker(
                            user_index=index,
                            session=session,
                            base_url=base_url,
                            api_key=api_key,
                            duration_seconds=config.duration_seconds,
                            spawn_rate=config.spawn_rate,
                            observations=observations,
                        )
                        for index in range(config.users)
                    ]
                )

    summary = build_summary(
        observations,
        mode=config.mode,
        target=base_url,
        duration_seconds=config.duration_seconds,
    )
    write_summary_outputs(
        summary,
        json_path=config.summary_json_path,
        markdown_path=config.summary_md_path,
    )
    config.raw_log_path.parent.mkdir(parents=True, exist_ok=True)
    config.raw_log_path.write_text(
        _render_raw_log(config=config, target=base_url, observations=observations),
        encoding="utf-8",
    )
    return {
        "summary": summary,
        "observations": observations_to_dicts(observations),
        "raw_log_path": str(config.raw_log_path),
        "summary_json_path": str(config.summary_json_path),
        "summary_md_path": str(config.summary_md_path),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = build_config(args)
    result = asyncio.run(run_harness(config))
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    print(f"Wrote raw log to {result['raw_log_path']}")
    print(f"Wrote summary JSON to {result['summary_json_path']}")
    print(f"Wrote summary Markdown to {result['summary_md_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
