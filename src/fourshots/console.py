"""The demo console's server side: build a webhook, sign it, hand it back.

The console exists to make one claim visible: a decline arrives, a decision
comes out, and the decision is written into a log that verifies. Nothing here
decides anything. It constructs a ``payment.failed`` payload in Razorpay's own
shape, signs it, and returns the bytes -- the browser then posts them to
``/webhooks/razorpay`` like any other caller.

That indirection is the point rather than an inconvenience. The page never sees
the webhook secret, and the request it makes travels the *real* endpoint:
signature verification, parsing, classification, the budget rebuilt from the
log, the legality gate. A console that called the recovery service directly
would demonstrate a code path nobody in production uses.

Two things bound what this can be abused for. It only ever signs a payload it
built itself from a closed set of scenarios -- it is not a signing oracle for
arbitrary bytes -- and it refuses any mandate id outside the ``sub_demo_``
namespace, so a demo can never write against a real subscription's history.
It is also off unless ``CONSOLE_ENABLED`` says otherwise.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from fourshots.config import optional

DEMO_MANDATE_PREFIX = "sub_demo_"
"""Namespace every console-generated mandate lives in.

A real subscription id could not be produced by this endpoint even by mistake,
so the demo cannot append a fabricated decline to a genuine mandate's history
and quietly consume one of its four attempts.
"""

_MANDATE_PATTERN = re.compile(
    r"^" + re.escape(DEMO_MANDATE_PREFIX) + r"[A-Za-z0-9]{4,24}$"
)

MAX_DEMO_AMOUNT = Decimal("500000")
"""Ceiling on a demo amount, in rupees.

Well above the elevated AFA threshold, so every band the legality gate
distinguishes is reachable, and far below anything that would look like a
plausible real debit in a log someone reads later.
"""


class ConsoleDisabled(RuntimeError):
    """The console is not switched on in this environment."""


class ScenarioRejected(ValueError):
    """The requested demo payload is outside what this endpoint will sign."""


@dataclass(frozen=True)
class Scenario:
    """One decline the console can produce, and what it is there to show.

    `reason` is a real Razorpay `error_reason`; `description` is the prose the
    rail attaches to it. Both are passed through untouched, so the taxonomy
    reads exactly what it would read from a live webhook.

    `key` identifies the scenario rather than the code, because one gateway
    reason can arrive in more than one shape -- the same ambiguous
    `payment_declined` means something quite different when the rail's own
    response code comes with it.
    """

    key: str
    reason: str
    label: str
    description: str
    npci_code: str | None
    shows: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="insufficient_balance",
        reason="insufficient_funds",
        label="Insufficient balance",
        description=(
            "Your account does not have enough balance to complete this "
            "transaction."
        ),
        npci_code=None,
        shows=(
            "The headline case. Retryable, but not tomorrow -- the floor is 24 "
            "hours and the attempts spread across the cycle instead of "
            "stacking onto consecutive days."
        ),
    ),
    Scenario(
        key="issuer_downtime",
        reason="bank_technical_error",
        label="Issuer downtime",
        description=(
            "The customer's bank is facing a technical issue. Please retry "
            "after some time."
        ),
        npci_code=None,
        shows=(
            "Also retryable, but on the opposite timescale. Waiting a full day "
            "here is too slow, not too fast."
        ),
    ),
    Scenario(
        key="customer_absent",
        reason="payment_collect_request_expired",
        label="Customer did not approve",
        description=(
            "The customer did not approve the collect request within the "
            "allowed time."
        ),
        npci_code=None,
        shows=(
            "Nothing clears silently until a person acts, so this escalates "
            "rather than spending an attempt on an unattended phone."
        ),
    ),
    Scenario(
        key="mandate_dead",
        reason="invalid_vpa",
        label="Mandate no longer valid",
        description="The customer is not a valid user on the UPI application.",
        npci_code=None,
        shows=(
            "Terminal. No amount of waiting fixes it, so it must never consume "
            "an attempt -- this is where the documented default spends three "
            "retries discovering the mandate is dead."
        ),
    ),
    Scenario(
        key="ambiguous",
        reason="payment_declined",
        label="Ambiguous decline",
        description="The funds could not be debited from the customer's account.",
        npci_code=None,
        shows=(
            "The taxonomy refuses to map this: the wording spans a balance "
            "shortfall and an issuer risk decline, and guessing wrong costs an "
            "attempt. This is the input the model-backed triage layer exists "
            "for, and the conservative floor stands when it has no reading."
        ),
    ),
    Scenario(
        key="limit_breach",
        reason="payment_declined",
        label="Limit breached (direct PSP feed)",
        description="The funds could not be debited from the customer's account.",
        npci_code="Z8",
        shows=(
            "The same ambiguous gateway wording, but this merchant has direct "
            "PSP connectivity and receives the rail's own code alongside it. "
            "Z8 says a per-transaction limit was breached, which retrying the "
            "same amount cannot fix -- so the more specific code wins and the "
            "decline is read as terminal rather than left unmapped."
        ),
    ),
    Scenario(
        key="instrument_rejected",
        reason="international_transaction_not_allowed",
        label="Instrument rejected",
        description="International cards are not supported for this merchant.",
        npci_code=None,
        shows=(
            "The same instrument can never clear here, so no retry is proposed."
        ),
    ),
)

_BY_KEY = {scenario.key: scenario for scenario in SCENARIOS}


def console_enabled() -> bool:
    """Whether the demo console may build and sign payloads.

    Off by default. This endpoint can write entries into the decision log, and
    a service that receives real webhooks should not also accept synthetic ones
    because nobody thought about it.
    """
    return optional("CONSOLE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def new_mandate_id() -> str:
    """A fresh demo mandate, so a run starts with its full four-attempt budget.

    The console has no "clear the log" button and should not have one -- the
    log is append-only and hash-chained, and a demo that deleted history would
    be demonstrating the opposite of the claim. Starting a new mandate gives a
    clean budget while leaving every previous entry in place and verifiable.
    """
    return DEMO_MANDATE_PREFIX + secrets.token_hex(6)


def _amount_paise(amount: Any) -> int:
    """Rupees in, paise out, exactly. Rejects anything finer than a paise."""
    try:
        rupees = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ScenarioRejected(repr(amount) + " is not an amount") from None
    if not rupees.is_finite() or rupees <= 0:
        raise ScenarioRejected("amount must be positive")
    if rupees > MAX_DEMO_AMOUNT:
        raise ScenarioRejected("demo amounts are capped at Rs 5,00,000")
    scaled = rupees * 100
    paise = scaled.to_integral_value()
    if paise != scaled:
        raise ScenarioRejected(str(rupees) + " is not a whole number of paise")
    return int(paise)


def build_payload(
    *,
    mandate_id: str,
    scenario_key: str,
    amount: Any,
    at: datetime | None = None,
) -> dict[str, Any]:
    """A ``payment.failed`` event in Razorpay's shape, from a demo scenario.

    Every field the parser reads is populated the way the live gateway
    populates it -- `error_reason` granular and `error_code` coarse, the amount
    in paise, the entity nested under ``payload.payment.entity`` -- because the
    console is worth nothing if it exercises a payload shape the real endpoint
    would never receive.
    """
    if not _MANDATE_PATTERN.match(mandate_id):
        raise ScenarioRejected(
            "mandate id must match " + DEMO_MANDATE_PREFIX + "<4-24 alphanumerics>"
        )
    scenario = _BY_KEY.get(scenario_key)
    if scenario is None:
        raise ScenarioRejected(
            repr(scenario_key) + " is not one of the demo scenarios"
        )

    moment = at or datetime.now(timezone.utc)
    payment: dict[str, Any] = {
        "id": "pay_demo_" + secrets.token_hex(6),
        "entity": "payment",
        "subscription_id": mandate_id,
        "amount": _amount_paise(amount),
        "currency": "INR",
        "status": "failed",
        "method": "upi",
        "error_code": "BAD_REQUEST_ERROR",
        "error_reason": scenario.reason,
        "error_description": scenario.description,
        "error_source": "customer",
        "error_step": "payment_authentication",
    }
    if scenario.npci_code:
        # Razorpay surfaces the rail's own response code here when it has one,
        # and the taxonomy prefers it: an NPCI code is strictly more specific
        # than the gateway's own reason string.
        payment["acquirer_data"] = {
            "rrn": secrets.token_hex(6),
            "npci_response_code": scenario.npci_code,
        }

    return {
        "entity": "event",
        "account_id": "acc_demo",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": payment}},
        "created_at": int(moment.timestamp()),
    }


def canonical_body(payload: dict[str, Any]) -> bytes:
    """Serialise once, so the bytes that are signed are the bytes that are sent.

    The browser posts this string verbatim rather than re-encoding a parsed
    object: a signature commits to bytes, and a round trip through any JSON
    library is free to reorder keys or change spacing and break it.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign(body: bytes, secret: str) -> str:
    """The same HMAC-SHA256 the gateway computes, over the same bytes."""
    if not secret:
        raise ScenarioRejected("no webhook secret configured")
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def scenario_catalogue() -> list[dict[str, Any]]:
    """The picker's options, for the page to render."""
    return [
        {
            "key": scenario.key,
            "reason": scenario.reason,
            "label": scenario.label,
            "description": scenario.description,
            "npci_code": scenario.npci_code,
            "shows": scenario.shows,
        }
        for scenario in SCENARIOS
    ]
