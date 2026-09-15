# Design

Concrete specifications. Everything here should be buildable without further
decisions.

---

## 1. Data model

Extends the existing `branch_runs`, `checkpoints`, `run_events` tables.

```sql
-- a harness version; immutable once created
CREATE TABLE harnesses (
    id              UUID PRIMARY KEY,
    parent_id       UUID REFERENCES harnesses(id),
    memory_version  UUID NOT NULL REFERENCES memory_versions(id),
    status          TEXT NOT NULL,   -- candidate|evaluating|active|rejected|archived
    snapshot        JSONB NOT NULL,  -- I, S, V, C  (NOT memory)
    complexity      JSONB NOT NULL,  -- prompt_tokens, skill_count, policy_count
    created_at      TIMESTAMP NOT NULL
);

-- memory versioned independently; frozen during comparison
CREATE TABLE memory_versions (
    id          UUID PRIMARY KEY,
    parent_id   UUID REFERENCES memory_versions(id),
    entries     JSONB NOT NULL,
    size_bytes  INT NOT NULL,
    created_at  TIMESTAMP NOT NULL
);

-- one row per task execution
CREATE TABLE trajectories (
    id              UUID PRIMARY KEY,
    branch_run_id   UUID NOT NULL,
    harness_id      UUID NOT NULL REFERENCES harnesses(id),
    task_id         TEXT NOT NULL,
    base_commit     TEXT NOT NULL,
    model_version   TEXT NOT NULL,   -- exact, pinned
    image_digest    TEXT NOT NULL,
    fork_parent     UUID REFERENCES trajectories(id),
    fork_step       INT,             -- NULL if from scratch
    result          TEXT NOT NULL,   -- success|failure|timeout|infra_error
    tokens          BIGINT,
    tool_calls      INT,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE task_evaluations (
    id                  UUID PRIMARY KEY,
    trajectory_id       UUID NOT NULL REFERENCES trajectories(id),
    success             BOOLEAN,
    tests_passed        INT,
    tests_total         INT,
    diff_size           INT,
    files_out_of_scope  INT,
    tests_modified      BOOLEAN,     -- audit flag; TRUE voids the run
    tool_calls          INT,
    redundant_reads     INT,
    tokens              BIGINT,
    created_at          TIMESTAMP NOT NULL
);

CREATE TABLE failure_patterns (
    id                  UUID PRIMARY KEY,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL,
    trajectory_ids      UUID[] NOT NULL,
    divergence_steps    INT[] NOT NULL,   -- parallel to trajectory_ids
    occurrence_count    INT NOT NULL,
    affected_component  TEXT,             -- memory|verification|skill|instruction
    scope               TEXT NOT NULL,    -- local|global
    created_at          TIMESTAMP NOT NULL
);

CREATE TABLE mutations (
    id              UUID PRIMARY KEY,
    parent_harness  UUID NOT NULL REFERENCES harnesses(id),
    child_harness   UUID NOT NULL REFERENCES harnesses(id),
    pattern_id      UUID NOT NULL REFERENCES failure_patterns(id),
    hypothesis      TEXT NOT NULL,
    predicted_fix   TEXT[] NOT NULL,   -- task ids: the falsifiable contract
    predicted_risk  TEXT[] NOT NULL,
    payload         JSONB NOT NULL
);

CREATE TABLE comparisons (
    id              UUID PRIMARY KEY,
    parent_harness  UUID NOT NULL,
    child_harness   UUID NOT NULL,
    task_set        TEXT NOT NULL,   -- trigger|holdout|regression
    method          TEXT NOT NULL,   -- fork|scratch
    n_tasks         INT NOT NULL,
    n_reps          INT NOT NULL,
    metric          TEXT NOT NULL,
    delta           DOUBLE PRECISION NOT NULL,
    ci_lower        DOUBLE PRECISION NOT NULL,
    ci_upper        DOUBLE PRECISION NOT NULL,
    p_value         DOUBLE PRECISION,
    decision        TEXT NOT NULL,   -- promote|reject
    created_at      TIMESTAMP NOT NULL
);
```

Note `model_version` and `image_digest` on every trajectory. Model drift under a
stable alias silently invalidates every comparison spanning it, and nothing warns
you. Record it, and run a periodic baseline canary.

---

## 2. Refinement policy

Cheap, non-LLM, runs after every task.

```python
def should_reflect(stats: HarnessStats) -> bool:
    if stats.tasks_since_last_reflection >= 20:
        return True
    if stats.repeated_failure_count >= 3:
        return True
    if stats.success_rate_drop >= NOISE_FLOOR * 2:
        return True
    if stats.tool_cost_increase >= 0.40:
        return True
    return False
```

`NOISE_FLOOR` is loaded from config, populated by Experiment 1. It is never
hardcoded. A threshold below the noise floor fires on randomness.

**Regression to the mean.** Refinement triggers after a bad run. Performance
improves. It would have improved anyway. This is precisely why the trigger
condition must never also be the success condition — improvement must be
demonstrated on tasks unrelated to the trigger.

---

## 3. Reflection

Input: current harness, recent trajectories, evaluations, historical baseline.

Output:

```python
class ReflectionReport(BaseModel):
    strengths:        list[str]
    failure_patterns: list[FailurePattern]
    should_evolve:    bool
```

### The step attribution requirement

Reflection must emit, for each pattern, the **divergence step** in each
supporting trajectory. Without it there is no fork point and the system falls
back to expensive from-scratch evaluation.

Prompt shape:

```
For each recurring pattern:
  name
  description
  supporting trajectory ids
  for each: the event sequence number where the pattern first manifests
  affected component: memory | verification | skill | instruction
  scope: local (one decision point) | global (behavior from step 0)
```

### Failure classification

Distinguish, before proposing anything:

```
environmental failure   → not a lesson
model randomness        → not a lesson
task ambiguity          → not a lesson
repository issue        → not a lesson
harness deficiency      → a lesson
```

Validate this by hand-labelling first. Two humans label independently; if
inter-rater agreement is poor, the LLM cannot do better and the reflector's
output should not be trusted.

Measure the reflector's **false-positive rate** explicitly. A reflector that
invents patterns generates mutations that fix nothing and burns the whole budget.

### Known limitation

Attribution is reliable at predicting which tasks an edit will fix and near-random
at predicting which it will break. Design accordingly: the regression suite is
not optional, and `predicted_risk` is recorded to be scored later, not trusted now.

---

## 4. Mutation proposal

Four mutation types, in build priority order:

1. **Memory** — repo facts, procedural rules
2. **Verification policy** — what must pass before submit
3. **Skills** — declarative procedures with triggers
4. **Instructions** — prompt overlays (*lowest* priority; instruction-only
   evolution regresses in the published ablation)

Also permitted, and usually neglected: **deletion**. Removing a stale memory fact
or a misfiring skill is as valuable as adding one, and is the only defense
against harness rot.

Every proposal carries a falsifiable contract — `predicted_fix` and
`predicted_risk` task ids — checked on the next cycle. This converts
trial-and-error into hypothesis testing and gives you a measurable track record
for the proposer.

Branch factor: 3.

---

## 5. Task sets

Three disjoint sets, **fixed before any results are viewed**.

| Set | Purpose | Promotion relevance |
|---|---|---|
| trigger | where the weakness appeared | none — improvement here proves nothing |
| holdout | same capability, different tasks | **the promotion criterion** |
| regression | unrelated | must not degrade |

Draw evolution tasks from a repo set disjoint from the reporting benchmark.

### Selecting for discrimination, not difficulty

A task helps selection only where candidates disagree. Define:

```
Disc(t) = Var_h [ V(π_h, t) ]
```

With a binary verifier and fraction `ρ` of candidates passing, `Disc = ρ(1-ρ)`.
Both "everyone passes" and "everyone fails" are dead zones, however hard the task.

Prefer **partial-pass tasks** — some replications pass, some fail. These are the
most valuable diagnostics. The discriminating band moves as the candidate pool
improves, so re-estimate it each cycle.

Difficulty alone is a *worse* selector than random in published ablation; it
needs difficulty × diversity.

### Task hygiene

Before any task enters a set, run the **gold patch test**: apply the known-correct
fix, k times. Any task where the correct fix does not pass consistently is flaky
and is excluded. Also verify every task *fails* at base commit — a task that
passes empty is broken.

---

## 6. Comparison protocol

### Design

- **Paired.** Same task, same base commit, same memory version, both arms.
- **Repeated.** k ≥ 5, k ≥ 10 for tasks near the pass/fail boundary.
- **Interleaved.** Never all-of-A then all-of-B — that confounds harness with
  time-of-day load, rate limiting, and provider-side change.
- **Fixed.** Model version, temperature, tool set, image digest, memory version.

### Metrics

| Tier | Metric | Role |
|---|---|---|
| primary | tool calls, tokens, redundant reads | continuous; where wins are detectable |
| guard | task success | must not decrease |
| quality | diff size, out-of-scope files, regressions | catches "passed, wrecked the code" |
| complexity | harness bytes, skill count, injected tokens | penalty term |

Binary success is low-information. At 20 tasks and k=5, a 25% efficiency delta is
detectable; a 3-point success delta is not. **Lead with efficiency, guard with
success.**

Latency is dominated by provider queueing and is not a harness metric. Report it
with caveats or not at all.

### Statistics

Wilcoxon signed-rank on per-task deltas. Non-parametric, paired, small-n
appropriate.

Report effect size and a bootstrap confidence interval. Compare **CI lower
bounds**, not point estimates. A +5% point estimate with CI [-3%, +13%] is not an
improvement.

Correct for multiple comparisons — three candidates against one parent is three
chances to get lucky.

### The gate

```python
def promotable(parent, candidate, cfg) -> bool:
    if candidate.holdout_tasks < cfg.MIN_TASKS:      return False
    if candidate.reps_per_task < cfg.MIN_REPS:       return False
    if candidate.tests_modified:                     return False   # audit
    if candidate.success_ci_lower < parent.success:  return False
    if candidate.regression_delta < 0:               return False
    if candidate.cost_delta > cfg.MAX_COST:          return False
    if candidate.effect_size < cfg.NOISE_FLOOR * 2:  return False
    return True
```

`cfg.NOISE_FLOOR` comes from Experiment 1. Score is penalized for complexity:

```
Score' = Score - λ · Complexity(H)
```

Prefer the smallest harness producing the improvement.

---

## 7. Experiments

Ordered by how cheaply each can kill the project. Pre-register metric, threshold,
and task sets in a timestamped file **before** running.

### E0 — Real workload

Replace the mock-bench fixture curve with a real agent, real tasks, real
verification. Success: full test suite and 10,000-seed DST still pass with real
nondeterminism attached.

*This is itself a result — durability invariants holding under a real stochastic
workload is worth reporting.*

### E1 — Noise floor

H vs H, 20 tasks, k=5, interleaved.

Output: the noise floor, written to config. Also the **null test** — if H vs
itself shows a significant difference, the pipeline is broken and nothing above
it is trustworthy.

Also run the **sabotage test**: H vs a deliberately terrible harness. Must show a
clear loss, confirming you can detect real effects at all.

*Kill criterion:* if detectable effect size exceeds plausible harness effects,
stop and publish the negative result. Nobody has published this cleanly and
everyone in the field needs it.

### E2 — Does forking reduce variance

**The core experiment.**

Same harness change, evaluated both ways:

- fork-based: fork at step `d`, run tail, k reps
- from-scratch: full rerun, k reps

Report: variance ratio, cost ratio, and agreement between the two methods'
conclusions.

*If forking cuts required runs 3–5×* — methods contribution, and everything
downstream gets cheaper.

*If it doesn't* — because divergence compounds quickly and a shared prefix buys
less than expected — that is also a finding, and far better learned in week three
than month five.

### E3 — Reflection accuracy

No new agent runs. Hand-label the trajectories from E1/E2 by failure cause, check
inter-rater agreement, then measure the reflector's precision and recall on
"harness deficiency."

*Kill criterion:* a high false-positive rate means every refinement cycle chases
phantoms.

### E4 — One real comparison

Hand-written good harness vs empty, paired, at an effect size the noise floor
permits. Efficiency primary, success as guard.

### E5 — Generalization

Same mutation, evaluated on trigger vs holdout.

*Kill criterion:* improvement on trigger but not holdout means the system is an
overfitting machine. This is the failure mode most likely to fool you into
thinking it works.

---

## 8. Build phases

| Phase | Deliverable | Gate to proceed |
|---|---|---|
| 0 | real workload wired in; invariants hold | DST green |
| 1 | experience store + trigger policy | E1 null test passes |
| 2 | fork-based evaluation harness | E2 produces a ratio |
| 3 | memory versioning; freeze-during-compare | memory isolated in comparison |
| 4 | reflection + step attribution | E3 FPR acceptable |
| 5 | comparison gate + promotion | E4/E5 clear |
| 6 | continuous loop under budget | all above |

**Do not build until Phase 5 clears:** Pareto frontier, shadow mode, canary
rollout, successive halving, multi-agent adapters, dashboard expansion. Each
presumes an affordable, trustworthy comparison. Phases 1–2 decide whether one
exists.

---

## 9. Budget

Per comparison: `tasks × reps × 2 arms` full agent executions.

At 20 tasks, k=5, that is 200 from-scratch runs. Fork-based runs execute only the
tail, so the marginal cost is a fraction of that — quantifying the fraction is E2.

Per refinement cycle:

```yaml
refinement:
  branch_factor: 3
  cheap_eval_tasks: 3
  survivors: 1-2
  holdout_tasks: 10
  regression_tasks: 5
  max_wall_clock_minutes: 120
```

Cost controls: cheap model for reflection, cache results per
`(harness, task, memory_version)`, early termination on cheap tasks, reuse parent
baselines across cycles, and compress trajectories before they reach the
reflector.

---

## 10. Failure modes to watch

**Contamination** — model drift under a stable alias; benchmark leakage from
training data; sandbox reuse warming caches; prompt caching making the second run
cheaper.

**Measurement** — ordering effects; timeouts logged as plain failures (a thorough
harness may time out where a sloppy one finishes fast and wrong); flaky benchmark
tasks; binary scoring discarding partial credit.

**Gaming** — test deletion; `@skip` decorators; catch-all exception handlers that
pass tests and destroy code; empty diffs on already-passing tasks.

**Self-deception** — peeking before setting thresholds; cherry-picked holdout;
multiple comparisons uncorrected; compounding false promotions across generations.

For the last one: periodically re-test the **active harness against the original
H0**, not just against its immediate parent. If `H21` does not beat `H0` by more
than the sum of claimed deltas, the chain has drifted and the intermediate
promotions were noise.

**Harness-layer** — empty or degenerate trajectories; context exhaustion caused by
harness injection itself; stale memory after a refactor (a confidently wrong repo
fact is worse than no fact); skills with overlapping triggers and contradictory
steps; instruction interference, where adding the eighth instruction degrades the
third.
