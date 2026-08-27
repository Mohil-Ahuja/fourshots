"""Tests for the pre-registered parameter file.

Two jobs. First, the shipped file must load and satisfy every invariant --
these run against the real `params/cohort.yaml`, so a bad edit to it fails the
suite rather than silently changing the benchmark's world.

Second, and more important: the parameter file and the enforced policy must
agree. The simulator reads its world from the file; the engine reads its rules
from `policy`. If those drift apart the benchmark measures a world whose rules
differ from the ones being obeyed, and the headline number stops meaning
anything. `test_params_and_policy_cannot_drift` is the guard.
"""

import pytest
import yaml

from fourshots import policy
from fourshots.params import DEFAULT_PATH, Params, ParamsInvalid, load


@pytest.fixture()
def shipped() -> Params:
    return load()


# --- The shipped file ------------------------------------------------------

def test_shipped_params_load_and_validate(shipped: Params) -> None:
    assert shipped.size > 0
    assert shipped.trials > 1, "a single trial cannot show reliability"


def test_distributions_are_normalised(shipped: Params) -> None:
    assert sum(shipped.decline_mix.values()) == pytest.approx(1.0)
    assert sum(shipped.purposes.values()) == pytest.approx(1.0)
    assert sum(shipped.salary_weights.values()) == pytest.approx(1.0)


def test_balance_failures_dominate_the_decline_mix(shipped: Params) -> None:
    """The whole thesis is about attempts wasted on empty accounts. If balance
    failures were rare the project would be measuring the wrong thing."""
    mix = shipped.decline_mix
    assert mix["insufficient_balance"] == max(mix.values())


def test_cohort_contains_unmappable_codes(shipped: Params) -> None:
    """Real rails emit codes the taxonomy has not seen -- confirmed live. A
    cohort with no unmapped codes would flatter the engine."""
    assert shipped.decline_mix.get("unclassified", 0) > 0


def test_seed_is_fixed_so_results_reproduce(shipped: Params) -> None:
    assert isinstance(shipped.seed, int)


def test_provenance_is_declared_for_every_section(shipped: Params) -> None:
    """Any section presenting numbers must say where they came from."""
    summary = shipped.provenance_summary()
    declared = {s for sections in summary.values() for s in sections}
    for section in ("decline_mix", "salary_credit", "balance", "amounts", "purposes"):
        assert section in declared, f"{section} declares no provenance"


def test_assumptions_are_admitted_rather_than_dressed_up(shipped: Params) -> None:
    """The honest labels matter more than the cited ones. If nothing were
    marked `assumed`, the file would be claiming a rigour it does not have."""
    summary = shipped.provenance_summary()
    assert summary.get("assumed"), "no parameter admits to being an assumption"
    assert summary.get("documented"), "no parameter is sourced"


# --- Cross-check against enforced policy -----------------------------------

def test_params_and_policy_cannot_drift(shipped: Params) -> None:
    """The guard that makes the benchmark meaningful."""
    shipped.check_matches_policy()  # must not raise

    assert shipped.max_attempts == policy.MAX_ATTEMPTS_PER_CYCLE
    assert shipped.afa_general == policy.AFA_THRESHOLD_GENERAL
    assert shipped.afa_elevated == policy.AFA_THRESHOLD_ELEVATED


def test_drift_is_detected(tmp_path) -> None:
    """Prove the guard actually fires -- a check that cannot fail is decoration."""
    document = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    document["regulatory"]["max_attempts_per_cycle"] = 5  # NPCI says 4

    with pytest.raises(ParamsInvalid, match="max attempts"):
        Params(document).check_matches_policy()


def test_afa_drift_is_detected() -> None:
    document = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    document["regulatory"]["afa_threshold_general_inr"] = 25000

    with pytest.raises(ParamsInvalid, match="AFA general"):
        Params(document).check_matches_policy()


def test_peak_window_drift_is_detected() -> None:
    document = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    document["regulatory"]["peak_windows_ist"] = [["09:00", "12:00"]]

    with pytest.raises(ParamsInvalid, match="peak windows"):
        Params(document).check_matches_policy()


# --- Invariants ------------------------------------------------------------

def test_unnormalised_decline_mix_is_rejected() -> None:
    document = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    document["decline_mix"]["values"]["insufficient_balance"] += 0.2

    with pytest.raises(ParamsInvalid, match="decline_mix"):
        Params(document).validate()


def test_cohort_must_exercise_the_afa_path() -> None:
    """A benchmark where no mandate crosses the AFA threshold would leave that
    branch untested while appearing to pass."""
    document = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    document["amounts"]["bands"] = [{"min": 99, "max": 499, "weight": 1.0}]

    with pytest.raises(ParamsInvalid, match="AFA"):
        Params(document).validate()


def test_inverted_amount_band_is_rejected() -> None:
    document = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    document["amounts"]["bands"][0] = {"min": 5000, "max": 100, "weight": 0.34}

    with pytest.raises(ParamsInvalid, match="min > max"):
        Params(document).validate()


def test_empty_cohort_is_rejected() -> None:
    document = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    document["cohort"]["size"] = 0

    with pytest.raises(ParamsInvalid, match="positive"):
        Params(document).validate()


# --- Baseline arm ----------------------------------------------------------

def test_baseline_is_razorpays_documented_consecutive_day_policy(shipped: Params) -> None:
    """The control arm must remain the real, quotable, shipped policy. If this
    ever changes, the comparison is against a strawman we invented."""
    assert shipped.baseline_offsets_days == [1, 2, 3]
    assert shipped.raw["baseline"]["varies_by_failure_reason"] is False
    assert shipped.raw["baseline"]["provenance"] == "documented"


def test_baseline_spends_exactly_the_regulatory_budget(shipped: Params) -> None:
    """One original execution plus three retries is the whole budget. The
    baseline is not being handicapped -- it uses every attempt available."""
    assert len(shipped.baseline_offsets_days) + 1 == shipped.max_attempts
