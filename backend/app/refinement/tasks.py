"""Task corpus, disjoint task sets, hygiene, and pre-registration (DESIGN §5, §7).

- **Corpus** — ``eval/corpus/<task-id>/{task.json, workspace/, gold/}``.
- **Task sets** — trigger / holdout / regression, disjoint, drawn by a
  seeded capability-stratified split and **fixed before any results are
  viewed**: the split is written into a pre-registration file whose
  hash is checked before every experiment run.
- **Hygiene** — the gold patch test: apply the known-correct fix ``k``
  times; any task where it doesn't pass every time is flaky and
  excluded. Also every task must *fail* at its base state — a task that
  passes empty is broken.
- **Pre-registration** — metric, threshold, and task sets in a
  timestamped file before running; ``load_preregistration`` refuses a
  file whose content no longer matches its recorded hash.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.base import TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_DIR = REPO_ROOT / "eval" / "corpus"
PREREGISTRATION_DIR = REPO_ROOT / "experiments" / "preregistered"
SET_NAMES = ("trigger", "holdout", "regression")


def load_task(task_dir: Path) -> TaskSpec:
    spec = json.loads((task_dir / "task.json").read_text())
    return TaskSpec(
        id=spec["id"],
        instruction=spec["instruction"],
        test_command=spec["test_command"],
        full_test_command=spec.get("full_test_command", "pytest -q"),
        scope_files=tuple(spec.get("scope_files", ())),
        test_files=tuple(spec.get("test_files", ())),
        capability=spec.get("capability", "unknown"),
        difficulty=spec.get("difficulty", "medium"),
        source_dir=task_dir,
        base_commit=spec.get("base_commit", _dir_digest(task_dir / "workspace")),
    )


def _dir_digest(path: Path) -> str:
    """Stable content digest of a pristine workspace (its 'base commit')."""
    h = hashlib.sha256()
    if not path.exists():
        return "missing"
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        if "__pycache__" in file.parts or ".pytest_cache" in file.parts:
            continue
        h.update(file.relative_to(path).as_posix().encode())
        h.update(file.read_bytes())
    return "sha256:" + h.hexdigest()[:16]


def load_corpus(root: Path = CORPUS_DIR) -> dict[str, TaskSpec]:
    if not root.exists():
        return {}
    tasks = {}
    for task_dir in sorted(p for p in root.iterdir() if (p / "task.json").exists()):
        task = load_task(task_dir)
        tasks[task.id] = task
    return tasks


@dataclass
class TaskSets:
    trigger: list[str]
    holdout: list[str]
    regression: list[str]
    seed: int
    spare: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        sets = [set(self.trigger), set(self.holdout), set(self.regression)]
        if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
            raise ValueError("task sets must be disjoint")

    def to_json(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "holdout": self.holdout,
            "regression": self.regression,
            "spare": self.spare,
            "seed": self.seed,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TaskSets":
        return cls(
            trigger=list(data["trigger"]),
            holdout=list(data["holdout"]),
            regression=list(data["regression"]),
            spare=list(data.get("spare", [])),
            seed=int(data["seed"]),
        )

    def all_ids(self) -> list[str]:
        return self.trigger + self.holdout + self.regression

    def which(self, task_id: str) -> str | None:
        for name in SET_NAMES:
            if task_id in getattr(self, name):
                return name
        return None


def split_task_sets(
    tasks: dict[str, TaskSpec],
    *,
    seed: int,
    n_trigger: int = 5,
    n_holdout: int = 10,
    n_regression: int = 5,
) -> TaskSets:
    """Seeded, capability-stratified, disjoint split.

    Tasks are dealt round-robin across capabilities after a seeded
    shuffle, so each set covers the capability mix; trigger and holdout
    therefore test "same capability, different tasks".
    """
    rng = random.Random(seed)
    by_cap: dict[str, list[str]] = {}
    for task in tasks.values():
        by_cap.setdefault(task.capability, []).append(task.id)
    for ids in by_cap.values():
        ids.sort()
        rng.shuffle(ids)
    order: list[str] = []
    caps = sorted(by_cap)
    while any(by_cap.values()):
        for cap in caps:
            if by_cap[cap]:
                order.append(by_cap[cap].pop())
    needed = n_trigger + n_holdout + n_regression
    if len(order) < needed:
        raise ValueError(f"need {needed} tasks for the split, corpus has {len(order)}")
    # Deal in the stratified order: trigger, holdout, holdout, regression …
    pattern = ["trigger"] * n_trigger + ["holdout"] * n_holdout + ["regression"] * n_regression
    # interleave the pattern so each set draws across the capability cycle
    slots = _interleave(pattern, rng)
    assigned: dict[str, list[str]] = {"trigger": [], "holdout": [], "regression": []}
    for task_id, slot in zip(order, slots):
        assigned[slot].append(task_id)
    return TaskSets(
        trigger=sorted(assigned["trigger"]),
        holdout=sorted(assigned["holdout"]),
        regression=sorted(assigned["regression"]),
        spare=sorted(order[needed:]),
        seed=seed,
    )


def _interleave(pattern: list[str], rng: random.Random) -> list[str]:
    counts = {name: pattern.count(name) for name in SET_NAMES}
    total = len(pattern)
    out: list[str] = []
    placed = {name: 0 for name in SET_NAMES}
    for i in range(total):
        # pick the set furthest behind its proportional quota
        def deficit(name: str) -> float:
            return counts[name] * (i + 1) / total - placed[name]

        best = max(SET_NAMES, key=lambda n: (deficit(n), rng.random()))
        out.append(best)
        placed[best] += 1
    return out


# ── hygiene ──────────────────────────────────────────────────────────


@dataclass
class HygieneResult:
    task_id: str
    base_fails: bool
    gold_passes: int
    k: int
    ok: bool
    notes: list[str] = field(default_factory=list)


def check_task_hygiene(task: TaskSpec, verifier: Any, *, k: int = 3) -> HygieneResult:
    """Gold patch test (k times) + must-fail-at-base, through the verifier."""
    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="harness-hygiene-") as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(task.workspace_dir, base)
        base_result = verifier.verify(task, base)
        base_fails = not base_result.success
        if not base_fails:
            notes.append("passes at base state — broken task")
        if base_result.regressions:
            notes.append(f"unrelated tests fail at base ({base_result.regressions})")

        gold_passes = 0
        for i in range(k):
            ws = Path(tmp) / f"gold-{i}"
            shutil.copytree(task.workspace_dir, ws)
            if not task.gold_dir.exists():
                notes.append("no gold fix")
                break
            for src in task.gold_dir.rglob("*"):
                if src.is_file():
                    dest = ws / src.relative_to(task.gold_dir)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
            result = verifier.verify(task, ws)
            if result.success and result.regressions == 0:
                gold_passes += 1
        if gold_passes < k:
            notes.append(f"gold passed {gold_passes}/{k} — flaky or wrong, excluded")
    return HygieneResult(
        task_id=task.id,
        base_fails=base_fails,
        gold_passes=gold_passes,
        k=k,
        ok=base_fails and gold_passes == k,
        notes=notes,
    )


# ── pre-registration ─────────────────────────────────────────────────


def _spec_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def preregister(
    experiment: str,
    spec: dict[str, Any],
    *,
    directory: Path = PREREGISTRATION_DIR,
    now: datetime | None = None,
) -> Path:
    """Write a timestamped, hashed pre-registration. Never overwrites."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stamp}-{experiment}.json"
    if path.exists():
        raise FileExistsError(f"pre-registration already exists: {path}")
    document = {
        "experiment": experiment,
        "registered_at": stamp,
        "spec": spec,
        "sha256": _spec_hash(spec),
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path


def load_preregistration(path: Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text())
    if _spec_hash(document["spec"]) != document["sha256"]:
        raise ValueError(
            f"pre-registration {path} was edited after registration "
            "(spec hash mismatch) — results under it are not pre-registered"
        )
    return document


def latest_preregistration(experiment: str, directory: Path = PREREGISTRATION_DIR) -> Path | None:
    if not directory.exists():
        return None
    matches = sorted(directory.glob(f"*-{experiment}.json"))
    return matches[-1] if matches else None
