"""The experiment harness.

Runs one policy against one cohort and records what happened. Both arms go
through this same code path, and the fairness of the comparison rests on three
properties of it:

1. **Identical legality treatment.** Every proposed attempt passes through
   `policy.check_legality`. An illegal proposal is corrected the same way for
   either arm -- rolled to the earliest legal instant -- rather than being
   scored as a failure for one and forgiven for the other.

2. **Identical budget.** Both arms get `MAX_ATTEMPTS_PER_CYCLE` executions,
   including the original. Neither is handicapped.

3. **Identical information.** The policy receives an `Observation` built only
   from merchant-visible facts. It never sees the `World` or the `Mandate`.
   The harness holds both; the policy is handed neither.

Every decision is written to the audit log with the reason behind it, so a run
can be replayed and checked afterwards rather than taken on trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from fourshots.audit import AuditLog
from fourshots.policy import (
    IST,
    MAX_ATTEMPTS_PER_CYCLE,
    ProposedAttempt,
    check_legality,
    earliest_schedulable,
    next_execution_window,
)
from fourshots.policies import RetryPolicy
from fourshots.simulator import DeclineRecord, FailureMode, Mandate, Observation, World


UNREPAIRABLE_MODES = frozenset({FailureMode.MANDATE_DEAD})
"""Ground-truth modes where the mandate itself is gone, not merely blocked.

An expired card, a breached per-transaction limit or an unusable instrument can
all be fixed by the customer re-authorising. A VPA that no longer resolves
cannot -- there is nothing left to repair.
"""


class Outcome:
    """How a mandate's cycle ended."""

    RECOVERED = "recovered"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STOPPED_EARLY = "stopped_early"


@dataclass(frozen=True)
class CycleResult:
    """What happened to one mandate over one execution cycle."""

    mandate_id: str
    amount: Decimal
    outcome: str
    attempts_used: int
    recovered_at: datetime | None
    repairable: bool
    """Ground truth: whether the underlying mandate could be re-authorised.

    Scored from the world, not from anything the policy saw. Keeping the
    information barrier on the *policy* is what makes the comparison fair;
    scoring the outcome honestly requires ground truth, and withholding it
    here would only let a policy take credit it has not earned.
    """

    @property
    def recovered(self) -> bool:
        return self.outcome == Outcome.RECOVERED

    @property
    def mandate_survived(self) -> bool:
        """Whether the mandate lives to see another cycle.

        A cycle that exhausts its budget is cancelled. Stopping early leaves the
        mandate intact *only if the mandate was repairable to begin with* --
        an expired card or a breached limit can be re-authorised by the
        customer, but a VPA that no longer exists cannot.

        The distinction matters because it is exactly where this metric could
        flatter the engine. Stopping early on a dead mandate is still the right
        call -- it saves three wasted attempts -- but it does not save a
        customer, and counting it as though it did would be dishonest.
        """
        if self.outcome == Outcome.RECOVERED:
            return True
        return self.outcome == Outcome.STOPPED_EARLY and self.repairable


@dataclass
class RunResult:
    """Aggregate outcome of one policy against one cohort."""

    policy_name: str
    cycles: list[CycleResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cycles)

    @property
    def recovered_count(self) -> int:
        return sum(1 for c in self.cycles if c.recovered)

    @property
    def recovered_value(self) -> Decimal:
        return sum((c.amount for c in self.cycles if c.recovered), Decimal(0))

    @property
    def exposed_value(self) -> Decimal:
        """Total rupees at risk across the cohort."""
        return sum((c.amount for c in self.cycles), Decimal(0))

    @property
    def mandates_saved(self) -> int:
        """Cycles that did not end in cancellation.

        The retention metric. Ties directly to the roughly 20 million UPI
        AutoPay revocations per month attributed to low balances.
        """
        return sum(1 for c in self.cycles if c.mandate_survived)

    @property
    def attempts_spent(self) -> int:
        return sum(c.attempts_used for c in self.cycles)

    @property
    def attempts_per_recovery(self) -> float:
        """How much budget each recovery cost. Lower is better."""
        return self.attempts_spent / self.recovered_count if self.recovered_count else float("inf")

    def summary(self) -> dict[str, object]:
        return {
            "policy": self.policy_name,
            "mandates": self.total,
            "recovered": self.recovered_count,
            "recovery_rate": self.recovered_count / self.total if self.total else 0.0,
            "recovered_inr": str(self.recovered_value),
            "exposed_inr": str(self.exposed_value),
            "mandates_saved": self.mandates_saved,
            "mandate_survival_rate": self.mandates_saved / self.total if self.total else 0.0,
            "attempts_spent": self.attempts_spent,
            "attempts_per_recovery": round(self.attempts_per_recovery, 3),
        }


def _first_debit_at(mandate: Mandate, month: date) -> datetime:
    """The scheduled original execution, moved into a legal window.

    Even the first debit obeys the NPCI window rules -- it is a mandate
    execution like any other.
    """
    scheduled = datetime(month.year, month.month, mandate.debit_day, 9, 0, tzinfo=IST)
    return next_execution_window(scheduled)


def run_cycle(
    mandate: Mandate,
    world: World,
    retry_policy: RetryPolicy,
    month: date,
    audit: AuditLog | None = None,
) -> CycleResult:
    """Run one mandate through one execution cycle under `retry_policy`."""
    now = _first_debit_at(mandate, month)
    cycle_start = now
    history: list[DeclineRecord] = []
    attempts_used = 0

    while attempts_used < MAX_ATTEMPTS_PER_CYCLE:
        result = world.attempt(mandate, now, cycle_start)
        attempts_used += 1
        history.append(DeclineRecord(now, result.razorpay_code, result.npci_code))

        if audit:
            audit.append(
                "attempt_made",
                {
                    "policy": retry_policy.name,
                    "attempt": attempts_used,
                    "at": now.isoformat(),
                    "cleared": result.cleared,
                    "code": result.razorpay_code,
                    "npci_code": result.npci_code,
                    "amount_inr": str(mandate.amount),
                },
                mandate_id=mandate.id,
                at=now,
            )

        if result.cleared:
            return CycleResult(
                mandate.id, mandate.amount, Outcome.RECOVERED, attempts_used, now,
                repairable=mandate.true_mode not in UNREPAIRABLE_MODES,
            )

        if attempts_used >= MAX_ATTEMPTS_PER_CYCLE:
            break

        observation = Observation(
            mandate_id=mandate.id,
            amount=mandate.amount,
            purpose=mandate.purpose,
            now=now,
            attempts_used=attempts_used,
            history=tuple(history),
        )
        proposed = retry_policy.propose(observation)

        if proposed is None:
            # Declining to spend an attempt is a decision, and a good one when
            # the debit cannot succeed. The mandate survives, re-authorisable.
            if audit:
                audit.append(
                    "stopped_early",
                    {
                        "policy": retry_policy.name,
                        "attempts_used": attempts_used,
                        "last_code": result.razorpay_code,
                        "last_npci_code": result.npci_code,
                    },
                    mandate_id=mandate.id,
                    at=now,
                )
            return CycleResult(
                mandate.id, mandate.amount, Outcome.STOPPED_EARLY, attempts_used, None,
                repairable=mandate.true_mode not in UNREPAIRABLE_MODES,
            )

        now = _legalise(proposed, now, mandate, attempts_used, retry_policy, audit)

    return CycleResult(
        mandate.id, mandate.amount, Outcome.BUDGET_EXHAUSTED, attempts_used, None,
        repairable=mandate.true_mode not in UNREPAIRABLE_MODES,
    )


def _legalise(
    proposed: datetime,
    decided_at: datetime,
    mandate: Mandate,
    attempts_used: int,
    retry_policy: RetryPolicy,
    audit: AuditLog | None,
) -> datetime:
    """Move a proposed attempt to the earliest legal instant at or after it.

    Applied identically to both arms. A policy that ignores the 24-hour
    pre-debit notice or proposes a peak-hour slot is not punished for it -- the
    proposal is simply corrected, exactly as it would be for its opponent.
    """
    floor = earliest_schedulable(decided_at)
    scheduled = next_execution_window(max(proposed, floor))

    if audit and scheduled != proposed:
        audit.append(
            "attempt_rescheduled",
            {
                "policy": retry_policy.name,
                "proposed": proposed.isoformat(),
                "scheduled": scheduled.isoformat(),
                "reason": "moved to earliest legal instant (notice + window rules)",
            },
            mandate_id=mandate.id,
            at=decided_at,
        )

    # Confirm the corrected instant is genuinely legal. A violation here is a
    # bug in the harness, not a policy decision, so it must be loud.
    check = check_legality(
        ProposedAttempt(
            mandate_id=mandate.id,
            amount=mandate.amount,
            purpose=mandate.purpose,
            execute_at=scheduled,
            notified_at=decided_at,
            attempts_used=attempts_used,
            afa_obtained=False,
        )
    )
    illegal = [v for v in check.violations if v.value != "afa_required_but_not_obtained"]
    if illegal:
        raise AssertionError(
            f"harness scheduled an illegal attempt for {mandate.id}: {illegal}"
        )

    return scheduled


def run_cohort(
    cohort: list[Mandate],
    world: World,
    retry_policy: RetryPolicy,
    month: date,
    audit: AuditLog | None = None,
) -> RunResult:
    """Run every mandate in the cohort under one policy."""
    return RunResult(
        policy_name=retry_policy.name,
        cycles=[run_cycle(m, world, retry_policy, month, audit) for m in cohort],
    )
