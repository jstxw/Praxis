"""Local-mode SQLite store: the properties the shared contract suite in
``test_store.py`` cannot show with one connection.

Named after the invariants in docs/INVARIANTS.md.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.meta_harness.sqlite_store import SQLiteStateStore  # noqa: E402
from app.meta_harness.store import StateStore  # noqa: E402


def test_sqlite_store_satisfies_state_store_protocol(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    assert isinstance(store, StateStore)


def test_i5_concurrent_connections_never_double_claim(tmp_path):
    """Many connections (think: processes) on one file race to claim.
    BEGIN IMMEDIATE serializes the claim-and-increment, so every branch
    is claimed exactly once and every fence is 1."""
    path = tmp_path / "state.db"
    setup = SQLiteStateStore(path)
    asyncio.run(setup.setup())
    n_branches = 40
    for i in range(n_branches):
        asyncio.run(
            setup.create_branch(
                branch_id=f"b{i}",
                run_id="race",
                thread_id=f"race.fork.b{i}",
                parent_thread_id="race",
                parent_checkpoint_id="c0",
                mods={},
            )
        )

    claims: list[tuple[str, str, int]] = []
    lock = threading.Lock()

    def claimer(worker_id: str) -> None:
        store = SQLiteStateStore(path)
        while True:
            row = asyncio.run(
                store.claim_next_branch(worker_id=worker_id, lease_ttl_s=300)
            )
            if row is None:
                break
            with lock:
                claims.append((row.branch_id, worker_id, row.lease_generation))
        asyncio.run(store.close())

    threads = [threading.Thread(target=claimer, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed_ids = [c[0] for c in claims]
    assert len(claimed_ids) == n_branches
    assert len(set(claimed_ids)) == n_branches
    assert {c[2] for c in claims} == {1}


def test_i1_fenced_record_is_exactly_once_across_connections(tmp_path):
    """A stale fence from a second connection is rejected by the data
    layer, and a duplicate record key is a no-op."""
    path = tmp_path / "state.db"
    clock_now = [1000.0]
    a = SQLiteStateStore(path, clock=lambda: clock_now[0])
    b = SQLiteStateStore(path, clock=lambda: clock_now[0])

    async def scenario() -> None:
        from app.meta_harness.store import StaleFenceError

        await a.setup()
        await a.create_branch(
            branch_id="b0",
            run_id="r",
            thread_id="r.fork.b0",
            parent_thread_id="r",
            parent_checkpoint_id="c0",
            mods={},
        )
        stalled = await a.claim_next_branch(worker_id="wa", lease_ttl_s=1.0)
        clock_now[0] += 5.0
        owner = await b.claim_next_branch(worker_id="wb", lease_ttl_s=60.0)
        assert owner.lease_generation == stalled.lease_generation + 1

        try:
            await a.record_iteration(
                run_id="r", iteration=1, candidate="c", row={"from": "a"},
                branch_id="b0", fence=stalled.lease_generation,
            )
        except StaleFenceError:
            pass
        else:  # pragma: no cover - the assertion is the point
            raise AssertionError("stale fence must be rejected")

        assert await b.record_iteration(
            run_id="r", iteration=1, candidate="c", row={"from": "b"},
            branch_id="b0", fence=owner.lease_generation,
        )
        assert not await b.record_iteration(
            run_id="r", iteration=1, candidate="c", row={"from": "b-dup"},
            branch_id="b0", fence=owner.lease_generation,
        )
        rows = await a.list_iterations(run_id="r")
        assert rows == [{"from": "b"}]

    asyncio.run(scenario())


def test_i7_events_survive_reopen_and_stay_gapless(tmp_path):
    path = tmp_path / "state.db"

    async def write(n: int) -> None:
        store = SQLiteStateStore(path)
        await store.setup()
        for i in range(n):
            await store.append_event(run_id="r", event_type="state-update", payload={"i": i})
        await store.close()

    asyncio.run(write(5))
    asyncio.run(write(5))  # a "restart" continues the same sequence

    async def read() -> list[int]:
        store = SQLiteStateStore(path)
        events = await store.list_events(run_id="r")
        await store.close()
        return [e.seq for e in events]

    assert asyncio.run(read()) == list(range(1, 11))
