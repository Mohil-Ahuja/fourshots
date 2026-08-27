"""Tests for the cohort and the world it lives in.

The most important test in this file is `test_policy_cannot_reach_ground_truth`.
The project's headline number is a comparison between two policies, and the
obvious attack is that the engine wins because it can see the world it is being
scored in. The information barrier is the defence, so it is asserted
structurally here rather than assumed.
"""

import dataclasses
import random
from datetime import date, datetime
from decimal import Decimal

import pytest

from fourshots.params import load
from fourshots.policy import IST, MandatePurpose
from fourshots.simulator import (
    TERMINAL_MODES,
    AttemptResult,
    FailureMode,
    Mandate,
    Observation,
    World,
    build_cohort,
)
from fourshots.taxonomy import UNCLASSIFIED, classify


@pytest.fixture()
def params():
    return load()


@pytest.fixture()
def rng():
    return random.Random(20260827)


# --- The information barrier ----------------------------------------------

def test_policy_cannot_reach_ground_truth() -> None:
    """A policy sees Observation and AttemptResult. Neither may expose balance,
    salary timing, the true failure mode, or anything else the world knows.

    Asserted on the type rather than by inspection, so adding a leaky field
    later fails the suite instead of quietly invalidating the benchmark.
    """
    forbidden = {
        "balance",
        "salary_day",
        "true_mode",
        "opening_multiple",
        "daily_burn",
        "code_is_unmappable",
        "world",
        "mandate",
    }

    for exposed in (Observation, AttemptResult):
        names = {f.name for f in dataclasses.fields(exposed)}
        leaked = names & forbidden
        assert not leaked, f"{exposed.__name__} leaks ground truth: {leaked}"


def test_observation_carries_only_merchant_visible_facts() -> None:
    """Everything here is knowable from a Razorpay webhook plus your own books."""
    assert {f.name for f in dataclasses.fields(Observation)} == {
        "mandate_id",
        "amount",
        "purpose",
        "now",
        "attempts_used",
        "history",
    }


def test_attempt_result_reveals_only_outcome_and_code() -> None:
    assert {f.name for f in dataclasses.fields(AttemptResult)} == {
        "cleared",
        "at",
        "razorpay_code",
    }


# --- Balance model ---------------------------------------------------------

def mandate(**overrides) -> Mandate:
    base = dict(
        id="mand_test",
        amount=Decimal("1000"),
        purpose=MandatePurpose.GENERAL,
        debit_day=26,
        salary_day=1,
        opening_multiple=3.0,
        daily_burn=0.1,
        true_mode=FailureMode.BALANCE,
        code_is_unmappable=False,
    )
    base.update(overrides)
    return Mandate(**base)


def test_balance_is_highest_on_payday() -> None:
    m = mandate(salary_day=1)
    assert m.balance_on(date(2026, 9, 1)) > m.balance_on(date(2026, 9, 15))


def test_balance_decays_through_the_month() -> None:
    """The mechanism the whole project exploits: a debit late in the cycle
    meets a thinner account than the same debit just after payday."""
    m = mandate(salary_day=1)
    balances = [m.balance_on(date(2026, 9, day)) for day in (1, 5, 10, 20, 28)]
    assert balances == sorted(balances, reverse=True)


def test_balance_never_goes_negative() -> None:
    m = mandate(salary_day=1, opening_multiple=0.5, daily_burn=0.5)
    assert m.balance_on(date(2026, 9, 28)) >= 0


def test_a_late_debit_can_be_short_while_a_post_salary_retry_clears() -> None:
    """This is the scenario the headline result rests on. If it were not
    reproducible in the model, the thesis would be wrong."""
    m = mandate(salary_day=1, debit_day=26, opening_multiple=3.0, daily_burn=0.15)
    assert m.balance_on(date(2026, 9, 26)) < m.amount
    assert m.balance_on(date(2026, 10, 1)) >= m.amount


# --- Attempt resolution ----------------------------------------------------

def at(day: int, hour: int = 8) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=IST)


def test_balance_failure_clears_once_money_arrives(params, rng) -> None:
    world = World(params, rng)
    m = mandate(salary_day=1, debit_day=26, opening_multiple=3.0, daily_burn=0.15)

    assert not world.attempt(m, at(26)).cleared
    assert world.attempt(m, datetime(2026, 10, 1, 8, 0, tzinfo=IST)).cleared


@pytest.mark.parametrize("mode", sorted(TERMINAL_MODES, key=lambda m: m.value))
def test_terminal_modes_never_clear(params, rng, mode: FailureMode) -> None:
    """No amount of waiting helps. Any attempt spent on these is wasted, which
    is exactly what a uniform retry policy does."""
    world = World(params, rng)
    m = mandate(true_mode=mode)
    for day in (1, 5, 15, 28):
        assert not world.attempt(m, at(day)).cleared


def test_healthy_mandate_clears_immediately(params, rng) -> None:
    world = World(params, rng)
    assert world.attempt(mandate(true_mode=FailureMode.NONE), at(10)).cleared


def test_failure_returns_a_code_and_success_does_not(params, rng) -> None:
    world = World(params, rng)
    failed = world.attempt(mandate(true_mode=FailureMode.MANDATE_DEAD), at(10))
    assert not failed.cleared and failed.razorpay_code == "invalid_vpa"

    ok = world.attempt(mandate(true_mode=FailureMode.NONE), at(10))
    assert ok.cleared and ok.razorpay_code is None


def test_issuer_outage_is_stable_within_a_run(params, rng) -> None:
    """Downtime must not be re-rolled on every query, or a policy could
    'discover' an outage had ended by asking twice."""
    world = World(params, rng)
    day = date(2026, 9, 12)
    assert world._outage_on(day) == world._outage_on(day)


# --- Unmappable codes ------------------------------------------------------

def test_unmappable_code_hides_a_real_failure_mode(params, rng) -> None:
    """`unclassified` is not a failure mode -- it is a real mode wearing a code
    the taxonomy cannot read. That gap is what makes the conservative fallback
    cost something measurable rather than being free."""
    world = World(params, rng)
    m = mandate(true_mode=FailureMode.BALANCE, code_is_unmappable=True)

    result = world.attempt(m, at(26))
    assert not result.cleared
    assert classify(razorpay_code=result.razorpay_code).failure_class is UNCLASSIFIED


def test_mappable_balance_failure_is_readable(params, rng) -> None:
    world = World(params, rng)
    result = world.attempt(mandate(true_mode=FailureMode.BALANCE), at(26))
    assert classify(razorpay_code=result.razorpay_code).failure_class is not UNCLASSIFIED


# --- Cohort generation -----------------------------------------------------

def test_every_declared_mode_is_a_real_failure_mode(params) -> None:
    """The parameter file names failure modes as strings; this enum defines
    them. If the two drift, cohort generation dies at run time on a name it
    cannot resolve -- which is how this was originally caught."""
    for name in params.decline_mix:
        if name == "unclassified":
            continue  # not a mode: a real mode wearing an unreadable code
        FailureMode(name)  # raises if the parameter file names something unknown


def test_cohort_has_the_declared_size(params, rng) -> None:
    assert len(build_cohort(params, rng)) == params.size


def test_cohort_is_reproducible_from_the_seed(params) -> None:
    """Pre-registration is worthless if a reader cannot reproduce our numbers."""
    a = build_cohort(params, random.Random(params.seed))
    b = build_cohort(params, random.Random(params.seed))
    assert [m.id for m in a] == [m.id for m in b]
    assert [m.amount for m in a] == [m.amount for m in b]
    assert [m.true_mode for m in a] == [m.true_mode for m in b]


def test_balance_failures_dominate_the_cohort(params, rng) -> None:
    cohort = build_cohort(params, rng)
    counts: dict[FailureMode, int] = {}
    for m in cohort:
        counts[m.true_mode] = counts.get(m.true_mode, 0) + 1
    assert max(counts, key=counts.get) is FailureMode.BALANCE


def test_cohort_contains_unmappable_codes(params, rng) -> None:
    cohort = build_cohort(params, rng)
    assert any(m.code_is_unmappable for m in cohort)


def test_cohort_exercises_the_afa_path(params, rng) -> None:
    """A benchmark where no mandate crosses the AFA threshold would leave that
    branch untested while appearing to pass."""
    cohort = build_cohort(params, rng)
    assert any(m.amount > params.afa_general for m in cohort)


def test_cohort_covers_both_afa_threshold_tiers(params, rng) -> None:
    cohort = build_cohort(params, rng)
    purposes = {m.purpose for m in cohort}
    assert MandatePurpose.GENERAL in purposes
    assert purposes & {
        MandatePurpose.INSURANCE,
        MandatePurpose.MUTUAL_FUND_SIP,
        MandatePurpose.CREDIT_CARD_BILL,
    }


def test_debit_days_stay_within_the_month(params, rng) -> None:
    assert all(1 <= m.debit_day <= 28 for m in build_cohort(params, rng))


def test_burn_rate_stays_in_bounds(params, rng) -> None:
    """Guards the gaussian draw: a burn rate at or above 1.0 would empty the
    account instantly and a negative one would grow it."""
    assert all(0.0 <= m.daily_burn <= 0.9 for m in build_cohort(params, rng))
