"""Tests for the Razorpay client.

No network is touched: the transport is injected, so what is actually being
checked is the request this class builds -- the mode guard, the auth header,
the paise conversion and the idempotency reference.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from fourshots.razorpay_client import (
    CredentialsMissing,
    LiveModeRefused,
    RazorpayClient,
    RazorpayError,
    _to_paise,
)

TEST_ID = "rzp_test_abcdef123456"
TEST_SECRET = "secret"


class RecordingTransport:
    """Captures the request and returns a canned Razorpay response."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {
            "id": "plink_Test01",
            "short_url": "https://rzp.io/i/abcd",
            "amount": 249900,
            "reference_id": "sub_Test01:pay_Test01",
            "created_at": 1788000000,
        }
        self.calls: list[tuple[str, str, dict, dict]] = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, json.loads(body), headers))
        return self.status, json.dumps(self.body)


def make_client(transport=None) -> tuple[RazorpayClient, RecordingTransport]:
    transport = transport or RecordingTransport()
    return (
        RazorpayClient(TEST_ID, TEST_SECRET, transport=transport),
        transport,
    )


# --- The mode guard --------------------------------------------------------


def test_a_live_key_is_refused_in_code_not_in_a_readme_warning() -> None:
    """This project has no reason to reach a live account, so the live path is
    made unrepresentable rather than merely discouraged."""
    with pytest.raises(LiveModeRefused):
        RazorpayClient("rzp_live_abcdef123456", TEST_SECRET)


def test_missing_credentials_raise_rather_than_degrading_to_a_no_op() -> None:
    """A client that silently does nothing would report escalations as sent."""
    with pytest.raises(CredentialsMissing):
        RazorpayClient("", "")


def test_client_or_none_returns_none_without_keys(monkeypatch) -> None:
    monkeypatch.setenv("RAZORPAY_KEY_ID", "")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
    from fourshots.razorpay_client import client_or_none

    assert client_or_none() is None


def test_client_or_none_does_not_swallow_a_live_key(monkeypatch) -> None:
    """Absent credentials are a supported mode. A live key is a mistake, and
    must not be quietly downgraded into that mode."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_abcdef123456")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    from fourshots.razorpay_client import client_or_none

    with pytest.raises(LiveModeRefused):
        client_or_none()


# --- Money -----------------------------------------------------------------


def test_rupees_convert_to_paise_exactly() -> None:
    assert _to_paise(Decimal("2499.00")) == 249900
    assert _to_paise(Decimal("499.99")) == 49999


def test_a_fractional_paisa_is_refused_rather_than_rounded() -> None:
    """Rounding here would silently charge a customer a different amount from
    the one the message told them about."""
    with pytest.raises(ValueError):
        _to_paise(Decimal("10.005"))


# --- The request -----------------------------------------------------------


def test_the_payment_link_request_is_shaped_as_razorpay_expects() -> None:
    client, transport = make_client()
    client.create_payment_link(
        amount=Decimal("2499.00"),
        description="Recover failed auto-pay debit",
        reference_id="sub_Test01:pay_Test01",
        notes={"mandate_id": "sub_Test01"},
        expire_by=datetime(2026, 10, 3, tzinfo=timezone.utc),
    )
    method, url, body, headers = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/payment_links")
    assert body["amount"] == 249900
    assert body["currency"] == "INR"
    assert headers["Authorization"].startswith("Basic ")
    assert body["expire_by"] == int(
        datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp()
    )


def test_the_link_is_created_unnotified_so_no_personal_data_is_sent() -> None:
    """The copy goes out over whatever channel the merchant already owns. This
    project never handles a real customer's phone number or email."""
    client, transport = make_client()
    client.create_payment_link(amount=Decimal("100"), description="d")
    body = transport.calls[0][2]
    assert body["notify"] == {"sms": False, "email": False}
    assert body["reminder_enable"] is False
    assert "customer" not in body


def test_the_reference_id_makes_a_redelivered_webhook_harmless() -> None:
    """Razorpay rejects a duplicate reference_id, so a webhook delivered twice
    cannot raise a second link and ask the customer to pay twice."""
    client, transport = make_client()
    client.create_payment_link(
        amount=Decimal("100"), description="d", reference_id="sub_A:pay_B"
    )
    assert transport.calls[0][2]["reference_id"] == "sub_A:pay_B"


def test_the_response_is_parsed_back_into_rupees() -> None:
    client, _ = make_client()
    link = client.create_payment_link(amount=Decimal("2499.00"), description="d")
    assert link.id == "plink_Test01"
    assert link.short_url == "https://rzp.io/i/abcd"
    assert link.amount == Decimal("2499.00")


def test_an_api_rejection_raises_with_the_status_and_body() -> None:
    client, _ = make_client(
        RecordingTransport(status=400, body={"error": {"description": "bad amount"}})
    )
    with pytest.raises(RazorpayError) as raised:
        client.create_payment_link(amount=Decimal("100"), description="d")
    assert raised.value.status == 400
    assert "bad amount" in raised.value.body


# --- The default transport -------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def test_the_default_transport_sends_the_request_it_was_given(monkeypatch) -> None:
    """The injected transport is what the other tests exercise, so the real one
    is worth covering once: it is the code that actually reaches Razorpay."""
    import urllib.request

    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.method
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(200, json.dumps({"id": "plink_X", "amount": 10000}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = RazorpayClient(TEST_ID, TEST_SECRET)
    link = client.create_payment_link(amount=Decimal("100.00"), description="d")

    assert seen["method"] == "POST"
    assert seen["url"].endswith("/payment_links")
    assert seen["auth"].startswith("Basic ")
    assert link.id == "plink_X"


def test_an_http_error_is_turned_into_a_razorpay_error(monkeypatch) -> None:
    """`urlopen` raises on 4xx. Letting that escape as an HTTPError would make
    an ordinary API rejection look like a transport failure."""
    import io
    import urllib.error
    import urllib.request

    def failing_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":{"description":"amount too small"}}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    client = RazorpayClient(TEST_ID, TEST_SECRET)
    with pytest.raises(RazorpayError) as raised:
        client.create_payment_link(amount=Decimal("0.10"), description="d")
    assert raised.value.status == 400
    assert "amount too small" in raised.value.body


# --- reference ids ----------------------------------------------------------


def test_a_reference_never_exceeds_the_api_limit():
    """Found by driving the console against the live test-mode API.

    The obvious construction -- mandate id and payment id joined with a colon --
    is 37 characters for real Razorpay ids and over the limit for anything
    longer, so every escalation failed with a 400 after the decision had already
    been made.
    """
    from fourshots.razorpay_client import REFERENCE_ID_MAX, idempotency_reference

    for parts in [
        ("sub_MNq8vLPk2xYzAb", "pay_MNq8vLPk2xYzAb"),
        ("sub_demo_fa9f574b940e", "pay_demo_9c1e77a04b62"),
        ("s", "p"),
        ("x" * 200, "y" * 200),
    ]:
        assert len(idempotency_reference(*parts)) <= REFERENCE_ID_MAX


def test_a_reference_is_stable_for_the_same_event():
    """Idempotency is the whole point: the same failed cycle, the same id."""
    from fourshots.razorpay_client import idempotency_reference

    first = idempotency_reference("sub_abc", "pay_xyz")
    assert first == idempotency_reference("sub_abc", "pay_xyz")
    assert first != idempotency_reference("sub_abc", "pay_other")
    assert first != idempotency_reference("sub_other", "pay_xyz")


def test_an_over_long_reference_fails_locally_rather_than_at_the_api():
    """The limit is Razorpay's, so this client is where it should be caught."""
    from fourshots.razorpay_client import RazorpayClient

    client = RazorpayClient("rzp_test_abc", "secret")
    with pytest.raises(ValueError, match="reference_id"):
        client.create_payment_link(
            amount=Decimal("100"),
            description="test",
            reference_id="x" * 41,
        )
