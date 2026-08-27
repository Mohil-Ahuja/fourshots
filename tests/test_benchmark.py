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


# --- Reporting output ------------------------------------------------------
#
# The benchmark's output is quoted in the README and reproduced by CI, so the
# printing path is part of the deliverable rather than incidental.

def test_headline_prints_every_metric_the_readme_quotes(params, capsys) -> None:
    from fourshots.benchmark import print_headline

    print_headline(params)
    out = capsys.readouterr().out

    for label in (
        "recovery rate",
        "recovered (INR)",
        "mandates saved",
        "attempts spent",
        "attempts per recovery",
        "attempts on hopeless debits",
    ):
        assert label in out, f"headline output is missing {label!r}"

    assert "BASELINE" in out and "FOURSHOTS" in out
    # The cohort must be identified, or the numbers cannot be reproduced.
    assert str(params.seed) in out


def test_headline_declares_parameter_provenance(params, capsys) -> None:
    """A results table that does not say how much of its world is assumed is
    hiding the thing a reader most needs to weigh it."""
    from fourshots.benchmark import print_headline

    print_headline(params)
    out = capsys.readouterr().out
    assert "provenance" in out.lower()
    assert "assumed" in out.lower()


def test_headline_returns_both_arms(params, capsys) -> None:
    from fourshots.benchmark import print_headline

    baseline, engine = print_headline(params)
    capsys.readouterr()
    assert baseline.policy_name == "razorpay_documented_default"
    assert engine.policy_name == "constraint_aware_engine"
    assert engine.recovered_value > baseline.recovered_value


def test_sweep_prints_every_shift_and_a_worst_case(params, capsys) -> None:
    from fourshots.benchmark import print_sweep

    print_sweep(params)
    out = capsys.readouterr().out

    for shift in range(-3, 4):
        assert f"{shift:+d}" in out, f"sweep output is missing shift {shift:+d}"
    assert "worst case" in out.lower()


def test_cli_entrypoint_runs(params, capsys, monkeypatch) -> None:
    """CI runs this exact command; if it cannot execute, the README is lying."""
    import sys

    from fourshots.benchmark import main

    monkeypatch.setattr(sys, "argv", ["benchmark", "--sweep"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "BASELINE" in out and "worst case" in out.lower()


# --- Replications ----------------------------------------------------------
#
# The parameter file declares 20 independent replications and says the result
# should be a distribution rather than one lucky draw. For a while it declared
# that and the code ran a single cohort -- a promise the build did not keep,
# and exactly the kind of thing a reader checking cohort.yaml against the
# results would catch.

def test_replications_use_distinct_derived_seeds(params) -> None:
    """Distinct so the cohorts differ; derived so the whole set reproduces."""
    from fourshots.benchmark import replicate
    from fourshots.engine import ConstraintAwareEngine

    results = replicate(ConstraintAwareEngine(), params, trials=4)
    assert len(results) == 4
    recovered = [r.recovered_value for r in results]
    assert len(set(recovered)) > 1, "replications produced identical cohorts"


def test_replications_are_reproducible(params) -> None:
    from fourshots.benchmark import replicate
    from fourshots.engine import ConstraintAwareEngine

    first = [r.recovered_value for r in replicate(ConstraintAwareEngine(), params, 3)]
    second = [r.recovered_value for r in replicate(ConstraintAwareEngine(), params, 3)]
    assert first == second


def test_engine_wins_every_replication(params) -> None:
    """The claim the headline actually rests on. A mean advantage with losing
    replications underneath it would be a much weaker result, and worth
    reporting differently."""
    from fourshots.benchmark import replicate
    from fourshots.engine import ConstraintAwareEngine

    trials = 8  # a subset; the full declared set runs in the benchmark
    baselines = replicate(RazorpayDefault(params.baseline_offsets_days), params, trials)
    engines = replicate(ConstraintAwareEngine(), params, trials)

    for i, (baseline, engine) in enumerate(zip(baselines, engines)):
        assert engine.recovered_value > baseline.recovered_value, (
            f"engine lost replication {i}"
        )


def test_the_declared_trial_count_is_actually_used(params, capsys) -> None:
    """Guards the specific defect this section exists for: cohort.yaml
    promising replications the code never ran."""
    from fourshots.benchmark import print_replications

    print_replications(params)
    out = capsys.readouterr().out
    assert f"of {params.trials} replications" in out


# --- Decline-mix sensitivity ----------------------------------------------

def test_rescaling_the_balance_share_keeps_the_mix_normalised(params) -> None:
    from fourshots.benchmark import _rescale_decline_mix

    for share in (0.35, 0.55, 0.75):
        mix = _rescale_decline_mix(params, share).decline_mix
        assert sum(mix.values()) == pytest.approx(1.0)
        assert mix["insufficient_balance"] == pytest.approx(share)


def test_rescaling_preserves_relative_weights_of_other_modes(params) -> None:
    """Moving one parameter must not silently reshape the whole distribution."""
    from fourshots.benchmark import _rescale_decline_mix

    original = params.decline_mix
    rescaled = _rescale_decline_mix(params, 0.35).decline_mix
    ratio_before = original["issuer_down"] / original["psp_transient"]
    ratio_after = rescaled["issuer_down"] / rescaled["psp_transient"]
    assert ratio_after == pytest.approx(ratio_before)


def test_engine_leads_across_the_declared_sensitivity_range(params) -> None:
    """cohort.yaml names the balance share as the parameter the headline is
    most exposed to. If the advantage vanished inside the declared range, the
    result would be a property of our assumption rather than of the policy."""
    from fourshots.benchmark import _rescale_decline_mix, run
    from fourshots.engine import ConstraintAwareEngine

    low, high = params.raw["decline_mix"]["sensitivity"]
    for share in (low, (low + high) / 2, high):
        shifted = _rescale_decline_mix(params, share)
        _, baseline = run(RazorpayDefault(shifted.baseline_offsets_days), shifted)
        _, engine = run(ConstraintAwareEngine(), shifted)
        assert engine.recovered_value > baseline.recovered_value, (
            f"engine lost at balance share {share}"
        )
