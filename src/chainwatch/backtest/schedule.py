"""Retraining schedules under an explicit label-availability model.

Timeline conventions (one unit = one Elliptic time step, about two weeks):

* Transactions in step ``s`` and all their features are available at the **end** of
  step ``s``, which is when step ``s`` is scored (end-of-step batch scoring; see
  docs/PHASE2_PROTOCOL.md §1).
* The label of a transaction in step ``u`` becomes available at the end of step
  ``u + latency``.
* A model (re)trained at the end of step ``tau`` may use labels of steps
  ``u <= tau - latency``. It scores steps ``tau + 1, ...`` until the next retrain.

Everything here is pure (no data, no models), so the causal guarantees can be unit
tested exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any

STRATEGY_KINDS = ("static", "expanding", "periodic", "sliding", "recency")
FIRST_STEP = 1


class CausalityError(RuntimeError):
    """A training plan would use information not available at its training time."""


@dataclass(frozen=True)
class StrategySpec:
    """One concrete strategy configuration, e.g. ``sliding`` with ``window=5``."""

    family: str
    kind: str
    params: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in STRATEGY_KINDS:
            raise ValueError(f"unknown strategy kind {self.kind!r}; choose from {STRATEGY_KINDS}")
        p = dict(self.params)
        required = {"periodic": "frequency", "sliding": "window", "recency": "half_life"}
        if self.kind in required and required[self.kind] not in p:
            raise ValueError(f"{self.kind} strategy requires parameter {required[self.kind]!r}")
        for key in ("frequency", "window"):
            if key in p and int(p[key]) < 1:
                raise ValueError(f"{key} must be >= 1")
        if "half_life" in p and float(p["half_life"]) <= 0:
            raise ValueError("half_life must be > 0")

    @property
    def name(self) -> str:
        if not self.params:
            return self.family
        return self.family + "_" + "_".join(f"{k}{v}" for k, v in self.params)

    def get(self, key: str) -> Any:
        return dict(self.params)[key]


def expand_strategies(strategies: dict[str, dict[str, Any]]) -> list[StrategySpec]:
    """Turn the YAML ``strategies`` block into concrete specs (grid expanded)."""
    specs: list[StrategySpec] = []
    for family, conf in strategies.items():
        grid = conf.get("grid", {})
        keys = sorted(grid)
        if not keys:
            specs.append(StrategySpec(family, conf["kind"]))
            continue
        for values in product(*(grid[k] for k in keys)):
            specs.append(StrategySpec(family, conf["kind"], tuple(zip(keys, values, strict=True))))
    return specs


def available_label_steps(tau: int, latency: int) -> list[int]:
    """Steps whose labels are known at the end of step ``tau``."""
    if latency < 0:
        raise ValueError("latency must be >= 0")
    return list(range(FIRST_STEP, tau - latency + 1))


@dataclass(frozen=True)
class TrainingPlan:
    tau: int  # training time (end of this step)
    train_steps: tuple[int, ...]
    half_life: float | None = None  # recency weighting, if any

    def step_weights(self) -> dict[int, float]:
        """Per-step sample weight. Age is measured from the newest *available* step."""
        if self.half_life is None:
            return {u: 1.0 for u in self.train_steps}
        newest = max(self.train_steps)
        return {u: 0.5 ** ((newest - u) / self.half_life) for u in self.train_steps}

    @property
    def cache_key(self) -> tuple:
        return (self.train_steps, self.half_life)


@dataclass(frozen=True)
class Deployment:
    """A model trained at ``plan.tau`` that scores ``scored_steps``."""

    plan: TrainingPlan
    scored_steps: tuple[int, ...]


@dataclass
class Schedule:
    spec: StrategySpec
    latency: int
    deployments: list[Deployment] = field(default_factory=list)

    @property
    def n_fits(self) -> int:
        return len(self.deployments)


def retrain_times(spec: StrategySpec, origin: int, end: int) -> list[int]:
    """Ends of steps at which the strategy (re)trains, for scoring steps origin+1..end."""
    if spec.kind == "static":
        return [origin]
    if spec.kind == "periodic":
        f = int(spec.get("frequency"))
        return list(range(origin, end, f))
    # expanding, sliding, recency retrain before every scored step
    return list(range(origin, end))


def training_plan(spec: StrategySpec, tau: int, latency: int) -> TrainingPlan:
    steps = available_label_steps(tau, latency)
    if not steps:
        raise CausalityError(f"no labels available at tau={tau} with latency={latency}")
    if spec.kind == "sliding":
        steps = steps[-int(spec.get("window")) :]
    half_life = float(spec.get("half_life")) if spec.kind == "recency" else None
    return TrainingPlan(tau=tau, train_steps=tuple(steps), half_life=half_life)


def build_schedule(spec: StrategySpec, origin: int, end: int, latency: int) -> Schedule:
    times = retrain_times(spec, origin, end)
    sched = Schedule(spec=spec, latency=latency)
    for i, tau in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else end
        scored = tuple(range(tau + 1, nxt + 1))
        sched.deployments.append(Deployment(training_plan(spec, tau, latency), scored))
    assert_causal(sched, origin, end)
    return sched


def assert_causal(sched: Schedule, origin: int, end: int) -> None:
    """Every scored step is covered exactly once, by a model that only saw past labels."""
    covered: list[int] = []
    for dep in sched.deployments:
        plan = dep.plan
        if max(plan.train_steps) > plan.tau - sched.latency:
            raise CausalityError(
                f"{sched.spec.name}: trained at end of step {plan.tau} with labels up to "
                f"{max(plan.train_steps)} but latency is {sched.latency}"
            )
        if dep.scored_steps and min(dep.scored_steps) <= plan.tau:
            raise CausalityError(f"{sched.spec.name}: scores step <= its training time {plan.tau}")
        if set(plan.train_steps) & set(dep.scored_steps):
            raise CausalityError(f"{sched.spec.name}: trains on a step it scores")
        covered.extend(dep.scored_steps)
    if sorted(covered) != list(range(origin + 1, end + 1)):
        raise CausalityError(f"{sched.spec.name}: scored steps {covered} != {origin + 1}..{end}")
