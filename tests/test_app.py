"""End-to-end tests through the HTTP endpoint.

These exercise the path a real Razorpay webhook takes: signed bytes arrive,
the signature is verified, the decline is classified, and the decision lands in
a verifiable audit chain. The unit tests cover the pieces; this covers the
wiring between them.
"""

import hmac
import json
import os
from hashlib import sha256

import pytest

SECRET = "endtoend_test_secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A test client with an isolated audit log.

    `fourshots.app` builds its AuditLog at import time, so the environment has
    to be set before the module is first imported and the module cache has to
    be cleared between tests that want separate logs.
    """
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))

    import sys

    sys.modules.pop("fourshots.app", None)
    from fastapi.testclient import TestClient

    import fourshots.app as app_module

    yield TestClient(app_module.app), app_module
    sys.modules.pop("fourshots.app", None)


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
    assert response.json() == {
        "accepted": True,
        "event": "payment.failed",
        "failure_class": "insufficient_balance",
        "terminal": False,
    }


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
    http, module = client
    http.post(
        "/webhooks/razorpay",
        content=json.dumps(decline("insufficient_funds")).encode(),
        headers={"X-Razorpay-Signature": "0" * 64},
    )
    entries = list(module.audit.read())
    assert [e.kind for e in entries] == ["webhook_rejected"]
    assert "pay_Test01" not in json.dumps(entries[0].data)


def test_decision_lands_in_a_verifiable_chain(client) -> None:
    http, module = client
    post_signed(http, decline("insufficient_funds"))
    post_signed(http, decline("invalid_vpa"))

    verified = http.get("/audit/verify").json()
    assert verified["verified"] is True
    assert verified["entries"] == 2


def test_audit_entry_records_reasoning_and_provenance(client) -> None:
    """The audit trail has to answer 'why', not just 'what' -- including how
    confident the code-to-class mapping was."""
    http, module = client
    post_signed(http, decline("insufficient_funds"))

    entry = next(iter(module.audit.read()))
    assert entry.kind == "decline_observed"
    assert entry.mandate_id == "sub_Test01"
    assert entry.data["failure_class"] == "insufficient_balance"
    assert entry.data["blocker"] == "balance"
    assert entry.data["raw_reason"] == "insufficient_funds"
    assert entry.data["mapping_confidence"] == "documented"
    assert entry.data["min_backoff_hours"] >= 24.0


def test_amount_is_recorded_in_rupees_without_precision_loss(client) -> None:
    http, module = client
    post_signed(http, decline("insufficient_funds", amount=49999))
    entry = next(iter(module.audit.read()))
    assert entry.data["amount_inr"] == "499.99"


def test_downtime_event_is_recorded(client) -> None:
    http, module = client
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
    entry = next(iter(module.audit.read()))
    assert entry.kind == "downtime_observed"
    assert entry.data["issuer"] == "HDFC"


def test_subscription_halted_is_recorded(client) -> None:
    http, module = client
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
    entry = next(iter(module.audit.read()))
    assert entry.kind == "subscription_state"
    assert entry.data["state"] == "halted"


def test_unmodelled_event_is_still_logged(client) -> None:
    """The log should be a complete account of what the endpoint saw."""
    http, module = client
    post_signed(http, {"event": "settlement.processed", "payload": {}})
    entry = next(iter(module.audit.read()))
    assert entry.kind == "event_ignored"
