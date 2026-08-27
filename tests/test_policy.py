"""Tests for the regulatory constraint lattice.

These assert rules issued by NPCI and the RBI, so they are written as
statements about the regulation rather than about the implementation. If one
of these fails, either the code is wrong or the rule changed -- and the second
case should force a re-read of the circular, not a patched assertion.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fourshots.policy import (
    IST,
    MAX_ATTEMPTS_PER_CYCLE,
    LegalityCheck,
    MandatePurpose,
    PreDebitNotification,
    ProposedAttempt,
    Violation,
    afa_required,
    check_legality,
    earliest_schedulable,
    is_in_execution_window,
    next_execution_window,
)


def ist(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# --- Constraint 2: execution windows --------------------------------------

@pytest.mark.parametrize(
    "hh,mm,legal",
    [
        (0, 0, True),     # midnight: legal
        (9, 59, True),    # just before morning peak
        (10, 0, False),   # morning peak opens -- half-open, so blocked
        (12, 59, False),  # still morning peak
        (13, 0, True),    # peak closes -- legal again
        (16, 59, True),   # afternoon window
        (17, 0, False),   # evening peak opens
        (21, 29, False),  # still evening peak
        (21, 30, True),   # evening peak closes
        (23, 59, True),   # late night: legal
    ],
)
def test_execution_window_boundaries(hh: int, mm: int, legal: bool) -> None:
    assert is_in_execution_window(ist(2026, 9, 15, hh, mm)) is legal


def test_next_window_rolls_forward_out_of_peak() -> None:
    # 11:00 sits inside the morning peak; the next legal instant is 13:00.
    assert next_execution_window(ist(2026, 9, 15, 11, 0)) == ist(2026, 9, 15, 13, 0)


def test_next_window_is_identity_when_already_legal() -> None:
    already = ist(2026, 9, 15, 14, 30)
    assert next_execution_window(already) == already


# --- Constraint 3: pre-debit notification ---------------------------------

def test_earliest_schedulable_respects_notice_and_window() -> None:
    # Decide at 14:00 on the 15th. +24h lands at 14:00 on the 16th, which is
    # already a legal window, so no further rolling is needed.
    assert earliest_schedulable(ist(2026, 9, 15, 14, 0)) == ist(2026, 9, 16, 14, 0)


def test_earliest_schedulable_rolls_past_peak() -> None:
    # Decide at 11:00. +24h lands at 11:00 next day -- inside morning peak --
    # so the earliest legal execution is 13:00.
    assert earliest_schedulable(ist(2026, 9, 15, 11, 0)) == ist(2026, 9, 16, 13, 0)


def test_attempt_with_less_than_24h_notice_is_rejected() -> None:
    execute_at = ist(2026, 9, 16, 14, 0)
    attempt = ProposedAttempt(
        mandate_id="mand_1",
        amount=Decimal("499"),
        purpose=MandatePurpose.GENERAL,
        execute_at=execute_at,
        notified_at=execute_at - timedelta(hours=23, minutes=59),
        attempts_used=1,
    )
    assert Violation.INSUFFICIENT_NOTICE in check_legality(attempt).violations


# --- Constraint 1: the attempt cap ----------------------------------------

def test_fourth_retry_is_over_budget() -> None:
    # Four attempts have been used (1 original + 3 retries): the cycle is done.
    execute_at = ist(2026, 9, 20, 8, 0)
    attempt = ProposedAttempt(
        mandate_id="mand_1",
        amount=Decimal("499"),
        purpose=MandatePurpose.GENERAL,
        execute_at=execute_at,
        notified_at=execute_at - timedelta(hours=25),
        attempts_used=MAX_ATTEMPTS_PER_CYCLE,
    )
    assert Violation.ATTEMPT_BUDGET_EXHAUSTED in check_legality(attempt).violations


def test_third_retry_is_within_budget() -> None:
    execute_at = ist(2026, 9, 20, 8, 0)
    attempt = ProposedAttempt(
        mandate_id="mand_1",
        amount=Decimal("499"),
        purpose=MandatePurpose.GENERAL,
        execute_at=execute_at,
        notified_at=execute_at - timedelta(hours=25),
        attempts_used=MAX_ATTEMPTS_PER_CYCLE - 1,
    )
    assert check_legality(attempt).is_legal


# --- Constraint 4: AFA thresholds -----------------------------------------

@pytest.mark.parametrize(
    "amount,purpose,required",
    [
        ("15000", MandatePurpose.GENERAL, False),   # at threshold: no AFA
        ("15000.01", MandatePurpose.GENERAL, True), # above: AFA
        ("99999", MandatePurpose.MUTUAL_FUND_SIP, False),
        ("100000", MandatePurpose.INSURANCE, False),
        ("100000.01", MandatePurpose.CREDIT_CARD_BILL, True),
        ("20000", MandatePurpose.INSURANCE, False), # elevated threshold applies
        ("20000", MandatePurpose.GENERAL, True),    # same amount, general: AFA
    ],
)
def test_afa_thresholds(amount: str, purpose: MandatePurpose, required: bool) -> None:
    assert afa_required(Decimal(amount), purpose) is required


# --- The gate --------------------------------------------------------------

def test_gate_reports_every_violation_not_just_the_first() -> None:
    """The audit log needs the complete reason, not the first tripped rule."""
    attempt = ProposedAttempt(
        mandate_id="mand_1",
        amount=Decimal("50000"),          # AFA required, not obtained
        purpose=MandatePurpose.GENERAL,
        execute_at=ist(2026, 9, 20, 11, 0),  # inside morning peak
        notified_at=ist(2026, 9, 20, 10, 0), # only 1h notice
        attempts_used=MAX_ATTEMPTS_PER_CYCLE,  # over budget
        customer_opted_out=True,
        mandate_active=False,
    )
    got = set(check_legality(attempt).violations)
    assert got == {
        Violation.ATTEMPT_BUDGET_EXHAUSTED,
        Violation.MANDATE_NOT_ACTIVE,
        Violation.CUSTOMER_OPTED_OUT,
        Violation.INSUFFICIENT_NOTICE,
        Violation.PEAK_WINDOW,
        Violation.AFA_REQUIRED,
    }


def test_opt_out_is_honoured_even_when_otherwise_perfect() -> None:
    execute_at = ist(2026, 9, 20, 8, 0)
    attempt = ProposedAttempt(
        mandate_id="mand_1",
        amount=Decimal("499"),
        purpose=MandatePurpose.GENERAL,
        execute_at=execute_at,
        notified_at=execute_at - timedelta(hours=25),
        attempts_used=1,
        customer_opted_out=True,
    )
    check = check_legality(attempt)
    assert not check.is_legal
    assert check.violations == (Violation.CUSTOMER_OPTED_OUT,)


def test_empty_violations_means_legal() -> None:
    assert LegalityCheck(()).is_legal


# --- Notification payload --------------------------------------------------

def test_notification_carries_every_mandated_field() -> None:
    execute_at = ist(2026, 9, 20, 8, 0)
    attempt = ProposedAttempt(
        mandate_id="mand_abc123",
        amount=Decimal("499"),
        purpose=MandatePurpose.GENERAL,
        execute_at=execute_at,
        notified_at=execute_at - timedelta(hours=25),
        attempts_used=1,
    )
    note = PreDebitNotification.build(
        merchant_name="Acme Streaming",
        attempt=attempt,
        reason="Monthly subscription renewal",
    )
    # The RBI framework enumerates these: merchant, amount, date and time,
    # mandate reference, reason -- plus a route to opt out.
    assert note.merchant_name == "Acme Streaming"
    assert note.amount == Decimal("499")
    assert note.debit_at == execute_at
    assert note.mandate_reference == "mand_abc123"
    assert note.reason
    assert note.opt_out_deadline >= execute_at
