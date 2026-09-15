# Claude Code — Session Kickoff Prompt

**Paste this at the start of a session, or keep it at the repo root and say "read KICKOFF.md and continue."**

---

## Read first

- `documents/REPOSITIONING_PLAN.md` — phases, invariants I1–I7, non-goals, honesty rules
- `documents/MCP_SERVER_SPEC.md` — Phase 6 only, expanded

Read both fully before writing code.

## Disambiguation — read this before interpreting anything

- **"MCP" in these docs means the MCP server this project exposes (Phase 6).** It is a thing that was *built* (`backend/app/mcp_server.py`), not a tool to *call*. No task in this plan requires you to use an MCP connector.
- **"the plan" = `documents/REPOSITIONING_PLAN.md`.** Not a plan you generate.
- **"the runtime" = this project's durable execution layer.** Not a language runtime.
- **"branch" = a `branch_runs` row / forked execution thread.** Not a git branch.
- **"fence" / "generation" = `lease_generation`, the fencing token.** Not a git or DB fence.
- If a term is still ambiguous after reading both docs, **ask before implementing.** Do not pick the more convenient reading.

## Current state

<!-- EDIT THIS BLOCK EACH SESSION. It is the only part that goes stale. -->

- Runtime plan (REPOSITIONING_PLAN): phases 0–6 complete (unchanged).
- Refinement layer (documents/VISION.md, ARCHITECTURE.md, DESIGN.md) on branch
  `praxis-refinement`: DESIGN §8 phases 0–6 **built**; see `docs/REFINEMENT.md`
  and `docs/DECISIONS.md`. Statistical gates (E1–E5) evaluated on the synthetic
  agent only; **not run on a real agent** (budget decision); E3 needs two human
  label files.
- Suite status (`cd backend && uv run python -m pytest tests -q | tail -1`, 2026-09-15):
  `252 passed, 2 skipped` (skips = live-LLM test, opt-in live Claude fork test)
- DST: `cd backend && uv run python -m sim.run --seeds 10000 [--backend sqlite]` → 0 failures on both backings
  (found bugs documented with seeds in `docs/INVARIANTS.md`: DST-1 seed 7, DST-2 seed 9270)
- Next: real-agent E1 (`harness experiment e1 --agent claude --model <pinned> --register`, then run);
  E3 labelling (`harness experiment e3 --template labels.json`)

## This session

Pick up the first unchecked item in the earliest incomplete phase, unless told otherwise.

Before starting, confirm the previous phase's **exit criteria** actually passes — do not trust checkboxes, run the verification command in `REPOSITIONING_PLAN.md` §6.

## Standing rules

1. **Non-goals are hard.** No Harbor migration, no partial-credit scoring, no accuracy-number work, no Temporal/Celery/Kafka/K8s/gRPC/second database, no Rust rewrite, no new SSE event types. If a task seems to require one of these, stop and say so rather than doing it.
2. **Honesty rules.** Any number that lands in the README, docs, or UI must be reproducible by a command written next to it. Mock-bench values are fixtures and must be labeled as such. Never restore the 62%→85% figure.
3. **Verify before trusting docs.** `docs/PROJECT_STATUS.md` was stale by three months once and contained at least one claim contradicted by the tests. Check source, not docs.
4. **Invariants are named.** New tests covering I1–I7 are named after the invariant they cover.
5. **`StateStore` protocol is load-bearing.** All state access goes through it, with a Postgres impl and an in-memory deterministic fake. Writing directly against psycopg breaks Phase 3.
6. **Ask when blocked rather than substituting.** If an approach isn't working, report it — don't silently swap in a different one.

## Definition of done for any item

- The checkbox's stated behavior actually happens when run
- A test exists, named after its invariant where applicable
- `PROJECT_STATUS.md` updated with real output, not intent
- Nothing in the non-goals list was touched
