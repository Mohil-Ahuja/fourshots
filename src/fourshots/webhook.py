"""Razorpay webhook verification and event parsing.

Two jobs, kept apart from the HTTP layer so both are testable without a server.

1. Verify the signature. Razorpay signs the webhook body with HMAC-SHA256 under
   the secret configured in the dashboard and sends it as `X-Razorpay-Signature`.
   Verification runs against the *raw request bytes* -- re-serialising the
   parsed JSON changes whitespace and key order and silently breaks the digest.

2. Turn the payload into a domain event. The interesting one is
   `payment.failed`, which carries the decline reason: that string is the entire
   input to the taxonomy, and everything downstream keys off it.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any

from fourshots.taxonomy import Classification, classify

SIGNATURE_HEADER = "X-Razorpay-Signature"


class SignatureInvalid(Exception):
    """Raised when a payload's signature does not verify.

    Treated as a hard rejection rather than a warning: an endpoint that
    processes unverified webhooks is an endpoint anyone on the internet can
    drive, and this one moves money.
    """


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> None:
    """Verify a webhook body against its signature.

    Raises SignatureInvalid on any failure. Comparison uses `compare_digest`
    so a timing side-channel cannot be used to forge a signature byte by byte.
    """
    if not signature:
        raise SignatureInvalid(f"missing {SIGNATURE_HEADER} header")
    if not secret:
        raise SignatureInvalid("no webhook secret configured")

    expected = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip()):
        raise SignatureInvalid("signature does not match payload")


# --- Domain events ---------------------------------------------------------

@dataclass(frozen=True)
class DeclineObserved:
    """A payment failed and told us why.

    `classification` is the taxonomy's reading of the decline. `raw_reason` and
    `raw_code` are retained verbatim so the audit log records what the rail
    actually said, not just our interpretation of it -- if a mapping is later
    found wrong, historic logs can be re-read against a corrected taxonomy.
    """

    payment_id: str
    subscription_id: str | None
    order_id: str | None
    amount: Decimal
    method: str | None
    at: datetime
    raw_code: str | None
    raw_reason: str | None
    raw_description: str | None
    npci_code: str | None
    classification: Classification


@dataclass(frozen=True)
class DowntimeObserved:
    """Razorpay reported issuer or method downtime.

    Worth subscribing to because it converts an inference into an observation:
    instead of guessing from a failed attempt that a bank is down, the
    scheduler is told. A retry proposed into a known outage is a wasted
    attempt, and attempts are the scarce resource.
    """

    downtime_id: str
    method: str | None
    issuer: str | None
    status: str
    severity: str | None
    at: datetime
    resolved: bool


@dataclass(frozen=True)
class SubscriptionStateChanged:
    """A mandate moved state.

    `halted` is the one that matters most: it is Razorpay declaring the retry
    budget spent and the mandate dead. That transition is the outcome this
    whole project exists to make less frequent, so it is the primary outcome
    variable in the results.
    """

    subscription_id: str
    state: str
    at: datetime
    paid_count: int | None
    remaining_count: int | None


ParsedEvent = DeclineObserved | DowntimeObserved | SubscriptionStateChanged


def _entity(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """Razorpay nests entities as payload.<name>.entity. Missing is not fatal."""
    return payload.get("payload", {}).get(name, {}).get("entity", {}) or {}


def _timestamp(raw: dict[str, Any]) -> datetime:
    """Razorpay sends epoch seconds in `created_at`.

    Falls back to now() when absent so a malformed payload still lands in the
    log with an approximately-right time rather than being dropped.
    """
    epoch = raw.get("created_at")
    if isinstance(epoch, (int, float)):
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _paise_to_rupees(amount: Any) -> Decimal:
    """Razorpay denominates in paise. Convert exactly -- never through float."""
    if amount is None:
        return Decimal("0")
    return Decimal(int(amount)) / Decimal(100)


def _npci_code(payment: dict[str, Any]) -> str | None:
    """Pull the rail's own response code out of a payment entity, if present.

    Razorpay's error-mapping layer normally translates NPCI codes into its own
    `error_reason` strings before a webhook is sent, so this is usually absent
    on a standard integration -- `acquirer_data` carries identifiers (`rrn`,
    `upi_transaction_id`) rather than response codes. Merchants with direct PSP
    or bank connectivity do receive it, and it is strictly more informative
    than the translation: Z8 states a breached limit, which is terminal, where
    the rendered reason may only say the payment was declined.

    Read from the two places a code plausibly appears, and returns None
    otherwise. Absence is the normal case, not an error.
    """
    acquirer = payment.get("acquirer_data") or {}
    for key in ("npci_response_code", "response_code", "bank_response_code"):
        value = acquirer.get(key) or payment.get(key)
        if value:
            return str(value).strip().upper()
    return None


def parse_event(raw: dict[str, Any]) -> ParsedEvent | None:
    """Turn a verified webhook payload into a domain event.

    Returns None for events we subscribe to but do not act on (successful
    charges, lifecycle noise). The caller still logs those; only the events
    that change a scheduling decision are modelled.
    """
    event = raw.get("event", "")

    if event == "payment.failed":
        payment = _entity(raw, "payment")
        # `error_reason` is the granular code the taxonomy maps ("insufficient_funds").
        # `error_code` is the coarse envelope ("BAD_REQUEST_ERROR") and is far
        # too broad to schedule against, so it is recorded but not classified on.
        reason = payment.get("error_reason")
        npci = _npci_code(payment)
        return DeclineObserved(
            payment_id=payment.get("id", ""),
            subscription_id=payment.get("subscription_id")
            or (payment.get("notes") or {}).get("subscription_id"),
            order_id=payment.get("order_id"),
            amount=_paise_to_rupees(payment.get("amount")),
            method=payment.get("method"),
            at=_timestamp(raw),
            raw_code=payment.get("error_code"),
            raw_reason=reason,
            raw_description=payment.get("error_description"),
            npci_code=npci,
            classification=classify(razorpay_code=reason, npci_code=npci),
        )

    if event.startswith("payment.downtime."):
        downtime = _entity(raw, "payment.downtime")
        return DowntimeObserved(
            downtime_id=downtime.get("id", ""),
            method=downtime.get("method"),
            issuer=(downtime.get("instrument") or {}).get("issuer"),
            status=downtime.get("status", ""),
            severity=downtime.get("severity"),
            at=_timestamp(raw),
            resolved=event.endswith(".resolved"),
        )

    if event.startswith("subscription."):
        sub = _entity(raw, "subscription")
        return SubscriptionStateChanged(
            subscription_id=sub.get("id", ""),
            state=sub.get("status") or event.removeprefix("subscription."),
            at=_timestamp(raw),
            paid_count=sub.get("paid_count"),
            remaining_count=sub.get("remaining_count"),
        )

    return None
