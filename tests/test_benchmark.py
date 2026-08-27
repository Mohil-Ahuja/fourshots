"""Tests for the benchmark harness itself.

The harness is a measuring instrument. A bug here does not crash anything --
it silently reports the wrong number, which is worse. These pin the properties
that make its output trustworthy.
"""

import pytest

from fourshots.benchmark import DAYS_IN_MONTH_SPACE, _shift_payday, run
from fourshots.engine import ConstraintAwareEngine
from fourshots.params import load
from fourshots.policies import RazorpayDefault


@pytest.fixture()
def params():
    return load()


def test_zero_shift_is_the_identity(params) -> None:
    """Caught a real bug: the shift used modulus 30 over a 1..31 day space, so
    day 31 folded onto day 1 even at shift zero. The 'unshifted' sweep column
    silently disagreed with the headline, which is exactly how a measuring
    instrument misleads without failing.
    """
    assert _shift_payday(params, 0).salary_weights == params.salary_weights


def test_shift_preserves_total_probability_mass(params) -> None:
    """Moving payday must not create or destroy mandates."""
    for shift in range(-5, 6):
        weights = _shift_payday(params, shift).salary_weights
        assert sum(weights.values()) == pytest.approx(1.0)


def test_shift_stays_inside_the_day_of_month_space(params) -> None:
    for shift in range(-5, 6):
        for day in _shift_payday(params, shift).salary_weights:
            if day == "other":
                continue
            assert 1 <= int(day) <= DAYS_IN_MONTH_SPACE


def test_a_full_cycle_of_shifts_returns_to_the_start(params) -> None:
    """A shift of one whole month is the identity, which only holds if the
    modulus matches the day space."""
    assert (
        _shift_payday(params, DAYS_IN_MONTH_SPACE).salary_weights
        == params.salary_weights
    )


def test_both_arms_face_an_identical_world(params) -> None:
    """Each arm rebuilds the cohort from the same seed, so a difference in
    outcome is a difference in policy and nothing else."""
    baseline_cohort, _ = run(RazorpayDefault(params.baseline_offsets_days), params)
    engine_cohort, _ = run(ConstraintAwareEngine(), params)

    assert [m.id for m in baseline_cohort] == [m.id for m in engine_cohort]
    assert [m.amount for m in baseline_cohort] == [m.amount for m in engine_cohort]
    assert [m.true_mode for m in baseline_cohort] == [m.true_mode for m in engine_cohort]


def test_engine_advantage_survives_every_payday_shift(params) -> None:
    """The headline must not depend on the cohort being shaped conveniently.
    The engine holds no payday belief, so moving the world's payday should not
    rescue or destroy it."""
    for shift in range(-3, 4):
        shifted = _shift_payday(params, shift)
        _, baseline = run(RazorpayDefault(shifted.baseline_offsets_days), shifted)
        _, engine = run(ConstraintAwareEngine(), shifted)
        assert engine.recovered_value > baseline.recovered_value, (
            f"engine failed to beat the baseline at payday shift {shift:+d}"
        )
