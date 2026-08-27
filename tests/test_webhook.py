"""Tests for webhook verification and event parsing.

Signature verification is the security boundary of the whole service, so it
gets adversarial cases rather than a happy path and a shrug.
"""

import hmac
import json
from decimal import Decimal
from hashlib import sha256

import pytest

from fourshots.taxonomy import INSUFFICIENT_BALANCE, MANDATE_DEAD, UNCLASSIFIED
from fourshots.webhook import (
    DeclineObserved,
    DowntimeObserved,
    SignatureInvalid,
    SubscriptionStateChanged,
    parse_event,
    verify_signature,
)

SECRET = "test_webhook_secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, sha256).hexdigest()


# --- Signature verification ------------------------------------------------

def test_valid_signature_passes() -> None:
    body = b'{"event":"payment.failed"}'
    verify_signature(body, sign(body), SECRET)  # must not raise


def test_tampered_body_is_rejected() -> None:
    body = b'{"event":"payment.failed","amount":100}'
    signature = sign(body)
    tampered = b'{"event":"payment.failed","amount":999999}'
    with pytest.raises(SignatureInvalid):
        verify_signature(tampered, signature, SECRET)


def test_wrong_secret_is_rejected() -> None:
    body = b'{"event":"payment.failed"}'
    with pytest.raises(SignatureInvalid):
        verify_signature(body, sign(body, "attacker_guess"), SECRET)


def test_missing_signature_header_is_rejected() -> None:
    with pytest.raises(SignatureInvalid, match="missing"):
        verify_signature(b"{}", None, SECRET)


def test_empty_secret_is_rejected_rather_than_trusted() -> None:
    """An unconfigured secret must fail closed. Failing open here would turn
    verification into a formality while still looking like it works."""
    body = b"{}"
    with pytest.raises(SignatureInvalid, match="no webhook secret"):
        verify_signature(body, sign(body), "")


def test_whitespace_around_signature_is_tolerated() -> None:
    body = b'{"event":"payment.failed"}'
    verify_signature(body, f"  {sign(body)}\n", SECRET)


# --- payment.failed --------------------------------------------------------

def decline_payload(reason: str | None, amount: int = 49900) -> dict:
    return {
        "event": "payment.failed",
        "created_at": 1788000000,
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_TestPayment01",
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "method": "upi",
                    "order_id": "order_Test01",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_reason": reason,
                    "error_description": "Payment failed",
                    "notes": {"subscription_id": "sub_Test01"},
                }
            }
        },
    }


def test_decline_is_parsed_and_classified() -> None:
    event = parse_event(decline_payload("insufficient_funds"))
    assert isinstance(event, DeclineObserved)
    assert event.payment_id == "pay_TestPayment01"
    assert event.subscription_id == "sub_Test01"
    assert event.classification.failure_class is INSUFFICIENT_BALANCE


def test_amount_is_converted_from_paise_exactly() -> None:
    """Razorpay denominates in paise. Going through float loses paise, and
    losing paise in a system that reports rupees recovered is not acceptable."""
    event = parse_event(decline_payload("insufficient_funds", amount=49999))
    assert isinstance(event, DeclineObserved)
    assert event.amount == Decimal("499.99")


def test_raw_reason_is_retained_alongside_the_classification() -> None:
    """If a mapping is later found wrong, historic logs must be re-readable
    against a corrected taxonomy."""
    event = parse_event(decline_payload("invalid_vpa"))
    assert isinstance(event, DeclineObserved)
    assert event.raw_reason == "invalid_vpa"
    assert event.raw_code == "BAD_REQUEST_ERROR"
    assert event.classification.failure_class is MANDATE_DEAD


def test_missing_error_reason_does_not_crash() -> None:
    """Rails omit fields. A missing reason must degrade to the cautious
    default, not take the endpoint down."""
    event = parse_event(decline_payload(None))
    assert isinstance(event, DeclineObserved)
    assert event.classification.failure_class is UNCLASSIFIED


def test_timestamp_comes_from_the_payload() -> None:
    event = parse_event(decline_payload("insufficient_funds"))
    assert event.at.timestamp() == 1788000000


# --- downtime --------------------------------------------------------------

def test_downtime_started_is_parsed() -> None:
    event = parse_event(
        {
            "event": "payment.downtime.started",
            "created_at": 1788000000,
            "payload": {
                "payment.downtime": {
                    "entity": {
                        "id": "down_Test01",
                        "method": "upi",
                        "status": "started",
                        "severity": "high",
                        "instrument": {"issuer": "HDFC"},
                    }
                }
            },
        }
    )
    assert isinstance(event, DowntimeObserved)
    assert event.issuer == "HDFC"
    assert event.severity == "high"
    assert not event.resolved


def test_downtime_resolved_sets_the_flag() -> None:
    event = parse_event(
        {
            "event": "payment.downtime.resolved",
            "payload": {"payment.downtime": {"entity": {"id": "d1", "status": "resolved"}}},
        }
    )
    assert isinstance(event, DowntimeObserved)
    assert event.resolved


# --- subscription ----------------------------------------------------------

def test_subscription_halted_is_parsed() -> None:
    """`halted` is Razorpay declaring the retry budget spent and the mandate
    dead -- the primary outcome variable in the results."""
    event = parse_event(
        {
            "event": "subscription.halted",
            "payload": {
                "subscription": {
                    "entity": {
                        "id": "sub_Test01",
                        "status": "halted",
                        "paid_count": 3,
                        "remaining_count": 9,
                    }
                }
            },
        }
    )
    assert isinstance(event, SubscriptionStateChanged)
    assert event.state == "halted"
    assert event.paid_count == 3


def test_unhandled_event_returns_none_rather_than_raising() -> None:
    assert parse_event({"event": "settlement.processed", "payload": {}}) is None


def test_malformed_payload_does_not_crash_the_parser() -> None:
    """Anything that got past signature verification is well-intentioned but
    may still be shaped differently than expected."""
    event = parse_event({"event": "payment.failed"})
    assert isinstance(event, DeclineObserved)
    assert event.payment_id == ""
    assert event.classification.failure_class is UNCLASSIFIED


def test_signed_roundtrip_matches_what_the_endpoint_will_do() -> None:
    """End-to-end shape check: serialise, sign, verify, parse."""
    payload = decline_payload("insufficient_funds")
    body = json.dumps(payload).encode()
    verify_signature(body, sign(body), SECRET)
    event = parse_event(json.loads(body))
    assert isinstance(event, DeclineObserved)
    assert event.classification.failure_class is INSUFFICIENT_BALANCE
