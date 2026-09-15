"""SQLite-backed :class:`StateStore` — local mode (ARCHITECTURE §1a).

The same branch lifecycle, leases, fencing tokens, crash reconciliation
and gap-free event sequence as :class:`PostgresStateStore`, backed by a
single file (default ``~/.harness/state.db``) or ``:memory:``.

Fencing is not a Postgres feature. ``lease_generation`` is a column and
the claim-and-increment is a transaction: SQLite's ``BEGIN IMMEDIATE``
takes the database write lock up front, which gives the same
serialization a row lock does at single-writer scale. Several processes
may open the same file; they serialize on that lock.

Two deliberate properties:

- **Injectable clock.** Leases are epoch floats computed from ``clock``,
  never from SQL ``now()``, so the Phase 3 simulator can drive this
  store under its virtual clock exactly as it drives the in-memory fake.
- **No real awaits.** The ``sqlite3`` driver is synchronous; every
  method is an ``async def`` that completes without suspending. That is
  what lets ``sim.run --backend sqlite`` drive the production store with
  a one-shot ``coro.send(None)``. The cost — a brief event-loop block per
  statement — is acceptable for a local single-developer store.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

from app.meta_harness.store import (
    TERMINAL_STATUSES,
    BranchRow,
    StaleFenceError,
    StoredEvent,
    UnknownBranchError,
    WorkerRow,
    claim_filter_sql,
)

DEFAULT_STATE_PATH = Path.home() / ".harness" / "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS branch_runs (
    branch_id            TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    thread_id            TEXT NOT NULL UNIQUE,
    parent_thread_id     TEXT,
    parent_checkpoint_id TEXT,
    status               TEXT NOT NULL,
    mods                 TEXT NOT NULL DEFAULT '{}',
    name                 TEXT,
    result               TEXT,
    error                TEXT,
    lease_owner          TEXT,
    lease_generation     INTEGER NOT NULL DEFAULT 0,
    lease_expires_at     REAL,
    created_at           REAL NOT NULL,
    started_at           REAL,
    finished_at          REAL
);
CREATE INDEX IF NOT EXISTS branch_runs_status_lease_idx
    ON branch_runs (status, lease_expires_at);
CREATE INDEX IF NOT EXISTS branch_runs_run_id_idx
    ON branch_runs (run_id);

CREATE TABLE IF NOT EXISTS run_event_seq (
    run_id   TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id  TEXT PRIMARY KEY,
    pid        INTEGER NOT NULL,
    hostname   TEXT NOT NULL,
    started_at REAL NOT NULL,
    last_seen  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS iteration_log (
    run_id    TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    candidate TEXT NOT NULL,
    row       TEXT NOT NULL,
    ts        REAL NOT NULL,
    PRIMARY KEY (run_id, iteration, candidate)
);

CREATE TABLE IF NOT EXISTS run_events (
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload    TEXT NOT NULL,
    ts         REAL NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


class SQLiteStateStore:
    """Single-file :class:`StateStore` for local mode."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Callable[[], float] | None = None,
        busy_timeout_s: float = 30.0,
    ) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time
        # isolation_level=None: we issue BEGIN/COMMIT ourselves so the
        # claim transaction is explicitly IMMEDIATE.
        self._conn = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
            timeout=busy_timeout_s,
        )
        self._conn.row_factory = sqlite3.Row
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_s * 1000)};")
        self._event_waiters: dict[str, list[asyncio.Event]] = {}

    @property
    def path(self) -> str:
        return self._path

    async def close(self) -> None:
        self._conn.close()

    async def setup(self) -> None:
        self._conn.executescript(_SCHEMA)

    def _now(self) -> float:
        return self._clock()

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        """One write transaction holding the database write lock."""
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK;")
            raise
        else:
            self._conn.execute("COMMIT;")

    def _fetch_branch(self, branch_id: str) -> BranchRow | None:
        record = self._conn.execute(
            "SELECT * FROM branch_runs WHERE branch_id = ?;", (branch_id,)
        ).fetchone()
        return _row(record) if record else None

    # ── branch lifecycle ─────────────────────────────────────────────

    async def create_branch(
        self,
        *,
        branch_id: str,
        run_id: str,
        thread_id: str,
        parent_thread_id: str | None,
        parent_checkpoint_id: str | None,
        mods: dict[str, Any],
        name: str | None = None,
    ) -> BranchRow:
        with self._immediate() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO branch_runs (
                        branch_id, run_id, thread_id, parent_thread_id,
                        parent_checkpoint_id, status, mods, name, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?);
                    """,
                    (
                        branch_id,
                        run_id,
                        thread_id,
                        parent_thread_id,
                        parent_checkpoint_id,
                        json.dumps(mods),
                        name,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"branch_id already exists: {branch_id}") from exc
            row = self._fetch_branch(branch_id)
        assert row is not None
        return row

    async def claim_next_branch(
        self,
        *,
        worker_id: str,
        lease_ttl_s: float,
        run_prefix: str | None = None,
        exclude_run_prefix: str | None = None,
    ) -> BranchRow | None:
        now = self._now()
        filters, filter_params = claim_filter_sql(
            run_prefix, exclude_run_prefix, placeholder="?"
        )
        with self._immediate() as conn:
            # rowid breaks created_at ties FIFO (frozen virtual clock).
            record = conn.execute(
                f"""
                SELECT branch_id FROM branch_runs
                WHERE (status = 'created'
                   OR (status = 'running' AND lease_expires_at < ?)){filters}
                ORDER BY created_at, rowid
                LIMIT 1;
                """,
                (now, *filter_params),
            ).fetchone()
            if record is None:
                return None
            branch_id = record["branch_id"]
            conn.execute(
                """
                UPDATE branch_runs SET
                    status           = 'running',
                    lease_owner      = ?,
                    lease_generation = lease_generation + 1,
                    lease_expires_at = ?,
                    started_at       = COALESCE(started_at, ?)
                WHERE branch_id = ?;
                """,
                (worker_id, now + lease_ttl_s, now, branch_id),
            )
            row = self._fetch_branch(branch_id)
        return row

    async def heartbeat(
        self, *, branch_id: str, fence: int, lease_ttl_s: float
    ) -> None:
        with self._immediate() as conn:
            cur = conn.execute(
                """
                UPDATE branch_runs SET lease_expires_at = ?
                WHERE branch_id = ? AND status = 'running'
                  AND lease_generation = ?;
                """,
                (self._now() + lease_ttl_s, branch_id, fence),
            )
            if cur.rowcount == 0:
                if self._fetch_branch(branch_id) is None:
                    raise UnknownBranchError(branch_id)
                raise StaleFenceError(
                    f"branch {branch_id}: heartbeat fence {fence} is stale — "
                    "lease was reclaimed or branch cancelled; abort"
                )

    async def finish_branch(
        self,
        *,
        branch_id: str,
        fence: int,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"finish_branch status must be terminal, got {status!r}")
        with self._immediate() as conn:
            cur = conn.execute(
                """
                UPDATE branch_runs SET
                    status           = ?,
                    result           = ?,
                    error            = ?,
                    finished_at      = ?,
                    lease_owner      = NULL,
                    lease_expires_at = NULL
                WHERE branch_id = ? AND status = 'running'
                  AND lease_generation = ?;
                """,
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    error,
                    self._now(),
                    branch_id,
                    fence,
                ),
            )
            if cur.rowcount == 0:
                if self._fetch_branch(branch_id) is None:
                    raise UnknownBranchError(branch_id)
                raise StaleFenceError(
                    f"branch {branch_id}: finish fence {fence} is stale — "
                    "another worker owns this branch; abort without retry"
                )

    async def request_cancel(self, branch_id: str) -> BranchRow:
        with self._immediate() as conn:
            existing = self._fetch_branch(branch_id)
            if existing is None:
                raise UnknownBranchError(branch_id)
            if existing.status in TERMINAL_STATUSES:
                return existing
            # Fence bump + terminal status in one durable write (I5, I6).
            conn.execute(
                """
                UPDATE branch_runs SET
                    status           = 'cancelled',
                    lease_generation = lease_generation + 1,
                    finished_at      = ?,
                    lease_owner      = NULL,
                    lease_expires_at = NULL
                WHERE branch_id = ?;
                """,
                (self._now(), branch_id),
            )
            row = self._fetch_branch(branch_id)
        assert row is not None
        return row

    async def get_branch(self, branch_id: str) -> BranchRow | None:
        return self._fetch_branch(branch_id)

    async def get_branch_by_thread(self, thread_id: str) -> BranchRow | None:
        record = self._conn.execute(
            "SELECT * FROM branch_runs WHERE thread_id = ?;", (thread_id,)
        ).fetchone()
        return _row(record) if record else None

    async def list_branches(self, *, run_id: str | None = None) -> list[BranchRow]:
        if run_id is None:
            records = self._conn.execute(
                "SELECT * FROM branch_runs ORDER BY created_at, rowid;"
            ).fetchall()
        else:
            records = self._conn.execute(
                "SELECT * FROM branch_runs WHERE run_id = ? "
                "ORDER BY created_at, rowid;",
                (run_id,),
            ).fetchall()
        return [_row(r) for r in records]

    async def reconcile_on_boot(self) -> list[BranchRow]:
        now = self._now()
        with self._immediate() as conn:
            records = conn.execute(
                """
                SELECT branch_id FROM branch_runs
                WHERE status = 'running'
                  AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                ORDER BY created_at, rowid;
                """,
                (now,),
            ).fetchall()
            ids = [r["branch_id"] for r in records]
            # Requeue; the fence is preserved (monotonic forever).
            conn.executemany(
                """
                UPDATE branch_runs SET
                    status = 'created', lease_owner = NULL, lease_expires_at = NULL
                WHERE branch_id = ?;
                """,
                [(b,) for b in ids],
            )
            rows = [self._fetch_branch(b) for b in ids]
        return [r for r in rows if r is not None]

    # ── worker registry ──────────────────────────────────────────────

    async def register_worker(
        self, *, worker_id: str, pid: int, hostname: str
    ) -> WorkerRow:
        now = self._now()
        with self._immediate() as conn:
            conn.execute(
                """
                INSERT INTO workers (worker_id, pid, hostname, started_at, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (worker_id) DO UPDATE SET
                    pid = excluded.pid,
                    hostname = excluded.hostname,
                    last_seen = excluded.last_seen;
                """,
                (worker_id, pid, hostname, now, now),
            )
        worker = await self.get_worker(worker_id)
        assert worker is not None
        return worker

    async def touch_worker(self, worker_id: str) -> None:
        with self._immediate() as conn:
            conn.execute(
                "UPDATE workers SET last_seen = ? WHERE worker_id = ?;",
                (self._now(), worker_id),
            )

    async def remove_worker(self, worker_id: str) -> None:
        with self._immediate() as conn:
            conn.execute("DELETE FROM workers WHERE worker_id = ?;", (worker_id,))

    async def reap_stale_workers(self, *, older_than_s: float) -> list[str]:
        cutoff = self._now() - older_than_s
        with self._immediate() as conn:
            stale = [
                r["worker_id"]
                for r in conn.execute(
                    "SELECT worker_id FROM workers WHERE last_seen < ?;", (cutoff,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM workers WHERE last_seen < ?;", (cutoff,))
        return stale

    async def list_workers(self) -> list[WorkerRow]:
        records = self._conn.execute(
            "SELECT * FROM workers ORDER BY started_at, rowid;"
        ).fetchall()
        return [_worker_row(r) for r in records]

    async def get_worker(self, worker_id: str) -> WorkerRow | None:
        record = self._conn.execute(
            "SELECT * FROM workers WHERE worker_id = ?;", (worker_id,)
        ).fetchone()
        return _worker_row(record) if record else None

    # ── authoritative iteration log (I1) ─────────────────────────────

    async def record_iteration(
        self,
        *,
        run_id: str,
        iteration: int,
        candidate: str,
        row: dict[str, Any],
        branch_id: str | None = None,
        fence: int | None = None,
    ) -> bool:
        with self._immediate() as conn:
            if branch_id is not None:
                # Fence check and insert inside ONE write transaction: no
                # other writer can bump the fence between them.
                branch = self._fetch_branch(branch_id)
                if branch is None:
                    raise UnknownBranchError(branch_id)
                if branch.status != "running" or branch.lease_generation != fence:
                    raise StaleFenceError(
                        f"branch {branch_id}: record_iteration fence {fence} is "
                        f"stale (status={branch.status}, "
                        f"generation={branch.lease_generation})"
                    )
            cur = conn.execute(
                """
                INSERT INTO iteration_log (run_id, iteration, candidate, row, ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (run_id, iteration, candidate) DO NOTHING;
                """,
                (run_id, iteration, candidate, json.dumps(row), self._now()),
            )
            return cur.rowcount == 1

    async def list_iterations(self, *, run_id: str) -> list[dict[str, Any]]:
        records = self._conn.execute(
            """
            SELECT row FROM iteration_log WHERE run_id = ?
            ORDER BY iteration, candidate;
            """,
            (run_id,),
        ).fetchall()
        return [json.loads(r["row"]) for r in records]

    # ── per-run event log (I7) ───────────────────────────────────────

    async def append_event(
        self, *, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> StoredEvent:
        ts = self._now()
        with self._immediate() as conn:
            # Counter bump and event insert commit together: a crash can
            # never leak a seq without its event.
            conn.execute(
                """
                INSERT INTO run_event_seq (run_id, last_seq) VALUES (?, 1)
                ON CONFLICT (run_id) DO UPDATE SET last_seq = last_seq + 1;
                """,
                (run_id,),
            )
            seq = conn.execute(
                "SELECT last_seq FROM run_event_seq WHERE run_id = ?;", (run_id,)
            ).fetchone()["last_seq"]
            conn.execute(
                """
                INSERT INTO run_events (run_id, seq, event_type, payload, ts)
                VALUES (?, ?, ?, ?, ?);
                """,
                (run_id, seq, event_type, json.dumps(payload), ts),
            )
        for waiter in self._event_waiters.get(run_id, []):
            waiter.set()
        return StoredEvent(
            run_id=run_id,
            seq=seq,
            event_type=event_type,
            payload=dict(payload),
            ts=ts,
        )

    async def list_events(
        self, *, run_id: str, after_seq: int = 0
    ) -> list[StoredEvent]:
        records = self._conn.execute(
            """
            SELECT run_id, seq, event_type, payload, ts FROM run_events
            WHERE run_id = ? AND seq > ? ORDER BY seq;
            """,
            (run_id, after_seq),
        ).fetchall()
        return [
            StoredEvent(
                run_id=r["run_id"],
                seq=r["seq"],
                event_type=r["event_type"],
                payload=json.loads(r["payload"]),
                ts=r["ts"],
            )
            for r in records
        ]

    async def stream_events(
        self, *, run_id: str, after_seq: int = 0, poll_interval_s: float = 0.25
    ) -> AsyncIterator[StoredEvent]:
        """Yield events in seq order forever.

        In-process appends wake the subscriber immediately (local mode's
        in-memory queue); appends from another process are picked up by
        polling. Reads always go through ``after_seq``, so a duplicate or
        spurious wake is harmless (same rule as LISTEN/NOTIFY).
        """
        seq = after_seq
        waiter = asyncio.Event()
        self._event_waiters.setdefault(run_id, []).append(waiter)
        try:
            while True:
                fresh = await self.list_events(run_id=run_id, after_seq=seq)
                if not fresh:
                    waiter.clear()
                    try:
                        await asyncio.wait_for(waiter.wait(), poll_interval_s)
                    except asyncio.TimeoutError:
                        pass
                    continue
                for event in fresh:
                    seq = event.seq
                    yield event
        finally:
            self._event_waiters.get(run_id, []).remove(waiter)


def _row(record: sqlite3.Row) -> BranchRow:
    return BranchRow(
        branch_id=record["branch_id"],
        run_id=record["run_id"],
        thread_id=record["thread_id"],
        parent_thread_id=record["parent_thread_id"],
        parent_checkpoint_id=record["parent_checkpoint_id"],
        status=record["status"],
        mods=json.loads(record["mods"]) if record["mods"] else {},
        name=record["name"],
        result=json.loads(record["result"]) if record["result"] else None,
        error=record["error"],
        lease_owner=record["lease_owner"],
        lease_generation=record["lease_generation"],
        lease_expires_at=record["lease_expires_at"],
        created_at=record["created_at"],
        started_at=record["started_at"],
        finished_at=record["finished_at"],
    )


def _worker_row(record: sqlite3.Row) -> WorkerRow:
    return WorkerRow(
        worker_id=record["worker_id"],
        pid=record["pid"],
        hostname=record["hostname"],
        started_at=record["started_at"],
        last_seen=record["last_seen"],
    )
