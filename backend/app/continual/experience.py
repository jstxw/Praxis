"""Experience store — DESIGN §1 data model behind one interface.

Tables: ``memory_versions``, ``harnesses``, ``trajectories`` (+
``trajectory_steps``, the per-step checkpoints), ``task_evaluations``,
``failure_patterns``, ``mutations``, ``comparisons``, plus the
bookkeeping the loop needs: ``reflections``, ``projects``,
``harness_events``.

Two backings, one query text (ARCHITECTURE §1a: "no SQL that only one
backend supports"): SQLite for local mode, Postgres for service mode.
Portability choices, deliberately boring:

- ids are TEXT (uuid hex), arrays and JSON documents are TEXT holding
  JSON — DESIGN's ``UUID[]``/``JSONB`` columns are stored portably;
- booleans are INTEGER 0/1, timestamps DOUBLE PRECISION epoch seconds;
- placeholders are written ``?`` and rewritten to ``%s`` for psycopg.

Extensions to DESIGN §1, each for a stated reason:

- ``trajectories.memory_version`` — memory is pinned per comparison
  (DESIGN §6 "Fixed"), so every trajectory must record the version it
  ran with or the freeze is unverifiable.
- ``trajectories.agent`` — synthetic-agent trajectories must never be
  mixed with real-agent ones in any reported number (honesty rule 2).
- ``task_evaluations.voided`` / ``audit`` / ``isolation`` — the audit
  verdict and the isolation boundary the verifier ran under.
- ``comparisons.details`` / ``experiment`` — full per-metric statistics
  and gate reasons, so ``harness status`` can show evidence, not a flag.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from app.continual.harness_object import (
    HarnessSnapshot,
    MemoryEntry,
    canonical_json,
)

HARNESS_STATUSES = frozenset({"candidate", "evaluating", "active", "rejected", "archived"})
TRAJECTORY_RESULTS = frozenset({"success", "failure", "timeout", "infra_error"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_versions (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT,
    entries     TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS harnesses (
    id              TEXT PRIMARY KEY,
    parent_id       TEXT,
    memory_version  TEXT NOT NULL,
    status          TEXT NOT NULL,
    snapshot        TEXT NOT NULL,
    complexity      TEXT NOT NULL,
    digest          TEXT NOT NULL,
    label           TEXT,
    created_at      DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectories (
    id              TEXT PRIMARY KEY,
    branch_run_id   TEXT NOT NULL,
    harness_id      TEXT NOT NULL,
    memory_version  TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    base_commit     TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    image_digest    TEXT NOT NULL,
    agent           TEXT NOT NULL,
    fork_parent     TEXT,
    fork_step       INTEGER,
    result          TEXT NOT NULL,
    tokens          BIGINT,
    tool_calls      INTEGER,
    context_tokens  INTEGER,
    final_snapshot  TEXT,
    metadata        TEXT NOT NULL,
    created_at      DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS trajectories_harness_idx ON trajectories (harness_id);
CREATE INDEX IF NOT EXISTS trajectories_task_idx ON trajectories (task_id);

CREATE TABLE IF NOT EXISTS trajectory_steps (
    trajectory_id  TEXT NOT NULL,
    step           INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    tool           TEXT,
    input          TEXT NOT NULL,
    output         TEXT NOT NULL,
    is_error       INTEGER NOT NULL,
    tokens         INTEGER NOT NULL,
    snapshot_id    TEXT,
    agent_state    TEXT,
    ts             DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (trajectory_id, step)
);

CREATE TABLE IF NOT EXISTS task_evaluations (
    id                  TEXT PRIMARY KEY,
    trajectory_id       TEXT NOT NULL UNIQUE,
    success             INTEGER,
    tests_passed        INTEGER,
    tests_total         INTEGER,
    diff_size           INTEGER,
    files_out_of_scope  INTEGER,
    tests_modified      INTEGER,
    tool_calls          INTEGER,
    redundant_reads     INTEGER,
    tokens              BIGINT,
    voided              INTEGER NOT NULL,
    audit               TEXT NOT NULL,
    isolation           TEXT NOT NULL,
    created_at          DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_patterns (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL,
    trajectory_ids      TEXT NOT NULL,
    divergence_steps    TEXT NOT NULL,
    occurrence_count    INTEGER NOT NULL,
    affected_component  TEXT,
    scope               TEXT NOT NULL,
    classification      TEXT NOT NULL,
    reflection_id       TEXT,
    created_at          DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS mutations (
    id              TEXT PRIMARY KEY,
    parent_harness  TEXT NOT NULL,
    child_harness   TEXT NOT NULL,
    pattern_id      TEXT NOT NULL,
    hypothesis      TEXT NOT NULL,
    predicted_fix   TEXT NOT NULL,
    predicted_risk  TEXT NOT NULL,
    payload         TEXT NOT NULL,
    created_at      DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS comparisons (
    id              TEXT PRIMARY KEY,
    parent_harness  TEXT NOT NULL,
    child_harness   TEXT NOT NULL,
    task_set        TEXT NOT NULL,
    method          TEXT NOT NULL,
    n_tasks         INTEGER NOT NULL,
    n_reps          INTEGER NOT NULL,
    metric          TEXT NOT NULL,
    delta           DOUBLE PRECISION NOT NULL,
    ci_lower        DOUBLE PRECISION NOT NULL,
    ci_upper        DOUBLE PRECISION NOT NULL,
    p_value         DOUBLE PRECISION,
    decision        TEXT NOT NULL,
    details         TEXT NOT NULL,
    experiment      TEXT,
    created_at      DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS reflections (
    id          TEXT PRIMARY KEY,
    project_id  TEXT,
    harness_id  TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    reflector   TEXT NOT NULL,
    report      TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    root            TEXT NOT NULL UNIQUE,
    agent           TEXT NOT NULL,
    active_harness  TEXT NOT NULL,
    h0_harness      TEXT NOT NULL,
    memory_version  TEXT NOT NULL,
    config          TEXT NOT NULL,
    created_at      DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS harness_events (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    harness_id  TEXT NOT NULL,
    from_harness TEXT,
    summary     TEXT NOT NULL,
    details     TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL
);
"""


def new_id() -> str:
    return uuid.uuid4().hex


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


# ─────────────────────────────────────────────────────────────────────
# Records
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MemoryVersion:
    id: str
    parent_id: str | None
    entries: list[MemoryEntry]
    size_bytes: int
    created_at: float

    def entry_ids(self) -> set[str]:
        return {e.id for e in self.entries}


@dataclass(frozen=True)
class HarnessRecord:
    id: str
    parent_id: str | None
    memory_version: str
    status: str
    snapshot: HarnessSnapshot
    complexity: dict[str, int]
    digest: str
    label: str | None
    created_at: float


@dataclass
class TrajectoryRecord:
    branch_run_id: str
    harness_id: str
    memory_version: str
    task_id: str
    agent: str
    result: str
    base_commit: str = "unknown"
    model_version: str = "unknown"
    image_digest: str = "unknown"
    fork_parent: str | None = None
    fork_step: int | None = None
    tokens: int | None = None
    tool_calls: int | None = None
    context_tokens: int | None = None
    final_snapshot: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)


@dataclass
class StepRecord:
    """One trajectory step — and the checkpoint after it.

    ``snapshot_id`` is the workspace after the step; ``agent_state`` is
    whatever the adapter needs to resume from here (opaque JSON).
    """

    step: int
    kind: str  # context | tool_call | message | submit
    tool: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    is_error: bool = False
    tokens: int = 0
    snapshot_id: str | None = None
    agent_state: dict[str, Any] | None = None
    ts: float = field(default_factory=time.time)


@dataclass
class EvaluationRecord:
    trajectory_id: str
    success: bool | None
    isolation: str
    tests_passed: int | None = None
    tests_total: int | None = None
    diff_size: int | None = None
    files_out_of_scope: int | None = None
    tests_modified: bool | None = None
    tool_calls: int | None = None
    redundant_reads: int | None = None
    tokens: int | None = None
    voided: bool = False
    audit: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)


@dataclass
class FailurePatternRecord:
    name: str
    description: str
    trajectory_ids: list[str]
    divergence_steps: list[int]
    affected_component: str | None
    scope: str  # local | global
    classification: str = "harness_deficiency"
    reflection_id: str | None = None
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)

    @property
    def occurrence_count(self) -> int:
        return len(self.trajectory_ids)


@dataclass
class MutationRecord:
    parent_harness: str
    child_harness: str
    pattern_id: str
    hypothesis: str
    predicted_fix: list[str]
    predicted_risk: list[str]
    payload: dict[str, Any]
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)


@dataclass
class ComparisonRecord:
    parent_harness: str
    child_harness: str
    task_set: str  # trigger | holdout | regression | null | sabotage | drift
    method: str  # fork | scratch
    n_tasks: int
    n_reps: int
    metric: str
    delta: float
    ci_lower: float
    ci_upper: float
    p_value: float | None
    decision: str  # promote | reject | report
    details: dict[str, Any] = field(default_factory=dict)
    experiment: str | None = None
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)


@dataclass
class ReflectionRecord:
    harness_id: str
    trigger: str
    reflector: str
    report: dict[str, Any]
    project_id: str | None = None
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)


@dataclass
class ProjectRecord:
    root: str
    agent: str
    active_harness: str
    h0_harness: str
    memory_version: str
    config: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)


@dataclass
class HarnessEventRecord:
    project_id: str
    kind: str  # init | promote | reject | rollback | drift_check | memory
    harness_id: str
    summary: str
    from_harness: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)


# ─────────────────────────────────────────────────────────────────────
# SQL backends
# ─────────────────────────────────────────────────────────────────────


class _SQLiteBackend:
    dialect = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=30000;")
        self._in_tx = False

    async def executescript(self, script: str) -> None:
        self._conn.executescript(script)

    async def execute(self, query: str, params: tuple = ()) -> int:
        return self._conn.execute(query, params).rowcount

    async def fetchone(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        row = self._conn.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        if self._in_tx:
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE;")
        self._in_tx = True
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK;")
            raise
        else:
            self._conn.execute("COMMIT;")
        finally:
            self._in_tx = False

    async def close(self) -> None:
        self._conn.close()


class _PostgresBackend:
    dialect = "postgres"

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, dsn: str) -> "_PostgresBackend":
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row

        conn = await AsyncConnection.connect(
            conninfo=dsn, row_factory=dict_row, autocommit=True
        )
        return cls(conn)

    @staticmethod
    def _q(query: str) -> str:
        return query.replace("?", "%s")

    async def executescript(self, script: str) -> None:
        await self._conn.execute(script)

    async def execute(self, query: str, params: tuple = ()) -> int:
        cur = await self._conn.execute(self._q(query), params)
        return cur.rowcount

    async def fetchone(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        cur = await self._conn.execute(self._q(query), params)
        return await cur.fetchone()

    async def fetchall(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = await self._conn.execute(self._q(query), params)
        return list(await cur.fetchall())

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        async with self._conn.transaction():
            yield

    async def close(self) -> None:
        await self._conn.close()


# ─────────────────────────────────────────────────────────────────────
# The store
# ─────────────────────────────────────────────────────────────────────


class ExperienceStore:
    """Everything the continual harness and refinement loop persist."""

    def __init__(self, backend: Any) -> None:
        self._db = backend

    @property
    def dialect(self) -> str:
        return self._db.dialect

    @classmethod
    def sqlite(cls, path: str | Path = ":memory:") -> "ExperienceStore":
        return cls(_SQLiteBackend(path))

    @classmethod
    async def postgres(cls, dsn: str) -> "ExperienceStore":
        return cls(await _PostgresBackend.connect(dsn))

    async def setup(self) -> None:
        await self._db.executescript(_SCHEMA)

    async def close(self) -> None:
        await self._db.close()

    # ── memory versions ──────────────────────────────────────────────

    async def create_memory_version(
        self, entries: list[MemoryEntry], *, parent_id: str | None = None
    ) -> MemoryVersion:
        payload = [e.model_dump(mode="json") for e in entries]
        encoded = canonical_json(payload)
        version = MemoryVersion(
            id=new_id(),
            parent_id=parent_id,
            entries=[MemoryEntry.model_validate(e) for e in payload],
            size_bytes=len(encoded.encode()),
            created_at=time.time(),
        )
        await self._db.execute(
            "INSERT INTO memory_versions (id, parent_id, entries, size_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?);",
            (version.id, parent_id, encoded, version.size_bytes, version.created_at),
        )
        return version

    async def derive_memory_version(
        self,
        parent_id: str,
        *,
        add: list[MemoryEntry] | None = None,
        remove_ids: list[str] | None = None,
    ) -> MemoryVersion:
        """New version = parent − removed + added. The parent is untouched."""
        parent = await self.get_memory_version(parent_id)
        if parent is None:
            raise KeyError(f"unknown memory version {parent_id}")
        removed = set(remove_ids or [])
        kept = [e for e in parent.entries if e.id not in removed]
        existing = {e.id for e in kept}
        added = [e for e in (add or []) if e.id not in existing]
        return await self.create_memory_version(kept + added, parent_id=parent_id)

    async def get_memory_version(self, version_id: str) -> MemoryVersion | None:
        row = await self._db.fetchone(
            "SELECT * FROM memory_versions WHERE id = ?;", (version_id,)
        )
        if row is None:
            return None
        return MemoryVersion(
            id=row["id"],
            parent_id=row["parent_id"],
            entries=[MemoryEntry.model_validate(e) for e in _loads(row["entries"])],
            size_bytes=int(row["size_bytes"]),
            created_at=float(row["created_at"]),
        )

    # ── harnesses ────────────────────────────────────────────────────

    async def create_harness(
        self,
        snapshot: HarnessSnapshot,
        *,
        memory_version: str,
        parent_id: str | None = None,
        status: str = "candidate",
        label: str | None = None,
    ) -> HarnessRecord:
        if status not in HARNESS_STATUSES:
            raise ValueError(f"invalid harness status {status!r}")
        record = HarnessRecord(
            id=new_id(),
            parent_id=parent_id,
            memory_version=memory_version,
            status=status,
            snapshot=HarnessSnapshot.from_json(snapshot.to_json()),
            complexity=snapshot.complexity(),
            digest=snapshot.digest(),
            label=label,
            created_at=time.time(),
        )
        await self._db.execute(
            "INSERT INTO harnesses (id, parent_id, memory_version, status, snapshot, "
            "complexity, digest, label, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (
                record.id,
                parent_id,
                memory_version,
                status,
                canonical_json(record.snapshot.to_json()),
                _dumps(record.complexity),
                record.digest,
                label,
                record.created_at,
            ),
        )
        return record

    async def get_harness(self, harness_id: str) -> HarnessRecord | None:
        row = await self._db.fetchone("SELECT * FROM harnesses WHERE id = ?;", (harness_id,))
        return _harness(row) if row else None

    async def list_harnesses(self, *, status: str | None = None) -> list[HarnessRecord]:
        if status is None:
            rows = await self._db.fetchall("SELECT * FROM harnesses ORDER BY created_at;")
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM harnesses WHERE status = ? ORDER BY created_at;", (status,)
            )
        return [_harness(r) for r in rows]

    async def set_harness_status(self, harness_id: str, status: str) -> None:
        """Only status moves; snapshot content is immutable once created."""
        if status not in HARNESS_STATUSES:
            raise ValueError(f"invalid harness status {status!r}")
        changed = await self._db.execute(
            "UPDATE harnesses SET status = ? WHERE id = ?;", (status, harness_id)
        )
        if changed == 0:
            raise KeyError(f"unknown harness {harness_id}")

    async def lineage(self, harness_id: str) -> list[HarnessRecord]:
        """``[harness, parent, grandparent, …, root]``."""
        chain: list[HarnessRecord] = []
        current: str | None = harness_id
        while current is not None:
            record = await self.get_harness(current)
            if record is None:
                break
            chain.append(record)
            current = record.parent_id
        return chain

    # ── trajectories ─────────────────────────────────────────────────

    async def record_trajectory(
        self, trajectory: TrajectoryRecord, steps: list[StepRecord]
    ) -> bool:
        """Insert a trajectory and its steps atomically. Idempotent by id.

        Returns ``False`` if a trajectory with this id already exists.
        """
        if trajectory.result not in TRAJECTORY_RESULTS:
            raise ValueError(f"invalid trajectory result {trajectory.result!r}")
        async with self._db.transaction():
            inserted = await self._db.execute(
                "INSERT INTO trajectories (id, branch_run_id, harness_id, memory_version, "
                "task_id, base_commit, model_version, image_digest, agent, fork_parent, "
                "fork_step, result, tokens, tool_calls, context_tokens, final_snapshot, "
                "metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?) ON CONFLICT (id) DO NOTHING;",
                (
                    trajectory.id,
                    trajectory.branch_run_id,
                    trajectory.harness_id,
                    trajectory.memory_version,
                    trajectory.task_id,
                    trajectory.base_commit,
                    trajectory.model_version,
                    trajectory.image_digest,
                    trajectory.agent,
                    trajectory.fork_parent,
                    trajectory.fork_step,
                    trajectory.result,
                    trajectory.tokens,
                    trajectory.tool_calls,
                    trajectory.context_tokens,
                    trajectory.final_snapshot,
                    _dumps(trajectory.metadata),
                    trajectory.created_at,
                ),
            )
            if inserted == 0:
                return False
            for s in steps:
                await self._db.execute(
                    "INSERT INTO trajectory_steps (trajectory_id, step, kind, tool, input, "
                    "output, is_error, tokens, snapshot_id, agent_state, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    (
                        trajectory.id,
                        s.step,
                        s.kind,
                        s.tool,
                        _dumps(s.input),
                        s.output.replace("\x00", ""),  # Postgres TEXT rejects NUL
                        int(s.is_error),
                        s.tokens,
                        s.snapshot_id,
                        _dumps(s.agent_state) if s.agent_state is not None else None,
                        s.ts,
                    ),
                )
        return True

    async def get_trajectory(self, trajectory_id: str) -> TrajectoryRecord | None:
        row = await self._db.fetchone(
            "SELECT * FROM trajectories WHERE id = ?;", (trajectory_id,)
        )
        return _trajectory(row) if row else None

    async def list_trajectories(
        self,
        *,
        harness_id: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        branch_run_prefix: str | None = None,
        limit: int | None = None,
    ) -> list[TrajectoryRecord]:
        clauses, params = [], []
        if harness_id is not None:
            clauses.append("harness_id = ?")
            params.append(harness_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if agent is not None:
            clauses.append("agent = ?")
            params.append(agent)
        if branch_run_prefix is not None:
            clauses.append("branch_run_id LIKE ?")
            params.append(branch_run_prefix + "%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM trajectories {where} ORDER BY created_at, id"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        rows = await self._db.fetchall(query + ";", tuple(params))
        return [_trajectory(r) for r in rows]

    async def get_steps(
        self, trajectory_id: str, *, upto: int | None = None
    ) -> list[StepRecord]:
        if upto is None:
            rows = await self._db.fetchall(
                "SELECT * FROM trajectory_steps WHERE trajectory_id = ? ORDER BY step;",
                (trajectory_id,),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM trajectory_steps WHERE trajectory_id = ? AND step <= ? "
                "ORDER BY step;",
                (trajectory_id, upto),
            )
        return [
            StepRecord(
                step=int(r["step"]),
                kind=r["kind"],
                tool=r["tool"],
                input=_loads(r["input"]) or {},
                output=r["output"],
                is_error=bool(r["is_error"]),
                tokens=int(r["tokens"]),
                snapshot_id=r["snapshot_id"],
                agent_state=_loads(r["agent_state"]),
                ts=float(r["ts"]),
            )
            for r in rows
        ]

    async def full_steps(self, trajectory_id: str) -> list[StepRecord]:
        """Steps including the inherited prefix of a fork, in order."""
        trajectory = await self.get_trajectory(trajectory_id)
        if trajectory is None:
            return []
        own = await self.get_steps(trajectory_id)
        if trajectory.fork_parent is None or trajectory.fork_step is None:
            return own
        prefix = [
            s for s in await self.full_steps(trajectory.fork_parent)
            if s.step <= trajectory.fork_step
        ]
        return prefix + own

    # ── evaluations (trusted plane only) ─────────────────────────────

    async def record_evaluation(self, evaluation: EvaluationRecord) -> bool:
        inserted = await self._db.execute(
            "INSERT INTO task_evaluations (id, trajectory_id, success, tests_passed, "
            "tests_total, diff_size, files_out_of_scope, tests_modified, tool_calls, "
            "redundant_reads, tokens, voided, audit, isolation, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (trajectory_id) DO NOTHING;",
            (
                evaluation.id,
                evaluation.trajectory_id,
                None if evaluation.success is None else int(evaluation.success),
                evaluation.tests_passed,
                evaluation.tests_total,
                evaluation.diff_size,
                evaluation.files_out_of_scope,
                None if evaluation.tests_modified is None else int(evaluation.tests_modified),
                evaluation.tool_calls,
                evaluation.redundant_reads,
                evaluation.tokens,
                int(evaluation.voided),
                _dumps(evaluation.audit),
                evaluation.isolation,
                evaluation.created_at,
            ),
        )
        return inserted == 1

    async def get_evaluation(self, trajectory_id: str) -> EvaluationRecord | None:
        row = await self._db.fetchone(
            "SELECT * FROM task_evaluations WHERE trajectory_id = ?;", (trajectory_id,)
        )
        return _evaluation(row) if row else None

    async def evaluated_trajectories(
        self, *, harness_id: str | None = None, agent: str | None = None,
        limit: int | None = None,
    ) -> list[tuple[TrajectoryRecord, EvaluationRecord]]:
        """Trajectories joined with their evaluation, oldest first."""
        clauses, params = [], []
        if harness_id is not None:
            clauses.append("t.harness_id = ?")
            params.append(harness_id)
        if agent is not None:
            clauses.append("t.agent = ?")
            params.append(agent)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            "SELECT t.id AS tid FROM trajectories t JOIN task_evaluations e "
            f"ON e.trajectory_id = t.id {where} ORDER BY t.created_at, t.id"
        )
        rows = await self._db.fetchall(query + ";", tuple(params))
        ids = [r["tid"] for r in rows]
        if limit is not None:
            ids = ids[-limit:]
        out = []
        for tid in ids:
            trajectory = await self.get_trajectory(tid)
            evaluation = await self.get_evaluation(tid)
            if trajectory is not None and evaluation is not None:
                out.append((trajectory, evaluation))
        return out

    # ── reflection artifacts ─────────────────────────────────────────

    async def record_reflection(self, reflection: ReflectionRecord) -> None:
        await self._db.execute(
            "INSERT INTO reflections (id, project_id, harness_id, trigger, reflector, "
            "report, created_at) VALUES (?, ?, ?, ?, ?, ?, ?);",
            (
                reflection.id,
                reflection.project_id,
                reflection.harness_id,
                reflection.trigger,
                reflection.reflector,
                _dumps(reflection.report),
                reflection.created_at,
            ),
        )

    async def list_reflections(self, *, project_id: str | None = None) -> list[ReflectionRecord]:
        if project_id is None:
            rows = await self._db.fetchall("SELECT * FROM reflections ORDER BY created_at;")
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM reflections WHERE project_id = ? ORDER BY created_at;",
                (project_id,),
            )
        return [
            ReflectionRecord(
                id=r["id"],
                project_id=r["project_id"],
                harness_id=r["harness_id"],
                trigger=r["trigger"],
                reflector=r["reflector"],
                report=_loads(r["report"]),
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]

    async def record_failure_pattern(self, pattern: FailurePatternRecord) -> None:
        if len(pattern.trajectory_ids) != len(pattern.divergence_steps):
            raise ValueError("divergence_steps must be parallel to trajectory_ids")
        await self._db.execute(
            "INSERT INTO failure_patterns (id, name, description, trajectory_ids, "
            "divergence_steps, occurrence_count, affected_component, scope, "
            "classification, reflection_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (
                pattern.id,
                pattern.name,
                pattern.description,
                _dumps(pattern.trajectory_ids),
                _dumps(pattern.divergence_steps),
                pattern.occurrence_count,
                pattern.affected_component,
                pattern.scope,
                pattern.classification,
                pattern.reflection_id,
                pattern.created_at,
            ),
        )

    async def get_failure_pattern(self, pattern_id: str) -> FailurePatternRecord | None:
        row = await self._db.fetchone(
            "SELECT * FROM failure_patterns WHERE id = ?;", (pattern_id,)
        )
        return _pattern(row) if row else None

    async def list_failure_patterns(self) -> list[FailurePatternRecord]:
        rows = await self._db.fetchall("SELECT * FROM failure_patterns ORDER BY created_at;")
        return [_pattern(r) for r in rows]

    async def record_mutation(self, mutation: MutationRecord) -> None:
        await self._db.execute(
            "INSERT INTO mutations (id, parent_harness, child_harness, pattern_id, "
            "hypothesis, predicted_fix, predicted_risk, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (
                mutation.id,
                mutation.parent_harness,
                mutation.child_harness,
                mutation.pattern_id,
                mutation.hypothesis,
                _dumps(mutation.predicted_fix),
                _dumps(mutation.predicted_risk),
                _dumps(mutation.payload),
                mutation.created_at,
            ),
        )

    async def get_mutation_for_child(self, child_harness: str) -> MutationRecord | None:
        row = await self._db.fetchone(
            "SELECT * FROM mutations WHERE child_harness = ?;", (child_harness,)
        )
        return _mutation(row) if row else None

    async def list_mutations(self, *, parent_harness: str | None = None) -> list[MutationRecord]:
        if parent_harness is None:
            rows = await self._db.fetchall("SELECT * FROM mutations ORDER BY created_at;")
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM mutations WHERE parent_harness = ? ORDER BY created_at;",
                (parent_harness,),
            )
        return [_mutation(r) for r in rows]

    async def record_comparison(self, comparison: ComparisonRecord) -> None:
        await self._db.execute(
            "INSERT INTO comparisons (id, parent_harness, child_harness, task_set, method, "
            "n_tasks, n_reps, metric, delta, ci_lower, ci_upper, p_value, decision, "
            "details, experiment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (
                comparison.id,
                comparison.parent_harness,
                comparison.child_harness,
                comparison.task_set,
                comparison.method,
                comparison.n_tasks,
                comparison.n_reps,
                comparison.metric,
                comparison.delta,
                comparison.ci_lower,
                comparison.ci_upper,
                comparison.p_value,
                comparison.decision,
                _dumps(comparison.details),
                comparison.experiment,
                comparison.created_at,
            ),
        )

    async def list_comparisons(
        self,
        *,
        child_harness: str | None = None,
        parent_harness: str | None = None,
        experiment: str | None = None,
    ) -> list[ComparisonRecord]:
        clauses, params = [], []
        for column, value in (
            ("child_harness", child_harness),
            ("parent_harness", parent_harness),
            ("experiment", experiment),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._db.fetchall(
            f"SELECT * FROM comparisons {where} ORDER BY created_at;", tuple(params)
        )
        return [_comparison(r) for r in rows]

    # ── projects + events ────────────────────────────────────────────

    async def create_project(self, project: ProjectRecord) -> ProjectRecord:
        await self._db.execute(
            "INSERT INTO projects (id, root, agent, active_harness, h0_harness, "
            "memory_version, config, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            (
                project.id,
                project.root,
                project.agent,
                project.active_harness,
                project.h0_harness,
                project.memory_version,
                _dumps(project.config),
                project.created_at,
            ),
        )
        return project

    async def get_project(
        self, *, root: str | None = None, project_id: str | None = None
    ) -> ProjectRecord | None:
        if root is not None:
            row = await self._db.fetchone("SELECT * FROM projects WHERE root = ?;", (root,))
        elif project_id is not None:
            row = await self._db.fetchone("SELECT * FROM projects WHERE id = ?;", (project_id,))
        else:
            raise ValueError("pass root or project_id")
        return _project(row) if row else None

    async def update_project(
        self,
        project_id: str,
        *,
        active_harness: str | None = None,
        memory_version: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        sets, params = [], []
        if active_harness is not None:
            sets.append("active_harness = ?")
            params.append(active_harness)
        if memory_version is not None:
            sets.append("memory_version = ?")
            params.append(memory_version)
        if config is not None:
            sets.append("config = ?")
            params.append(_dumps(config))
        if not sets:
            return
        params.append(project_id)
        await self._db.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?;", tuple(params)
        )

    async def record_harness_event(self, event: HarnessEventRecord) -> None:
        await self._db.execute(
            "INSERT INTO harness_events (id, project_id, kind, harness_id, from_harness, "
            "summary, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            (
                event.id,
                event.project_id,
                event.kind,
                event.harness_id,
                event.from_harness,
                event.summary,
                _dumps(event.details),
                event.created_at,
            ),
        )

    async def list_harness_events(
        self, project_id: str, *, kind: str | None = None
    ) -> list[HarnessEventRecord]:
        if kind is None:
            rows = await self._db.fetchall(
                "SELECT * FROM harness_events WHERE project_id = ? ORDER BY created_at;",
                (project_id,),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM harness_events WHERE project_id = ? AND kind = ? "
                "ORDER BY created_at;",
                (project_id, kind),
            )
        return [
            HarnessEventRecord(
                id=r["id"],
                project_id=r["project_id"],
                kind=r["kind"],
                harness_id=r["harness_id"],
                from_harness=r["from_harness"],
                summary=r["summary"],
                details=_loads(r["details"]),
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]


# ─────────────────────────────────────────────────────────────────────
# Row mapping
# ─────────────────────────────────────────────────────────────────────


def _harness(row: dict[str, Any]) -> HarnessRecord:
    return HarnessRecord(
        id=row["id"],
        parent_id=row["parent_id"],
        memory_version=row["memory_version"],
        status=row["status"],
        snapshot=HarnessSnapshot.from_json(_loads(row["snapshot"])),
        complexity=_loads(row["complexity"]),
        digest=row["digest"],
        label=row["label"],
        created_at=float(row["created_at"]),
    )


def _trajectory(row: dict[str, Any]) -> TrajectoryRecord:
    return TrajectoryRecord(
        id=row["id"],
        branch_run_id=row["branch_run_id"],
        harness_id=row["harness_id"],
        memory_version=row["memory_version"],
        task_id=row["task_id"],
        base_commit=row["base_commit"],
        model_version=row["model_version"],
        image_digest=row["image_digest"],
        agent=row["agent"],
        fork_parent=row["fork_parent"],
        fork_step=_int(row["fork_step"]),
        result=row["result"],
        tokens=_int(row["tokens"]),
        tool_calls=_int(row["tool_calls"]),
        context_tokens=_int(row["context_tokens"]),
        final_snapshot=row["final_snapshot"],
        metadata=_loads(row["metadata"]) or {},
        created_at=float(row["created_at"]),
    )


def _evaluation(row: dict[str, Any]) -> EvaluationRecord:
    return EvaluationRecord(
        id=row["id"],
        trajectory_id=row["trajectory_id"],
        success=_bool(row["success"]),
        tests_passed=_int(row["tests_passed"]),
        tests_total=_int(row["tests_total"]),
        diff_size=_int(row["diff_size"]),
        files_out_of_scope=_int(row["files_out_of_scope"]),
        tests_modified=_bool(row["tests_modified"]),
        tool_calls=_int(row["tool_calls"]),
        redundant_reads=_int(row["redundant_reads"]),
        tokens=_int(row["tokens"]),
        voided=bool(row["voided"]),
        audit=_loads(row["audit"]) or {},
        isolation=row["isolation"],
        created_at=float(row["created_at"]),
    )


def _pattern(row: dict[str, Any]) -> FailurePatternRecord:
    return FailurePatternRecord(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        trajectory_ids=_loads(row["trajectory_ids"]),
        divergence_steps=_loads(row["divergence_steps"]),
        affected_component=row["affected_component"],
        scope=row["scope"],
        classification=row["classification"],
        reflection_id=row["reflection_id"],
        created_at=float(row["created_at"]),
    )


def _mutation(row: dict[str, Any]) -> MutationRecord:
    return MutationRecord(
        id=row["id"],
        parent_harness=row["parent_harness"],
        child_harness=row["child_harness"],
        pattern_id=row["pattern_id"],
        hypothesis=row["hypothesis"],
        predicted_fix=_loads(row["predicted_fix"]),
        predicted_risk=_loads(row["predicted_risk"]),
        payload=_loads(row["payload"]),
        created_at=float(row["created_at"]),
    )


def _comparison(row: dict[str, Any]) -> ComparisonRecord:
    return ComparisonRecord(
        id=row["id"],
        parent_harness=row["parent_harness"],
        child_harness=row["child_harness"],
        task_set=row["task_set"],
        method=row["method"],
        n_tasks=int(row["n_tasks"]),
        n_reps=int(row["n_reps"]),
        metric=row["metric"],
        delta=float(row["delta"]),
        ci_lower=float(row["ci_lower"]),
        ci_upper=float(row["ci_upper"]),
        p_value=None if row["p_value"] is None else float(row["p_value"]),
        decision=row["decision"],
        details=_loads(row["details"]) or {},
        experiment=row["experiment"],
        created_at=float(row["created_at"]),
    )


def _project(row: dict[str, Any]) -> ProjectRecord:
    return ProjectRecord(
        id=row["id"],
        root=row["root"],
        agent=row["agent"],
        active_harness=row["active_harness"],
        h0_harness=row["h0_harness"],
        memory_version=row["memory_version"],
        config=_loads(row["config"]) or {},
        created_at=float(row["created_at"]),
    )


def record_to_json(record: Any) -> dict[str, Any]:
    """Plain JSON for CLI/debug output."""
    data = asdict(record)
    return json.loads(json.dumps(data, default=str))
