"""Tests for the escalation copy.

The thing under test is not "does a model write nice prose". It is the
boundary: a model may choose the words, and may not choose a number, a
recipient or a moment. Most of these tests are about what a draft is *not*
allowed to contain, because that is where the customer-visible harm lives.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from fourshots.outreach import (
    ASKS,
    LANGUAGES,
    MAX_BODY_CHARS,
    ClaudeDrafter,
    Draft,
    OutreachFacts,
    TemplateDrafter,
    compose,
    render,
    validate_template,
)
from fourshots.taxonomy import Blocker

FACTS = OutreachFacts(
    merchant_name="Example Merchant",
    amount=Decimal("2499.00"),
    pay_link="https://rzp.io/i/example",
    deadline=datetime(2026, 9, 30),
)


class FixedDrafter:
    """A drafter that returns exactly what a test hands it."""

    name = "fixed"
    model = "test-model"

    def __init__(self, text: str | None) -> None:
        self._text = text

    def draft(self, blocker: Blocker, language: str) -> str | None:
        return self._text


# --- Validation ------------------------------------------------------------


def test_a_draft_containing_a_digit_is_rejected() -> None:
    """The model is never told the amount, so a digit is an invented figure --
    and a wrong rupee amount in a customer's SMS is worse than a template."""
    assert validate_template("Pay {amount} of 3 instalments here: {link}") is None


def test_a_draft_inventing_a_placeholder_is_rejected() -> None:
    """`{account_holder}` implies data this system does not hold, and would
    render as literal braces in the message."""
    assert validate_template("Hi {account_holder}, pay {amount} at {link}") is None


def test_a_draft_without_the_amount_or_the_link_is_rejected() -> None:
    assert validate_template("Your payment failed. Please pay at {link}") is None
    assert validate_template("Your {amount} payment failed.") is None


def test_an_unbalanced_brace_is_rejected() -> None:
    assert validate_template("Pay {amount at {link}") is None


def test_an_overlong_draft_is_rejected() -> None:
    padding = "word " * MAX_BODY_CHARS
    assert validate_template(f"{padding} {{amount}} {{link}}") is None


def test_a_valid_draft_is_returned_with_whitespace_normalised() -> None:
    cleaned = validate_template("  Pay   {amount}\n  here: {link}  ")
    assert cleaned == "Pay {amount} here: {link}"


# --- Rendering -------------------------------------------------------------


def test_facts_are_substituted_by_this_code_not_the_model() -> None:
    body = render("{merchant} wants {amount} by {deadline}: {link}", FACTS)
    assert body == (
        "Example Merchant wants Rs 2,499.00 by 30 Sep 2026: https://rzp.io/i/example"
    )


def test_the_amount_keeps_its_paise() -> None:
    facts = OutreachFacts(
        merchant_name="M",
        amount=Decimal("499.99"),
        pay_link="l",
        deadline=datetime(2026, 9, 30),
    )
    assert "Rs 499.99" in render("{amount} {link}", facts)


# --- The shipped templates -------------------------------------------------


@pytest.mark.parametrize("blocker", sorted(ASKS, key=lambda b: b.value))
@pytest.mark.parametrize("language", LANGUAGES)
def test_every_escalation_has_copy_in_every_language(blocker, language) -> None:
    draft = compose(blocker, FACTS, language=language)
    assert isinstance(draft, Draft)
    assert draft.source == "template"


@pytest.mark.parametrize("blocker", sorted(ASKS, key=lambda b: b.value))
@pytest.mark.parametrize("language", LANGUAGES)
def test_the_shipped_templates_pass_their_own_validator(blocker, language) -> None:
    """The rules applied to a model's output are not a lower bar than the one
    the hand-written copy meets."""
    template = TemplateDrafter().draft(blocker, language)
    assert validate_template(template) == template


@pytest.mark.parametrize("blocker", sorted(ASKS, key=lambda b: b.value))
def test_every_message_says_no_money_was_taken(blocker) -> None:
    """A customer who has just seen a failed debit assumes the opposite, and
    reassuring them is most of the reason to write at all."""
    body = compose(blocker, FACTS).body.lower()
    assert any(
        phrase in body
        for phrase in ("nothing has been debited", "not retried", "declined")
    )


@pytest.mark.parametrize("blocker", sorted(ASKS, key=lambda b: b.value))
def test_the_hinglish_copy_reassures_too(blocker) -> None:
    body = compose(blocker, FACTS, language="hinglish").body.lower()
    assert any(phrase in body for phrase in ("cut nahi hua", "decline"))


def test_balance_failures_get_no_message() -> None:
    """A customer whose salary has simply not landed yet is retried silently.
    Messaging them is noise that costs goodwill for nothing."""
    assert compose(Blocker.CUSTOMER_BALANCE, FACTS) is None
    assert compose(Blocker.NOTHING, FACTS) is None
    assert compose(Blocker.UNKNOWN, FACTS) is None


# --- The model layer -------------------------------------------------------


def test_a_usable_model_draft_is_used_and_attributed() -> None:
    drafter = FixedDrafter("Please approve {amount} for {merchant}: {link}")
    draft = compose(Blocker.CUSTOMER_ACTION, FACTS, drafter=drafter)
    assert draft.source == "fixed"
    assert draft.model == "test-model"
    assert "Rs 2,499.00" in draft.body


def test_an_unusable_model_draft_falls_back_silently_to_the_template() -> None:
    """A rejected draft is the ordinary case, not an error: the customer still
    gets a message, and the log records which one they got."""
    draft = compose(
        Blocker.CUSTOMER_ACTION, FACTS, drafter=FixedDrafter("Pay 5000 now: {link}")
    )
    assert draft.source == "template"
    assert draft.template == TemplateDrafter().draft(Blocker.CUSTOMER_ACTION, "english")


def test_a_drafter_returning_nothing_falls_back() -> None:
    draft = compose(Blocker.MANDATE_REPAIR, FACTS, drafter=FixedDrafter(None))
    assert draft.source == "template"


def test_a_model_outage_never_changes_behaviour() -> None:
    """The one property the AI layer must have: absent, broken or rate-limited,
    the system does exactly what it does without it."""

    class Exploding:
        class messages:  # noqa: N801 - mirrors the SDK's attribute shape
            @staticmethod
            def create(**_kwargs):
                raise RuntimeError("network is down")

    draft = compose(
        Blocker.AMOUNT_CHANGE, FACTS, drafter=ClaudeDrafter(client=Exploding())
    )
    assert draft.source == "template"


def test_the_prompt_forbids_what_the_validator_rejects() -> None:
    """The two halves of the boundary should agree. If the prompt ever stops
    forbidding digits, this fails rather than quietly relying on the validator
    to catch a model that was never told."""
    from fourshots.outreach import SYSTEM_PROMPT

    assert "Never write a digit" in SYSTEM_PROMPT
    for placeholder in ("{merchant}", "{amount}", "{link}", "{deadline}"):
        assert placeholder in SYSTEM_PROMPT


class StubResponse:
    def __init__(self, text: str) -> None:
        self.content = [type("Block", (), {"type": "text", "text": text})()]


class StubAnthropic:
    """Enough of the SDK surface for the drafter to call."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict] = []
        outer = self

        class Messages:
            @staticmethod
            def create(**kwargs):
                outer.calls.append(kwargs)
                return StubResponse(outer._text)

        self.messages = Messages()


def test_the_model_is_never_told_the_amount() -> None:
    """The bound is structural, not a matter of the model behaving: it cannot
    write a correct figure because it is never given one."""
    stub = StubAnthropic("Please approve {amount} for {merchant}: {link}")
    ClaudeDrafter(client=stub).draft(Blocker.CUSTOMER_ACTION, "english")
    sent = str(stub.calls[0]["messages"])
    assert "2499" not in sent
    assert "Rs" not in sent


def test_the_model_is_asked_for_the_language_and_the_ask() -> None:
    stub = StubAnthropic("Pay {amount}: {link}")
    ClaudeDrafter(client=stub).draft(Blocker.MANDATE_REPAIR, "hinglish")
    sent = str(stub.calls[0]["messages"])
    assert "hinglish" in sent
    assert "set up the auto-pay mandate again" in sent


def test_a_model_draft_with_an_invented_figure_never_reaches_a_customer() -> None:
    stub = StubAnthropic("Pay Rs 5000 now at {link} for {amount}")
    assert ClaudeDrafter(client=stub).draft(Blocker.CUSTOMER_ACTION, "english") is None


def test_a_model_asked_about_a_blocker_that_is_not_escalated_declines() -> None:
    stub = StubAnthropic("Pay {amount}: {link}")
    assert ClaudeDrafter(client=stub).draft(Blocker.CUSTOMER_BALANCE, "english") is None
    assert ClaudeDrafter(client=stub).draft(Blocker.CUSTOMER_ACTION, "klingon") is None
    assert stub.calls == []


def test_a_response_with_no_text_block_falls_back() -> None:
    class Empty:
        content = []

    class Silent(StubAnthropic):
        def __init__(self) -> None:
            super().__init__("")

            class Messages:
                @staticmethod
                def create(**_kwargs):
                    return Empty()

            self.messages = Messages()

    assert ClaudeDrafter(client=Silent()).draft(Blocker.CUSTOMER_ACTION, "english") is None


def test_the_default_drafter_is_the_template_without_a_key(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from fourshots.outreach import default_drafter

    assert isinstance(default_drafter(), TemplateDrafter)


def test_the_default_drafter_uses_the_model_when_a_key_exists(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    from fourshots.outreach import default_drafter

    assert isinstance(default_drafter(), ClaudeDrafter)


def test_the_cli_prints_every_message_the_system_can_send(capsys) -> None:
    """A reviewer should be able to read the whole set on one screen."""
    from fourshots.outreach import _main

    assert _main([]) == 0
    printed = capsys.readouterr().out
    assert printed.count("[template]") == len(ASKS) * len(LANGUAGES)


def test_the_cli_can_emit_json(capsys) -> None:
    import json

    from fourshots.outreach import _main

    assert _main(["--json"]) == 0
    drafts = json.loads(capsys.readouterr().out)
    assert len(drafts) == len(ASKS) * len(LANGUAGES)
    assert all("{amount}" in draft["template"] for draft in drafts)
