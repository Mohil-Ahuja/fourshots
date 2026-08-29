"""The loop, closed: a live decline in, a recorded recovery action out.

Everything else in this project decides. This module is the one place that
*acts*, which is the track's literal verb, so it is also the place where the
distinction between deciding and acting has to be kept scrupulously.

The pipeline for one ``payment.failed`` webhook:

1. Rebuild what is known about this mandate from the audit log -- the previous
   declines, and therefore how many of the four attempts are gone. State lives
   in the log, not in process memory, so a restarted server resumes with the
   same history rather than a fresh budget.
2. Ask :class:`~fourshots.engine.ConstraintAwareEngine` for a date, or for
   nothing. Unchanged and untouched: the same code the benchmark measures.
3. Put any proposed date through :func:`~fourshots.policy.check_legality`.
   A date the engine likes but the NPCI and RBI rules forbid is not booked.
4. Execute, and record exactly what was executed.

Step 4 is where the honesty lives. Two different things happen and they are
never conflated:

**A booked retry is booked, not fired.** Razorpay's API has no "attempt this
mandate again on this date" call -- the subscription's own schedule is the
gateway's. So a retry is written into the schedule with its pre-debit
notification, and the audit entry says ``executed_against_rail: false``. It
would be easy to log "attempt_scheduled" and let a reader assume the rail was
touched. It was not, and the log says so on every entry.

**An escalation is executed.** For a debit that cannot clear silently there is
a real, available action: raise a Payment Link in test mode and hand the
customer a way to pay. That call goes out, the link id and URL come back, and
both are hash-chained into the log. The message that accompanies it is drafted
by :mod:`fourshots.outreach` -- a model writes the sentence, this file supplies
every figure in it.

With no credentials configured nothing outward happens and every decision is
still made, gated and logged, with ``executed: false`` recorded rather than
implied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from fourshots.audit import AuditLog
from fourshots.engine import ConstraintAwareEngine
from fourshots.outreach import Draft, OutreachFacts, compose
from fourshots.policy import (
    MAX_ATTEMPTS_PER_CYCLE,
    MandatePurpose,
    PreDebitNotification,
    ProposedAttempt,
    Violation,
    check_legality,
)
from fourshots.razorpay_client import RazorpayError, RazorpayClient
from fourshots.simulator import DeclineRecord, Observation
from fourshots.taxonomy import Blocker
from fourshots.webhook import DeclineObserved

CYCLE_WINDOW = timedelta(days=32)
"""How far back a decline is still part of the same execution cycle.

The four-attempt budget is per cycle, so attempts have to be counted per cycle
and not per mandate lifetime. A live integration gets the boundary exactly from
``subscription.charged``; absent that, declines older than one cycle belong to a
previous one. Erring long is the safe direction -- it counts an extra attempt as
used and refuses one that might have been legal, rather than proposing a fifth.
"""

ESCALATION_VALIDITY = timedelta(days=7)
"""How long an escalation payment link stays open."""


class Action(enum.Enum):
    """What was done about a decline. Recorded verbatim in the audit log."""

    RETRY_BOOKED = "retry_booked"
    ESCALATED = "escalated"
    STOPPED = "stopped"
    REJECTED = "rejected"


@dataclass(frozen=True)
class RecoveryDecision:
    """The complete account of one decline: what was decided, and what ran.

    `executed` is deliberately separate from `action`. An escalation that was
    decided but could not be raised -- no credentials, an API error -- is still
    an escalation, and reporting it as anything else would misstate what the
    system did.
    """

    mandate_id: str
    action: Action
    failure_class: str
    blocker: Blocker
    attempts_used: int
    execute_at: datetime | None = None
    violations: tuple[Violation, ...] = ()
    draft: Draft | None = None
    payment_link_id: str | None = None
    payment_link_url: str | None = None
    executed: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "failure_class": self.failure_class,
            "blocker": self.blocker.value,
            "attempts_used": self.attempts_used,
            "attempts_remaining": MAX_ATTEMPTS_PER_CYCLE - self.attempts_used,
            "execute_at": self.execute_at.isoformat() if self.execute_at else None,
            "violations": [violation.value for violation in self.violations],
            "message": self.draft.body if self.draft else None,
            "message_source": self.draft.source if self.draft else None,
            "message_language": self.draft.language if self.draft else None,
            "payment_link_id": self.payment_link_id,
            "payment_link_url": self.payment_link_url,
            "executed": self.executed,
            "note": self.note,
        }


@dataclass
class BookedAttempt:
    """A retry written into the schedule, with the notice that must precede it."""

    mandate_id: str
    execute_at: datetime
    amount: Decimal
    notification: PreDebitNotification
    attempt_number: int


class RecoveryService:
    """Turns observed declines into recorded, bounded recovery actions.

    Every dependency is injectable, and the defaults are the safe ones: the
    real engine, no Razorpay client (so nothing outward happens), and the
    reviewed outreach templates rather than a model.
    """

    def __init__(
        self,
        audit: AuditLog,
        *,
        engine: ConstraintAwareEngine | None = None,
        client: RazorpayClient | None = None,
        drafter=None,
        merchant_name: str = "your merchant",
        language: str = "english",
        purpose: MandatePurpose = MandatePurpose.GENERAL,
    ) -> None:
        self._audit = audit
        self._engine = engine or ConstraintAwareEngine()
        self._client = client
        self._drafter = drafter
        self._merchant_name = merchant_name
        self._language = language
        self._purpose = purpose
        self._booked: list[BookedAttempt] = []

    # --- state, recovered from the log ------------------------------------

    def history_for(self, mandate_id: str, before: datetime) -> tuple[DeclineRecord, ...]:
        """The declines already recorded for this mandate's current cycle.

        Read back out of the audit log rather than held in memory, so the
        attempt budget survives a restart. `raw_reason` is the field the
        taxonomy classifies on -- `raw_code` is Razorpay's coarse envelope and
        is far too broad to schedule against.
        """
        records: list[DeclineRecord] = []
        for entry in self._audit.read():
            if entry.kind != "decline_observed" or entry.mandate_id != mandate_id:
                continue
            at = entry.at
            if before - at > CYCLE_WINDOW or at > before:
                continue
            records.append(
                DeclineRecord(
                    at=at,
                    razorpay_code=entry.data.get("raw_reason"),
                    npci_code=entry.data.get("npci_code"),
                    description=entry.data.get("raw_description"),
                )
            )
        records.sort(key=lambda record: record.at)
        return tuple(records)

    def booked_attempts(self) -> tuple[BookedAttempt, ...]:
        """Retries booked by this process, in the order they were booked."""
        return tuple(self._booked)

    # --- the loop ----------------------------------------------------------

    def handle(self, decline: DeclineObserved) -> RecoveryDecision:
        """Decide and act on one observed decline.

        The caller is expected to have already recorded the `decline_observed`
        entry, which is what makes this decline part of the history the budget
        is counted from.
        """
        mandate_id = decline.subscription_id or decline.payment_id
        history = self.history_for(mandate_id, decline.at)
        if not history:
            # The decline being handled has not been logged (a caller that
            # decides before recording, or a payment with no subscription).
            # Counting it is what keeps the budget honest.
            history = (
                DeclineRecord(
                    at=decline.at,
                    razorpay_code=decline.raw_reason,
                    npci_code=decline.npci_code,
                    description=decline.raw_description,
                ),
            )

        observation = Observation(
            mandate_id=mandate_id,
            amount=decline.amount,
            purpose=self._purpose,
            now=decline.at,
            attempts_used=len(history),
            history=history,
        )

        classification = decline.classification
        failure_class = classification.failure_class
        proposed = self._engine.propose(observation)

        if proposed is None:
            return self._not_retrying(decline, observation, classification)

        attempt = ProposedAttempt(
            mandate_id=mandate_id,
            amount=decline.amount,
            purpose=self._purpose,
            execute_at=proposed,
            # The notice goes out now; the engine's floor already guarantees at
            # least the mandated 24 hours between this instant and the debit.
            notified_at=decline.at,
            attempts_used=observation.attempts_used,
        )
        legality = check_legality(attempt)

        if legality.is_legal:
            return self._book(decline, observation, attempt, failure_class.name)

        if set(legality.violations) == {Violation.AFA_REQUIRED}:
            # Not a dead end. An above-threshold debit needs the customer to
            # authenticate, which is precisely an escalation -- and knowing it
            # before spending an attempt is the cheapest win in the system.
            return self._escalate(
                decline,
                observation,
                Blocker.CUSTOMER_ACTION,
                failure_class.name,
                note="additional factor of authentication required above threshold",
            )

        self._audit.append(
            "attempt_rejected",
            {
                "failure_class": failure_class.name,
                "proposed_execute_at": proposed.isoformat(),
                "violations": [violation.value for violation in legality.violations],
                "attempts_used": observation.attempts_used,
            },
            mandate_id=mandate_id,
            at=decline.at,
        )
        return RecoveryDecision(
            mandate_id=mandate_id,
            action=Action.REJECTED,
            failure_class=failure_class.name,
            blocker=failure_class.blocker,
            attempts_used=observation.attempts_used,
            execute_at=proposed,
            violations=legality.violations,
            note="proposed attempt failed the regulatory gate and was not booked",
        )

    # --- branches ----------------------------------------------------------

    def _not_retrying(
        self, decline: DeclineObserved, observation: Observation, classification
    ) -> RecoveryDecision:
        """The engine declined to spend an attempt. Escalate, or stop."""
        failure_class = classification.failure_class
        blocker = failure_class.blocker

        if blocker in (
            Blocker.CUSTOMER_ACTION,
            Blocker.MANDATE_REPAIR,
            Blocker.AMOUNT_CHANGE,
        ):
            return self._escalate(
                decline, observation, blocker, failure_class.name, note=""
            )

        # Budget spent, or an unreadable code the engine will not guess at
        # twice. Nothing to say to the customer and nothing worth attempting.
        exhausted = observation.attempts_used >= MAX_ATTEMPTS_PER_CYCLE
        note = (
            "attempt budget exhausted for this cycle"
            if exhausted
            else "no further attempt would help and no customer action would fix it"
        )
        self._audit.append(
            "mandate_halted",
            {
                "failure_class": failure_class.name,
                "blocker": blocker.value,
                "attempts_used": observation.attempts_used,
                "reason": note,
            },
            mandate_id=observation.mandate_id,
            at=decline.at,
        )
        return RecoveryDecision(
            mandate_id=observation.mandate_id,
            action=Action.STOPPED,
            failure_class=failure_class.name,
            blocker=blocker,
            attempts_used=observation.attempts_used,
            note=note,
        )

    def _book(
        self,
        decline: DeclineObserved,
        observation: Observation,
        attempt: ProposedAttempt,
        failure_class_name: str,
    ) -> RecoveryDecision:
        """Write a legal retry into the schedule, with its pre-debit notice."""
        notification = PreDebitNotification.build(
            merchant_name=self._merchant_name,
            attempt=attempt,
            reason=f"retry of a failed auto-pay debit ({failure_class_name})",
        )
        booked = BookedAttempt(
            mandate_id=attempt.mandate_id,
            execute_at=attempt.execute_at,
            amount=attempt.amount,
            notification=notification,
            attempt_number=observation.attempts_used + 1,
        )
        self._booked.append(booked)

        self._audit.append(
            "attempt_booked",
            {
                "failure_class": failure_class_name,
                "execute_at": attempt.execute_at.isoformat(),
                "attempt_number": booked.attempt_number,
                "amount_inr": str(attempt.amount),
                "pre_debit_notice": {
                    "merchant_name": notification.merchant_name,
                    "amount_inr": str(notification.amount),
                    "debit_at": notification.debit_at.isoformat(),
                    "mandate_reference": notification.mandate_reference,
                    "reason": notification.reason,
                    "opt_out_deadline": notification.opt_out_deadline.isoformat(),
                },
                # Razorpay exposes no call that re-attempts a mandate on a
                # chosen date, so this attempt is scheduled and notified but
                # has not been submitted to the rail. Stated rather than
                # left to be inferred.
                "executed_against_rail": False,
            },
            mandate_id=attempt.mandate_id,
            at=decline.at,
        )
        return RecoveryDecision(
            mandate_id=attempt.mandate_id,
            action=Action.RETRY_BOOKED,
            failure_class=failure_class_name,
            blocker=decline.classification.failure_class.blocker,
            attempts_used=observation.attempts_used,
            execute_at=attempt.execute_at,
            executed=False,
            note="booked and notified; not submitted to the rail",
        )

    def _escalate(
        self,
        decline: DeclineObserved,
        observation: Observation,
        blocker: Blocker,
        failure_class_name: str,
        *,
        note: str,
    ) -> RecoveryDecision:
        """Raise a payment link and draft the message that carries it."""
        mandate_id = observation.mandate_id
        deadline = decline.at + ESCALATION_VALIDITY
        link_id: str | None = None
        link_url: str | None = None
        executed = False
        failure_note = note

        if self._client is not None:
            try:
                link = self._client.create_payment_link(
                    amount=decline.amount,
                    description=(
                        f"Recover failed auto-pay debit for {mandate_id} "
                        f"({failure_class_name})"
                    ),
                    # Mandate plus the failed payment, so a repeated webhook
                    # for the same cycle cannot raise a second link.
                    reference_id=f"{mandate_id}:{decline.payment_id}",
                    notes={
                        "mandate_id": mandate_id,
                        "failure_class": failure_class_name,
                        "blocker": blocker.value,
                    },
                    expire_by=deadline,
                )
            except RazorpayError as failure:
                failure_note = f"payment link not created: {failure}"
            else:
                link_id, link_url = link.id, link.short_url
                executed = True
        else:
            failure_note = failure_note or (
                "no test-mode credentials configured; escalation decided and "
                "drafted but no payment link was created"
            )

        draft = compose(
            blocker,
            OutreachFacts(
                merchant_name=self._merchant_name,
                amount=decline.amount,
                pay_link=link_url or "<payment link pending>",
                deadline=deadline,
            ),
            language=self._language,
            drafter=self._drafter,
        )

        self._audit.append(
            "escalation_executed" if executed else "escalation_drafted",
            {
                "failure_class": failure_class_name,
                "blocker": blocker.value,
                "attempts_used": observation.attempts_used,
                "amount_inr": str(decline.amount),
                "deadline": deadline.isoformat(),
                "payment_link_id": link_id,
                "payment_link_url": link_url,
                "executed": executed,
                # The template carries no figures, so recording it lets a
                # reviewer check the wording that was approved separately from
                # the facts this mandate put into it.
                "message_template": draft.template if draft else None,
                "message": draft.body if draft else None,
                "message_source": draft.source if draft else None,
                "message_model": draft.model if draft else None,
                "message_language": draft.language if draft else None,
                "note": failure_note,
            },
            mandate_id=mandate_id,
            at=decline.at,
        )
        return RecoveryDecision(
            mandate_id=mandate_id,
            action=Action.ESCALATED,
            failure_class=failure_class_name,
            blocker=blocker,
            attempts_used=observation.attempts_used,
            draft=draft,
            payment_link_id=link_id,
            payment_link_url=link_url,
            executed=executed,
            note=failure_note,
        )


def decisions_from(entries: Iterable[Any]) -> list[dict[str, Any]]:
    """Pull the action entries out of an audit log, for the schedule endpoint."""
    kinds = {
        "attempt_booked",
        "attempt_rejected",
        "escalation_executed",
        "escalation_drafted",
        "mandate_halted",
    }
    return [
        {
            "seq": entry.seq,
            "at": entry.at.isoformat(),
            "kind": entry.kind,
            "mandate_id": entry.mandate_id,
            **entry.data,
        }
        for entry in entries
        if entry.kind in kinds
    ]
