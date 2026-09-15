# Refinement — what is built, how to run it, what is proven

Companion to `documents/VISION.md`, `documents/ARCHITECTURE.md`,
`documents/DESIGN.md` (the spec) and `docs/DECISIONS.md` (every departure
from it). Every number below is reproducible by the command printed next to
it; synthetic-agent numbers are pipeline validation, never capability.

---

## 1. The three layers, as code

```
REFINEMENT LOOP        app/refinement/
  policy.py            cheap trigger after every task
  reflection.py        patterns + divergence step per trajectory; classification
  mutation.py          3 candidates per lesson across components; deletions
  evaluation.py        comparisons as fenced branches on the runtime
  evaluator.py         trusted plane: hidden verifier (docker) + audit + metrics
  stats.py · gate.py   paired statistics, promotion gate, complexity penalty
  loop.py              observe → reflect → screen → gate → promote → drift check
  experiments.py       E0–E5, pre-registered
  tasks.py · config.py corpus, task sets, hygiene, pre-registration, noise floors

CONTINUAL HARNESS      app/continual/
  harness_object.py    H = (I, S, V, C) immutable + context injection
  experience.py        DESIGN §1 tables, one query text for SQLite + Postgres
  snapshots.py         content-addressed workspace snapshots (checkpoint files)

DURABLE RUNTIME        app/meta_harness/  (existing, extended)
  store.py             StateStore protocol: InMemory + Postgres (+ claim filters)
  sqlite_store.py      local mode: same protocol, one file, BEGIN IMMEDIATE

AGENTS                 app/agents/
  base.py              adapter contract incl. resume-from-checkpoint
  recorder.py          one checkpoint per tool call; staged trajectories
  claude_code.py       Claude Code: stream-json, pinned model, real session forks
  synthetic.py         seeded oracle agent for offline pipeline runs

CLI                    app/harness_cli.py  → `harness`
```

The correspondence VISION §4 promised, made literal:

| concept | implementation |
|---|---|
| a harness version **is** checkpoint state | step 0 of every trajectory records the injected context; every step records `(snapshot_id, agent_state, harness_id, memory_version)` |
| a candidate **is** a branch | every candidate run is a `branch_runs` row claimed with a lease and fence (`evaluation.py`) |
| a rollback **is** a fence increment | a reclaimed or cancelled evaluation branch's stale writes are rejected by `record_iteration` (I5) |
| a promotion **is** `update_frontier` | `RefinementLoop._promote`: parent archived, child active, evidence recorded |

## 2. Running it

```bash
# one-line install (from a checkout; see script header for curl form)
sh scripts/install.sh

# a project, local mode (SQLite under ~/.harness)
cd ~/my-repo
harness init
harness wrap claude          # your normal Claude Code session, observed
harness status

# candidate evaluation needs the sandbox image
docker build -t meta-harness-sandbox -f infra/sandbox.Dockerfile infra

# corpus hygiene and the fixed task sets
harness tasks check          # gold patch 3×, must fail at base, in docker
harness tasks sets

# experiments: register first, then run (runs refuse without registration)
harness experiment e1 --agent synthetic --register
harness experiment e1 --agent synthetic --workers 4

# one refinement cycle for a project
harness refine --observe 20

# the MCP channel, same binary
claude mcp add harness -- harness mcp
```

## 3. Guarantees that are enforced, not documented

| rule (source) | enforced by | test |
|---|---|---|
| I1–I7 hold in local mode (ARCH §1a) | `SQLiteStateStore` under the same contract + DST | `test_store.py` (memory/sqlite/postgres), `test_sim.py`, `sim.run --backend sqlite` |
| memory frozen during any comparison (ARCH §2) | `assert_memory_frozen` at submit; memory check at collect | `test_memory_frozen_during_comparison` |
| workers never write scores (ARCH §6) | staged trajectories → fenced `record_iteration` → trusted `collect` | `test_i5_reclaimed_evaluation_branch_is_recorded_once` |
| verifier hidden, not merely isolated (ARCH §5) | pristine tests restored in a private copy, docker `--network none` | `test_verifier_uses_pristine_tests_and_audit_voids_tampering` |
| no docker → evaluation refuses (ARCH §1a) | `Verifier.docker()` raises `SandboxUnavailable` | CLI exits 3 |
| interleaved, paired, repeated (DESIGN §6) | `plan_comparison` | `test_i1_scratch_plan_runs_every_spec_exactly_once_interleaved` |
| noise floor never hardcoded (DESIGN §2) | `HarnessConfig.noise_floor` raises; policy skips the drop trigger | `test_noise_floor_is_never_defaulted`, `test_policy_triggers_and_never_defaults_noise_floor` |
| null test before any comparison is trusted (VISION §6.5) | floor with failed null test refused | same |
| trigger never the success condition (DESIGN §2) | trigger screen recorded `decision=report`; gate on holdout | `test_policy_holds_until_evidence_then_a_full_cycle_records_everything` |
| task sets fixed before results (DESIGN §5) | hashed pre-registration checked before every run | `test_preregistration_detects_edits`, `test_experiments_refuse_without_matching_preregistration` |
| status shows no ungated deltas (VISION §6.6) | `status_text` prints gate-cleared promotion evidence only | `test_wrap_hooks_record_a_trajectory_and_status_reports_it` |
| divergence step per supporting trajectory (DESIGN §3) | `FailurePattern` parallel arrays validated; LLM citations checked | `test_llm_reflector_drops_citations_that_do_not_exist` |

Full suite: `cd backend && uv run python -m pytest tests -q`.

## 4. Build phases (DESIGN §8) — status as of this branch

| phase | deliverable | gate | status |
|---|---|---|---|
| 0 | real workload wired in; invariants hold | DST green | built; E0 result below |
| 1 | experience store + trigger policy | E1 null test passes | built; E1 below is on the **synthetic** agent only |
| 2 | fork-based evaluation harness | E2 produces a ratio | built; E2 below is on the **synthetic** agent only |
| 3 | memory versioning; freeze-during-compare | memory isolated in comparison | built and tested |
| 4 | reflection + step attribution | E3 FPR acceptable | built; **E3 not run — needs two human label files** |
| 5 | comparison gate + promotion | E4/E5 clear | built; E4/E5 below are on the **synthetic** agent only |
| 6 | continuous loop under budget | all above | built; exercised end to end on the synthetic agent |

**None of the statistical gates has been cleared on a real agent.** The
real-agent path (Claude Code adapter, forking a live session) is built and
verified mechanically (one live fork, 2026-09-15); running E1–E5 on it is a
budget decision, not a code gap:

```bash
harness experiment e1 --agent claude --model claude-haiku-4-5-20251001 --register
harness experiment e1 --agent claude --model claude-haiku-4-5-20251001 --workers 2
```

At 20 tasks × 5 reps × 2 arms × (null + sabotage), E1 is 400 agent runs.

## 5. Experiment results

Results land in `experiments/results/` with the command that produced them.

_Filled in from the result files once the runs complete — see §5 entries
below._
