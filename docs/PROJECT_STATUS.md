# Project Status — Meta-Harness

> Last updated: 2026-09-15 (macOS workspace, `praxis-refinement` branch).
> The refinement layer (VISION / ARCHITECTURE / DESIGN in `documents/`) is
> reported in its own section directly below; the runtime sections after it
> are unchanged history.

---

## Refinement layer — verified snapshot (2026-09-15)

What was built and how it maps to the spec: [`docs/REFINEMENT.md`](REFINEMENT.md).
Every departure from the spec and why: [`docs/DECISIONS.md`](DECISIONS.md).

Durability claims and capability claims are reported separately (VISION §6
rule 4). **No capability claim is made about a real agent.**

### Durability / mechanics (deterministic)

```bash
cd backend && uv run pytest tests -q
```

Result: **244 passed, 2 skipped** (skips: live-LLM inner-loop test without
`ANTHROPIC_API_KEY`; opt-in live Claude fork test without
`HARNESS_LIVE_CLAUDE=1`). Postgres-backed tests execute.

```bash
cd backend && uv run python -m sim.run --seeds 10000                    # 10000 seeds run, 0 failed (fenced_store, memory)
cd backend && uv run python -m sim.run --seeds 10000 --backend sqlite   # 10000 seeds run, 0 failed (fenced_store, sqlite)
```

Local mode (SQLite) passes I1–I7 under the same simulator, and the same seed
yields the identical schedule and verdict on both backings
(`test_sqlite_backend_reproduces_dst1_and_matches_memory_schedule`).

```bash
harness tasks check        # 25 tasks, 0 failed hygiene (docker verifier, k=3)
```

**E0 — real workload wired in, invariants hold** (synthetic agent, docker
verifier, pre-registered `experiments/preregistered/20260915T082259Z-E0.json`):

```bash
HARNESS_HOME=~/.harness/research harness experiment e0 --agent synthetic --workers 4
```

Result (`experiments/results/20260915T082604Z-E0-synthetic.json`): passed —
20 specs, 20 fenced iteration records, 0 duplicates, 20 evaluations; a worker
killed mid-branch had its branch reclaimed at fence 2; DST 500 seeds on
memory and SQLite, 0 failures.

**Real agent through the full stack (mechanics, n=2 runs — not a measurement):**

```bash
harness init --agent claude --model claude-haiku-4-5-20251001
harness run --task task-001-fix-typo            # success, 11/11 tests, docker verifier
harness run --task task-004-handle-error --seed 1   # success, 11/11 tests, docker verifier
HARNESS_LIVE_CLAUDE=1 uv run pytest tests/test_claude_code_adapter.py::test_live_claude_trajectory_forks_mid_run   # 1 passed
```

The first run surfaced an environmental failure (host had no `python`;
`python3 -m pytest` needed approval) that would have been mis-learned as an
"unverified submit" lesson; fixed in `claude_code.agent_env` (commit b175ba5).

### Capability (statistical) — synthetic agent only

E1, E2, E4, E5 on the synthetic agent are running at the time of writing;
their results are recorded in [`docs/REFINEMENT.md`](REFINEMENT.md) §5 with
reproduction commands. Synthetic-agent results validate the measurement
pipeline and are not evidence that refinement helps a real agent. E3 needs
two human label files and has not been run.

### DESIGN §8 phase gates

| Phase | Deliverable | Gate | Status |
|---|---|---|---|
| 0 | real workload wired in; invariants hold | DST green | ✅ built; E0 passed; DST green on both backings |
| 1 | experience store + trigger policy | E1 null test passes | built; gate evaluated on synthetic agent only (REFINEMENT §5); **not run on a real agent** |
| 2 | fork-based evaluation harness | E2 produces a ratio | built; ratio on synthetic agent only; **not run on a real agent** |
| 3 | memory versioning; freeze-during-compare | memory isolated in comparison | ✅ built and tested (`test_memory_frozen_during_comparison`) |
| 4 | reflection + step attribution | E3 FPR acceptable | built; **E3 not run (needs two human labelers)** |
| 5 | comparison gate + promotion | E4/E5 clear | built; synthetic agent only; **not run on a real agent** |
| 6 | continuous loop under budget | all above | built; end-to-end cycle exercised on the synthetic agent (`test_refinement_loop.py`) |

---

**Framing:** this project is a *durable execution runtime for long-horizon
agent workflows* — checkpointed, forkable, crash-recoverable — verified by
deterministic simulation testing. The self-improving harness search is the
reference workload that stresses the runtime, not the thesis. See
`documents/REPOSITIONING_PLAN.md` for the full plan and
`docs/INVARIANTS.md` for the spec the runtime is verified against.

Every number in this file is reproducible by the command printed next to it.

---

## Verified snapshot (2026-08-05)

Environment: macOS (Darwin), Python via `uv`, Postgres 16 in Docker.

```bash
docker compose -f infra/docker-compose.yml up -d postgres
cd backend && uv run pytest tests -q
```

Result: **92 passed, 1 skipped in ~6s.**

The single skip is `tests/test_inner.py` (live-LLM smoke test) when
`ANTHROPIC_API_KEY` is not set. All Postgres-backed tests (checkpointing,
memory, forks) **execute** — they no longer skip, and no test performs a
module-import-time healthcheck (replaced by the session-scoped
`postgres_available` fixture in `backend/tests/conftest.py`).

```bash
cd backend && uv run meta-harness loop --proposer mock --mock-bench --budget 2 --fresh
```

Result: completes in seconds with `"iterations_completed": 2`,
`"persistent": true`, and an `evolution_summary.jsonl` with no duplicate
iteration numbers:

```bash
cat runs/<run>/evolution_summary.jsonl | jq -r .iteration | sort | uniq -d   # empty
```

**Phase 0 exit criteria met:** suite green with Postgres tests executing,
one complete mock-bench loop observed end to end.

---

## Known synthetic values (do not present as results)

- Mock-bench accuracy is `min(0.95, 0.60 + 0.20 * (iteration - 1))` — a
  deterministic fixture, useful for testing, meaningless as a measurement.
  The old "62% → 85%" demo arc derives from this constant and must not be
  quoted as a result anywhere.
- Real-bench path writes literal zeros for `tokens` and `cost_usd`.
- `MemoryPanel.tsx` contains hardcoded fixture patterns.

---

## Phase status (per `documents/REPOSITIONING_PLAN.md`)

| Phase | What | Status |
|---|---|---|
| 0 | Unblock: Postgres up, suite green, mock-bench loop runs | ✅ Complete (evidence above) |
| 1 | `docs/INVARIANTS.md` spec, tests named after invariants | ✅ Complete |
| 2 | Durable branches: `StateStore`, `branch_runs`, leases + fencing tokens, boot reconciliation, worker/API split, LISTEN/NOTIFY SSE | ✅ Complete — exit criterion verified by `tests/test_worker_recovery.py`: worker SIGKILLed mid-branch, second worker reclaims with fence 2, no duplicate iterations |
| 3 | Deterministic simulation testing (`backend/sim/`) + Hypothesis stateful | ✅ Complete — `uv run python -m sim.run --seeds 10000` → 0 failures; two real bugs found and documented with seeds (DST-1 seed 7, DST-2 seed 9270 — see `docs/INVARIANTS.md`) |
| 4 | Observability frontend (4.0 honesty fixes → 4.1 chaos button → 4.2 seed replay) | 4.0 ✅ Complete (demo-run fallback deleted, explicit disconnected state, fixtures labeled). 4.1 ✅ Complete: `POST /debug/kill-worker/{id}` gated by `META_HARNESS_CHAOS=1`, worker registry in the store, "chaos" dashboard tab with per-worker kill -9 buttons, live branch-lease view (status, owner, lease countdown, **fence generation badge**) and a fence-increment log — kill a worker, watch gen N → N+1 as another claims. 4.3 ✅ (folded into the same panel). 4.2 (seed replay viewer), 4.4 (fork UI existed pre-plan), 4.5 (gated on Phase 5) remain |
| 4.2 | Seed replay viewer | ✅ Complete — `/replay` (prerendered static, zero backend): scrubber over exported DST traces, fault markers, invariant violations highlighted at their exact step. Bundled traces: seed 7 (unfenced, I1 double-append), seed 9270 (zombie checkpoint), seed 42 (clean). Any seed: `cd backend && uv run python -m sim.export --seed N -o trace.json` → load in the page. Export is byte-deterministic (`tests/test_sim.py`) |
| 5 | wasmtime sandbox (gated; Docker-per-trial fallback) | ✅ Landed as **Docker-per-trial** per the plan's fallback rule — the wasm spike concluded structurally: WASI has no subprocess/shell and the frozen tool contract includes `run_bash` (see `docs/PHASE5_SANDBOX.md`). `META_HARNESS_SANDBOX=docker` → one container per trial, `--network none`, 512MB/1cpu, workspace bind mount; sandbox kind recorded in every `eval-result.json`. `uv run pytest tests/test_sandbox_docker.py -q` → 4 passed |
| 6 | MCP server (`documents/MCP_SERVER_SPEC.md`) | ✅ Complete — thin stdio adapter (`meta-harness-mcp`, registered in `.mcp.json`) with the six spec tools over REST; gaps filled: `POST /runs/{id}/branches`, `GET`/`DELETE /branches/{branch_id}` (exposes `lease_generation` + `lease_valid`), idempotent `POST /runs/{id}/resume`. Acceptance scenario automated in `backend/tests/test_mcp_acceptance.py`: an outside MCP client starts a run, forks a mid-point checkpoint with mods, the owning worker is SIGKILLed, the client's next poll shows the fence incremented 1→2 under a new owner, the branch converges, and `evolution_summary.jsonl` has no duplicate iterations. `uv run pytest tests/test_mcp_acceptance.py -q` → 1 passed |

Notes vs. the plan:

- The plan's Phase 0 "move to WSL2" item is moot — this workspace is macOS,
  so the Unix `/tmp` assumptions hold natively. The 2026-04-26 Windows
  failure report (31 passed / 21 skipped / 3 failed / 31 errors) does not
  reproduce here and is superseded by the snapshot above.
- `asyncio_mode = "auto"` was already set in `backend/pyproject.toml`; the
  hand-rolled `async_test` decorators in `test_streaming.py` and
  `test_branches.py` have been deleted.

---

## Component triage (what is core vs. workload)

- **Core (the product):** `AsyncPostgresSaver` checkpointing,
  `branches.py` fork semantics, `resume_outer_loop`, SSE streaming, the D3
  trajectory tree (observability UI).
- **Workload (still works, not the claim):** `proposer.py`, the 11
  override points, `SKILL.md`, the Pareto frontier, the 5 search + 2
  holdout eval tasks.
- **Known durability gaps (Phase 2 targets):** branch registry is
  in-process `dict`s in `branches.py` — lost on restart; `EventRegistry`
  is in-process — breaks the moment worker and API are separate processes;
  no leases, no fencing, no boot reconciliation.

---

## Known issues

| Issue | Severity | Notes |
|-------|----------|-------|
| ~~Branch registry/metadata in-process only~~ | Fixed (Phase 2) | `branch_runs` table + leases + fencing tokens; in-process registry remains only as the memory-mode fallback |
| ~~Dashboard falls back to a mock demo run when backend is unreachable~~ | Fixed (Phase 4.0) | Explicit disconnected state; fabricated demo fixture deleted |
| ~~Frontend `getDiff()` / `getTestOutput()` return `null`~~ | Was already fixed | Verified wired to the real endpoints; the doc claim was stale |
| `tokens` / `cost_usd` are zero in real-bench results | Low (demoted) | Real token accounting is explicitly optional in the repositioning plan; no token axis ships in the UI |
| Zombie trailing checkpoint write (DST-2, seed 9270) | Low (documented) | LangGraph checkpoint writes are unfenced; benign — see `docs/INVARIANTS.md` |

---

## Architecture reference

```
meta_harness/
├── agents/               # Candidate harness modules (baseline + generated)
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI REST routers
│   │   ├── meta_harness/ # Core engine (outer/inner loops, branches, persistence)
│   │   ├── cli.py        # Typer CLI
│   │   ├── main.py       # FastAPI app factory
│   │   └── streaming.py  # SSE event registry (closed set of 11 event types)
│   └── tests/            # pytest suite (asyncio_mode = "auto")
├── eval/                 # 5 search tasks + 2 holdout tasks + scorer
├── frontend/             # Next.js dashboard
├── infra/                # docker-compose.yml (Postgres 16)
├── documents/            # REPOSITIONING_PLAN.md — the active plan
└── docs/                 # INVARIANTS.md (spec), historical design docs
```

---

## Key documents

- [`documents/REPOSITIONING_PLAN.md`](../documents/REPOSITIONING_PLAN.md) — the active plan; read first
- [`docs/INVARIANTS.md`](INVARIANTS.md) — invariants I1–I7 the runtime is tested against
- [`docs/INTERFACES.md`](INTERFACES.md) — cross-component contracts
- Historical (pre-repositioning, contain synthetic demo-arc numbers):
  `BUILD_ORDER.md`, `DEFINITION_OF_DONE.md`, `PROJECT_KNOWLEDGE_BASE.md`,
  `TEAM_HANDOFF.md`
