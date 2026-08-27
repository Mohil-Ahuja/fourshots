"""Two independent implementations of the regulatory gate must agree.

The rules in `policy.py` decide whether a debit against someone's account is
permitted at all. Everywhere else in this system a bug shows up as a worse
number; here it shows up as an illegal attempt the benchmark still counts as
fine, and no aggregate metric would reveal it.

So the rules are written twice — once in Python, once in Rust, from the
circulars rather than from each other — and these tests generate inputs and
require the two to return identical answers. Two independent implementations
agreeing across the whole input space is evidence. One implementation passing
its own tests is an assertion.

The Rust side is not used in production. Python remains the only execution
path, so behaviour never depends on whether an extension compiled on a given
machine. Without the toolchain these tests skip and nothing else changes.

Build the oracle with:
    cd rust && maturin build --release
    pip install rust/target/wheels/fourshots_rules-*.whl
"""

import random
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fourshots.policy import (
    AFA_THRESHOLD_ELEVATED,
    AFA_THRESHOLD_GENERAL,
    IST,
    MAX_ATTEMPTS_PER_CYCLE,
    PRE_DEBIT_NOTICE,
    MandatePurpose,
    ProposedAttempt,
    Violation,
    afa_required,
    check_legality,
    is_in_execution_window,
    next_execution_window,
)

rust = pytest.importorskip(
    "fourshots_rules",
    reason="Rust oracle not built. cd rust && maturin build --release",
)

# The two implementations name violations independently -- Python an enum,
# Rust an integer. Mapping them here rather than sharing a definition is
# deliberate: a shared constant would let one mistake propagate to both.
VIOLATION_CODES = {
    Violation.ATTEMPT_BUDGET_EXHAUSTED: rust.V_ATTEMPT_BUDGET_EXHAUSTED,
    Violation.INSUFFICIENT_NOTICE: rust.V_INSUFFICIENT_NOTICE,
    Violation.PEAK_WINDOW: rust.V_PEAK_WINDOW,
    Violation.CUSTOMER_OPTED_OUT: rust.V_CUSTOMER_OPTED_OUT,
    Violation.AFA_REQUIRED: rust.V_AFA_REQUIRED,
    Violation.MANDATE_NOT_ACTIVE: rust.V_MANDATE_NOT_ACTIVE,
}

BASE_DAY = datetime(2026, 9, 15, 0, 0, tzinfo=IST)


def at_minute(minute: int) -> datetime:
    return BASE_DAY + timedelta(minutes=minute)


# --- Execution windows: exhaustive, not sampled ---------------------------

def test_execution_window_agrees_for_every_minute_of_the_day() -> None:
    """1440 minutes is small enough to check completely, so sampling would be
    a choice to know less. An off-by-one at a peak boundary is invisible in
    every aggregate metric this project reports."""
    disagreements = [
        minute
        for minute in range(1440)
        if is_in_execution_window(at_minute(minute))
        != rust.is_in_execution_window(minute)
    ]
    assert not disagreements, f"implementations differ at minutes: {disagreements[:10]}"


def test_wait_until_legal_agrees_for_every_minute() -> None:
    for minute in range(1440):
        python_target = next_execution_window(at_minute(minute))
        python_wait = int((python_target - at_minute(minute)).total_seconds() // 60)
        assert python_wait == rust.minutes_until_legal(minute), (
            f"disagreement at minute {minute}: python {python_wait}, "
            f"rust {rust.minutes_until_legal(minute)}"
        )


def test_both_implementations_always_reach_a_legal_minute() -> None:
    """A property neither implementation should be able to violate: waiting the
    advised interval must actually land somewhere legal."""
    for minute in range(1440):
        wait = rust.minutes_until_legal(minute)
        assert rust.is_in_execution_window(minute + wait)
        assert is_in_execution_window(at_minute(minute) + timedelta(minutes=wait))


# --- AFA thresholds --------------------------------------------------------

@pytest.mark.parametrize(
    "purpose,threshold",
    [
        (MandatePurpose.GENERAL, AFA_THRESHOLD_GENERAL),
        (MandatePurpose.INSURANCE, AFA_THRESHOLD_ELEVATED),
        (MandatePurpose.MUTUAL_FUND_SIP, AFA_THRESHOLD_ELEVATED),
        (MandatePurpose.CREDIT_CARD_BILL, AFA_THRESHOLD_ELEVATED),
    ],
)
def test_afa_agrees_around_the_threshold(purpose, threshold) -> None:
    """Clustered on the boundary, where an inclusive/exclusive slip lives.
    Getting it wrong costs a customer an authentication step they are not owed
    and shows up in no metric."""
    threshold_paise = int(threshold * 100)
    for delta in (-2, -1, 0, 1, 2):
        amount = threshold + Decimal(delta)
        assert afa_required(amount, purpose) == rust.afa_required(
            int(amount * 100), threshold_paise
        ), f"disagreement at {amount} for {purpose}"


def test_afa_agrees_across_random_amounts() -> None:
    rng = random.Random(20260828)
    for _ in range(5000):
        amount = Decimal(rng.randint(1, 500_000))
        purpose = rng.choice(list(MandatePurpose))
        threshold = (
            AFA_THRESHOLD_GENERAL
            if purpose is MandatePurpose.GENERAL
            else AFA_THRESHOLD_ELEVATED
        )
        assert afa_required(amount, purpose) == rust.afa_required(
            int(amount * 100), int(threshold * 100)
        ), f"disagreement at {amount} for {purpose}"


# --- The full gate ---------------------------------------------------------

def _random_case(rng: random.Random):
    """One randomly-shaped proposed attempt, skewed toward the interesting.

    Uniform random inputs would almost never produce a legal attempt or a
    boundary case, so notice periods cluster around the 24-hour requirement and
    amounts around the AFA thresholds -- the places a disagreement can hide.
    """
    minute = rng.randrange(1440)
    notice_minutes = rng.choice(
        [
            rng.randrange(0, 3000),
            1440 + rng.randint(-2, 2),  # right at the 24-hour requirement
        ]
    )
    purpose = rng.choice(list(MandatePurpose))
    threshold = (
        AFA_THRESHOLD_GENERAL
        if purpose is MandatePurpose.GENERAL
        else AFA_THRESHOLD_ELEVATED
    )
    amount = rng.choice(
        [
            Decimal(rng.randint(1, 200_000)),
            threshold + Decimal(rng.randint(-2, 2)),  # right at the threshold
        ]
    )
    return {
        "minute": minute,
        "notice_minutes": notice_minutes,
        "purpose": purpose,
        "threshold": threshold,
        "amount": amount,
        "attempts_used": rng.randrange(0, MAX_ATTEMPTS_PER_CYCLE + 2),
        "mandate_active": rng.random() > 0.15,
        "opted_out": rng.random() < 0.15,
        "afa_obtained": rng.random() < 0.3,
    }


def test_full_gate_agrees_across_random_attempts() -> None:
    """The headline claim of this file: both implementations refuse and permit
    exactly the same attempts, for exactly the same reasons, in the same order.
    """
    rng = random.Random(20260828)

    for index in range(20_000):
        case = _random_case(rng)
        execute_at = at_minute(case["minute"])

        python_violations = check_legality(
            ProposedAttempt(
                mandate_id="mand_diff",
                amount=case["amount"],
                purpose=case["purpose"],
                execute_at=execute_at,
                notified_at=execute_at - timedelta(minutes=case["notice_minutes"]),
                attempts_used=case["attempts_used"],
                mandate_active=case["mandate_active"],
                customer_opted_out=case["opted_out"],
                afa_obtained=case["afa_obtained"],
            )
        ).violations

        rust_violations = rust.check_legality(
            case["attempts_used"],
            MAX_ATTEMPTS_PER_CYCLE,
            case["notice_minutes"],
            int(PRE_DEBIT_NOTICE.total_seconds() // 60),
            case["minute"],
            case["mandate_active"],
            case["opted_out"],
            int(case["amount"] * 100),
            int(case["threshold"] * 100),
            case["afa_obtained"],
        )

        assert [VIOLATION_CODES[v] for v in python_violations] == rust_violations, (
            f"case {index} disagrees.\n"
            f"  input: {case}\n"
            f"  python: {[v.name for v in python_violations]}\n"
            f"  rust:   {rust_violations}"
        )


def test_random_cases_actually_cover_both_outcomes() -> None:
    """A differential test where every generated case is illegal would prove
    only that both implementations can say no."""
    rng = random.Random(20260828)
    legal = illegal = 0

    for _ in range(2000):
        case = _random_case(rng)
        violations = rust.check_legality(
            case["attempts_used"],
            MAX_ATTEMPTS_PER_CYCLE,
            case["notice_minutes"],
            int(PRE_DEBIT_NOTICE.total_seconds() // 60),
            case["minute"],
            case["mandate_active"],
            case["opted_out"],
            int(case["amount"] * 100),
            int(case["threshold"] * 100),
            case["afa_obtained"],
        )
        if violations:
            illegal += 1
        else:
            legal += 1

    assert legal > 50, f"only {legal} legal cases generated; the test is one-sided"
    assert illegal > 50, f"only {illegal} illegal cases generated"


def test_every_violation_kind_is_exercised() -> None:
    """Agreement on rules that never fire proves nothing about those rules."""
    rng = random.Random(20260828)
    seen: set[int] = set()

    for _ in range(5000):
        case = _random_case(rng)
        seen.update(
            rust.check_legality(
                case["attempts_used"],
                MAX_ATTEMPTS_PER_CYCLE,
                case["notice_minutes"],
                int(PRE_DEBIT_NOTICE.total_seconds() // 60),
                case["minute"],
                case["mandate_active"],
                case["opted_out"],
                int(case["amount"] * 100),
                int(case["threshold"] * 100),
                case["afa_obtained"],
            )
        )

    missing = set(VIOLATION_CODES.values()) - seen
    assert not missing, f"violation codes never generated: {missing}"
