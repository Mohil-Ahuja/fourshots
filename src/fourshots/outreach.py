"""Escalation copy: what to say to the customer when retrying cannot work.

The engine refuses to spend an attempt on roughly 470 mandates per run -- a
dead mandate, a breached limit, an AFA flow that needs a human. Those are the
right refusals, and they are also the point at which the system currently goes
quiet. Something has to be *said* to those customers, and saying it is a
genuine language job: the ask differs by blocker, and in India a large share of
recovery messaging is written in Hinglish rather than English.

So this is the second place a model earns its keep, on the same terms as
:mod:`fourshots.triage`:

**The model writes the sentence. It never chooses the moment, and it never
touches a number.** Whether to escalate is decided in :mod:`fourshots.engine`
by deterministic code that can be tested exhaustively. What the model returns
is prose containing placeholders -- ``{merchant}``, ``{amount}``, ``{link}``,
``{deadline}`` -- and the facts are substituted here, from the mandate, after
the draft is validated. A draft is rejected outright if it contains a digit,
because a digit is the model inventing a figure, and a wrong rupee amount in a
customer message is worse than a plain template.

Rejection is never fatal. Every failure path -- no key, a network error, a
malformed draft, an invented number -- falls back to the deterministic
template, which is what ships by default. Nothing in the benchmark calls a
model, and the recovery service behaves identically with the layer absent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from fourshots.taxonomy import Blocker

MODEL = "claude-opus-5"

PLACEHOLDERS = frozenset({"merchant", "amount", "link", "deadline"})
"""The only substitutions a draft may contain. Anything else is rejected."""

REQUIRED_PLACEHOLDERS = frozenset({"amount", "link"})
"""A message that names neither the sum nor the way to pay it is not outreach."""

MAX_BODY_CHARS = 480
"""Long enough for a considered message, short enough for SMS/WhatsApp."""

LANGUAGES = ("english", "hinglish")

_PLACEHOLDER_PATTERN = re.compile(r"\{([a-z_]*)\}")
_DIGIT_PATTERN = re.compile(r"\d")


# --- What the customer is being asked to do --------------------------------

ASKS: dict[Blocker, str] = {
    Blocker.CUSTOMER_ACTION: (
        "approve the payment themselves, because the bank requires the customer "
        "to be present and a silent retry has nobody to answer it"
    ),
    Blocker.MANDATE_REPAIR: (
        "set up the auto-pay mandate again, because the existing one has been "
        "cancelled or revoked at the bank and cannot be repaired from our side"
    ),
    Blocker.AMOUNT_CHANGE: (
        "pay this one bill manually and raise the per-transaction limit on the "
        "mandate, because the amount is above the limit the mandate was "
        "registered with"
    ),
}
"""Escalation reasons only.

Balance and transient failures are not here on purpose: those are retried
silently, and messaging a customer whose salary simply has not landed yet is
noise that costs goodwill for nothing.
"""


@dataclass(frozen=True)
class OutreachFacts:
    """The figures the message is allowed to state. All from the mandate."""

    merchant_name: str
    amount: Decimal
    pay_link: str
    deadline: datetime

    def substitutions(self) -> dict[str, str]:
        return {
            "merchant": self.merchant_name,
            "amount": f"Rs {self.amount:,.2f}",
            "link": self.pay_link,
            "deadline": self.deadline.strftime("%d %b %Y"),
        }


@dataclass(frozen=True)
class Draft:
    """One escalation message, before and after the facts are filled in.

    `template` is what the drafter produced and is the thing worth reviewing:
    it contains no figures, so it can be read once and approved for a whole
    class of mandates. `body` is that template with this mandate's facts in it.
    """

    blocker: Blocker
    language: str
    template: str
    body: str
    source: str
    model: str | None = None


class Drafter(Protocol):
    """Anything that can produce escalation prose for a blocker."""

    name: str

    def draft(self, blocker: Blocker, language: str) -> str | None:
        """Return a template, or None to fall back to the deterministic one."""


# --- Validation ------------------------------------------------------------


def validate_template(text: str) -> str | None:
    """Return the cleaned template, or None if it may not be sent.

    Four ways to be rejected, and each of them is a way a generated message
    could mislead a customer:

    - **A digit anywhere.** The model has no access to the amount and no reason
      to write one, so a digit means it invented a figure or a date.
    - **An unknown placeholder.** ``{account_last4}`` renders as literal braces
      in a customer's SMS at best, and at worst implies data this system does
      not hold.
    - **A missing required placeholder.** A message with no amount or no link
      is not an escalation, it is an apology.
    - **Length.** Past the cap it stops being a message and starts being a page.
    """
    cleaned = " ".join(text.strip().split())
    if not cleaned or len(cleaned) > MAX_BODY_CHARS:
        return None

    found = set(_PLACEHOLDER_PATTERN.findall(cleaned))
    if not found <= PLACEHOLDERS:
        return None
    if not REQUIRED_PLACEHOLDERS <= found:
        return None

    # Braces that did not parse as a known placeholder would raise or render
    # literally at substitution time. Count them instead of trusting the regex.
    if cleaned.count("{") != len(_PLACEHOLDER_PATTERN.findall(cleaned)):
        return None
    if cleaned.count("{") != cleaned.count("}"):
        return None

    if _DIGIT_PATTERN.search(cleaned):
        return None

    return cleaned


def render(template: str, facts: OutreachFacts) -> str:
    """Fill a validated template with this mandate's facts."""
    return template.format_map(facts.substitutions())


# --- The deterministic default ---------------------------------------------

TEMPLATES: dict[tuple[Blocker, str], str] = {
    (Blocker.CUSTOMER_ACTION, "english"): (
        "Your {amount} payment to {merchant} could not be completed because "
        "your bank needs you to approve it yourself. We have not retried it, "
        "so nothing has been debited. Please approve it here before "
        "{deadline}: {link}"
    ),
    (Blocker.CUSTOMER_ACTION, "hinglish"): (
        "{merchant} ka {amount} ka payment complete nahi ho paya -- bank ko "
        "aapka apna approval chahiye. Humne dobara try nahi kiya, isliye paisa "
        "cut nahi hua hai. {deadline} se pehle yahan approve kar dijiye: {link}"
    ),
    (Blocker.MANDATE_REPAIR, "english"): (
        "Your auto-pay mandate with {merchant} is no longer active at your "
        "bank, so your {amount} payment could not be collected and nothing has "
        "been debited. Further silent attempts would fail too, so we have "
        "stopped. Please pay this one here and set auto-pay up again before "
        "{deadline}: {link}"
    ),
    (Blocker.MANDATE_REPAIR, "hinglish"): (
        "{merchant} ke saath aapka auto-pay mandate ab bank mein active nahi "
        "hai, isliye {amount} ka payment nahi ho paya -- aur paisa cut nahi "
        "hua hai. Baar baar try karne ka fayda nahi, isliye humne rok diya. "
        "{deadline} tak yahan pay karke auto-pay dobara set kar dijiye: {link}"
    ),
    (Blocker.AMOUNT_CHANGE, "english"): (
        "Your {amount} payment to {merchant} is above the per-transaction "
        "limit your auto-pay mandate was set up with, so your bank declined "
        "it. Please pay this one here before {deadline}, and raise the mandate "
        "limit so the next one goes through automatically: {link}"
    ),
    (Blocker.AMOUNT_CHANGE, "hinglish"): (
        "{amount} ka payment aapke auto-pay mandate ki per-transaction limit "
        "se zyada hai, isliye bank ne {merchant} ka debit decline kar diya. "
        "{deadline} se pehle yahan pay kijiye, aur mandate ki limit badha "
        "dijiye taaki agli baar apne aap ho jaye: {link}"
    ),
}


class TemplateDrafter:
    """The offline default. Deterministic, reviewed, and always available."""

    name = "template"

    def draft(self, blocker: Blocker, language: str) -> str | None:
        return TEMPLATES.get((blocker, language))


class ClaudeDrafter:
    """Asks Claude for the prose, then holds it to the same rules.

    Wraps a fallback rather than replacing it: an unusable draft is not an
    error condition, it is the ordinary case in which the template is sent
    instead. The caller cannot tell the difference except by reading `source`
    on the resulting :class:`Draft`, which is exactly what the audit log
    records.
    """

    name = "claude"

    def __init__(self, client=None, model: str = MODEL) -> None:
        self.model = model
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def draft(self, blocker: Blocker, language: str) -> str | None:
        ask = ASKS.get(blocker)
        if ask is None or language not in LANGUAGES:
            return None

        try:
            response = self._ensure_client().messages.create(
                model=self.model,
                max_tokens=1000,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Language: {language}\n"
                            f"What the customer needs to do: {ask}\n"
                            "Write the message."
                        ),
                    }
                ],
            )
        except Exception:
            # Deliberately broad, for the same reason as the triage layer:
            # whatever went wrong, the answer is the reviewed template, not a
            # customer left unmessaged and not an outage that changes behaviour.
            return None

        text = _first_text(response)
        return validate_template(text) if text else None


SYSTEM_PROMPT = """\
You write payment-recovery messages for an Indian subscription merchant. A
recurring auto-pay debit has failed in a way that retrying cannot fix, so the
customer has to do something themselves.

Rules, all of them hard:

- Write ONE message. No greeting line, no sign-off, no subject line, no
  markdown, no quotes around it. Plain text under 400 characters.
- Use ONLY these placeholders, and write them exactly, in braces:
  {merchant} {amount} {link} {deadline}. {amount} and {link} are required.
  Any other placeholder is a failure.
- Never write a digit. Not a rupee figure, not a date, not a phone number, not
  an account number. You do not know any of them; they are substituted later.
- Say plainly that no money has been taken and that we are not retrying
  silently. Customers who have just seen a failed debit assume the opposite,
  and reassuring them is the point of writing at all.
- State what went wrong in one clause, without blaming the customer and
  without bank jargon.
- If the language is hinglish, write Hindi in Latin script mixed with English
  the way people actually message in urban India -- not translated-sounding
  Hindi, and not English with two Hindi words added. Keep payment nouns
  (auto-pay, mandate, limit, bank) in English, which is how they are said.
"""


def _first_text(response) -> str | None:
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", None)
    return None


# --- Assembly --------------------------------------------------------------


def compose(
    blocker: Blocker,
    facts: OutreachFacts,
    *,
    language: str = "english",
    drafter: Drafter | None = None,
) -> Draft | None:
    """Produce the message for one escalated mandate.

    Returns None for blockers that are not escalations -- balance and transient
    failures are retried, and messaging those customers is noise.
    """
    if blocker not in ASKS:
        return None

    fallback = TemplateDrafter()
    source = fallback.name
    model: str | None = None
    template: str | None = None

    if drafter is not None:
        candidate = drafter.draft(blocker, language)
        template = validate_template(candidate) if candidate else None
        if template is not None:
            source = drafter.name
            model = getattr(drafter, "model", None)

    if template is None:
        template = fallback.draft(blocker, language)
    if template is None:
        return None

    return Draft(
        blocker=blocker,
        language=language,
        template=template,
        body=render(template, facts),
        source=source,
        model=model,
    )


def default_drafter() -> Drafter:
    """Claude when a key is configured, the reviewed template otherwise.

    The template is not a degraded mode. It is the shipped default, and the
    only behaviour any test or benchmark run ever exercises.
    """
    from fourshots.triage import credentials_available

    return ClaudeDrafter() if credentials_available() else TemplateDrafter()


def _main(argv: list[str] | None = None) -> int:
    """Print every escalation message, both languages, with sample facts.

    A reviewer should be able to read the entire set of things this system will
    ever say to a customer in one screen. There are six.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Print the escalation copy for every escalated blocker."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="draft with a model instead of the shipped templates",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    drafter = default_drafter() if args.live else None
    facts = OutreachFacts(
        merchant_name="Example Merchant",
        amount=Decimal("2499.00"),
        pay_link="https://rzp.io/i/example",
        deadline=datetime(2026, 9, 30),
    )

    drafts = [
        draft
        for blocker in ASKS
        for language in LANGUAGES
        if (draft := compose(blocker, facts, language=language, drafter=drafter))
    ]

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "blocker": d.blocker.value,
                        "language": d.language,
                        "template": d.template,
                        "body": d.body,
                        "source": d.source,
                    }
                    for d in drafts
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    for draft in drafts:
        print(f"--- {draft.blocker.value} / {draft.language} [{draft.source}]")
        print(draft.body)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
