"""End-to-end tests through the HTTP endpoint.

These exercise the path a real Razorpay webhook takes: signed bytes arrive,
the signature is verified, the decline is classified, and the decision lands in
a verifiable audit chain. The unit tests cover the pieces; this covers the
wiring between them.
"""

import hmac
import json
from hashlib import sha256

import pytest

SECRET = "endtoend_test_secret"


@pytest.fixture()
def client(tmp_path):
    """A test client with an isolated audit log.

    The app is built by a factory, so each test gets its own log and its own
    secret by injection. No module-cache clearing, no environment mutation, and
    no chance of one test's decisions leaking into another's chain.

    The recovery service is injected with no Razorpay client, which is what
    makes this suite safe to run on a machine that has test keys in its `.env`:
    the decisions are all made and logged, and nothing leaves the process.
    """
    from fastapi.testclient import TestClient

    from fourshots.app import create_app
    from fourshots.audit import AuditLog
    from fourshots.recovery import RecoveryService

    audit = AuditLog(tmp_path / "audit.jsonl")
    application = create_app(
        audit=audit,
        webhook_secret=SECRET,
        recovery=RecoveryService(audit, merchant_name="Example Merchant"),
    )
    return TestClient(application), audit


def post_signed(client, payload: dict, secret: str = SECRET):
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, sha256).hexdigest()
    return client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "X-Razorpay-Signature": signature,
            "Content-Type": "application/json",
        },
    )


def decline(reason: str, amount: int = 49900) -> dict:
    return {
        "event": "payment.failed",
        "created_at": 1788000000,
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_Test01",
                    "amount": amount,
                    "currency": "INR",
                    "method": "upi",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_reason": reason,
                    "error_description": "Payment failed",
                    "notes": {"subscription_id": "sub_Test01"},
                }
            }
        },
    }


def test_health_reports_the_chain_head(client) -> None:
    http, _ = client
    body = http.get("/health").json()
    assert body["status"] == "ok"
    assert len(body["audit_head"]) == 64


def test_signed_decline_is_accepted_and_classified(client) -> None:
    http, _ = client
    response = post_signed(http, decline("insufficient_funds"))
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["event"] == "payment.failed"
    assert body["failure_class"] == "insufficient_balance"
    assert body["terminal"] is False
    # A balance failure is retryable, so the endpoint does not merely classify
    # it -- it books the next attempt and says when.
    assert body["decision"]["action"] == "retry_booked"
    assert body["decision"]["execute_at"] is not None


def test_terminal_decline_is_flagged_as_such(client) -> None:
    """A dead mandate should be identified immediately rather than after
    three wasted attempts."""
    http, _ = client
    body = post_signed(http, decline("invalid_vpa")).json()
    assert body["failure_class"] == "mandate_dead"
    assert body["terminal"] is True


def test_unsigned_request_is_rejected(client) -> None:
    http, _ = client
    response = http.post("/webhooks/razorpay", content=b'{"event":"payment.failed"}')
    assert response.status_code == 401
    assert response.json()["accepted"] is False


def test_request_signed_with_the_wrong_secret_is_rejected(client) -> None:
    http, _ = client
    response = post_signed(http, decline("insufficient_funds"), secret="attacker")
    assert response.status_code == 401


def test_rejected_request_body_is_never_recorded(client) -> None:
    """An unverified payload is attacker-controlled and must not enter the
    decision record -- only the fact of rejection does."""
    http, audit = client
    http.post(
        "/webhooks/razorpay",
        content=json.dumps(decline("insufficient_funds")).encode(),
        headers={"X-Razorpay-Signature": "0" * 64},
    )
    entries = list(audit.read())
    assert [e.kind for e in entries] == ["webhook_rejected"]
    assert "pay_Test01" not in json.dumps(entries[0].data)


def test_decision_lands_in_a_verifiable_chain(client) -> None:
    http, audit = client
    post_signed(http, decline("insufficient_funds"))
    post_signed(http, decline("invalid_vpa"))

    verified = http.get("/audit/verify").json()
    assert verified["verified"] is True
    # Two declines, and the action taken about each one.
    assert verified["entries"] == 4


def test_audit_entry_records_reasoning_and_provenance(client) -> None:
    """The audit trail has to answer 'why', not just 'what' -- including how
    confident the code-to-class mapping was."""
    http, audit = client
    post_signed(http, decline("insufficient_funds"))

    entry = next(iter(audit.read()))
    assert entry.kind == "decline_observed"
    assert entry.mandate_id == "sub_Test01"
    assert entry.data["failure_class"] == "insufficient_balance"
    assert entry.data["blocker"] == "balance"
    assert entry.data["raw_reason"] == "insufficient_funds"
    assert entry.data["mapping_confidence"] == "documented"
    assert entry.data["min_backoff_hours"] >= 24.0


def test_amount_is_recorded_in_rupees_without_precision_loss(client) -> None:
    http, audit = client
    post_signed(http, decline("insufficient_funds", amount=49999))
    entry = next(iter(audit.read()))
    assert entry.data["amount_inr"] == "499.99"


def test_downtime_event_is_recorded(client) -> None:
    http, audit = client
    post_signed(
        http,
        {
            "event": "payment.downtime.started",
            "created_at": 1788000000,
            "payload": {
                "payment.downtime": {
                    "entity": {
                        "id": "down_01",
                        "method": "upi",
                        "status": "started",
                        "severity": "high",
                        "instrument": {"issuer": "HDFC"},
                    }
                }
            },
        },
    )
    entry = next(iter(audit.read()))
    assert entry.kind == "downtime_observed"
    assert entry.data["issuer"] == "HDFC"


def test_subscription_halted_is_recorded(client) -> None:
    http, audit = client
    post_signed(
        http,
        {
            "event": "subscription.halted",
            "created_at": 1788000000,
            "payload": {
                "subscription": {
                    "entity": {"id": "sub_Test01", "status": "halted", "paid_count": 3}
                }
            },
        },
    )
    entry = next(iter(audit.read()))
    assert entry.kind == "subscription_state"
    assert entry.data["state"] == "halted"


def test_unmodelled_event_is_still_logged(client) -> None:
    """The log should be a complete account of what the endpoint saw."""
    http, audit = client
    post_signed(http, {"event": "settlement.processed", "payload": {}})
    entry = next(iter(audit.read()))
    assert entry.kind == "event_ignored"


def test_a_terminal_decline_escalates_over_http(client) -> None:
    """The endpoint does not stop at classifying. A debit that cannot clear
    silently comes back with the message that will be sent about it."""
    http, _ = client
    decision = post_signed(http, decline("invalid_vpa")).json()["decision"]
    assert decision["action"] == "escalated"
    assert "Example Merchant" in decision["message"]
    # No test keys are injected here, so nothing left the process -- and the
    # response says that rather than implying a link exists.
    assert decision["executed"] is False
    assert decision["payment_link_url"] is None


def test_recovery_actions_are_read_back_out_of_the_chain(client) -> None:
    http, _ = client
    post_signed(http, decline("insufficient_funds"))
    post_signed(http, decline("invalid_vpa"))

    body = http.get("/recovery/actions").json()
    assert [action["kind"] for action in body["actions"]] == [
        "attempt_booked",
        "escalation_drafted",
    ]
    assert body["count"] == 2
    # Same bytes as the verifier reads, so a schedule shown here cannot
    # disagree with a log that verifies.
    assert http.get("/audit/verify").json()["verified"] is True


def test_a_redelivered_webhook_cannot_spend_a_fifth_attempt(client) -> None:
    """Razorpay retries webhook delivery. Five deliveries must not become five
    executions -- NPCI permits four."""
    http, _ = client
    actions = [
        post_signed(http, decline("insufficient_funds")).json()["decision"]["action"]
        for _ in range(5)
    ]
    assert actions.count("retry_booked") == 3
    assert actions[-2:] == ["stopped", "stopped"]
