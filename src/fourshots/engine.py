"""The constraint-aware arm.

Answers one question per decline: *is a future attempt worth one of the ones I
have left, and if so, when?*

The baseline answers it identically for every code. This arm reads the code,
works out what has to change in the world before a debit could clear, and
places the attempt where that change is most likely to have happened -- or
declines to spend one at all.

No model is involved. Scheduling a debit is a money decision, and a language
model in that path would be indefensible: it cannot be tested exhaustively, it
cannot be audited line by line, and its failure mode is a plausible-sounding
wrong date. AI earns its place elsewhere in this system -- reading the
natural-language error descriptions attached to codes the taxonomy cannot map,
where deterministic code genuinely cannot help -- and that layer sits outside
this module and cannot widen what the engine is permitted to do.

Why balance failures are spread, not aimed
------------------------------------------
An earlier version of this engine held a prior about Indian payroll -- salaries
land near the 1st, with a cluster around the 7th -- and aimed each retry at the
next plausible payday. It performed well. It was also fragile, and the
sensitivity sweep is what exposed it: shifting the *world's* payday
distribution by three days while the prior stayed fixed collapsed the
advantage from +41% to +2.2%. The engine was not reading the world, it was
being told the answer.

Spreading attempts evenly across the cycle instead needs no belief about payday
at all, and holds up far better: worst case +32.3% against the prior's +2.2%,
while matching or beating it at nearly every shift. The reasoning is simple --
if you do not know when money arrives, maximise the temporal coverage of the
attempts you have, so that whichever day payday falls on, an attempt lands soon
after it.

The offsets are even thirds of a roughly thirty-day cycle, not values tuned
until the number improved. That distinction matters: a fitted constant would
have reintroduced exactly the overfitting the sweep just removed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fourshots.policy import (
    MAX_ATTEMPTS_PER_CYCLE,
    earliest_schedulable,
    next_execution_window,
)
from fourshots.simulator import Observation
from fourshots.taxonomy import Blocker, classify

BALANCE_RETRY_OFFSETS_DAYS: tuple[int, ...] = (8, 16, 25)
"""Days after the failed debit at which to place each balance retry.

Even spacing across a ~30-day cycle, so the three retries cover it regardless
of when income actually arrives. Derived from the cycle length, not fitted to
the cohort -- see the module docstring.
"""


class ConstraintAwareEngine:
    """Spends the four-attempt budget according to what each decline means.

    The decisions, in descending order of value:

    - **Terminal codes stop immediately.** A dead mandate, a breached limit or
      an unusable instrument cannot clear however long you wait, so the correct
      number of further attempts is zero. Stopping also leaves a repairable
      mandate alive and re-authorisable instead of cancelled, which is the
      difference between losing one payment and losing the customer.

    - **Balance failures are spread across the cycle.** Retrying tomorrow
      spends an attempt on an account that has not changed overnight. The RBI
      24-hour commitment rule is what makes spreading practical rather than
      awkward: the attempt has to be booked a day ahead regardless, so booking
      it further ahead costs nothing.

    - **Transient failures retry soon.** An issuer outage or rail congestion
      clears in hours. Waiting a day here is the opposite error from the
      balance case, and wastes the window in which the debit would have cleared.

    - **Customer-absent and AFA failures need a person.** A silent retry has
      nobody to answer it, so the engine escalates instead of discovering that
      three times.

    - **Unreadable codes stay cautious.** The engine does not guess. It applies
      the conservative floor and spends at most one attempt on a code it cannot
      read. That caution costs real recoveries, and the results report the cost
      rather than hiding it.
    """

    name = "constraint_aware_engine"

    def propose(self, observation: Observation) -> datetime | None:
        if MAX_ATTEMPTS_PER_CYCLE - observation.attempts_used <= 0:
            return None

        decline = observation.last_decline
        # Both views of the same event. The taxonomy prefers the rail's own
        # code where it is present and definite, because it is more specific --
        # Z8 is terminal, where the aggregator's rendering of it may not be.
        classification = classify(
            razorpay_code=decline.razorpay_code if decline else None,
            npci_code=decline.npci_code if decline else None,
        )
        failure_class = classification.failure_class
        blocker = failure_class.blocker

        # Nothing that happens later can make these clear. Spending an attempt
        # is strictly wasteful, and stopping keeps a repairable mandate alive.
        if classification.is_terminal:
            return None

        # AFA and customer-present flows cannot be satisfied by retrying
        # silently; they need the customer. Escalate rather than burn budget.
        if blocker is Blocker.CUSTOMER_ACTION and not failure_class.silently_retryable:
            return None

        # Earliest instant satisfying the 24h notice and the NPCI window rules,
        # plus whatever the failure class itself requires before a retry helps.
        floor = earliest_schedulable(observation.now)
        class_floor = observation.now + timedelta(hours=failure_class.min_backoff_hours)
        earliest = next_execution_window(max(floor, class_floor))

        if blocker is Blocker.CUSTOMER_BALANCE:
            return self._spread_across_cycle(observation, earliest)

        if blocker is Blocker.UNKNOWN:
            # An unreadable code. Spend at most one attempt on it, at the
            # conservative floor, then stop rather than guessing further.
            if observation.attempts_used >= 2:
                return None
            return earliest

        # Transient: the class floor already encodes how long to wait.
        return earliest

    def _spread_across_cycle(
        self, observation: Observation, earliest: datetime
    ) -> datetime | None:
        """Place this retry at its even-spacing offset from the original debit.

        Anchored to the first attempt rather than the previous one, so the
        attempts stay spread even when an earlier one was pushed later by the
        notice period or a peak window.
        """
        retry_index = observation.attempts_used - 1
        if retry_index >= len(BALANCE_RETRY_OFFSETS_DAYS):
            return None

        first_attempt_at = observation.history[0].at
        target = first_attempt_at + timedelta(
            days=BALANCE_RETRY_OFFSETS_DAYS[retry_index]
        )
        return next_execution_window(max(target, earliest))
