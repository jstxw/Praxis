"""Trusted plane: hidden verifier, audit, and metrics (ARCHITECTURE §5).

The candidate cannot reach any of this: it runs in the evaluating
process, reads staged trajectories, and is the only writer of
``trajectories.result`` and ``task_evaluations``.

**The verifier is hidden, not merely isolated.** It never trusts the
agent's test files: it materializes the final workspace into a private
copy, overwrites every pristine test file (and deletes any conftest the
agent added), and runs the suite there. An agent that edits or skips
tests gains nothing from it — and the audit voids the run anyway.

**Isolation is honest.** ``Verifier.docker()`` runs the suite in a
throwaway container (``--network none``, 512MB, 1 CPU). Without Docker,
candidate evaluation refuses to run (ARCHITECTURE §1a). The
``subprocess`` verifier exists for unit tests and is only constructible
by name; its isolation label is written into every evaluation it makes.
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.base import TOOL_PATCH, TOOL_READ, TOOL_WRITE, TaskSpec
from app.agents.recorder import StagedTrajectory
from app.continual.experience import EvaluationRecord, StepRecord, TrajectoryRecord
from app.continual.snapshots import SnapshotStore
from app.meta_harness.sandbox import DOCKER_IMAGE

DEFAULT_DIFF_CAP_LINES = 400
_SKIP_MARKERS = re.compile(
    r"pytest\.mark\.(skip|skipif|xfail)|pytest\.(skip|xfail)\(|unittest\.skip|@skip\b"
)
_JUNIT = ".harness-verifier.xml"


class SandboxUnavailable(RuntimeError):
    """Candidate evaluation requires Docker; refusing to downgrade."""


@dataclass
class VerificationResult:
    success: bool
    tests_passed: int
    tests_total: int
    task_tests_passed: int
    task_tests_total: int
    regressions: int  # failures outside the task's own test files
    isolation: str
    output: str


@dataclass
class AuditResult:
    tests_modified: bool
    skip_markers_added: int
    files_out_of_scope: list[str]
    diff_size: int
    voided: bool
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "tests_modified": self.tests_modified,
            "skip_markers_added": self.skip_markers_added,
            "files_out_of_scope": self.files_out_of_scope,
            "diff_size": self.diff_size,
            "voided": self.voided,
            "reasons": self.reasons,
        }


def docker_available(image: str = DOCKER_IMAGE) -> bool:
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
        if info.returncode != 0:
            return False
        img = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=20
        )
        return img.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def image_digest(image: str = DOCKER_IMAGE) -> str:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, timeout=20,
        )
        return proc.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class Verifier:
    def __init__(self, isolation: str, *, image: str = DOCKER_IMAGE, timeout_s: int = 180):
        if isolation not in {"docker", "subprocess"}:
            raise ValueError(f"unknown isolation {isolation!r}")
        self.isolation = isolation
        self.image = image
        self.timeout_s = timeout_s
        self.image_digest = image_digest(image) if isolation == "docker" else "host-python"

    @classmethod
    def docker(cls, image: str = DOCKER_IMAGE) -> "Verifier":
        if not docker_available(image):
            raise SandboxUnavailable(
                f"candidate evaluation requires Docker and the {image} image; "
                "refusing to fall back to subprocess isolation. Build it with: "
                "docker build -t meta-harness-sandbox -f infra/sandbox.Dockerfile infra"
            )
        return cls("docker", image=image)

    @classmethod
    def subprocess_for_tests(cls) -> "Verifier":
        """Host-process verifier. Shares the host trust boundary; labeled."""
        return cls("subprocess")

    def verify(self, task: TaskSpec, workspace_files: Path) -> VerificationResult:
        with tempfile.TemporaryDirectory(prefix="harness-verify-") as tmp:
            private = Path(tmp) / "workspace"
            shutil.copytree(
                workspace_files, private,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
            )
            _restore_pristine_tests(task, private)
            command = task.full_test_command or task.test_command
            passed, total, per_file, output = self._run(private, command)
        task_files = set(task.test_files)
        task_passed = sum(v[0] for k, v in per_file.items() if k in task_files)
        task_total = sum(v[1] for k, v in per_file.items() if k in task_files)
        regressions = sum(v[1] - v[0] for k, v in per_file.items() if k not in task_files)
        return VerificationResult(
            success=task_total > 0 and task_passed == task_total,
            tests_passed=passed,
            tests_total=total,
            task_tests_passed=task_passed,
            task_tests_total=task_total,
            regressions=regressions,
            isolation=self.isolation,
            output=output[-3000:],
        )

    def _run(self, workspace: Path, command: str) -> tuple[int, int, dict[str, tuple[int, int]], str]:
        pytest_args = command.split()
        if pytest_args and pytest_args[0] == "pytest":
            pytest_args = pytest_args[1:]
        pytest_args += ["-p", "no:cacheprovider", f"--junitxml={_JUNIT}"]
        if self.isolation == "docker":
            argv = [
                "docker", "run", "--rm", "--network", "none", "--memory", "512m",
                "--cpus", "1", "-v", f"{workspace.resolve()}:/workspace",
                "-w", "/workspace", self.image, "python", "-m", "pytest", *pytest_args,
            ]
        else:
            argv = [sys.executable, "-m", "pytest", *pytest_args]
        try:
            proc = subprocess.run(
                argv, cwd=workspace, capture_output=True, text=True, timeout=self.timeout_s
            )
            output = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            output = "[verifier timeout]"
        per_file = _parse_junit(workspace / _JUNIT)
        passed = sum(v[0] for v in per_file.values())
        total = sum(v[1] for v in per_file.values())
        return passed, total, per_file, output


def _restore_pristine_tests(task: TaskSpec, workspace: Path) -> None:
    pristine = task.workspace_dir
    # Remove any conftest the agent introduced; restore pristine test tree.
    for conftest in workspace.rglob("conftest.py"):
        rel = conftest.relative_to(workspace)
        if not (pristine / rel).exists():
            conftest.unlink()
    for src in pristine.rglob("*"):
        rel = src.relative_to(pristine)
        is_test = rel.parts and (rel.parts[0] == "tests" or src.name.startswith("test_")
                                 or src.name in {"conftest.py", "pytest.ini"})
        if src.is_file() and is_test and "__pycache__" not in rel.parts:
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


def _parse_junit(path: Path) -> dict[str, tuple[int, int]]:
    """``{test_file: (passed, total)}`` from a pytest junit report."""
    if not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {}
    per_file: dict[str, list[int]] = {}
    for case in root.iter("testcase"):
        # A collection error (e.g. an ImportError in the test module) is
        # reported with an empty classname and the module in ``name``.
        classname = case.get("classname", "") or case.get("name", "")
        module = classname.split(".")
        # classname "tests.test_x" or "tests.test_x.TestClass"
        parts = [p for p in module if p]
        file_parts = []
        for part in parts:
            file_parts.append(part)
            if part.startswith("test_") or part.endswith("_test"):
                break
        rel = "/".join(file_parts) + ".py"
        failed = any(child.tag in {"failure", "error", "skipped"} for child in case)
        bucket = per_file.setdefault(rel, [0, 0])
        bucket[1] += 1
        if not failed:
            bucket[0] += 1
    return {k: (v[0], v[1]) for k, v in per_file.items()}


def _is_test_path(rel: str, task: TaskSpec) -> bool:
    name = Path(rel).name
    return (
        rel in task.test_files
        or rel.startswith("tests/")
        or name.startswith("test_")
        or name in {"conftest.py", "pytest.ini"}
    )


def audit_run(
    task: TaskSpec,
    snapshots: SnapshotStore,
    base_snapshot: str,
    final_snapshot: str,
    *,
    diff_cap_lines: int = DEFAULT_DIFF_CAP_LINES,
) -> AuditResult:
    """Tests touched, skip markers added, scope, diff size (ARCHITECTURE §5)."""
    diff = snapshots.diff(base_snapshot, final_snapshot)
    changed = diff["added"] + diff["modified"] + diff["removed"]
    reasons: list[str] = []

    tests_touched = sorted(p for p in changed if _is_test_path(p, task))
    tests_modified = bool(tests_touched)
    if tests_modified:
        reasons.append(f"test files modified: {tests_touched}")

    skip_added = 0
    diff_lines = 0
    for rel in diff["added"] + diff["modified"]:
        after = (snapshots.read_file(final_snapshot, rel) or b"").decode(errors="replace")
        before = (snapshots.read_file(base_snapshot, rel) or b"").decode(errors="replace")
        skip_added += max(0, len(_SKIP_MARKERS.findall(after)) - len(_SKIP_MARKERS.findall(before)))
        diff_lines += sum(
            1 for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
    for rel in diff["removed"]:
        before = (snapshots.read_file(base_snapshot, rel) or b"").decode(errors="replace")
        diff_lines += len(before.splitlines())
    if skip_added:
        reasons.append(f"skip/xfail markers added: {skip_added}")

    out_of_scope = sorted(
        p for p in changed if p not in task.scope_files and not _is_test_path(p, task)
    )
    if out_of_scope:
        reasons.append(f"files outside task scope changed: {out_of_scope}")
    if diff_lines > diff_cap_lines:
        reasons.append(f"diff size {diff_lines} exceeds cap {diff_cap_lines}")

    voided = tests_modified or skip_added > 0 or diff_lines > diff_cap_lines
    return AuditResult(
        tests_modified=tests_modified,
        skip_markers_added=skip_added,
        files_out_of_scope=out_of_scope,
        diff_size=diff_lines,
        voided=voided,
        reasons=reasons,
    )


def _written_paths(step: StepRecord) -> set[str]:
    raw = str(step.input.get("path", ""))
    return {p for p in raw.split(",") if p}


def count_redundant_reads(steps: list[StepRecord]) -> int:
    """Reads of a file already read with no write to it in between."""
    seen: set[str] = set()
    redundant = 0
    for step in steps:
        if step.kind != "tool_call":
            continue
        if step.tool == TOOL_READ:
            path = str(step.input.get("path", ""))
            if path in seen:
                redundant += 1
            seen.add(path)
        elif step.tool in {TOOL_PATCH, TOOL_WRITE}:
            seen -= _written_paths(step)
    return redundant


def evaluate_staged(
    staged: StagedTrajectory,
    task: TaskSpec,
    *,
    verifier: Verifier,
    snapshots: SnapshotStore,
    branch_run_id: str,
    prefix_steps: list[StepRecord] | None = None,
    diff_cap_lines: int = DEFAULT_DIFF_CAP_LINES,
) -> tuple[TrajectoryRecord, EvaluationRecord]:
    """Score one staged trajectory. Pure trusted-plane computation."""
    full_steps = list(prefix_steps or []) + list(staged.steps)

    audit = audit_run(
        task, snapshots, staged.base_snapshot, staged.final_snapshot,
        diff_cap_lines=diff_cap_lines,
    )
    if staged.status == "infra_error":
        verification = None
        result = "infra_error"
    else:
        with tempfile.TemporaryDirectory(prefix="harness-final-") as tmp:
            final_ws = Path(tmp) / "ws"
            snapshots.restore(staged.final_snapshot, final_ws)
            verification = verifier.verify(task, final_ws)
        if staged.status == "timeout":
            # A thorough harness may time out where a sloppy one finishes
            # fast and wrong; timeouts are recorded as such, not as plain
            # failures (DESIGN §10).
            result = "timeout"
        else:
            result = "success" if verification.success and not audit.voided else "failure"

    trajectory = TrajectoryRecord(
        id=staged.trajectory_id,
        branch_run_id=branch_run_id,
        harness_id=staged.harness_id,
        memory_version=staged.memory_version,
        task_id=staged.task_id,
        base_commit=task.base_commit,
        model_version=staged.model_version,
        image_digest=verifier.image_digest,
        agent=staged.agent,
        fork_parent=staged.fork_parent,
        fork_step=staged.fork_step,
        result=result,
        tokens=staged.tokens,
        tool_calls=staged.tool_calls,
        context_tokens=staged.context_tokens,
        final_snapshot=staged.final_snapshot,
        metadata={
            "seed": staged.seed,
            "status": staged.status,
            "error": staged.error,
            "wall_time_s": staged.wall_time_s,
            "base_snapshot": staged.base_snapshot,
            **staged.metadata,
        },
    )
    evaluation = EvaluationRecord(
        trajectory_id=staged.trajectory_id,
        success=(verification.success and not audit.voided) if verification else None,
        isolation=verifier.isolation,
        tests_passed=verification.tests_passed if verification else None,
        tests_total=verification.tests_total if verification else None,
        diff_size=audit.diff_size,
        files_out_of_scope=len(audit.files_out_of_scope),
        tests_modified=audit.tests_modified,
        tool_calls=staged.tool_calls,
        redundant_reads=count_redundant_reads(full_steps),
        tokens=staged.tokens,
        voided=audit.voided,
        audit={
            **audit.to_json(),
            "regressions": verification.regressions if verification else None,
            "raw_tests_success": verification.success if verification else None,
            "verifier_output_tail": verification.output[-800:] if verification else None,
        },
    )
    return trajectory, evaluation
