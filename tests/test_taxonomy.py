"""Tests for the failure-code taxonomy.

The behaviour worth protecting here is not "code X maps to class Y" -- it is
the three judgement calls that make the taxonomy safe to schedule against:
unknown codes degrade toward caution, ambiguous codes are not guessed, and the
rail's own code wins over the aggregator's rendering of it except when it is a
catch-all.
"""

import pytest

from fourshots.taxonomy import (
    Blocker,
    Confidence,
    INSUFFICIENT_BALANCE,
    ISSUER_DOWN,
    LIMIT_BREACH,
    MANDATE_DEAD,
    PSP_TRANSIENT,
    UNCLASSIFIED,
    classify,
    coverage,
)


def test_insufficient_funds_is_a_balance_failure() -> None:
    got = classify(razorpay_code="insufficient_funds")
    assert got.failure_class is INSUFFICIENT_BALANCE
    assert got.confidence is Confidence.DOCUMENTED


def test_balance_failures_have_a_full_day_floor() -> None:
    """The core claim: retrying a balance failure tomorrow is near-worthless.

    Nothing about the customer's account changes overnight; money arrives on
    payroll cycles. This floor is what stops the scheduler from spending
    attempts into an empty account.
    """
    assert INSUFFICIENT_BALANCE.min_backoff_hours >= 24.0
    assert INSUFFICIENT_BALANCE.blocker is Blocker.CUSTOMER_BALANCE
    # Time alone does not fix it, so there is no expected resolution time.
    assert INSUFFICIENT_BALANCE.typical_resolution_hours is None


def test_transient_failures_retry_far_sooner_than_balance_failures() -> None:
    """Rail outages clear in hours. Treating them like balance failures is the
    opposite error: too slow rather than too fast."""
    assert PSP_TRANSIENT.min_backoff_hours < ISSUER_DOWN.min_backoff_hours
    assert ISSUER_DOWN.min_backoff_hours < INSUFFICIENT_BALANCE.min_backoff_hours


def test_limit_breach_is_terminal_because_retrying_cannot_help() -> None:
    """Z8 is the clearest case of a wasted attempt under a uniform retry
    policy: the same amount breaches the same cap however long you wait."""
    got = classify(npci_code="Z8")
    assert got.failure_class is LIMIT_BREACH
    assert got.is_terminal
    assert not LIMIT_BREACH.silently_retryable


def test_dead_mandate_is_terminal() -> None:
    got = classify(razorpay_code="invalid_vpa")
    assert got.failure_class is MANDATE_DEAD
    assert got.is_terminal


def test_transient_classes_are_not_terminal() -> None:
    assert not PSP_TRANSIENT.is_terminal
    assert not INSUFFICIENT_BALANCE.is_terminal


def test_unknown_code_degrades_toward_caution_not_error() -> None:
    """An unmapped code must never raise, and must never become cheap to
    retry -- otherwise a new code from the rail silently burns attempts."""
    got = classify(razorpay_code="some_code_npci_invented_last_week")
    assert got.failure_class is UNCLASSIFIED
    assert UNCLASSIFIED.min_backoff_hours >= 24.0


def test_classify_with_no_codes_at_all_is_safe() -> None:
    got = classify()
    assert got.failure_class is UNCLASSIFIED
    assert got.source_code == "<none>"


def test_ambiguous_code_is_left_unclassified_rather_than_guessed() -> None:
    """`payment_declined` spans balance and risk declines. Guessing 'balance'
    would cost an attempt every time the guess is wrong, so it stays unmapped."""
    assert classify(razorpay_code="payment_declined").failure_class is UNCLASSIFIED


# --- Code precedence -------------------------------------------------------

def test_npci_code_wins_over_razorpay_code() -> None:
    """The rail's code is more specific than the aggregator's rendering."""
    got = classify(razorpay_code="payment_declined", npci_code="Z9")
    assert got.failure_class is INSUFFICIENT_BALANCE
    assert got.source_code == "Z9"


def test_catch_all_npci_code_yields_to_a_definite_razorpay_code() -> None:
    """U30 spans insufficient funds, frozen accounts and risk flags, so a
    confidently-mapped Razorpay code carries more information."""
    got = classify(razorpay_code="bank_technical_error", npci_code="U30")
    assert got.failure_class is ISSUER_DOWN


def test_catch_all_npci_code_survives_when_razorpay_code_is_also_vague() -> None:
    got = classify(razorpay_code="payment_declined", npci_code="U30")
    assert got.failure_class is UNCLASSIFIED
    assert got.source_code == "U30"


@pytest.mark.parametrize("raw", ["INSUFFICIENT_FUNDS", " insufficient funds ", "insufficient-funds"])
def test_code_normalisation_handles_source_inconsistency(raw: str) -> None:
    assert classify(razorpay_code=raw).failure_class is INSUFFICIENT_BALANCE


# --- Provenance ------------------------------------------------------------

def test_coverage_is_reportable_and_mostly_documented() -> None:
    """The submission claims honest metrics, so it must be able to say what
    fraction of its own domain model rests on primary sources."""
    cov = coverage()
    assert cov["documented"] + cov["inferred"] == cov["total_codes"]
    assert cov["documented"] > cov["inferred"]


# --- Regression: gaps found against live rail data -------------------------

def test_international_card_rejection_is_terminal() -> None:
    """Found live: a real test-mode payment returned
    `international_transaction_not_allowed` and fell through to UNCLASSIFIED,
    which parked it behind a 24-hour floor. Safe, but wrong -- the same
    instrument can never clear at a domestic-only merchant, so the correct
    answer is zero further attempts, not a delayed one."""
    from fourshots.taxonomy import INSTRUMENT_REJECTED

    got = classify(razorpay_code="international_transaction_not_allowed")
    assert got.failure_class is INSTRUMENT_REJECTED
    assert got.is_terminal
    assert got.failure_class.min_backoff_hours == 0.0
    assert not got.failure_class.silently_retryable
