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

> **Every number in this section is from the seeded synthetic agent**
> (`agent=synthetic`, an oracle simulator with *assumed* habits and harness
> compliance — `docs/DECISIONS.md` D9). They show whether the measurement
> machinery behaves; they are **not** evidence about real agents.
> Isolation for every evaluation: `docker` verifier. Task sets: seed
> 20260915 split (`harness tasks sets`). Reported below: analysis v2
> (D13); the v1 files are also committed and identical on tool calls,
> tokens and success.

All commands run with `HARNESS_HOME=~/.harness/research` from the repo root.

### E0 — real workload wired in, invariants hold — **passed**

```bash
harness experiment e0 --agent synthetic --workers 4
```
`experiments/results/20260915T082604Z-E0-synthetic.json` (registered
`20260915T082259Z-E0.json`): 20 specs → 20 fenced iteration records, 0
duplicates, 20 evaluations; a worker killed mid-branch had its branch
reclaimed at fence 2; DST 500 seeds on memory and SQLite, 0 failures.

### E1 — noise floor, null test, sabotage test — **null passed, sabotage detected**

```bash
harness experiment e1 --agent synthetic --workers 4
```
`experiments/results/20260915T085245Z-E1-synthetic.json` (registered
`20260915T084641Z-E1.json`), 20 tasks × 5 reps × 2 arms, interleaved.

| | improvement (95% CI) | Wilcoxon p |
|---|---|---|
| null H vs H — tool calls (relative) | −1.8% (−10.5%, +5.8%) | 0.93 |
| null H vs H — tokens (relative) | −3.3% (−13.8%, +5.8%) | 0.78 |
| null H vs H — success (abs.) | −7.0 pts (−19.0, +5.0) | 0.21 |
| sabotage — success (abs.) | **−34.0 pts (−46.0, −21.0)** | 0.001 |
| sabotage — tool calls (relative) | +40.2% (+34.1%, +45.8%) — "efficient" by skipping work | <0.001 |

Every null CI contains 0, no Holm-adjusted p ≤ 0.05; 16/100 sabotage runs
were voided by the audit (test tampering). Noise floor written to config:
tool calls **10.5%**, tokens 13.8%, success 19 pts, redundant reads 0.18.
Kill criterion (primary floor > 30% plausible effect) not triggered. Note
what the floor implies at this n: the gate needs a tool-call CI lower bound
above 2 × 10.5% = **20.9%** — only large effects can be promoted. The
sabotage row is also why efficiency is never promoted without the success
guard.

### E2 — does forking reduce variance — **cheaper and less variable, but does not agree with scratch**

```bash
harness experiment e2 --agent synthetic --workers 4
```
`experiments/results/20260915T085558Z-E2-synthetic.json` (registered
`20260915T084641Z-E2.json`). Change: add the `targeted_tests` rule. 10
holdout tasks × 5 reps × 2 arms, both methods; fork at the step before the
first test run of an H0 seed trajectory.

| metric | fork: improvement (CI) | scratch: improvement (CI) | variance ratio scratch/fork | sign agrees | significance agrees |
|---|---|---|---|---|---|
| tool calls | +4.9% (+1.3, +9.3) | +2.1% (−2.7, +7.4) | 1.75 | yes | **no** |
| tokens | +3.8% (−0.4, +9.3) | −4.7% (−12.1, +2.8) | 1.77 | **no** | yes |
| redundant reads | +0.04 (0.00, +0.10) | +0.08 (−0.04, +0.20) | 7.22 | yes | yes |
| success | −4.0 pts (−10.0, 0.0) | 0.0 pts (−16.0, +16.0) | 0.92 | yes | yes |

Cost: 308 executed tool calls for fork tails vs 809 from scratch (ratio
**2.63**; **1.98** including the 100 calls of seed trajectories). The two
methods also estimate different quantities: the fork arms' parent mean is
9.42 tool calls vs 8.20 from scratch, because forks condition on one
sampled prefix. On this agent the fork method is cheaper and lower-variance
but **disagrees** with scratch on significance (tool calls) and sign
(tokens) — so the holdout gate stays from-scratch (D5). Whether forking
agrees on a real agent is the open question E2 exists to answer.

### E3 — reflection accuracy — **not run**

Needs two independent human label files. `harness experiment e3 --template
labels.json` writes the template; `--labels a.json --labels b.json` scores it.

### E4 — one real comparison (hand-written good harness vs empty) — **rejected by the gate**

```bash
harness experiment e4 --agent synthetic --workers 4
```
`experiments/results/20260915T085816Z-E4-synthetic.json` (registered
`20260915T084642Z-E4.json`), 10 holdout + 5 regression tasks × 5 reps.

| metric | improvement (Bonferroni 95% CI) |
|---|---|
| tool calls (primary) | +1.1% (−12.1%, +13.5%), Holm p 0.96 |
| tokens | −11.8% (−25.1%, +1.5%) — more tokens (injected context) |
| success | **+14.0 pts (+4.0, +28.0)** |
| regression-set success | +32.0 pts (+24.0, +40.0) |
| redundant reads | +0.16 (+0.04, +0.28) |

Gate reasons: `cost: tokens +11.8% > MAX_COST 10%`; `effect: tool_calls CI
lower −0.121 < 2 × noise floor 0.209`; `significance: Holm-adjusted p 0.959`.

**This exposes a real design tension, not a bug:** DESIGN §6 says "lead
with efficiency, guard with success". A harness whose benefit is *success*
(here +14 pts on holdout, +32 on regression) and not efficiency can never
be promoted under an efficiency primary metric with a token cost cap. The
synthetic agent's token model is an assumption, so the magnitudes are not
evidence — but the gate logic would reject the same shape of result on a
real agent. Deciding whether success should be an alternative primary
criterion is left to the project owner.

### E5 — generalization (trigger vs holdout) — **no overfitting signal**

```bash
harness experiment e5 --agent synthetic --workers 4
```
`experiments/results/20260915T090042Z-E5-synthetic.json` (registered
`20260915T084642Z-E5.json`). Reflection on H0's trigger-set trajectories
chose `redundant_reads → skill: read_once`.

| | trigger (5 tasks × 5) | holdout (10 tasks × 5) |
|---|---|---|
| tool calls (primary) | +2.1% (−6.2%, +8.8%) | −0.9% (−11.9%, +9.7%) |
| redundant reads | +0.16 (−0.16, +0.44) | **+0.34 (+0.22, +0.46)** |
| success | +8.0 pts (−8.0, +24.0) | +2.0 pts (−14.0, +16.0) |

Kill criterion (trigger CI lower > 0 while holdout ≤ 0): not triggered.
The mutation did what its hypothesis said (fewer re-reads, detectable on
holdout) without moving the primary metric — so it would not be promoted.
