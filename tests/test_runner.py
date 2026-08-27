"""Tests for the baseline arm and the experiment harness.

The harness is where the comparison is either fair or worthless, so most of
these are about fairness: identical budget, identical legality treatment,
identical information. If the engine later beats the baseline, these tests are
what makes that result mean something.
"""

import random
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from fourshots.audit import AuditLog
from fourshots.params import load
from fourshots.policy import (
    IST,
    MAX_ATTEMPTS_PER_CYCLE,
    MandatePurpose,
    is_in_execution_window,
)
from fourshots.policies import RazorpayDefault
from fourshots.runner import Outcome, run_cohort, run_cycle
from fourshots.simulator import (
    DeclineRecord,
    FailureMode,
    Mandate,
    Observation,
    World,
    build_cohort,
)

MONTH = date(2026, 9, 1)


@pytest.fixture()
def params():
    return load()


@pytest.fixture()
def baseline(params):
    return RazorpayDefault(params.baseline_offsets_days)


def mandate(**overrides) -> Mandate:
    base = dict(
        id="mand_test",
        amount=Decimal("1000"),
        purpose=MandatePurpose.GENERAL,
        debit_day=26,
        salary_day=1,
        opening_multiple=3.0,
        daily_burn=0.15,
        true_mode=FailureMode.BALANCE,
        code_is_unmappable=False,
    )
    base.update(overrides)
    return Mandate(**base)


def observation(attempts_used: int, first_at: datetime, code: str = "insufficient_funds"):
    return Observation(
        mandate_id="mand_test",
        amount=Decimal("1000"),
        purpose=MandatePurpose.GENERAL,
        now=first_at,
        attempts_used=attempts_used,
        history=tuple(
            DeclineRecord(first_at + timedelta(days=i), code)
            for i in range(attempts_used)
        ),
    )


# --- The baseline policy ---------------------------------------------------

def test_baseline_retries_on_consecutive_days(baseline) -> None:
    """Razorpay's documented behaviour: retry the following day, three times."""
    first = datetime(2026, 9, 26, 9, 0, tzinfo=IST)
    proposals = [baseline.propose(observation(n, first)) for n in (1, 2, 3)]
    assert [p - first for p in proposals] == [
        timedelta(days=1),
        timedelta(days=2),
        timedelta(days=3),
    ]


def test_baseline_stops_only_when_the_budget_is_gone(baseline) -> None:
    first = datetime(2026, 9, 26, 9, 0, tzinfo=IST)
    assert baseline.propose(observation(3, first)) is not None
    assert baseline.propose(observation(4, first)) is None


def test_baseline_ignores_the_decline_code(baseline) -> None:
    """The documented policy states no variation by failure reason. A dead
    mandate and an empty account get the same schedule -- which is the entire
    reason it is worth beating."""
    first = datetime(2026, 9, 26, 9, 0, tzinfo=IST)
    codes = ["insufficient_funds", "invalid_vpa", "bank_technical_error", "Z8"]
    proposals = {baseline.propose(observation(1, first, code)) for code in codes}
    assert len(proposals) == 1


def test_baseline_spends_the_full_regulatory_budget(params, baseline) -> None:
    """Not handicapped: one original execution plus three retries."""
    assert len(params.baseline_offsets_days) + 1 == MAX_ATTEMPTS_PER_CYCLE


# --- Harness fairness ------------------------------------------------------

def test_no_cycle_exceeds_the_attempt_cap(params, baseline) -> None:
    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)
    result = run_cohort(cohort, World(params, rng), baseline, MONTH)
    assert all(c.attempts_used <= MAX_ATTEMPTS_PER_CYCLE for c in result.cycles)


def test_every_attempt_lands_in_a_legal_window(params, baseline, tmp_path) -> None:
    """NPCI blocks AutoPay execution during UPI peak hours. If the harness
    scheduled into peak, the benchmark would be measuring an illegal world."""
    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)[:200]
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_cohort(cohort, World(params, rng), baseline, MONTH, audit)

    attempts = [e for e in audit.read() if e.kind == "attempt_made"]
    assert attempts
    for entry in attempts:
        assert is_in_execution_window(datetime.fromisoformat(entry.data["at"]))


def test_retries_respect_the_pre_debit_notice_period(params, baseline, tmp_path) -> None:
    """Each attempt is committed 24 hours ahead under the RBI framework, so
    consecutive attempts cannot be closer together than that."""
    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)[:200]
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_cohort(cohort, World(params, rng), baseline, MONTH, audit)

    by_mandate: dict[str, list[datetime]] = {}
    for entry in audit.read():
        if entry.kind == "attempt_made":
            by_mandate.setdefault(entry.mandate_id, []).append(
                datetime.fromisoformat(entry.data["at"])
            )

    for times in by_mandate.values():
        for earlier, later in zip(times, times[1:]):
            assert later - earlier >= timedelta(hours=24)


def test_run_is_reproducible_from_the_seed(params, baseline) -> None:
    def once():
        rng = random.Random(params.seed)
        cohort = build_cohort(params, rng)
        return run_cohort(cohort, World(params, rng), baseline, MONTH).summary()

    assert once() == once()


# --- Outcome semantics -----------------------------------------------------

def test_recovery_is_recorded_with_its_timestamp(params, baseline) -> None:
    rng = random.Random(1)
    world = World(params, rng)
    result = run_cycle(mandate(true_mode=FailureMode.NONE), world, baseline, MONTH)
    assert result.outcome == Outcome.RECOVERED
    assert result.attempts_used == 1
    assert result.recovered_at is not None


def test_terminal_mandate_exhausts_the_whole_budget_under_the_baseline(
    params, baseline
) -> None:
    """The clearest waste in the documented policy: a mandate that can never
    clear still consumes all four attempts, because nothing reads the code."""
    rng = random.Random(1)
    world = World(params, rng)
    result = run_cycle(mandate(true_mode=FailureMode.MANDATE_DEAD), world, baseline, MONTH)
    assert result.outcome == Outcome.BUDGET_EXHAUSTED
    assert result.attempts_used == MAX_ATTEMPTS_PER_CYCLE
    assert not result.mandate_survived


def test_exhausting_the_budget_loses_the_mandate(params, baseline) -> None:
    rng = random.Random(1)
    result = run_cycle(
        mandate(true_mode=FailureMode.LIMIT_BREACH), World(params, rng), baseline, MONTH
    )
    assert not result.mandate_survived


def test_baseline_never_stops_early(params, baseline) -> None:
    """It has no mechanism to. Every failure therefore ends in cancellation,
    which is why mandates_saved equals recoveries for this arm."""
    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)
    result = run_cohort(cohort, World(params, rng), baseline, MONTH)

    assert not any(c.outcome == Outcome.STOPPED_EARLY for c in result.cycles)
    assert result.mandates_saved == result.recovered_count


# --- Aggregates ------------------------------------------------------------

def test_recovered_value_counts_only_recovered_mandates(params, baseline) -> None:
    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)
    result = run_cohort(cohort, World(params, rng), baseline, MONTH)

    expected = sum((c.amount for c in result.cycles if c.recovered), Decimal(0))
    assert result.recovered_value == expected
    assert result.recovered_value < result.exposed_value


def test_summary_reports_the_metrics_the_track_bar_asks_for(params, baseline) -> None:
    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)
    summary = run_cohort(cohort, World(params, rng), baseline, MONTH).summary()

    for key in (
        "recovered_inr",
        "recovery_rate",
        "mandates_saved",
        "attempts_spent",
        "attempts_per_recovery",
    ):
        assert key in summary


def test_audit_log_of_a_run_verifies(params, baseline, tmp_path) -> None:
    """A run's decision log must be checkable afterwards, not taken on trust."""
    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)[:100]
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_cohort(cohort, World(params, rng), baseline, MONTH, audit)

    assert audit.verify() > 0


# --- mandates_saved must not flatter a policy ------------------------------
#
# Added after mutation testing: reverting the repairability check went
# undetected by the whole suite. That is the metric most exposed to challenge,
# so it now has teeth.

def _stopped(repairable: bool):
    from fourshots.runner import CycleResult

    return CycleResult(
        mandate_id="mand_test",
        amount=Decimal("1000"),
        outcome=Outcome.STOPPED_EARLY,
        attempts_used=1,
        recovered_at=None,
        repairable=repairable,
    )


def test_stopping_early_on_a_repairable_mandate_saves_it() -> None:
    """An expired card or a breached limit can be re-authorised by the
    customer, so escalating instead of burning attempts keeps them."""
    assert _stopped(repairable=True).mandate_survived


def test_stopping_early_on_a_dead_mandate_does_not_save_it() -> None:
    """A VPA that no longer resolves cannot be repaired. Stopping early is
    still correct -- it saves three wasted attempts -- but it does not save a
    customer, and counting it as though it did would inflate the headline."""
    assert not _stopped(repairable=False).mandate_survived


def test_recovery_counts_as_saved_regardless_of_repairability() -> None:
    """A debit that cleared is a live mandate by definition."""
    from fourshots.runner import CycleResult

    assert CycleResult(
        "mand_test", Decimal("1000"), Outcome.RECOVERED, 1,
        datetime(2026, 9, 27, 8, 0, tzinfo=IST), repairable=False,
    ).mandate_survived


def test_budget_exhaustion_is_never_a_saved_mandate() -> None:
    from fourshots.runner import CycleResult

    for repairable in (True, False):
        assert not CycleResult(
            "mand_test", Decimal("1000"), Outcome.BUDGET_EXHAUSTED,
            MAX_ATTEMPTS_PER_CYCLE, None, repairable=repairable,
        ).mandate_survived


def test_engine_saved_count_excludes_unrepairable_early_stops(params) -> None:
    """End-to-end guard on the headline number itself."""
    from fourshots.engine import ConstraintAwareEngine

    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)
    result = run_cohort(cohort, World(params, rng), ConstraintAwareEngine(), MONTH)

    generous = sum(
        1 for c in result.cycles
        if c.outcome in (Outcome.RECOVERED, Outcome.STOPPED_EARLY)
    )
    assert result.mandates_saved < generous, (
        "mandates_saved must be stricter than counting every early stop"
    )
