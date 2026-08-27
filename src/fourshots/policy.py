"""The regulatory constraint lattice for Indian recurring-payment retries.

Everything in this module is deterministic and hand-checkable. No model, no
heuristic, no learned parameter -- these are rules issued by NPCI and the RBI,
and an engine that gambles on them is not a payments system.

That separation is deliberate and is the project's central design claim: the
*decision* of which attempt to spend and when is a judgement call, but the
*legality* of a proposed attempt is not, and the two must not share a code
path. `is_legal()` is the gate every proposed attempt passes through, whatever
proposed it.

The four constraints
--------------------
1. Attempt cap. NPCI allows four executions per mandate cycle -- one original
   plus three retries -- in force since 2025-08-01. The cycle is cancelled
   after the fourth failure.

2. Execution windows. AutoPay debits execute only outside UPI peak hours.
   Peak is 10:00-13:00 and 17:00-21:30 IST; everything else is legal. So a
   retry cannot be placed at an arbitrary time -- it lands in one of three
   daily windows or it does not land at all.

3. Pre-debit notification. Under the RBI Digital Payments E-mandate
   Framework, 2026 (notified 2026-04-21) the customer must be notified at
   least 24 hours before a debit, with amount, date and time, mandate
   reference and reason, and must be able to opt out of that individual
   transaction.

   The consequence is the thing most retry systems miss: **an attempt must be
   committed a full day before it executes**, which means scheduling happens
   without knowing whether the money will be there. This is a commitment
   problem, not a reactive loop.

4. AFA thresholds. Recurring transactions above INR 15,000 require Additional
   Factor of Authentication; insurance premiums, SIPs and credit-card bills
   carry a higher INR 1,00,000 threshold. Above threshold, a silent retry
   cannot succeed -- which is knowable *before* the attempt is spent.

Sources
-------
NPCI, Guidelines on usage of UPI and API (in force 2025-08-01)
RBI, Digital Payments - E-mandate Framework, 2026 (notified 2026-04-21)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

# India Standard Time. All NPCI window rules are expressed in IST, so the
# engine works in IST internally and converts at the edges rather than
# scattering offset arithmetic through the scheduling logic.
IST = timezone(timedelta(hours=5, minutes=30))

# --- Constraint 1: the attempt cap ----------------------------------------

MAX_ATTEMPTS_PER_CYCLE = 4
"""One original execution plus three retries, per NPCI (in force 2025-08-01).

NOTE (open question, must be resolved before results are published): sources
describe the cap as applying per mandate "identified by each sequence number",
which reads as per-execution-cycle rather than per-mandate-lifetime. The
simulator and the `mandates_saved` metric depend on this reading. It is
flagged in the README as an outstanding verification item against the primary
NPCI circular, and no headline claim should rest on it until confirmed.
"""

# --- Constraint 2: execution windows --------------------------------------

PEAK_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(10, 0), time(13, 0)),
    (time(17, 0), time(21, 30)),
)
"""UPI peak hours, during which AutoPay mandates are not executed."""


def is_in_execution_window(when: datetime) -> bool:
    """True if a debit may legally execute at this instant.

    Legal (non-peak) windows are therefore 00:00-10:00, 13:00-17:00 and
    21:30-24:00 IST. Peak boundaries are treated as half-open [start, end):
    10:00:00 is blocked, 13:00:00 is allowed.
    """
    local = when.astimezone(IST).time()
    return not any(start <= local < end for start, end in PEAK_WINDOWS)


def next_execution_window(after: datetime) -> datetime:
    """Earliest legal execution instant at or after `after`.

    Returns `after` unchanged when it is already legal, so callers can use this
    unconditionally without first testing.
    """
    local = after.astimezone(IST)
    for start, end in PEAK_WINDOWS:
        if start <= local.time() < end:
            # Roll forward to the end of the blocking peak window.
            return local.replace(
                hour=end.hour, minute=end.minute, second=0, microsecond=0
            )
    return local


# --- Constraint 3: pre-debit notification ---------------------------------

PRE_DEBIT_NOTICE = timedelta(hours=24)
"""Minimum lead time between notifying the customer and debiting them."""


def earliest_schedulable(now: datetime) -> datetime:
    """Earliest instant an attempt decided at `now` could legally execute.

    Combines the notification lead time with the window rules: notify now,
    wait the mandated 24 hours, then land in the first legal window at or
    after that point.
    """
    return next_execution_window(now + PRE_DEBIT_NOTICE)


# --- Constraint 4: AFA thresholds -----------------------------------------

class MandatePurpose(enum.Enum):
    """Purpose categories that carry different AFA thresholds."""

    GENERAL = "general"
    INSURANCE = "insurance"
    MUTUAL_FUND_SIP = "mutual_fund_sip"
    CREDIT_CARD_BILL = "credit_card_bill"


AFA_THRESHOLD_GENERAL = Decimal("15000")
AFA_THRESHOLD_ELEVATED = Decimal("100000")

_ELEVATED = frozenset(
    {
        MandatePurpose.INSURANCE,
        MandatePurpose.MUTUAL_FUND_SIP,
        MandatePurpose.CREDIT_CARD_BILL,
    }
)


def afa_threshold(purpose: MandatePurpose) -> Decimal:
    return AFA_THRESHOLD_ELEVATED if purpose in _ELEVATED else AFA_THRESHOLD_GENERAL


def afa_required(amount: Decimal, purpose: MandatePurpose) -> bool:
    """True if this debit needs Additional Factor of Authentication.

    Knowable before an attempt is spent, which is the point: an above-threshold
    mandate should be routed to a re-authorisation flow rather than being
    retried silently three times.
    """
    return amount > afa_threshold(purpose)


# --- The gate --------------------------------------------------------------

class Violation(enum.Enum):
    """Why a proposed attempt is not legal. Recorded verbatim in the audit log."""

    ATTEMPT_BUDGET_EXHAUSTED = "attempt_budget_exhausted"
    INSUFFICIENT_NOTICE = "insufficient_pre_debit_notice"
    PEAK_WINDOW = "outside_execution_window"
    CUSTOMER_OPTED_OUT = "customer_opted_out"
    AFA_REQUIRED = "afa_required_but_not_obtained"
    MANDATE_NOT_ACTIVE = "mandate_not_active"


@dataclass(frozen=True)
class ProposedAttempt:
    """A candidate debit, before it has been checked for legality."""

    mandate_id: str
    amount: Decimal
    purpose: MandatePurpose
    execute_at: datetime
    notified_at: datetime | None
    attempts_used: int
    mandate_active: bool = True
    customer_opted_out: bool = False
    afa_obtained: bool = False


@dataclass(frozen=True)
class LegalityCheck:
    """Outcome of the gate. `violations` is empty iff the attempt is legal."""

    violations: tuple[Violation, ...]

    @property
    def is_legal(self) -> bool:
        return not self.violations


def check_legality(attempt: ProposedAttempt) -> LegalityCheck:
    """Check a proposed attempt against all four constraints.

    Returns *every* violation rather than short-circuiting on the first, so the
    audit log records the complete reason an attempt was rejected. Both arms of
    the experiment run through this same function, which is what makes the
    comparison fair: the baseline is not being held to a different standard, it
    simply proposes worse attempts.
    """
    violations: list[Violation] = []

    if attempt.attempts_used >= MAX_ATTEMPTS_PER_CYCLE:
        violations.append(Violation.ATTEMPT_BUDGET_EXHAUSTED)

    if not attempt.mandate_active:
        violations.append(Violation.MANDATE_NOT_ACTIVE)

    if attempt.customer_opted_out:
        violations.append(Violation.CUSTOMER_OPTED_OUT)

    if (
        attempt.notified_at is None
        or attempt.execute_at - attempt.notified_at < PRE_DEBIT_NOTICE
    ):
        violations.append(Violation.INSUFFICIENT_NOTICE)

    if not is_in_execution_window(attempt.execute_at):
        violations.append(Violation.PEAK_WINDOW)

    if afa_required(attempt.amount, attempt.purpose) and not attempt.afa_obtained:
        violations.append(Violation.AFA_REQUIRED)

    return LegalityCheck(tuple(violations))


# --- Notification payload --------------------------------------------------

@dataclass(frozen=True)
class PreDebitNotification:
    """The notice the RBI framework requires before each debit.

    Emitted for real rather than described, because "compliant escalation" in
    the track bar means the mandated fields actually exist. The framework
    specifies merchant name, amount, date and time of debit, mandate reference
    and reason for debit, plus a route to opt out of this single transaction.
    """

    merchant_name: str
    amount: Decimal
    debit_at: datetime
    mandate_reference: str
    reason: str
    opt_out_deadline: datetime

    @classmethod
    def build(
        cls,
        *,
        merchant_name: str,
        attempt: ProposedAttempt,
        reason: str,
    ) -> "PreDebitNotification":
        return cls(
            merchant_name=merchant_name,
            amount=attempt.amount,
            debit_at=attempt.execute_at,
            mandate_reference=attempt.mandate_id,
            reason=reason,
            # The customer must be able to opt out right up to the debit.
            opt_out_deadline=attempt.execute_at,
        )
