# Architecture

---

## 1. Three layers

```
┌──────────────────────────────────────────────────────┐
│  REFINEMENT LOOP                     runs rarely     │
│  reflect · propose · gate                            │
└──────────────────────────┬───────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────┐
│  CONTINUAL HARNESS            persists across tasks  │
│  memory · skills · instructions · verification       │
└──────────────────────────┬───────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────┐
│  DURABLE RUNTIME        survives crashes, forks any  │
│  checkpoints · branches · fences · replay            │
└──────────────────────────────────────────────────────┘
```

The bottom layer exists and is proven. The middle layer is Prime Agent's harness
object, persisted as checkpoint state. The top layer is what this document
specifies.

The layering matters because harness state and execution state live in the
**same checkpoint**. That is what makes counterfactual evaluation a single
primitive rather than a subsystem.

---

## 1a. Deployment modes

One `StateStore` interface, two backings. Everything above the store is
identical.

| | local (default) | service |
|---|---|---|
| store | SQLite at `~/.harness/state.db` | Postgres |
| workers | in-process, async | separate processes |
| events | in-memory queue | LISTEN/NOTIFY |
| claiming | transaction + lease row | `FOR UPDATE SKIP LOCKED` + lease |
| install | `curl \| sh` | docker-compose |

**Fencing is not a Postgres feature.** `lease_generation` is a column; the
claim-and-increment is a transaction. SQLite's `BEGIN IMMEDIATE` gives the same
serialization a row lock does at single-writer scale. I1–I7 must pass in local
mode or local mode is broken.

The DST suite already runs against an in-memory store, so the invariant coverage
for local mode is mostly existing work — the new surface is the SQLite adapter,
not the guarantees.

Rules for keeping the split honest:

- no SQL that only one backend supports; all queries go through the store interface
- `sim.run` runs against both backends in CI
- the same `eval-result.json` schema regardless of mode
- local mode still requires Docker for sandboxed candidate evaluation; without
  it, candidate evaluation refuses to run rather than silently downgrading to
  `subprocess` isolation

Parallel candidate evaluation is where service mode earns itself: three branches
in-process on a laptop is three sequential tails; three branches across workers
is one wall-clock tail.

---

## 2. The harness object

```
H = (I, S, M, V, C)
```

| Component | Contents | Mutable |
|---|---|---|
| `I` instructions | prompt overlays | yes (lowest priority) |
| `S` skills | declarative procedures with triggers | yes |
| `M` memory | repo facts, behavioral, procedural | **separately versioned** |
| `V` verification | what must pass before submit | yes (highest priority) |
| `C` control | retry caps, read windows, budgets | yes |

Build order is `M → V → S → I`, deliberately inverted from intuition. AHE's
ablation found instruction-only evolution regresses and fails to transfer, while
memory and policy carry the gains.

### Memory is a separate axis

Memory accumulates continuously; the other components change discretely at
promotion. Conflating them breaks the comparison — a candidate evaluated later
has more accumulated repo knowledge than its parent, and the memory gain gets
attributed to the mutation.

Memory carries its own version. **It is frozen during any comparison.** Because
memory lives in the checkpoint, a fork inherits it exactly, which is why this
falls out for free here and does not in systems that rerun from scratch.

---

## 3. The loop

```
CODING TASK
    │
    ▼
ACTIVE HARNESS  H0                    ← checkpoint state
    │
    ▼
CODING AGENT                          ← inner loop:
    │                                   orient → plan → act → verify → submit
    ▼
TOOL / SHELL / FILE ACTIVITY          ← 6 fixed tools, 11 override points
    │
    ▼
TRAJECTORY                            ← run_events, gap-free monotonic seq
    │
    ▼
TASK EVALUATION                       ← eval-result.json, docker sandbox
    │
    ▼
EXPERIENCE STORE                      ← Postgres
    │
    ▼
REFINEMENT POLICY
    │
    ├── no evidence ──────────────────► continue
    │
    └── refinement justified
                  │
                  ▼
              REFLECTION               ← what failed, which component,
                  │                      AND at which step
                  ▼
          MUTATION HYPOTHESES
                  │
                  ▼
        fork_from_checkpoint(step, mods)
                  │
         ┌────────┼────────┐
         ▼        ▼        ▼
        H1       H2       H3           ← three branches, one shared prefix
         │        │        │
         └────────┼────────┘
                  ▼
          TARGETED EVALUATIONS         ← workers claim branches, one fence each
                  │
                  ▼
          COMPARE TO PARENT            ← paired, threshold from measured floor
                  │
         ┌────────┴─────────┐
         ▼                  ▼
       reject             promote
                            │
                            ▼
                           H*  →  update_frontier
```

Left column maps to existing `run_events` and `eval-result.json`. The fan-out is
`fork_from_checkpoint`. Only three nodes are new: **refinement policy**,
**reflection**, **compare to parent**.

---

## 4. Fork-based candidate evaluation

The mechanism that distinguishes this system.

### Naive (everyone else)

```
H1 → rerun task from scratch → score
H2 → rerun task from scratch → score
H3 → rerun task from scratch → score
```

Three full task executions. Every run resamples the entire trajectory, so
prefix variance is unshared and dominates the comparison.

### Fork-based (here)

Reflection reports a divergence step `d` — the point where the failure pattern
manifested.

```
fork_from_checkpoint(trajectory=T, step=d-1, mods=H1)
fork_from_checkpoint(trajectory=T, step=d-1, mods=H2)
fork_from_checkpoint(trajectory=T, step=d-1, mods=H3)
```

Each branch:

- inherits identical state up to `d-1` — files, git SHA, context, memory version
- diverges only at the decision the mutation targets
- executes only the tail
- claims its own lease with its own fencing token (I5)
- writes fenced, so a stalled worker cannot corrupt a sibling (I1)

Parent comparison runs the same tail from the same checkpoint under `H0`.

### Why it is cheaper

Variance in the outcome decomposes into prefix variance and tail variance.
Forking sets prefix variance to zero by construction. The remaining question —
how much of the total variance is prefix — is empirical, and measuring it is
Experiment 2 in `DESIGN.md`.

### Where it is limited

Forking measures the **local** effect of a mutation at one decision point. Some
harness changes are global (a memory fact that changes behavior from step 0).
Those need from-scratch evaluation and cost accordingly. The system supports
both; reflection tags which kind a mutation is.

---

## 5. Trust boundary

```
┌─────────────────────────────────────────────┐
│  TRUSTED CONTROL PLANE                      │
│  (separate process, not reachable)          │
│                                             │
│  evaluator · benchmark · score store        │
│  promotion gate · noise floor config        │
└──────────────────┬──────────────────────────┘
                   │  fenced writes only
                   ▼
┌─────────────────────────────────────────────┐
│  CANDIDATE SANDBOX                          │
│  docker: --network none, 512MB, 1 CPU       │
│                                             │
│  agent · repo copy · tools · harness H_n    │
└─────────────────────────────────────────────┘
```

The candidate cannot reach: hidden tests, evaluator source, benchmark
definitions, the score database, the promotion threshold, or sandbox config.

**The verifier is hidden, not merely isolated.** DGM found that objective hacking
happened *more* often when the checking code was visible.

Audit on every run:

- diff against `tests/` — any unrequested test modification voids the branch
- `@skip` / `xfail` decorators added
- files touched outside the task scope
- diff size against a cap

Note the existing honest labeling: `subprocess` mode shares the host trust
boundary and says so. Candidate evaluation requires `META_HARNESS_SANDBOX=docker`.

---

## 6. Data flow

```
worker                     Postgres                  trusted plane
  │                           │                            │
  ├─ claim branch ───────────►│ fence++                    │
  ├─ run tail                 │                            │
  ├─ emit events ────────────►│ run_events (seq)           │
  ├─ write checkpoint ───────►│ checkpoints (fenced)       │
  ├─ finish ─────────────────►│ branch_runs.completed      │
  │                           │                            │
  │                           │◄──── read trajectory ──────┤
  │                           │                            ├─ evaluate
  │                           │◄──── write score ──────────┤
  │                           │                            ├─ compare
  │                           │◄──── promote / reject ─────┤
```

Workers never write scores. The trusted plane never runs agent code.

---

## 7. Component inventory

### Exists

| Component | Status |
|---|---|
| branch lifecycle, fencing, leases | I1–I7 verified, 10k seeds |
| fork_from_checkpoint | working |
| crash reconciliation | working |
| deterministic simulation + replay | working |
| MCP adapter (6 tools) | working |
| docker trial isolation | working |
| outer loop skeleton | working, mock-benched |
| inner loop | working |
| cross-run memory | partial |

### To build

| Component | Depends on |
|---|---|
| real workload (agent + benchmark + verifier) | — |
| experience store | real workload |
| refinement policy (trigger) | experience store |
| reflection + component/step attribution | experience store |
| comparison gate | measured noise floor |
| memory versioning | — |
| complexity penalty | harness serialization |

---

## 8. Interfaces

### MCP surface

Existing: `start_run`, `fork_from_checkpoint`, `get_branch_status`,
`list_branches`, `cancel_branch`, `resume_run`.

Refinement adds no new MCP tools. Reflection and promotion are internal to the
trusted plane and are not exposed to any client — exposing them would put the
gate inside the searchable space.

### Agent adapter

```python
class CodingAgentAdapter:
    async def start_task(self, task, workspace, harness) -> str: ...
    async def stream_events(self): ...
    async def inject_context(self, context): ...
    async def terminate(self): ...
```

First implementation targets the agent that supports the richest event stream.
A stdout-only wrapper cannot produce the events reflection requires, so
model-agnosticism is deferred, not claimed.

### Context injection

```
<HARNESS_CONTEXT>
Repository facts:  <top-k retrieved>
Active skills:     <triggered only>
Verification:      <policy>
</HARNESS_CONTEXT>
```

Retrieve and rank; never inject the whole harness. Budget: < 2,000 additional
tokens per task. Injected size is a tracked metric, because context spent on the
harness is context taken from the task.
