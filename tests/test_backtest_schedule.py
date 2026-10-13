"""Retraining schedules and label availability: the causal core of Phase 2."""

from __future__ import annotations

import itertools

import pytest

from chainwatch.backtest.schedule import (
    CausalityError,
    Deployment,
    Schedule,
    StrategySpec,
    TrainingPlan,
    assert_causal,
    available_label_steps,
    build_schedule,
    expand_strategies,
    retrain_times,
    training_plan,
)

STATIC = StrategySpec("static", "static")
EXPANDING = StrategySpec("expanding", "expanding")


def periodic(f):
    return StrategySpec("periodic", "periodic", (("frequency", f),))


def sliding(w):
    return StrategySpec("sliding", "sliding", (("window", w),))


def recency(h):
    return StrategySpec("recency", "recency", (("half_life", h),))


ALL_SPECS = [STATIC, EXPANDING, periodic(1), periodic(3), sliding(1), sliding(4), recency(2.0)]


def test_label_availability():
    assert available_label_steps(10, 0) == list(range(1, 11))
    assert available_label_steps(10, 1) == list(range(1, 10))
    assert available_label_steps(10, 3) == list(range(1, 8))
    assert available_label_steps(2, 5) == []
    with pytest.raises(ValueError):
        available_label_steps(10, -1)


@pytest.mark.parametrize(
    "spec, origin, end, latency",
    list(itertools.product(ALL_SPECS, [5, 19, 34], [40, 49], [0, 1, 3])),
)
def test_every_schedule_is_causal_and_covers_each_step_once(spec, origin, end, latency):
    sched = build_schedule(spec, origin, end, latency)
    scored = [s for d in sched.deployments for s in d.scored_steps]
    assert scored == list(range(origin + 1, end + 1))
    for dep in sched.deployments:
        # labels used were available at training time ...
        assert max(dep.plan.train_steps) <= dep.plan.tau - latency
        # ... and the model only scores steps strictly after its training time
        assert min(dep.scored_steps) > dep.plan.tau
        assert not set(dep.plan.train_steps) & set(dep.scored_steps)


def test_static_trains_once_with_all_available_labels():
    sched = build_schedule(STATIC, 34, 49, latency=1)
    assert sched.n_fits == 1
    assert sched.deployments[0].plan.train_steps == tuple(range(1, 34))
    assert sched.deployments[0].scored_steps == tuple(range(35, 50))


def test_expanding_retrains_every_step_and_grows():
    sched = build_schedule(EXPANDING, 34, 49, latency=1)
    assert sched.n_fits == 15
    sizes = [len(d.plan.train_steps) for d in sched.deployments]
    assert sizes == list(range(33, 48))
    # the model scoring step s was trained at end of step s-1 with labels <= s-2
    for d in sched.deployments:
        (s,) = d.scored_steps
        assert d.plan.tau == s - 1
        assert max(d.plan.train_steps) == s - 2


def test_periodic_frequency_and_holding_periods():
    sched = build_schedule(periodic(4), 34, 49, latency=1)
    assert [d.plan.tau for d in sched.deployments] == [34, 38, 42, 46]
    assert [len(d.scored_steps) for d in sched.deployments] == [4, 4, 4, 3]
    assert retrain_times(periodic(1), 34, 49) == retrain_times(EXPANDING, 34, 49)


def test_periodic_models_are_expanding_models_at_retrain_times():
    """Enables fit caching: same training problem -> same cache key."""
    exp = {d.plan.tau: d.plan.cache_key for d in build_schedule(EXPANDING, 19, 34, 1).deployments}
    for d in build_schedule(periodic(4), 19, 34, 1).deployments:
        assert d.plan.cache_key == exp[d.plan.tau]


def test_sliding_window_uses_most_recent_available_steps():
    sched = build_schedule(sliding(3), 34, 49, latency=2)
    for d in sched.deployments:
        assert d.plan.train_steps == tuple(range(d.plan.tau - 4, d.plan.tau - 1))


def test_recency_weights_halve_every_half_life():
    plan = training_plan(recency(2.0), tau=10, latency=1)
    w = plan.step_weights()
    assert w[9] == 1.0  # newest available step
    assert w[7] == pytest.approx(0.5)
    assert w[5] == pytest.approx(0.25)
    assert set(w) == set(range(1, 10))
    assert all(w[u] < w[u + 1] for u in range(1, 9))
    assert TrainingPlan(10, (1, 2)).step_weights() == {1: 1.0, 2: 1.0}


def test_assert_causal_detects_future_labels():
    bad_plan = TrainingPlan(tau=10, train_steps=tuple(range(1, 11)))  # includes step 10
    sched = Schedule(EXPANDING, latency=1, deployments=[Deployment(bad_plan, (11,))])
    with pytest.raises(CausalityError, match="latency"):
        assert_causal(sched, 10, 11)


def test_assert_causal_detects_scoring_own_training_steps():
    plan = TrainingPlan(tau=10, train_steps=(1, 2, 3))
    sched = Schedule(EXPANDING, latency=0, deployments=[Deployment(plan, (10, 11))])
    with pytest.raises(CausalityError):
        assert_causal(sched, 9, 11)


def test_assert_causal_detects_gaps_and_double_coverage():
    plan = TrainingPlan(tau=10, train_steps=(1, 2))
    gap = Schedule(STATIC, 1, [Deployment(plan, (11,))])
    with pytest.raises(CausalityError, match="scored steps"):
        assert_causal(gap, 10, 12)
    double = Schedule(STATIC, 1, [Deployment(plan, (11, 12)), Deployment(plan, (12,))])
    with pytest.raises(CausalityError, match="scored steps"):
        assert_causal(double, 10, 12)


def test_no_labels_available_is_an_error():
    with pytest.raises(CausalityError, match="no labels"):
        build_schedule(EXPANDING, 2, 5, latency=3)


@pytest.mark.parametrize(
    "kind, params",
    [
        ("periodic", ()),
        ("sliding", (("window", 0),)),
        ("recency", (("half_life", -1),)),
        ("bogus", ()),
    ],
)
def test_invalid_specs_rejected(kind, params):
    with pytest.raises(ValueError):
        StrategySpec("x", kind, params)


def test_expand_strategies_grid_and_names():
    specs = expand_strategies(
        {"static": {"kind": "static"}, "sliding": {"kind": "sliding", "grid": {"window": [3, 5]}}}
    )
    assert [s.name for s in specs] == ["static", "sliding_window3", "sliding_window5"]
    assert {s.family for s in specs} == {"static", "sliding"}
