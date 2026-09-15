"""Verify every task in eval/corpus/ against its hygiene contract.

For each task directory:
  - task.json has all required fields with valid values; id == dir name
  - base (pristine workspace): test_command exits nonzero
  - base: the unrelated test file(s) (tests/test_*.py not in test_files) exist and pass
  - gold overlay: every gold file is listed in scope_files and differs from base
  - gold overlay: test_command passes 3/3, full_test_command passes
  - static hygiene: no network/randomness/timing imports in workspace or gold

Prints one line per task; exits nonzero if any check fails.

Usage:
    .venv/bin/python eval/corpus/_verify_gold.py [task-id ...]
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CORPUS = Path(__file__).resolve().parent
REPO = CORPUS.parent.parent
PYTHON = REPO / ".venv" / "bin" / "python"

REQUIRED = {
    "id", "capability", "instruction", "test_command", "full_test_command",
    "scope_files", "test_files", "difficulty",
}
CAPABILITIES = {"bug-fix", "add-function", "refactor", "error-handling", "implement-spec"}
DIFFICULTIES = {"easy", "medium", "hard"}
FORBIDDEN = re.compile(
    r"^\s*(?:import|from)\s+(random|time|socket|urllib|requests|http|asyncio|threading|"
    r"subprocess|secrets|uuid)\b|datetime\.now|time\.sleep|\bopen\(",
    re.MULTILINE,
)
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


def _pytest(cmd: str, cwd: Path, timeout: int = 60) -> tuple[int, str]:
    argv = shlex.split(cmd)
    if argv and argv[0] == "pytest":
        argv = [str(PYTHON), "-m", "pytest", "-p", "no:cacheprovider", *argv[1:]]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"}
    env.pop("PYTHONPATH", None)
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, lines[-1] if lines else ""


def verify(task_dir: Path) -> list[str]:
    problems: list[str] = []
    try:
        spec = json.loads((task_dir / "task.json").read_text())
    except Exception as exc:  # noqa: BLE001 — report any load failure
        return [f"task.json unreadable: {exc}"]

    missing = REQUIRED - spec.keys()
    if missing:
        problems.append(f"missing fields {sorted(missing)}")
        return problems
    if spec["id"] != task_dir.name:
        problems.append("id != directory name")
    if spec["capability"] not in CAPABILITIES:
        problems.append(f"bad capability {spec['capability']!r}")
    if spec["difficulty"] not in DIFFICULTIES:
        problems.append(f"bad difficulty {spec['difficulty']!r}")

    workspace = task_dir / "workspace"
    gold = task_dir / "gold"
    scope = set(spec["scope_files"])
    test_files = set(spec["test_files"])

    for rel in test_files | {"pytest.ini", "tests/__init__.py"}:
        if not (workspace / rel).is_file():
            problems.append(f"workspace missing {rel}")
    unrelated = sorted(
        p.relative_to(workspace).as_posix()
        for p in (workspace / "tests").glob("test_*.py")
        if p.relative_to(workspace).as_posix() not in test_files
    )
    if not unrelated:
        problems.append("no unrelated test file")

    gold_files = sorted(
        p.relative_to(gold).as_posix()
        for p in gold.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ) if gold.is_dir() else []
    if not gold_files:
        problems.append("gold/ is empty or missing")
    for rel in gold_files:
        if rel not in scope:
            problems.append(f"gold changes out-of-scope file {rel}")
        base_file = workspace / rel
        if base_file.is_file() and base_file.read_bytes() == (gold / rel).read_bytes():
            problems.append(f"gold {rel} identical to base")

    for root in (workspace, gold):
        for path in root.rglob("*.py"):
            if FORBIDDEN.search(path.read_text(encoding="utf-8")):
                problems.append(f"forbidden import/call in {path.relative_to(task_dir)}")

    with tempfile.TemporaryDirectory(prefix="corpus-verify-") as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(workspace, base, ignore=IGNORE)
        code, tail = _pytest(spec["test_command"], base)
        if code == 0:
            problems.append("base test_command PASSES (should fail)")
        if unrelated:
            code, tail = _pytest("pytest -q " + " ".join(unrelated), base)
            if code != 0:
                problems.append(f"base unrelated tests fail: {tail}")

        fixed = Path(tmp) / "gold"
        shutil.copytree(workspace, fixed, ignore=IGNORE)
        for rel in gold_files:
            (fixed / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(gold / rel, fixed / rel)
        for attempt in range(3):
            code, tail = _pytest(spec["test_command"], fixed)
            if code != 0:
                problems.append(f"gold test_command run {attempt + 1}/3 failed: {tail}")
                break
        start = time.monotonic()
        code, tail = _pytest(spec["full_test_command"], fixed)
        elapsed = time.monotonic() - start
        if code != 0:
            problems.append(f"gold full suite failed: {tail}")
        # pytest's own reported time excludes interpreter startup; use its summary if present
        m = re.search(r"in ([\d.]+)s", tail)
        suite_secs = float(m.group(1)) if m else elapsed
        if suite_secs >= 2.0:
            problems.append(f"full suite too slow ({suite_secs:.2f}s)")
    return problems


def main(argv: list[str]) -> int:
    dirs = sorted(p for p in CORPUS.iterdir() if p.is_dir() and p.name.startswith("task-"))
    if argv:
        dirs = [d for d in dirs if d.name in argv]
    failed = 0
    caps: dict[str, int] = {}
    for d in dirs:
        problems = verify(d)
        try:
            spec = json.loads((d / "task.json").read_text())
            caps[spec.get("capability", "?")] = caps.get(spec.get("capability", "?"), 0) + 1
            label = f"{spec.get('capability', '?'):<15} {spec.get('difficulty', '?'):<6}"
        except Exception:  # noqa: BLE001
            label = "?"
        if problems:
            failed += 1
            print(f"FAIL {d.name:<40} {label} " + "; ".join(problems))
        else:
            print(f"PASS {d.name:<40} {label}")
    print(f"capabilities: {dict(sorted(caps.items()))}")
    print(f"{len(dirs) - failed}/{len(dirs)} tasks passed verification")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
