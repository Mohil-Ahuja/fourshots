"""The demo console, which must be a window onto the service and not a mock.

Two things are worth proving here and the rest is detail. First, that the
console cannot reach a decision except through the real webhook endpoint --
signed, verified, logged -- because a demo that took a shortcut would be
showing a code path nobody in production uses. Second, that it refuses to sign
anything outside the closed set of demo scenarios, so it is not a signing
oracle bolted onto a service that receives real webhooks.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from fourshots import console
from fourshots.webhook import parse_event, verify_signature

SECRET = "console_test_secret"


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv("CONSOLE_ENABLED", "true")
    return True


@pytest.fixture()
def client(tmp_path, enabled):
    """The app with an isolated log, the console on, and nothing outward.

    No Razorpay client is injected, so an escalation is drafted and recorded
    but no payment link is raised -- which keeps this suite safe on a machine
    that has test keys configured.
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


def drive(http, mandate_id: str, scenario: str, amount: str = "2499"):
    """Do exactly what the page does: ask for a signature, then post the bytes."""
    signed = http.post(
        "/console/sign",
        json={"mandate_id": mandate_id, "scenario": scenario, "amount": amount},
    )
    if signed.status_code != 200:
        return signed, None
    envelope = signed.json()
    posted = http.post(
        "/webhooks/razorpay",
        content=envelope["body"].encode("utf-8"),
        headers={
            envelope["header"]: envelope["signature"],
            "Content-Type": "application/json",
        },
    )
    return signed, posted


# --- the payload is a real one ---------------------------------------------


def test_every_scenario_classifies_to_something_the_taxonomy_knows():
    """A scenario that landed on `unclassified` by accident would teach nothing.

    `ambiguous` is deliberately exempt: being unmappable is the whole point of
    that one.
    """
    for scenario in console.SCENARIOS:
        payload = console.build_payload(
            mandate_id=console.new_mandate_id(),
            scenario_key=scenario.key,
            amount="2499",
        )
        event = parse_event(payload)
        assert event is not None
        name = event.classification.failure_class.name
        if scenario.key == "ambiguous":
            assert name == "unclassified"
        else:
            assert name != "unclassified", scenario.key


def test_the_rails_own_code_overrides_the_ambiguous_gateway_reason():
    """The two `payment_declined` scenarios differ only by the NPCI code.

    They must therefore be read differently, or carrying the code is pointless.
    """
    both = {}
    for key in ("ambiguous", "limit_breach"):
        payload = console.build_payload(
            mandate_id=console.new_mandate_id(), scenario_key=key, amount="2499"
        )
        event = parse_event(payload)
        both[key] = event

    assert both["ambiguous"].raw_reason == both["limit_breach"].raw_reason
    assert both["ambiguous"].npci_code is None
    assert both["limit_breach"].npci_code == "Z8"
    assert both["ambiguous"].classification.failure_class.name == "unclassified"
    assert both["limit_breach"].classification.failure_class.name == "limit_breach"
    assert both["limit_breach"].classification.is_terminal


def test_scenario_keys_are_unique():
    keys = [scenario.key for scenario in console.SCENARIOS]
    assert len(keys) == len(set(keys))


def test_amount_survives_the_round_trip_through_paise():
    payload = console.build_payload(
        mandate_id=console.new_mandate_id(),
        scenario_key="insufficient_balance",
        amount="2499.50",
    )
    assert payload["payload"]["payment"]["entity"]["amount"] == 249950
    assert parse_event(payload).amount == Decimal("2499.50")


# --- what it refuses to sign -----------------------------------------------


@pytest.mark.parametrize(
    "mandate_id",
    [
        "sub_MNq8vLPk2xYzAb",  # a plausible real subscription id
        "sub_demo_",  # the prefix alone
        "sub_demo_ab",  # too short
        "sub_demo_" + "a" * 25,  # too long
        "sub_demo_has-a-dash",
        "",
    ],
)
def test_only_demo_mandates_may_be_signed(mandate_id):
    """The demo must not be able to spend a real mandate's attempt budget."""
    with pytest.raises(console.ScenarioRejected):
        console.build_payload(
            mandate_id=mandate_id, scenario_key="insufficient_balance", amount="1"
        )


def test_an_unknown_scenario_is_refused():
    with pytest.raises(console.ScenarioRejected):
        console.build_payload(
            mandate_id=console.new_mandate_id(), scenario_key="whatever", amount="1"
        )


@pytest.mark.parametrize("amount", ["0", "-5", "500000.01", "1.005", "abc", ""])
def test_implausible_amounts_are_refused(amount):
    with pytest.raises(console.ScenarioRejected):
        console.build_payload(
            mandate_id=console.new_mandate_id(),
            scenario_key="insufficient_balance",
            amount=amount,
        )


def test_the_ceiling_itself_is_allowed():
    """The cap is inclusive, so the boundary is exercisable rather than a cliff."""
    payload = console.build_payload(
        mandate_id=console.new_mandate_id(),
        scenario_key="insufficient_balance",
        amount=console.MAX_DEMO_AMOUNT,
    )
    assert payload["payload"]["payment"]["entity"]["amount"] == 50000000


def test_signing_needs_a_secret():
    with pytest.raises(console.ScenarioRejected):
        console.sign(b"{}", "")


# --- the bytes ---------------------------------------------------------------


def test_the_signature_verifies_against_the_bytes_that_are_returned():
    """The page posts this string verbatim; a re-encode would break it.

    Serialising once and signing that exact buffer is the reason the sign
    endpoint returns a string rather than an object.
    """
    payload = console.build_payload(
        mandate_id=console.new_mandate_id(),
        scenario_key="insufficient_balance",
        amount="2499",
    )
    body = console.canonical_body(payload)
    verify_signature(body, console.sign(body, SECRET), SECRET)


def test_a_re_encoded_body_would_not_verify():
    """Pins the reason the raw string is carried around instead of a dict."""
    payload = console.build_payload(
        mandate_id=console.new_mandate_id(),
        scenario_key="insufficient_balance",
        amount="2499",
    )
    body = console.canonical_body(payload)
    signature = console.sign(body, SECRET)
    re_encoded = json.dumps(json.loads(body)).encode("utf-8")
    assert re_encoded != body
    from fourshots.webhook import SignatureInvalid

    with pytest.raises(SignatureInvalid):
        verify_signature(re_encoded, signature, SECRET)


def test_canonical_body_is_stable_for_the_same_payload():
    payload = console.build_payload(
        mandate_id="sub_demo_abcd1234", scenario_key="mandate_dead", amount="99"
    )
    assert console.canonical_body(payload) == console.canonical_body(payload)


def test_a_fresh_mandate_is_in_the_demo_namespace_and_unique():
    ids = {console.new_mandate_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(one.startswith(console.DEMO_MANDATE_PREFIX) for one in ids)


# --- the switch --------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_console_is_on_for_the_affirmative_spellings(monkeypatch, value):
    monkeypatch.setenv("CONSOLE_ENABLED", value)
    assert console.console_enabled()


@pytest.mark.parametrize("value", ["false", "0", "no", "", "maybe"])
def test_console_is_off_for_everything_else(monkeypatch, value):
    monkeypatch.setenv("CONSOLE_ENABLED", value)
    assert not console.console_enabled()


def test_console_is_off_when_unset(monkeypatch):
    monkeypatch.delenv("CONSOLE_ENABLED", raising=False)
    assert not console.console_enabled()


def test_a_disabled_console_signs_nothing(tmp_path, monkeypatch):
    """The routes exist but refuse, rather than the app changing shape by env."""
    monkeypatch.setenv("CONSOLE_ENABLED", "false")
    from fastapi.testclient import TestClient

    from fourshots.app import create_app
    from fourshots.audit import AuditLog
    from fourshots.recovery import RecoveryService

    audit = AuditLog(tmp_path / "audit.jsonl")
    http = TestClient(
        create_app(audit=audit, webhook_secret=SECRET, recovery=RecoveryService(audit))
    )

    told = http.get("/console/status").json()
    assert told["enabled"] is False
    assert "CONSOLE_ENABLED" in told["reason"]
    assert "scenarios" not in told

    signed = http.post(
        "/console/sign",
        json={
            "mandate_id": "sub_demo_abcd1234",
            "scenario": "insufficient_balance",
            "amount": "1",
        },
    )
    assert signed.status_code == 403
    assert http.post("/console/new-mandate").status_code == 403
    assert list(audit.read()) == []


# --- through the real endpoint ------------------------------------------------


def test_the_console_drives_the_actual_webhook_route(client):
    """The decision comes back from `/webhooks/razorpay`, not from the console."""
    http, audit = client
    mandate = http.post("/console/new-mandate").json()["mandate_id"]

    _, posted = drive(http, mandate, "insufficient_balance")
    assert posted.status_code == 200
    body = posted.json()
    assert body["accepted"] is True
    assert body["failure_class"] == "insufficient_balance"
    assert body["decision"]["action"] == "retry_booked"

    kinds = [entry.kind for entry in audit.read()]
    assert kinds == ["decline_observed", "attempt_booked"]
    assert audit.verify() == 2


def test_a_booked_retry_never_claims_the_rail_was_touched(client):
    http, audit = client
    mandate = http.post("/console/new-mandate").json()["mandate_id"]
    drive(http, mandate, "insufficient_balance")

    booked = [entry for entry in audit.read() if entry.kind == "attempt_booked"]
    assert booked and booked[0].data["executed_against_rail"] is False


def test_a_terminal_decline_escalates_without_spending_an_attempt(client):
    http, _ = client
    mandate = http.post("/console/new-mandate").json()["mandate_id"]

    _, posted = drive(http, mandate, "mandate_dead")
    decision = posted.json()["decision"]
    assert decision["action"] == "escalated"
    assert decision["message"]
    # No payment link exists without credentials, and the log says so rather
    # than reporting an escalation that did not go out as though it had.
    assert decision["executed"] is False


def test_the_budget_runs_out_after_four_and_stays_out(client):
    """The cap survives because it is rebuilt from the log, not held in memory."""
    http, _ = client
    mandate = http.post("/console/new-mandate").json()["mandate_id"]

    actions = []
    for _ in range(6):
        _, posted = drive(http, mandate, "insufficient_balance")
        actions.append(posted.json()["decision"]["action"])

    assert actions[0] == "retry_booked"
    assert actions[-1] != "retry_booked"
    assert actions.count("retry_booked") <= 3


def test_a_new_mandate_starts_with_a_full_budget_and_deletes_nothing(client):
    http, audit = client
    first = http.post("/console/new-mandate").json()["mandate_id"]
    drive(http, first, "insufficient_balance")
    before = len(list(audit.read()))

    second = http.post("/console/new-mandate").json()["mandate_id"]
    assert second != first
    _, posted = drive(http, second, "insufficient_balance")

    assert posted.json()["decision"]["attempts_used"] == 1
    assert len(list(audit.read())) > before
    assert audit.verify() == len(list(audit.read()))


def test_status_reports_the_layers_honestly(client):
    """A run with no keys must say so on every field that has one."""
    http, _ = client
    status = http.get("/console/status").json()
    assert status["enabled"] is True
    assert status["service"]["executes_outward"] is False
    assert status["service"]["drafter"] == "template"
    assert status["service"]["max_attempts_per_cycle"] == 4
    assert len(status["scenarios"]) == len(console.SCENARIOS)


def test_the_sign_endpoint_reports_why_it_refused(client):
    http, audit = client
    refused = http.post(
        "/console/sign",
        json={
            "mandate_id": "sub_MNq8vLPk2xYz",
            "scenario": "insufficient_balance",
            "amount": "1",
        },
    )
    assert refused.status_code == 400
    assert "sub_demo_" in refused.json()["error"]
    assert list(audit.read()) == []


def test_the_page_is_served_and_is_self_contained(client):
    """No CDN, no font host: the demo must work on a laptop with no network."""
    http, _ = client
    page = http.get("/console")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "//fonts." not in page.text
    assert "cdn." not in page.text
    assert "<script src=" not in page.text
