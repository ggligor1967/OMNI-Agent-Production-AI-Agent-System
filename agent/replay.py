"""
OMNI AGENT - Session Replay
Record conversations and replay them against the current agent to detect
regressions, compare model outputs, and run automated QA on prompt changes.

Features:
- Recording: snapshot any session (messages + config + metadata)
- Replay: feed recorded messages through the current agent/model
- Comparison: diff replay outputs vs. original responses
- Regression detection: flag significant divergence from baseline
- Batch replay: run multiple recordings in parallel
- Assertions: define pass/fail checks on replay outputs
- Coverage: track which prompts/scenarios have been replayed
- Report: structured HTML/Markdown report of replay results
- SQLite: persistent recording storage with query by tag/date/model
- REST API: record, replay, compare, list recordings
"""
import re
import time
import uuid
import json
import asyncio
import sqlite3
import difflib
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# RECORDING
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RecordedTurn:
    """A single turn in a recorded conversation."""
    index: int
    role: str
    content: str
    model: str = ""
    tokens: int = 0
    latency_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "index": self.index, "role": self.role,
            "content": self.content, "model": self.model,
            "tokens": self.tokens, "latency_ms": self.latency_ms,
            "timestamp": self.timestamp, "metadata": self.metadata,
        }


@dataclass
class Recording:
    """A complete recorded session snapshot."""
    id: str
    name: str
    turns: List[RecordedTurn]
    system_prompt: str = ""
    model: str = ""
    persona: str = ""
    tags: List[str] = field(default_factory=list)
    description: str = ""
    session_id: str = ""
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict = field(default_factory=dict)

    @property
    def user_turns(self) -> List[RecordedTurn]:
        return [t for t in self.turns if t.role == "user"]

    @property
    def assistant_turns(self) -> List[RecordedTurn]:
        return [t for t in self.turns if t.role == "assistant"]

    def to_messages(self) -> List[Dict]:
        """Convert to LLM-ready message list."""
        return [{"role": t.role, "content": t.content} for t in self.turns
                if t.role in ("user", "assistant", "system")]

    def to_dict(self, include_turns: bool = True) -> Dict:
        d = {
            "id": self.id, "name": self.name,
            "system_prompt": self.system_prompt,
            "model": self.model, "persona": self.persona,
            "tags": self.tags, "description": self.description,
            "session_id": self.session_id,
            "recorded_at": self.recorded_at,
            "turn_count": len(self.turns),
            "metadata": self.metadata,
        }
        if include_turns:
            d["turns"] = [t.to_dict() for t in self.turns]
        return d


# ══════════════════════════════════════════════════════════════════════════════
# REPLAY RESULT
# ══════════════════════════════════════════════════════════════════════════════

class AssertionStatus(str, Enum):
    PASS    = "pass"
    FAIL    = "fail"
    SKIP    = "skip"
    ERROR   = "error"


@dataclass
class TurnComparison:
    """Comparison of a single turn's output between recording and replay."""
    turn_index: int
    original: str
    replayed: str
    similarity: float       # 0.0 – 1.0 (sequence matcher ratio)
    diff: str               # unified diff
    assertions: Dict[str, AssertionStatus] = field(default_factory=dict)

    @property
    def has_regression(self) -> bool:
        return self.similarity < 0.5 or any(
            v == AssertionStatus.FAIL for v in self.assertions.values()
        )

    def to_dict(self) -> Dict:
        return {
            "turn_index": self.turn_index,
            "similarity": round(self.similarity, 4),
            "has_regression": self.has_regression,
            "assertions": {k: v for k, v in self.assertions.items()},
            "diff": self.diff,
        }


@dataclass
class ReplayResult:
    """Result of replaying a recording against the current agent."""
    id: str
    recording_id: str
    recording_name: str
    model_used: str
    comparisons: List[TurnComparison]
    success: bool
    regressions: int
    total_turns: int
    avg_similarity: float
    duration_ms: float
    replayed_at: float = field(default_factory=time.time)
    error: str = ""

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "recording_id": self.recording_id,
            "recording_name": self.recording_name,
            "model_used": self.model_used,
            "success": self.success,
            "regressions": self.regressions,
            "total_turns": self.total_turns,
            "avg_similarity": round(self.avg_similarity, 4),
            "duration_ms": round(self.duration_ms, 1),
            "replayed_at": self.replayed_at,
            "error": self.error,
            "comparisons": [c.to_dict() for c in self.comparisons],
        }

    def markdown_report(self) -> str:
        lines = [
            f"# Replay Report: {self.recording_name}",
            f"**Model:** {self.model_used}  |  **Regressions:** {self.regressions}/{self.total_turns}  "
            f"|  **Avg Similarity:** {self.avg_similarity:.1%}  "
            f"|  **Duration:** {self.duration_ms:.0f}ms",
            "",
        ]
        for comp in self.comparisons:
            status = "🔴 REGRESSION" if comp.has_regression else "✅ OK"
            lines.append(f"## Turn {comp.turn_index + 1} — {status}")
            lines.append(f"Similarity: {comp.similarity:.1%}")
            if comp.has_regression and comp.diff:
                lines.append("```diff")
                lines.append(comp.diff[:1000])
                lines.append("```")
            if comp.assertions:
                for name, status_val in comp.assertions.items():
                    icon = "✅" if status_val == AssertionStatus.PASS else "❌"
                    lines.append(f"- {icon} {name}: {status_val}")
            lines.append("")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# ASSERTION LIBRARY
# ══════════════════════════════════════════════════════════════════════════════

class Assertions:
    """Built-in assertion functions for replay validation."""

    @staticmethod
    def contains(substring: str):
        """Assert output contains a substring."""
        def check(output: str) -> bool:
            return substring.lower() in output.lower()
        check.__name__ = f"contains({substring!r})"
        return check

    @staticmethod
    def not_contains(substring: str):
        def check(output: str) -> bool:
            return substring.lower() not in output.lower()
        check.__name__ = f"not_contains({substring!r})"
        return check

    @staticmethod
    def matches_regex(pattern: str):
        compiled = re.compile(pattern, re.IGNORECASE)
        def check(output: str) -> bool:
            return bool(compiled.search(output))
        check.__name__ = f"matches({pattern!r})"
        return check

    @staticmethod
    def min_length(n: int):
        def check(output: str) -> bool:
            return len(output) >= n
        check.__name__ = f"min_length({n})"
        return check

    @staticmethod
    def max_length(n: int):
        def check(output: str) -> bool:
            return len(output) <= n
        check.__name__ = f"max_length({n})"
        return check

    @staticmethod
    def similarity_above(threshold: float, original: str):
        def check(output: str) -> bool:
            ratio = difflib.SequenceMatcher(None, original, output).ratio()
            return ratio >= threshold
        check.__name__ = f"similarity≥{threshold:.0%}"
        return check

    @staticmethod
    def no_refusal():
        REFUSAL_PHRASES = [
            "i cannot", "i can't", "i'm unable", "i am unable",
            "i won't", "i will not", "as an ai", "i don't have the ability",
        ]
        def check(output: str) -> bool:
            lower = output.lower()
            return not any(p in lower for p in REFUSAL_PHRASES)
        check.__name__ = "no_refusal"
        return check


# ══════════════════════════════════════════════════════════════════════════════
# RECORDING STORE
# ══════════════════════════════════════════════════════════════════════════════

class RecordingStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS recordings (
                    id           TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    turns        TEXT NOT NULL,
                    system_prompt TEXT DEFAULT '',
                    model        TEXT DEFAULT '',
                    persona      TEXT DEFAULT '',
                    tags         TEXT DEFAULT '[]',
                    description  TEXT DEFAULT '',
                    session_id   TEXT DEFAULT '',
                    recorded_at  REAL,
                    metadata     TEXT DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS replay_results (
                    id              TEXT PRIMARY KEY,
                    recording_id    TEXT NOT NULL,
                    recording_name  TEXT,
                    model_used      TEXT,
                    result_json     TEXT,
                    success         INTEGER,
                    regressions     INTEGER,
                    avg_similarity  REAL,
                    duration_ms     REAL,
                    replayed_at     REAL
                );
                CREATE INDEX IF NOT EXISTS idx_rec_tags ON recordings(tags);
                CREATE INDEX IF NOT EXISTS idx_rep_rec ON replay_results(recording_id);
            """)

    def save_recording(self, rec: Recording):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO recordings
                (id,name,turns,system_prompt,model,persona,tags,
                 description,session_id,recorded_at,metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                rec.id, rec.name,
                json.dumps([t.to_dict() for t in rec.turns]),
                rec.system_prompt, rec.model, rec.persona,
                json.dumps(rec.tags), rec.description,
                rec.session_id, rec.recorded_at,
                json.dumps(rec.metadata),
            ))

    def get_recording(self, rec_id: str) -> Optional[Recording]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM recordings WHERE id=?",
                            (rec_id,)).fetchone()
        return self._row_to_recording(row) if row else None

    def list_recordings(self, tag: str = None, model: str = None,
                        limit: int = 50) -> List[Recording]:
        conditions, params = [], []
        if tag:
            conditions.append("tags LIKE ?"); params.append(f'%"{tag}"%')
        if model:
            conditions.append("model=?"); params.append(model)
        q = "SELECT * FROM recordings"
        if conditions:
            q += " WHERE " + " AND ".join(conditions)
        q += " ORDER BY recorded_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [self._row_to_recording(r) for r in rows]

    def delete_recording(self, rec_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM recordings WHERE id=?", (rec_id,))
        return cur.rowcount > 0

    def save_result(self, result: ReplayResult):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO replay_results
                (id,recording_id,recording_name,model_used,result_json,
                 success,regressions,avg_similarity,duration_ms,replayed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                result.id, result.recording_id, result.recording_name,
                result.model_used,
                json.dumps(result.to_dict()),
                1 if result.success else 0,
                result.regressions,
                result.avg_similarity,
                result.duration_ms,
                result.replayed_at,
            ))

    def list_results(self, recording_id: str = None,
                     limit: int = 20) -> List[Dict]:
        with self._conn() as c:
            if recording_id:
                rows = c.execute("""
                    SELECT * FROM replay_results WHERE recording_id=?
                    ORDER BY replayed_at DESC LIMIT ?
                """, (recording_id, limit)).fetchall()
            else:
                rows = c.execute("""
                    SELECT * FROM replay_results
                    ORDER BY replayed_at DESC LIMIT ?
                """, (limit,)).fetchall()
        return [json.loads(r["result_json"]) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            total_recs = c.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
            total_replays = c.execute("SELECT COUNT(*) FROM replay_results").fetchone()[0]
            regression_rate = c.execute("""
                SELECT AVG(CASE WHEN regressions > 0 THEN 1.0 ELSE 0.0 END)
                FROM replay_results
            """).fetchone()[0] or 0.0
        return {
            "total_recordings": total_recs,
            "total_replays": total_replays,
            "regression_rate": round(regression_rate, 4),
        }

    def _row_to_recording(self, row) -> Recording:
        turns_data = json.loads(row["turns"] or "[]")
        turns = [
            RecordedTurn(
                index=t["index"], role=t["role"], content=t["content"],
                model=t.get("model", ""), tokens=t.get("tokens", 0),
                latency_ms=t.get("latency_ms", 0.0),
                timestamp=t.get("timestamp", 0.0),
                metadata=t.get("metadata", {}),
            )
            for t in turns_data
        ]
        return Recording(
            id=row["id"], name=row["name"], turns=turns,
            system_prompt=row["system_prompt"] or "",
            model=row["model"] or "", persona=row["persona"] or "",
            tags=json.loads(row["tags"] or "[]"),
            description=row["description"] or "",
            session_id=row["session_id"] or "",
            recorded_at=row["recorded_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )


# ══════════════════════════════════════════════════════════════════════════════
# REPLAY ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class ReplayEngine:
    """
    Session replay for regression testing and QA.

    Usage:
        engine = ReplayEngine(agent_fn=my_agent.chat)

        # Record a session
        rec = engine.record_from_messages(
            messages=[
                {"role": "user", "content": "What is Python?"},
                {"role": "assistant", "content": "Python is a..."},
            ],
            name="python-qa-baseline",
            model="claude-3-5-sonnet",
            tags=["baseline", "qa"],
        )

        # Replay and compare
        result = await engine.replay(rec.id,
                                     model="claude-3-5-haiku",
                                     assertions=[Assertions.no_refusal()])

        print(result.markdown_report())
        if result.regressions > 0:
            raise Exception("Regression detected!")
    """

    def __init__(self, agent_fn: Callable = None,
                 db_path: str = "data/replay.db",
                 regression_threshold: float = 0.5):
        self._agent_fn = agent_fn
        self._store = RecordingStore(db_path)
        self._regression_threshold = regression_threshold

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_from_messages(self, messages: List[Dict],
                              name: str,
                              model: str = "",
                              system_prompt: str = "",
                              persona: str = "",
                              tags: List[str] = None,
                              description: str = "",
                              session_id: str = "",
                              metadata: Dict = None) -> Recording:
        turns = []
        for i, msg in enumerate(messages):
            turns.append(RecordedTurn(
                index=i,
                role=msg.get("role", "user"),
                content=str(msg.get("content", "")),
                model=model if msg.get("role") == "assistant" else "",
            ))
        rec = Recording(
            id=str(uuid.uuid4())[:12],
            name=name, turns=turns,
            system_prompt=system_prompt, model=model, persona=persona,
            tags=tags or [], description=description,
            session_id=session_id, metadata=metadata or {},
        )
        self._store.save_recording(rec)
        logger.info(f"Recording saved: '{name}' id={rec.id} turns={len(turns)}")
        return rec

    def get_recording(self, rec_id: str) -> Optional[Recording]:
        return self._store.get_recording(rec_id)

    def list_recordings(self, **kwargs) -> List[Recording]:
        return self._store.list_recordings(**kwargs)

    def delete_recording(self, rec_id: str) -> bool:
        return self._store.delete_recording(rec_id)

    # ── Replay ────────────────────────────────────────────────────────────────

    async def replay(self, recording_id: str,
                     model: str = "",
                     system_prompt: str = None,
                     assertions: List[Callable] = None,
                     timeout_s: float = 60.0) -> ReplayResult:
        """
        Replay a recording against the current agent_fn.
        Compares each assistant turn against the recorded original.
        """
        rec = self._store.get_recording(recording_id)
        if not rec:
            return ReplayResult(
                id=str(uuid.uuid4())[:10],
                recording_id=recording_id,
                recording_name="",
                model_used=model,
                comparisons=[], success=False,
                regressions=0, total_turns=0,
                avg_similarity=0.0, duration_ms=0.0,
                error="Recording not found",
            )

        start = time.time()
        comparisons: List[TurnComparison] = []
        sys_prompt = system_prompt if system_prompt is not None else rec.system_prompt

        # Build replay conversation turn by turn
        history: List[Dict] = []
        if sys_prompt:
            history.append({"role": "system", "content": sys_prompt})

        user_turns = [(i, t) for i, t in enumerate(rec.turns)
                      if t.role == "user"]

        for turn_i, (orig_idx, user_turn) in enumerate(user_turns):
            history.append({"role": "user", "content": user_turn.content})

            # Find the original assistant response following this user turn
            original_response = ""
            for j in range(orig_idx + 1, len(rec.turns)):
                if rec.turns[j].role == "assistant":
                    original_response = rec.turns[j].content
                    break

            # Call the agent
            replayed_response = ""
            try:
                if self._agent_fn:
                    replayed_response = await asyncio.wait_for(
                        self._invoke_agent(history, model, sys_prompt),
                        timeout=timeout_s,
                    )
                else:
                    # No agent fn: use original as replay (useful for testing)
                    replayed_response = original_response
            except asyncio.TimeoutError:
                replayed_response = "[TIMEOUT]"
            except Exception as e:
                replayed_response = f"[ERROR: {e}]"

            # Add to history for next turn
            history.append({"role": "assistant", "content": replayed_response})

            # Compare
            similarity = difflib.SequenceMatcher(
                None, original_response, replayed_response
            ).ratio()

            diff = "\n".join(difflib.unified_diff(
                original_response.splitlines(),
                replayed_response.splitlines(),
                fromfile="original",
                tofile="replayed",
                lineterm="",
            ))

            # Run assertions
            assertion_results: Dict[str, AssertionStatus] = {}
            for assertion_fn in (assertions or []):
                name = getattr(assertion_fn, "__name__", str(assertion_fn))
                try:
                    passed = assertion_fn(replayed_response)
                    assertion_results[name] = (AssertionStatus.PASS
                                               if passed else AssertionStatus.FAIL)
                except Exception as e:
                    assertion_results[name] = AssertionStatus.ERROR

            comparisons.append(TurnComparison(
                turn_index=turn_i,
                original=original_response,
                replayed=replayed_response,
                similarity=similarity,
                diff=diff,
                assertions=assertion_results,
            ))

        regressions = sum(1 for c in comparisons if c.has_regression)
        avg_sim = (sum(c.similarity for c in comparisons) / len(comparisons)
                   if comparisons else 0.0)

        result = ReplayResult(
            id=str(uuid.uuid4())[:10],
            recording_id=recording_id,
            recording_name=rec.name,
            model_used=model or rec.model,
            comparisons=comparisons,
            success=regressions == 0,
            regressions=regressions,
            total_turns=len(comparisons),
            avg_similarity=avg_sim,
            duration_ms=(time.time() - start) * 1000,
        )
        self._store.save_result(result)
        logger.info(f"Replay complete: '{rec.name}' "
                   f"regressions={regressions}/{len(comparisons)} "
                   f"avg_sim={avg_sim:.1%}")
        return result

    async def _invoke_agent(self, history: List[Dict],
                             model: str, system_prompt: str) -> str:
        """Invoke the configured agent function."""
        if asyncio.iscoroutinefunction(self._agent_fn):
            response = await self._agent_fn(
                messages=history, model=model, system_prompt=system_prompt
            )
        else:
            response = self._agent_fn(
                messages=history, model=model, system_prompt=system_prompt
            )
        if isinstance(response, dict):
            return response.get("content", str(response))
        return str(response)

    async def batch_replay(self, recording_ids: List[str],
                           model: str = "",
                           assertions: List[Callable] = None,
                           max_concurrent: int = 4) -> List[ReplayResult]:
        """Replay multiple recordings in parallel."""
        sem = asyncio.Semaphore(max_concurrent)
        async def _run(rid):
            async with sem:
                return await self.replay(rid, model=model, assertions=assertions)
        return await asyncio.gather(*[_run(rid) for rid in recording_ids])

    def list_results(self, recording_id: str = None, limit: int = 20) -> List[Dict]:
        return self._store.list_results(recording_id, limit)

    def stats(self) -> Dict:
        return self._store.stats()

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def list_recordings_ep(request):
            tag = request.rel_url.query.get("tag")
            model = request.rel_url.query.get("model")
            limit = int(request.rel_url.query.get("limit", 50))
            recs = self.list_recordings(tag=tag, model=model, limit=limit)
            return web.json_response(
                {"recordings": [r.to_dict(include_turns=False) for r in recs]}
            )

        async def create_recording_ep(request):
            data = await request.json()
            rec = self.record_from_messages(
                messages=data["messages"],
                name=data["name"],
                model=data.get("model", ""),
                system_prompt=data.get("system_prompt", ""),
                persona=data.get("persona", ""),
                tags=data.get("tags", []),
                description=data.get("description", ""),
                session_id=data.get("session_id", ""),
            )
            return web.json_response(rec.to_dict(include_turns=False), status=201)

        async def get_recording_ep(request):
            rec = self.get_recording(request.match_info["id"])
            if not rec:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response(rec.to_dict())

        async def delete_recording_ep(request):
            ok = self.delete_recording(request.match_info["id"])
            return web.json_response({"deleted": ok})

        async def replay_ep(request):
            rec_id = request.match_info["id"]
            data = await request.json() if request.content_length else {}
            result = await self.replay(
                rec_id,
                model=data.get("model", ""),
                system_prompt=data.get("system_prompt"),
            )
            return web.json_response(result.to_dict())

        async def report_ep(request):
            rec_id = request.match_info["id"]
            results = self.list_results(rec_id, limit=1)
            if not results:
                return web.json_response({"error": "no results"}, status=404)
            return web.Response(
                text=results[0].get("comparisons", "No report"),
                content_type="text/plain",
            )

        async def stats_ep(request):
            return web.json_response(self.stats())

        p = f"{prefix}/replay"
        app.router.add_get(   f"{p}/recordings",          list_recordings_ep)
        app.router.add_post(  f"{p}/recordings",          create_recording_ep)
        app.router.add_get(   f"{p}/recordings/{{id}}",   get_recording_ep)
        app.router.add_delete(f"{p}/recordings/{{id}}",   delete_recording_ep)
        app.router.add_post(  f"{p}/recordings/{{id}}/replay", replay_ep)
        app.router.add_get(   f"{p}/recordings/{{id}}/report", report_ep)
        app.router.add_get(   f"{p}/stats",               stats_ep)
        logger.info(f"Replay engine API routes registered at {prefix}/replay/")
