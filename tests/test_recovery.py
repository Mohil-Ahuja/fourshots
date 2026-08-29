"""Tests for the closed loop: a decline in, a recorded recovery action out.

The engine's scheduling arithmetic is tested in `test_engine.py` and the
regulatory gate in `test_policy.py`. What is tested here is the part that only
exists in production: that a decision becomes an action, that the action is the
one the log says it was, and that the four-attempt budget survives a restart.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fourshots.audit import AuditLog
from fourshots.policy import (
    MAX_ATTEMPTS_PER_CYCLE,
    IST,
    is_in_execution_window,
)
from fourshots.razorpay_client import PaymentLink, RazorpayError
from fourshots.recovery import Action, RecoveryService, decisions_from
from fourshots.taxonomy import Blocker, classify
from fourshots.webhook import DeclineObserved

# A Saturday morning in IST, outside the blocked UPI peak windows.
FIRST_DECLINE = datetime(2026, 9, 26, 9, 0, tzinfo=IST)


def make_decline(
    reason: str | None = "insufficient_funds",
    *,
    npci: str | None = None,
    amount: str = "2499.00",
    at: datetime | None = None,
    payment_id: str = "pay_Test01",
    subscription_id: str | None = "sub_Test01",
    description: str | None = "Payment failed",
) -> DeclineObserved:
    return DeclineObserved(
        payment_id=payment_id,
        subscription_id=subscription_id,
        order_id=None,
        amount=Decimal(amount),
        method="upi",
        at=at or FIRST_DECLINE,
        raw_code="BAD_REQUEST_ERROR",
        raw_reason=reason,
        raw_description=description,
        npci_code=npci,
        classification=classify(razorpay_code=reason, npci_code=npci),
    )


class StubClient:
    """A Razorpay client that records the call and returns a link."""

    def __init__(self, failing: bool = False) -> None:
        self.failing = failing
        self.calls: list[dict] = []

    def create_payment_link(self, **kwargs) -> PaymentLink:
        self.calls.append(kwargs)
        if self.failing:
            raise RazorpayError(400, '{"error":{"description":"link rejected"}}')
        return PaymentLink(
            id="plink_Test01",
            short_url="https://rzp.io/i/abcd",
            amount=kwargs["amount"],
            reference_id=kwargs.get("reference_id"),
            created_at=None,
        )


@pytest.fixture()
def audit(tmp_path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


def service(audit: AuditLog, **kwargs) -> RecoveryService:
    kwargs.setdefault("merchant_name", "Example Merchant")
    return RecoveryService(audit, **kwargs)


def observe(audit: AuditLog, decline: DeclineObserved) -> None:
    """Record the decline the way the HTTP layer does, before deciding.

    Order matters: the entry written here is part of the history the attempt
    budget is counted from.
    """
    audit.append(
        "decline_observed",
        {
            "raw_code": decline.raw_code,
            "raw_reason": decline.raw_reason,
            "npci_code": decline.npci_code,
            "raw_description": decline.raw_description,
        },
        mandate_id=decline.subscription_id,
        at=decline.at,
    )


# --- Booking a retry -------------------------------------------------------


def test_a_balance_decline_books_a_legal_retry(audit) -> None:
    decline = make_decline()
    observe(audit, decline)
    decision = service(audit).handle(decline)

    assert decision.action is Action.RETRY_BOOKED
    assert decision.execute_at is not None
    assert is_in_execution_window(decision.execute_at)
    # The RBI framework requires a day's notice, and the notice goes out now.
    assert decision.execute_at - decline.at >= timedelta(hours=24)


def test_a_booked_retry_carries_its_pre_debit_notification(audit) -> None:
    """"Compliant escalation" means the mandated fields exist, not that a
    document says they would."""
    decline = make_decline()
    observe(audit, decline)
    service(audit).handle(decline)

    booked = next(e for e in audit.read() if e.kind == "attempt_booked")
    notice = booked.data["pre_debit_notice"]
    assert notice["merchant_name"] == "Example Merchant"
    assert notice["amount_inr"] == "2499.00"
    assert notice["mandate_reference"] == "sub_Test01"
    assert notice["reason"]
    assert notice["debit_at"] == booked.data["execute_at"]


def test_a_booked_retry_never_claims_it_reached_the_rail(audit) -> None:
    """Razorpay exposes no call that re-attempts a mandate on a chosen date.
    The log says so on the entry rather than leaving a reader to assume."""
    decline = make_decline()
    observe(audit, decline)
    decision = service(audit).handle(decline)

    booked = next(e for e in audit.read() if e.kind == "attempt_booked")
    assert booked.data["executed_against_rail"] is False
    assert decision.executed is False


def test_a_booked_retry_appears_in_the_schedule(audit) -> None:
    svc = service(audit)
    decline = make_decline()
    observe(audit, decline)
    svc.handle(decline)

    (booked,) = svc.booked_attempts()
    assert booked.mandate_id == "sub_Test01"
    assert booked.attempt_number == 2  # the original debit was the first


# --- Escalating ------------------------------------------------------------


def test_a_dead_mandate_escalates_instead_of_burning_attempts(audit) -> None:
    decline = make_decline("invalid_vpa")
    assert decline.classification.failure_class.blocker is Blocker.MANDATE_REPAIR
    observe(audit, decline)
    decision = service(audit).handle(decline)

    assert decision.action is Action.ESCALATED
    assert decision.draft is not None
    assert "Rs 2,499.00" in decision.draft.body


def test_an_escalation_creates_a_real_payment_link_when_keys_exist(audit) -> None:
    client = StubClient()
    decline = make_decline("invalid_vpa")
    observe(audit, decline)
    decision = service(audit, client=client).handle(decline)

    assert decision.executed is True
    assert decision.payment_link_id == "plink_Test01"
    assert decision.draft.body.endswith("https://rzp.io/i/abcd")
    call = client.calls[0]
    assert call["amount"] == Decimal("2499.00")
    assert call["reference_id"] == "sub_Test01:pay_Test01"

    entry = next(e for e in audit.read() if e.kind == "escalation_executed")
    assert entry.data["payment_link_url"] == "https://rzp.io/i/abcd"


def test_without_keys_the_decision_is_still_made_and_says_it_was_not_executed(
    audit,
) -> None:
    """The interesting property of the no-credentials path: everything is
    decided, gated and logged, and nothing is reported as having happened."""
    decline = make_decline("invalid_vpa")
    observe(audit, decline)
    decision = service(audit).handle(decline)

    assert decision.action is Action.ESCALATED
    assert decision.executed is False
    assert decision.payment_link_url is None
    assert "no payment link was created" in decision.note
    assert [e.kind for e in audit.read()][-1] == "escalation_drafted"


def test_a_failed_payment_link_is_recorded_as_a_failure_not_a_success(audit) -> None:
    decline = make_decline("invalid_vpa")
    observe(audit, decline)
    decision = service(audit, client=StubClient(failing=True)).handle(decline)

    assert decision.action is Action.ESCALATED
    assert decision.executed is False
    assert "link rejected" in decision.note
    assert [e.kind for e in audit.read()][-1] == "escalation_drafted"


def test_an_escalation_records_the_wording_that_was_sent(audit) -> None:
    """The template carries no figures, so it can be reviewed once for a whole
    class of mandates -- separately from the facts this one put into it."""
    decline = make_decline("invalid_vpa")
    observe(audit, decline)
    service(audit).handle(decline)

    entry = next(e for e in audit.read() if e.kind == "escalation_drafted")
    assert "{amount}" in entry.data["message_template"]
    assert "Rs 2,499.00" in entry.data["message"]
    assert entry.data["message_source"] == "template"


def test_an_above_threshold_debit_escalates_rather_than_failing_the_gate(
    audit,
) -> None:
    """AFA is knowable before an attempt is spent. A debit that needs the
    customer to authenticate is an escalation, not a dead end."""
    decline = make_decline(amount="50000.00")
    observe(audit, decline)
    decision = service(audit).handle(decline)

    assert decision.action is Action.ESCALATED
    assert decision.blocker is Blocker.CUSTOMER_ACTION
    assert "authentication" in decision.note


def test_a_limit_breach_escalates_with_copy_about_the_limit(audit) -> None:
    decline = make_decline(None, npci="Z8")
    assert decline.classification.failure_class.blocker is Blocker.AMOUNT_CHANGE
    observe(audit, decline)
    decision = service(audit).handle(decline)

    assert decision.action is Action.ESCALATED
    assert "limit" in decision.draft.body.lower()


# --- Stopping --------------------------------------------------------------


def test_the_budget_stops_the_cycle_after_four_executions(audit) -> None:
    """NPCI allows four executions per cycle. The fifth is not merely wasteful,
    it is not permitted."""
    svc = service(audit)
    decision = None
    for index in range(MAX_ATTEMPTS_PER_CYCLE):
        decline = make_decline(
            at=FIRST_DECLINE + timedelta(days=index),
            payment_id=f"pay_Test{index:02d}",
        )
        observe(audit, decline)
        decision = svc.handle(decline)

    assert decision.action is Action.STOPPED
    assert "budget exhausted" in decision.note
    assert [e.kind for e in audit.read()][-1] == "mandate_halted"


def test_an_unreadable_code_is_not_guessed_at_indefinitely(audit) -> None:
    svc = service(audit)
    decision = None
    for index in range(3):
        decline = make_decline(
            "payment_declined",
            at=FIRST_DECLINE + timedelta(days=index),
            payment_id=f"pay_Test{index:02d}",
        )
        observe(audit, decline)
        decision = svc.handle(decline)

    assert decision.blocker is Blocker.UNKNOWN
    assert decision.action is Action.STOPPED


# --- State -----------------------------------------------------------------


def test_the_attempt_budget_survives_a_restart(audit) -> None:
    """State lives in the audit log, not in process memory, so a redeployed
    server does not hand every mandate a fresh set of four attempts."""
    for index in range(MAX_ATTEMPTS_PER_CYCLE - 1):
        decline = make_decline(
            at=FIRST_DECLINE + timedelta(days=index),
            payment_id=f"pay_Test{index:02d}",
        )
        observe(audit, decline)
        service(audit).handle(decline)  # a fresh service each time

    final = make_decline(at=FIRST_DECLINE + timedelta(days=3), payment_id="pay_Final")
    observe(audit, final)
    decision = service(audit).handle(final)
    assert decision.attempts_used == MAX_ATTEMPTS_PER_CYCLE
    assert decision.action is Action.STOPPED


def test_declines_from_a_previous_cycle_do_not_count_against_this_one(audit) -> None:
    """The budget is per execution cycle. Counting a decline from two months
    ago would refuse an attempt that is legally available."""
    old = make_decline(
        at=FIRST_DECLINE - timedelta(days=60), payment_id="pay_Old"
    )
    observe(audit, old)
    fresh = make_decline()
    observe(audit, fresh)

    decision = service(audit).handle(fresh)
    assert decision.attempts_used == 1
    assert decision.action is Action.RETRY_BOOKED


def test_another_mandates_declines_are_not_counted(audit) -> None:
    for index in range(MAX_ATTEMPTS_PER_CYCLE):
        other = make_decline(
            at=FIRST_DECLINE + timedelta(days=index),
            payment_id=f"pay_Other{index}",
            subscription_id="sub_Other",
        )
        observe(audit, other)

    ours = make_decline()
    observe(audit, ours)
    decision = service(audit).handle(ours)
    assert decision.attempts_used == 1
    assert decision.action is Action.RETRY_BOOKED


# --- The record ------------------------------------------------------------


def test_every_action_lands_in_a_chain_that_still_verifies(audit) -> None:
    for reason in ("insufficient_funds", "invalid_vpa"):
        decline = make_decline(reason, payment_id=f"pay_{reason}")
        observe(audit, decline)
        service(audit, client=StubClient()).handle(decline)

    assert audit.verify() == 4
    actions = decisions_from(audit.read())
    assert [action["kind"] for action in actions] == [
        "attempt_booked",
        "escalation_executed",
    ]


def test_editing_a_recorded_decision_breaks_the_chain(audit) -> None:
    """The whole point of logging the decision rather than asserting it."""
    from fourshots.audit import ChainBroken

    decline = make_decline()
    observe(audit, decline)
    service(audit).handle(decline)

    lines = audit.path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1].replace('"attempt_number": 2', '"attempt_number": 9')
    audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ChainBroken):
        AuditLog(audit.path).verify()


def test_the_decision_dict_reports_what_remains_of_the_budget(audit) -> None:
    decline = make_decline()
    observe(audit, decline)
    payload = service(audit).handle(decline).to_dict()

    assert payload["action"] == "retry_booked"
    assert payload["attempts_used"] == 1
    assert payload["attempts_remaining"] == MAX_ATTEMPTS_PER_CYCLE - 1
    assert payload["executed"] is False


def test_a_decline_with_no_subscription_still_gets_a_decision(audit) -> None:
    """A one-off payment carries no subscription id. It is keyed by payment
    instead of being dropped."""
    decline = make_decline(subscription_id=None)
    decision = service(audit).handle(decline)
    assert decision.mandate_id == "pay_Test01"
    assert decision.action in {Action.RETRY_BOOKED, Action.ESCALATED}


def test_utc_and_ist_declines_are_treated_identically(audit) -> None:
    """Webhooks arrive in UTC and the NPCI windows are IST. A comparison that
    got this wrong would book attempts inside a blocked peak window."""
    decline = make_decline(at=FIRST_DECLINE.astimezone(timezone.utc))
    observe(audit, decline)
    decision = service(audit).handle(decline)
    assert is_in_execution_window(decision.execute_at)
