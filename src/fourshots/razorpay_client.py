"""A very small Razorpay REST client, restricted to test mode.

Only what the recovery loop actually needs. Two deliberate constraints shape
this file:

**Test mode is enforced in code, not in a README warning.** The constructor
refuses any key id that does not begin with ``rzp_test_``. This project is a
benchmark submission; there is no circumstance in which it should be able to
reach a live account, and the cheapest way to guarantee that is to make the
live path unrepresentable rather than merely discouraged.

**No new dependency.** ``urllib`` is enough for two POSTs, and this file
handles credentials -- the same reasoning as :mod:`fourshots.config`, where a
ten-line parser was preferred to a library for auditability.

What it can do is narrow on purpose. Razorpay's API does not expose "retry this
mandate on this date" -- the subscription schedule belongs to the gateway, not
to the merchant. So the executable recovery action here is the one that
genuinely exists: raise a Payment Link for a debit that cannot clear silently,
and hand the customer a way to pay. Booked retries are recorded in the schedule
and never described as fired against the rail, because they are not.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from fourshots.config import load_env, optional

API_BASE = "https://api.razorpay.com/v1"
TEST_KEY_PREFIX = "rzp_test_"


class CredentialsMissing(RuntimeError):
    """No test keys configured. Raised instead of degrading to a no-op.

    The recovery service catches this and records that the action was decided
    but not executed. What is not acceptable is silently reporting success.
    """


class LiveModeRefused(RuntimeError):
    """A non-test key was supplied. Always fatal."""


class RazorpayError(RuntimeError):
    """The API rejected the request. Carries the status and the body."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"razorpay returned {status}: {body[:400]}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class PaymentLink:
    """A created payment link, as the audit log records it."""

    id: str
    short_url: str
    amount: Decimal
    reference_id: str | None
    created_at: datetime | None


REFERENCE_ID_MAX = 40
"""Razorpay's documented ceiling on `reference_id`.

Enforced here rather than discovered from a 400. An escalation that fails
because an identifier was two characters too long is a wasted recovery, and it
fails at the least convenient moment: after the decision, in production, on the
mandates that most need reaching.
"""


def idempotency_reference(*parts: str) -> str:
    """A stable reference for one logical event, inside the length limit.

    Digested rather than concatenated because the obvious construction --
    mandate id and payment id joined -- lands at 37 characters for real
    Razorpay ids, three below the ceiling, and silently exceeds it for anything
    longer. An identifier whose validity depends on both ids staying exactly
    their current width is not an identifier, it is a latent outage.

    The digest is deterministic, so the idempotency property is unchanged: the
    same mandate and the same failed payment produce the same reference, and
    Razorpay rejects the second link rather than asking a customer to pay
    twice. The readable ids travel in `notes` and in the description.
    """
    joined = ":".join(parts)
    return "fs_" + sha256(joined.encode("utf-8")).hexdigest()[:32]


def _to_paise(amount: Decimal) -> int:
    """Rupees to paise, exactly. Razorpay rejects fractional paise."""
    scaled = amount * 100
    paise = scaled.to_integral_value()
    if paise != scaled:
        raise ValueError(f"{amount} is not a whole number of paise")
    return int(paise)


class RazorpayClient:
    """Test-mode REST client.

    `transport` is injectable so the tests exercise the request this class
    actually builds -- headers, encoding, paise conversion -- without a network
    or a live account.
    """

    def __init__(
        self,
        key_id: str | None = None,
        key_secret: str | None = None,
        *,
        base_url: str = API_BASE,
        timeout: float = 10.0,
        transport=None,
    ) -> None:
        if key_id is None or key_secret is None:
            load_env()
            key_id = key_id if key_id is not None else optional("RAZORPAY_KEY_ID")
            key_secret = (
                key_secret
                if key_secret is not None
                else optional("RAZORPAY_KEY_SECRET")
            )

        if not key_id or not key_secret:
            raise CredentialsMissing(
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set. "
                "Copy .env.example to .env and fill in test-mode keys."
            )
        if not key_id.startswith(TEST_KEY_PREFIX):
            raise LiveModeRefused(
                f"key id does not start with {TEST_KEY_PREFIX!r}. This project "
                "runs against test mode only and will not open a live session."
            )

        self.key_id = key_id
        self._secret = key_secret
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or self._urlopen

    # --- transport ---------------------------------------------------------

    def _urlopen(
        self, method: str, url: str, body: bytes, headers: dict[str, str]
    ) -> tuple[int, str]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as failure:
            return failure.code, failure.read().decode("utf-8", "replace")

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = b64encode(f"{self.key_id}:{self._secret}".encode("utf-8")).decode(
            "ascii"
        )
        status, body = self._transport(
            "POST",
            f"{self._base}{path}",
            json.dumps(payload).encode("utf-8"),
            {
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
            },
        )
        if status >= 400:
            raise RazorpayError(status, body)
        return json.loads(body)

    # --- operations --------------------------------------------------------

    def create_payment_link(
        self,
        *,
        amount: Decimal,
        description: str,
        reference_id: str | None = None,
        notes: dict[str, str] | None = None,
        expire_by: datetime | None = None,
    ) -> PaymentLink:
        """Raise a payment link for a debit that cannot clear silently.

        `reference_id` is set to the mandate id and the failed cycle, which
        makes the call idempotent at Razorpay's end: a second link for the same
        failed cycle is rejected rather than asking the customer to pay twice.
        That is a property worth having in a system whose entire subject is not
        spending an attempt twice.

        Customer contact details are deliberately not sent. The link is created
        unnotified and the outreach copy is delivered by whatever channel the
        merchant already owns, so no real personal data passes through this
        project.
        """
        payload: dict[str, Any] = {
            "amount": _to_paise(amount),
            "currency": "INR",
            "description": description[:2048],
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
        }
        if reference_id:
            if len(reference_id) > REFERENCE_ID_MAX:
                raise ValueError(
                    f"reference_id is {len(reference_id)} characters; Razorpay "
                    f"allows {REFERENCE_ID_MAX}. Use idempotency_reference()."
                )
            payload["reference_id"] = reference_id
        if notes:
            payload["notes"] = notes
        if expire_by:
            payload["expire_by"] = int(expire_by.timestamp())

        created = self._post("/payment_links", payload)
        raw_created_at = created.get("created_at")
        return PaymentLink(
            id=str(created.get("id", "")),
            short_url=str(created.get("short_url", "")),
            amount=Decimal(str(created.get("amount", 0))) / Decimal(100),
            reference_id=created.get("reference_id"),
            created_at=(
                datetime.fromtimestamp(raw_created_at)
                if isinstance(raw_created_at, (int, float))
                else None
            ),
        )


def client_or_none() -> RazorpayClient | None:
    """The configured client, or None when no test keys are present.

    Lets the service run end to end with no account at all -- decisions are
    still made, still gated, still logged; only the outward call is absent, and
    the log says so. A live-mode key is not swallowed here: that raises.
    """
    try:
        return RazorpayClient()
    except CredentialsMissing:
        return None
