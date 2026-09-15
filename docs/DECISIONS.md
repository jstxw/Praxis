# DECISIONS — where the build departs from, or adds to, the spec

`documents/DESIGN.md` says everything should be buildable without further
decisions. A handful weren't. Each is recorded here with the reason, so a
reviewer can disagree with the reason rather than discover the departure.

---

## D1 — Success guard in the promotion gate is paired and noise-aware

**Spec (DESIGN §6):** `if candidate.success_ci_lower < parent.success: return False`

**Built (`app/refinement/gate.py`):** reject if the lower bound of the
*paired per-task success-improvement* CI is below `−NOISE_FLOOR[success]`.

**Why:** taken literally, the rule compares a candidate's CI lower bound
with the parent's point estimate. Any bootstrap CI at n=10 tasks dips below
its own mean, so a candidate with exactly the parent's success rate would
always be rejected — promotion would be impossible precisely when an
efficiency win is clean and success is held. "Must not decrease" is kept;
"decrease" means beyond what the null test (E1) says is noise.

## D2 — Regression-set guard is noise-aware

**Spec:** `if candidate.regression_delta < 0: return False`

**Built:** reject if the regression-set success improvement point estimate
is below `−NOISE_FLOOR[success]`.

**Why:** same argument as D1. A raw `< 0` on five regression tasks fires on
one unlucky rep.

## D3 — Effect size is the CI lower bound of the primary metric

**Spec:** `if candidate.effect_size < cfg.NOISE_FLOOR * 2` and "compare CI
lower bounds, not point estimates".

**Built:** `effect_size` = lower bound of the bootstrap CI on the
per-task *relative* reduction in the primary metric (default `tool_calls`),
with the CI widened by Bonferroni for the number of candidates. Holm-
adjusted Wilcoxon p ≤ alpha is additionally required.

## D4 — Noise floor definition

**Spec:** "the noise floor, written to config" (E1), not defined numerically.

**Built (`app/refinement/experiments.py::run_e1`):** for each metric, the
larger magnitude of the two endpoints of the null comparison's (H vs H)
95% bootstrap CI. It is the smallest paired effect the pipeline can
distinguish from zero at that n and k. Floors are keyed by
`(agent, model_version)` and carry provenance; a floor whose null test
failed is stored but refused by the gate (`NoiseFloorUnmeasured`).

## D5 — Holdout comparisons run from scratch until E2 says forks agree

**Spec:** fork-based evaluation is "the mechanism that distinguishes this
system"; the gate is on holdout tasks.

**Built:** the loop's cheap *trigger screen* is fork-based (fork at `d−1`
of the triggering trajectories) for local patterns. The *holdout gate*
runs from scratch.

**Why:** forking measures the local effect at one decision point
(ARCHITECTURE §4 "where it is limited"), and whether a fork-based estimate
agrees with a from-scratch one is exactly what E2 measures. Promoting on a
method before its agreement is measured would put an unvalidated estimator
in front of the gate. `run_e2` reports sign and significance agreement; if
it holds, switching the holdout method is a one-argument change
(`method="fork"` in `RefinementLoop.cycle`).

## D6 — Independent seeds per arm, even for the synthetic agent

A seeded agent *could* share random numbers across arms (common random
numbers for the whole run). A hosted model cannot. Sharing seeds would make
from-scratch comparisons look less noisy than they would be on a real
agent, and would flatter the fork method in E2 by comparison. Seeds are
derived per `(arm, task, rep)`; forking shares the *prefix*, never the tail.

## D7 — Data model extensions (DESIGN §1)

- `trajectories.memory_version` — memory is pinned per comparison; without
  the column the freeze is unverifiable. The trusted plane refuses to
  evaluate a staged trajectory whose memory version doesn't match its arm.
- `trajectories.agent` — synthetic and real trajectories must never be
  mixed in a reported number.
- `trajectory_steps` — the per-step checkpoints (workspace snapshot id +
  adapter resume state). DESIGN says the model "extends checkpoints"; the
  LangGraph checkpoint tables are unchanged and harness checkpoints live
  here.
- `task_evaluations.voided / audit / isolation`, `comparisons.details /
  experiment`, and `reflections`, `projects`, `harness_events`.
- Portable types (TEXT JSON instead of JSONB / UUID[]) so one query text
  runs on SQLite and Postgres (ARCHITECTURE §1a).

## D8 — Claim partitioning on the shared `branch_runs` table

In service mode the LangGraph outer-loop workers and harness-evaluation
workers share `branch_runs`. Unfiltered `claim_next_branch` lets either
fleet claim the other's branches. Added optional `run_prefix` /
`exclude_run_prefix` to `StateStore.claim_next_branch` in all three
backings (portable `LIKE … ESCAPE '!'`), evaluation runs use `hx-…` ids,
and `app/worker.py` excludes them. Covered by
`test_i5_claim_filters_partition_queues` on memory, SQLite and Postgres.

## D9 — The synthetic agent

Not in the spec. It exists because the pipeline (capture, fork, evaluate,
statistics, gate, loop) has to be exercised end to end at a scale no live
model budget allows, deterministically. It is an oracle simulator with
*assumed* habits and *assumed* per-component compliance (verification 0.9 >
memory 0.85 > skill 0.75 > instruction 0.5 — echoing the published
ablation, not measured). Every trajectory is tagged `agent=synthetic`;
every experiment result file on it carries a label saying it is pipeline
validation, not a capability measurement. The experiments it can
legitimately answer are about the *machinery*: does the null test pass
when nothing changed, is sabotage detected, does the gate reject what it
should. It cannot answer whether refinement helps a real agent.

## D10 — Claude Code isolation boundary

ARCHITECTURE §5 draws the candidate sandbox as `docker --network none` with
the agent inside. A hosted-model agent needs the network for its model API,
so it cannot run under `--network none`. As built:

- the **agent** runs as a host process in a fresh workspace copy, with
  `--setting-sources local --strict-mcp-config`, a fixed `--allowedTools`
  list (Bash restricted to pytest/ls), and `acceptEdits` — confined by
  Claude Code's permission system, not by a container;
- the **verifier** runs in `docker --network none` (512MB, 1 CPU) against
  pristine tests restored into a private copy, and candidate evaluation
  refuses to run without Docker.

The agent never sees the evaluator, the corpus's gold fixes, the score
store, thresholds, or the hidden test restoration. Honest label: agent
isolation is permission-based; verifier isolation is container-based.

## D11 — Forking a real Claude Code trajectory

Claude Code has no checkpoint API. A fork copies the persisted session
JSONL truncated after the k-th tool result, rewrites the source workspace
path to the fork's workspace and the session id to a fresh one, and resumes
it with `--resume` plus one "Continue the task" user turn (identical for
every arm). `--system-prompt-snapshot off` makes the resumed process render
the *arm's* harness context rather than replaying the prefix's. Verified
live on 2026-09-15 (Haiku 4.5, one run): the resumed agent continued after
its first `Read`, edited the file in the fork workspace, and ran the test.
This depends on an undocumented on-disk format and can break with Claude
Code upgrades; `test_fork_session_truncates_after_k_tool_results_and_rewrites_paths`
pins the assumptions, and the opt-in live test (`HARNESS_LIVE_CLAUDE=1`)
re-checks them.

## D12 — Project-mode (`harness wrap`) evaluation scope

`harness wrap claude` observes real work through Claude Code hooks: context
injected per prompt (`UserPromptSubmit`), steps recorded (`PostToolUse`),
the project's verification command run at `Stop` and recorded with
isolation labeled `host (project verification command; not sandboxed)`.
The policy runs after every task and leaves a `reflection-due` marker.

What is **not** built: re-running a user's own past tasks as holdout
evaluations. Those tasks have no hidden verifier, no gold fix, and no
pristine-test contract, so a comparison on them could not satisfy the
audit and hygiene rules. `harness refine` therefore evaluates candidates on
the pre-registered corpus task sets with the project's agent. Wrap-mode
trajectories are also not forkable (no per-step workspace snapshots of a
user repo); reflection on them still yields patterns and divergence steps.

## D13 — Analysis revision after the v1 synthetic experiments

The first (v1) synthetic E1/E2/E4/E5 runs computed every non-success metric
as a *relative* per-task delta `(child − parent) / max(|parent|, 1e-9)`. For
count metrics whose parent mean is zero on a task — injected context tokens
under an empty H0, redundant reads, out-of-scope files, regressions — that
divides by ~0; E4 v1 reported a context-token "improvement" of −1.37e11.

**Revision (v2):** relative deltas only for `tool_calls` and `tokens` (never
zero); absolute per-task deltas for success and all count metrics. The
primary metric (`tool_calls`), and therefore every gate decision and the
tool-call noise floor, is computed identically in v1 and v2.

This changes an analysis definition after results were seen, which the
pre-registration rule exists to prevent. It is handled the honest way: the
v1 result files stay committed unchanged; the v2 experiments were
**re-registered** (new timestamped files whose spec carries
`analysis_revision`) and **re-run** from scratch, and v2 is what the docs
report. `test_count_metrics_with_zero_parent_use_absolute_deltas` pins it.

## D14 — Token counts from agents that report usage only at the end

Claude Code reports token usage only in its final `result` event. A run cut
off by the tool-call cap or the wall-clock budget terminates before that
event and would record almost no tokens — so a candidate that makes the
agent thrash to the cap would look *cheaper*. The recorder now marks such
runs `tokens_complete=false`; the gate refuses to clear the cost check when
any run in either arm has incomplete tokens.

## Review fixes (2026-09-15, post-build)

An independent read-only review of the branch found, and these commits fix:
holdout/regression task data could reach reflection through observed work
(now refused in `run_task` and filtered from policy stats and reflection
views — `test_holdout_tasks_never_reach_reflection`); the drift check summed
relative improvements that compound (`claimed_chain_improvement`); the
trigger screen could fall back to non-corpus task ids and crash on wrap-mode
projects; hooks ignored service mode, ran verification outside the project
root, and set no timeout; the docker verifier ran as root and wrote
bytecode into a bind mount (root-owned files on Linux); NUL bytes in step
output broke Postgres inserts; ASCII-escaped JSON broke fork path rewriting
under non-ASCII home directories; `status` printed an ungated drift delta.
