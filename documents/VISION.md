# Vision

> A coding agent whose harness improves through use — where every improvement is
> forked from a real trajectory, evaluated against its own parent, and promoted
> only when the evidence clears a threshold we measured rather than guessed.
>
> Installed with one line.

---

## 1. The claim

The underlying model does not change. Claude Opus stays Claude Opus; Codex stays
Codex. What changes is the harness around it — the instructions, the skills, the
repository memory, the verification policy.

The system observes real coding work, detects weaknesses that recur across tasks,
proposes harness changes with evidence attached, evaluates those changes by
forking the trajectories where they failed, and promotes only the ones that hold
up.

The one-line version:

> The coding agent may propose how its harness should improve, but it does not
> get to decide whether the improvement is real.

---

## 2. The end state

```bash
curl -fsSL https://harness.dev/install | sh
```

```bash
cd ~/my-repo
harness init
harness wrap claude
```

From here the developer uses the coding agent they already use. Nothing about
their workflow changes.

```
> fix the authentication race condition and add tests
```

Claude does the task. The harness sits around it — logging the trajectory,
injecting the repo facts it has learned, checking the verification policy before
submit. Twenty tasks later it notices the agent has rerun the full monorepo
suite eight times when it only touched one package. It forks the trajectories
where that happened, tries three fixes, and promotes the one that holds.

```bash
harness status
```

```
Project: my-repo          Agent: claude
Active:  H21              Tasks since promotion: 47

Success       88.4%   (H20: 82.1%)
Tool calls    9.1     (H20: 14.3)

Last promotion: H20 → H21
  Agent ran full monorepo tests unnecessarily in 8/31 tasks.
  Added targeted_monorepo_testing skill.
  Holdout: -34% shell runtime, -21% tokens. No regression detected.

Next reflection: 3 tasks
```

Everything is local. No account, no server, no data leaving the machine.

### How one line installs a distributed runtime

It doesn't — and this is the design constraint that matters most for the
product.

The system runs in two modes behind one `StateStore` interface:

| | **local** | **service** |
|---|---|---|
| store | SQLite, single file | Postgres |
| workers | in-process | separate processes |
| events | in-memory queue | LISTEN/NOTIFY |
| install | `curl \| sh` | docker-compose |
| for | one developer, one repo | teams, parallel evaluation, research runs |

Fencing tokens, lease generations, branch lifecycle, and crash reconciliation are
**identical in both**. They're rows in a table; the table's backing store is an
implementation detail. I1–I7 must hold in local mode or local mode ships broken.

The deterministic simulator already runs against an in-memory store, which means
the invariant suite covers local mode from day one. Local is the default. Service
is what you graduate to when you want twelve candidates evaluated in parallel.

### The second channel

The MCP adapter is already built — six tools, one line to register:

```bash
claude mcp add harness -- harness mcp
```

This gives the agent `fork_from_checkpoint` and `resume_run` as capabilities it
can call itself. It composes with the agent rather than wrapping it, and it's the
right surface for anyone who wants the durability without the refinement loop.

Both channels, same binary.

### What `curl | sh` has to be true for

- single binary, no runtime dependency beyond Docker for sandboxed evaluation
- zero config to first useful task
- works offline except for the model API
- uninstall is deleting one directory
- nothing writes outside the project directory and `~/.harness`

If a feature breaks any of these, it belongs in service mode.

---

## 3. Why this is not the twentieth harness-evolution paper

Harness evolution became a crowded field in 2026. AHE, DGM, HarnessX, SkillOpt,
GEPA, RHO, Prime Agent — all run some version of the same loop: represent,
propose, validate, evaluate, select, archive.

Every one of them evaluates a candidate harness by **rerunning tasks from
scratch**. That is the field's binding constraint. Evaluation cost dominates
everything, and it is why refinement cycles cost thousands of dollars and why
promotion thresholds get set at effect sizes too small to detect.

We evaluate candidates by **forking the trajectory at the point where the
weakness occurred**.

### Counterfactual harness evaluation

Reflection does not just identify *what* went wrong. It identifies *where* —
step 40, the blind retry after the test failed, the third redundant read of the
same file.

We fork the checkpoint at step 39 and run three candidate harnesses forward from
identical state.

```
            trajectory T
  step 0 ──────────────────► step 39 ──┬──► H1 tail
                                       ├──► H2 tail
                                       └──► H3 tail
```

Every source of variance before step 39 — model sampling, tool ordering,
environment noise, task difficulty — is **shared exactly** and cancels in the
comparison. This is common random numbers taken to its limit.

Consequences:

- far fewer replications needed to separate candidates
- only the tail is executed, not the whole task
- the three branches parallelize across workers by construction
- the divergence point is the hypothesis, made literal

Nobody else in the literature can do this, because nobody else built branch
lifecycle as durable state.

**This is the contribution. Everything else is table stakes.**

It is also what makes Section 2 possible. Refinement that costs a full benchmark
rerun belongs on a cluster. Refinement that costs three trajectory tails can run
on a laptop while the developer gets coffee.

---

## 4. What we inherit

**From Prime Agent** — the continual harness object. Histories, memories, skills,
prompts, and subagent specifications as persisted, serializable state that
survives across trajectories.

**From Meta-Harness** — the durable runtime. Branch lifecycle, lease-based
claiming with fencing tokens, crash reconciliation, fork-from-arbitrary-
checkpoint with state mutation, and seven invariants verified by seeded
fault-injecting simulation.

The fit is clean because both already treat harness state as serializable.
Prime Agent has continuity across trajectories but no way to fork one, evaluate
the fork, and reject it. Meta-Harness has forking but no harness content worth
forking.

Their object, our lifecycle:

- a harness version **is** checkpoint state
- a candidate **is** a branch
- a rollback **is** a fence increment
- a promotion **is** `update_frontier`

---

## 5. What is proven and what is open

Being precise about this is the difference between a credible project and a
demo.

### Proven today

Seven invariants (I1–I7), verified three independent ways — real processes under
SIGKILL, an outside MCP client, and 10,000 seeds of deterministic simulation
with zero failures. Two real bugs found and documented with their seeds.

These are *deterministic* claims. They hold or a counterexample exists. No
statistics required.

### Open

Whether harness refinement improves coding performance, in this system, at a
cost we can afford.

That is a *statistical* claim. It requires a measured noise floor, paired
comparison, held-out tasks, and pre-registered thresholds. It is the reason
Section 3 matters: forking is what makes the open question answerable on a
laptop instead of a cluster.

---

## 6. Honesty rules

Non-negotiable, and inherited from the existing README:

1. Every number in any doc is reproducible by a command in that doc.
2. Mock-bench scores follow a fixture curve and are **never** presented as
   measurements.
3. Historical design docs contain synthetic demo-arc numbers. They are not
   results and must not be quoted as such.
4. Claims about durability and claims about capability are reported separately
   and never blended into one headline.
5. A null test (H vs itself) is run before any comparison is trusted. If it
   fails, nothing above it is reported.
6. `harness status` never shows a delta that hasn't cleared the gate. No
   encouraging-looking numbers in the CLI that the statistics don't support.

The literature this project sits in has a credibility problem — several 2026
harness papers run on forward-dated model names, so only deltas are readable.
We do not add to that.

---

## 7. Prior art, honestly

| System | What it evolves | How it evaluates | What we take |
|---|---|---|---|
| AHE | 7 file-level components | rerun, greedy + rollback | component→failure attribution |
| DGM | whole agent repo | staged cascade 10→60→200 | archive; hide the verifier |
| SkillOpt | one skill document | held-out gate, strict splits | train/select/test discipline |
| GEPA | module prompts | Pareto over instances | reflection as gradient |
| RHO | harness, no grader | self-preference over rollouts | retrospective framing |
| Prime Agent | continual harness | — | the harness object itself |

Findings from that literature that shaped our design:

- **The prompt is the worst surface.** AHE's ablation: evolving tools,
  middleware, and memory drives gains; evolving the system prompt *alone*
  regresses by 2.3 points and fails to transfer. We build memory and
  verification policy before instructions.
- **Selection is the bottleneck, not proposal.** AHE's attribution is ~5× better
  than random at predicting which tasks an edit will fix, and barely better than
  random at predicting which it will break. Reliable for fixes, blind to
  regressions.
- **Discrimination ≠ difficulty.** A task helps selection only where candidates
  disagree. Partial-pass tasks are the most valuable diagnostic; uniformly
  passed or uniformly failed tasks carry zero signal regardless of hardness.
- **Harnesses rot.** Adaptive Auto-Harness watched a single densely-evolved
  harness overfit a task stream while its prompt ballooned from ~2 KB to 68 KB
  and accuracy declined. Complexity penalty from day one.
- **Verifiers get gamed.** STOP's scaffold flipped `use_sandbox=True → False`.
  A DGM agent scored perfectly by deleting the logging markers its own detector
  read — and hacking happened *more* often when the checking code was visible.
  Hide the verifier, don't just isolate it.

---

## 8. Success criteria

The project succeeds if:

- **Forking reduces variance.** A measured variance ratio between fork-based and
  from-scratch evaluation of the same harness change. Either direction is a
  result; the ratio is the deliverable.
- **Improvement is real.** A paired comparison clears the noise floor on
  held-out tasks, not just on the tasks that triggered refinement.
- **Improvement is attributable.** Every promotion carries evidence, hypothesis,
  mutation, evaluation, and delta — visible in `harness status`.
- **Cost stays bounded.** A refinement cycle fits in a laptop's overnight budget.
- **The loop repeats.** Not `H0 → H1`, but a chain with measurable gain at each
  step and a periodic re-test of the active harness against the original H0 to
  catch drift.
- **One line installs it.** Local mode, SQLite, invariants intact, first useful
  task with zero configuration.

---

## 9. Non-goals

- training or fine-tuning model weights
- replacing Claude Code or Codex
- evolving arbitrary executable code
- letting the candidate reach the evaluator, the benchmark, or the score store
- running refinement after every prompt
- requiring an account, a server, or any data leaving the machine
