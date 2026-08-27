"""Tests for the constraint-aware arm.

Two kinds. The first pin the per-code decisions: what the engine does with a
terminal code, a balance failure, a transient outage, an unreadable code. The
second pin the properties the headline result depends on -- that the engine
beats the baseline, spends less budget doing it, and never proposes an attempt
the regulations forbid.
"""

import random
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from fourshots.engine import BALANCE_RETRY_OFFSETS_DAYS, ConstraintAwareEngine
from fourshots.params import load
from fourshots.policy import (
    IST,
    MAX_ATTEMPTS_PER_CYCLE,
    MandatePurpose,
    PRE_DEBIT_NOTICE,
    is_in_execution_window,
)
from fourshots.policies import RazorpayDefault
from fourshots.runner import Outcome, run_cohort
from fourshots.simulator import Observation, World, build_cohort

MONTH = date(2026, 9, 1)
FIRST = datetime(2026, 9, 26, 9, 0, tzinfo=IST)


@pytest.fixture()
def params():
    return load()


@pytest.fixture()
def engine():
    return ConstraintAwareEngine()


def observation(code: str | None, attempts_used: int = 1, now: datetime | None = None):
    now = now or FIRST
    return Observation(
        mandate_id="mand_test",
        amount=Decimal("1000"),
        purpose=MandatePurpose.GENERAL,
        now=now,
        attempts_used=attempts_used,
        history=tuple((FIRST + timedelta(days=i), code) for i in range(attempts_used)),
    )


# --- Per-code decisions ----------------------------------------------------

@pytest.mark.parametrize(
    "code", ["invalid_vpa", "international_transaction_not_allowed", "Z8"]
)
def test_terminal_codes_spend_no_further_attempts(engine, code: str) -> None:
    """The cheapest win available: recognising on attempt one that no later
    attempt can succeed, instead of burning three discovering it."""
    if code == "Z8":
        pytest.skip("Z8 arrives as an NPCI code; covered in taxonomy tests")
    assert engine.propose(observation(code)) is None


def test_dead_mandate_stops_immediately(engine) -> None:
    assert engine.propose(observation("invalid_vpa")) is None


def test_balance_failure_is_not_retried_tomorrow(engine) -> None:
    """The core correction to the documented default. Nothing about the
    customer's account changes overnight."""
    proposed = engine.propose(observation("insufficient_funds"))
    assert proposed is not None
    assert proposed - FIRST > timedelta(days=2)


def test_balance_retries_follow_the_spread_offsets(engine) -> None:
    """Even spacing across the cycle, so coverage does not depend on knowing
    when income arrives."""
    gaps = []
    for attempt in (1, 2, 3):
        proposed = engine.propose(observation("insufficient_funds", attempts_used=attempt))
        assert proposed is not None
        gaps.append((proposed - FIRST).days)

    assert gaps == sorted(gaps), "retries must move outward, not cluster"
    for actual, offset in zip(gaps, BALANCE_RETRY_OFFSETS_DAYS):
        assert actual >= offset - 1  # may be pushed later by window rules


def test_balance_retries_stop_when_the_offsets_run_out(engine) -> None:
    assert engine.propose(observation("insufficient_funds", attempts_used=4)) is None


def test_transient_failure_retries_within_days_not_weeks(engine) -> None:
    """An issuer outage clears in hours. Waiting a fortnight would waste the
    window in which the debit would have cleared."""
    proposed = engine.propose(observation("bank_technical_error"))
    assert proposed is not None
    assert proposed - FIRST < timedelta(days=3)


def test_transient_retries_sooner_than_balance(engine) -> None:
    """The two errors point in opposite directions, which is precisely what a
    uniform policy cannot express."""
    transient = engine.propose(observation("bank_technical_error"))
    balance = engine.propose(observation("insufficient_funds"))
    assert transient < balance


def test_customer_absent_is_escalated_not_silently_retried(engine) -> None:
    """A silent retry has nobody to answer a collect request."""
    assert engine.propose(observation("payment_collect_request_expired")) is None


def test_unreadable_code_gets_one_cautious_attempt_then_stops(engine) -> None:
    """The engine does not guess at a code it cannot read. That caution costs
    real recoveries, and the results report the cost rather than hiding it."""
    first = engine.propose(observation("npci_reject_u91", attempts_used=1))
    assert first is not None
    assert first - FIRST >= timedelta(hours=24)
    assert engine.propose(observation("npci_reject_u91", attempts_used=2)) is None


def test_exhausted_budget_proposes_nothing(engine) -> None:
    assert engine.propose(observation("insufficient_funds", attempts_used=4)) is None


# --- Regulatory compliance -------------------------------------------------

@pytest.mark.parametrize(
    "code", ["insufficient_funds", "bank_technical_error", "partner_bank_downtime"]
)
def test_every_proposal_respects_the_notice_period(engine, code: str) -> None:
    proposed = engine.propose(observation(code))
    assert proposed is not None
    assert proposed - FIRST >= PRE_DEBIT_NOTICE


@pytest.mark.parametrize(
    "code", ["insufficient_funds", "bank_technical_error", "partner_bank_downtime"]
)
def test_every_proposal_lands_in_a_legal_window(engine, code: str) -> None:
    proposed = engine.propose(observation(code))
    assert proposed is not None
    assert is_in_execution_window(proposed)


# --- Properties the headline result rests on -------------------------------

def _run(policy, params):
    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)
    return run_cohort(cohort, World(params, rng), policy, MONTH)


def test_engine_recovers_more_than_the_documented_default(params, engine) -> None:
    baseline = _run(RazorpayDefault(params.baseline_offsets_days), params)
    result = _run(engine, params)
    assert result.recovered_value > baseline.recovered_value
    assert result.recovered_count > baseline.recovered_count


def test_engine_wins_while_spending_less_budget(params, engine) -> None:
    """Not a trade-off. More money recovered from fewer attempts, which is what
    makes the result hard to argue with."""
    baseline = _run(RazorpayDefault(params.baseline_offsets_days), params)
    result = _run(engine, params)
    assert result.attempts_spent < baseline.attempts_spent
    assert result.attempts_per_recovery < baseline.attempts_per_recovery


def test_engine_saves_more_mandates(params, engine) -> None:
    baseline = _run(RazorpayDefault(params.baseline_offsets_days), params)
    result = _run(engine, params)
    assert result.mandates_saved > baseline.mandates_saved


def test_engine_actually_stops_early(params, engine) -> None:
    """The mechanism behind the budget saving. The baseline has no such path."""
    result = _run(engine, params)
    assert any(c.outcome == Outcome.STOPPED_EARLY for c in result.cycles)


def test_engine_never_exceeds_the_attempt_cap(params, engine) -> None:
    result = _run(engine, params)
    assert all(c.attempts_used <= MAX_ATTEMPTS_PER_CYCLE for c in result.cycles)


def test_engine_holds_no_payday_belief() -> None:
    """An earlier version aimed retries at a hardcoded payday prior. The
    sensitivity sweep showed the advantage collapsed from +41% to +2.2% when
    the world's payday shifted three days -- it was being told the answer
    rather than reading the world. Nothing may reintroduce that.
    """
    import fourshots.engine as engine_module

    assert not hasattr(engine_module, "PAYDAY_CANDIDATES")
