"""Retry policies -- the arms of the experiment.

A policy answers one question: given what a merchant can actually see, when
should the next attempt be made, or should there not be one?

It returns a *proposed* time. It does not get to decide whether that time is
legal -- the runner puts every proposal through the same `check_legality` gate,
so neither arm can gain or lose by being sloppy about NPCI and RBI rules. A
policy that proposes an illegal instant simply has it corrected, identically to
its opponent.

Only the baseline lives here for now. The engine arrives once the baseline's
number is recorded, in that order, so the target cannot be shaded to fit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from fourshots.simulator import Observation


class RetryPolicy(Protocol):
    """Decides when -- or whether -- to spend the next attempt."""

    name: str

    def propose(self, observation: Observation) -> datetime | None:
        """Return the desired instant for the next attempt, or None to stop.

        Returning None is a real decision, not a failure: declining to spend an
        attempt on a debit that cannot succeed is the cheapest win available.
        """
        ...


class RazorpayDefault:
    """Razorpay's documented subscription retry policy.

    From the Razorpay Subscriptions documentation, Payment Retries:

        "We automatically retry the payment on the following day."

    Three retries on consecutive days after the original execution. The
    documentation states no variation by failure reason and exposes no merchant
    configurability, which is the whole point of using it as the control:

    - It never reads the decline code. A dead mandate, a breached limit and an
      empty account are all retried on the same schedule.
    - It never asks when money might arrive. A balance failure on the 26th is
      retried on the 27th, 28th and 29th, spending the entire regulator-capped
      budget on days the account is provably empty.

    This is a real, shipped, quotable policy -- not a strawman built to lose.
    It also spends the full budget available to it (one original plus three
    retries), so it is not handicapped relative to the engine.
    """

    name = "razorpay_documented_default"

    def __init__(self, offsets_days: list[int]) -> None:
        # Read from the pre-registered parameter file rather than hardcoded, so
        # the control arm cannot drift away from the documented policy.
        self._offsets = sorted(offsets_days)

    def propose(self, observation: Observation) -> datetime | None:
        # attempts_used counts the original execution, so the first retry is
        # attempt 2 and indexes offset[0].
        retry_index = observation.attempts_used - 1
        if retry_index >= len(self._offsets):
            return None

        first_attempt_at = (
            observation.history[0][0] if observation.history else observation.now
        )
        return first_attempt_at + timedelta(days=self._offsets[retry_index])
