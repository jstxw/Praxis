"""Harness configuration: measured noise floors and budgets.

``NOISE_FLOOR`` is loaded from config, populated by Experiment 1, and
**never hardcoded** (DESIGN §2). It is keyed by ``(agent, model)``: a
noise floor measured on the synthetic agent says nothing about Claude,
and a floor measured under one model version is invalidated by drift
under a stable alias.

Every noise floor carries provenance — the experiment file, the command,
the null-test and sabotage verdicts — so the gate can refuse to trust a
floor whose null test failed (VISION §6 rule 5).

Location: ``$HARNESS_HOME/config.json`` (default ``~/.harness``).
Nothing is written outside the project directory and ``~/.harness``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def harness_home() -> Path:
    return Path(os.environ.get("HARNESS_HOME", str(Path.home() / ".harness")))


class NoiseFloorUnmeasured(RuntimeError):
    """No trustworthy noise floor for this agent/model: run E1 first."""


@dataclass
class NoiseFloor:
    agent: str
    model_version: str
    metrics: dict[str, float]  # efficiency: relative; success: absolute
    null_test_passed: bool
    sabotage_detected: bool
    n_tasks: int
    reps: int
    experiment_file: str
    measured_at: str
    command: str

    def floor(self, metric: str) -> float:
        if metric not in self.metrics:
            raise NoiseFloorUnmeasured(
                f"noise floor for {self.agent}:{self.model_version} has no {metric!r}"
            )
        return self.metrics[metric]


@dataclass
class RefinementBudget:
    """DESIGN §9 per-cycle budget."""

    branch_factor: int = 3
    cheap_eval_tasks: int = 3
    survivors: int = 2
    holdout_tasks: int = 10
    regression_tasks: int = 5
    reps: int = 5
    max_wall_clock_minutes: int = 120


@dataclass
class GateSettings:
    min_tasks: int = 10
    min_reps: int = 5
    max_cost: float = 0.10  # max relative token increase allowed
    lambda_complexity: float = 0.002
    alpha: float = 0.05
    level: float = 0.95
    primary_metric: str = "tool_calls"
    plausible_effect: float = 0.30  # E1 kill criterion
    drift_retest_every: int = 3  # promotions between re-tests against H0


@dataclass
class HarnessConfig:
    noise_floors: dict[str, NoiseFloor] = field(default_factory=dict)
    budget: RefinementBudget = field(default_factory=RefinementBudget)
    gate: GateSettings = field(default_factory=GateSettings)

    @staticmethod
    def key(agent: str, model_version: str) -> str:
        return f"{agent}:{model_version}"

    def noise_floor(self, agent: str, model_version: str) -> NoiseFloor:
        nf = self.noise_floors.get(self.key(agent, model_version))
        if nf is None:
            raise NoiseFloorUnmeasured(
                f"no noise floor measured for {self.key(agent, model_version)}; "
                "run `harness experiment e1` first — a threshold below the noise "
                "floor fires on randomness"
            )
        if not nf.null_test_passed:
            raise NoiseFloorUnmeasured(
                f"the E1 null test FAILED for {self.key(agent, model_version)} "
                f"({nf.experiment_file}); the pipeline is not trustworthy and no "
                "comparison above it may be reported"
            )
        return nf

    def set_noise_floor(self, nf: NoiseFloor) -> None:
        self.noise_floors[self.key(nf.agent, nf.model_version)] = nf

    def to_json(self) -> dict[str, Any]:
        return {
            "noise_floors": {k: asdict(v) for k, v in self.noise_floors.items()},
            "budget": asdict(self.budget),
            "gate": asdict(self.gate),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "HarnessConfig":
        return cls(
            noise_floors={k: NoiseFloor(**v) for k, v in data.get("noise_floors", {}).items()},
            budget=RefinementBudget(**data.get("budget", {})),
            gate=GateSettings(**data.get("gate", {})),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "HarnessConfig":
        path = path or harness_home() / "config.json"
        if not path.exists():
            return cls()
        return cls.from_json(json.loads(path.read_text()))

    def save(self, path: Path | None = None) -> Path:
        path = path or harness_home() / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n")
        tmp.replace(path)
        return path
