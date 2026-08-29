"""The published figures, computed once, in every format they are quoted in.

Why this exists
---------------
Every prose document in this repository quotes numbers from the benchmark.
Twice now, a change to the model moved those numbers and the documents kept
the old ones -- the second time it survived a full verification pass and was
only caught by rebuilding a figure by hand.

CI already reproduces the benchmark, which proves the code still runs. It said
nothing about whether the README still told the truth about it. That gap is
what this module closes: it derives every quoted figure from a live run, and
`test_published_numbers.py` asserts each document actually contains the current
value. A stale number now fails the build and names the file.

Formats differ by document, so they are all produced here rather than
reinvented at each call site: plain grouping for machine-ish contexts
(4,985,233), Indian digit grouping for rupee amounts (49,85,233), and
lakh-rounded for prose (49.9L).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fourshots.benchmark import run
from fourshots.engine import ConstraintAwareEngine
from fourshots.params import Params, load
from fourshots.policies import RazorpayDefault
from fourshots.runner import Outcome, RunResult
from fourshots.simulator import TERMINAL_MODES


def indian_grouping(value: Decimal | int) -> str:
    """Group digits the Indian way: 4985233 -> '49,85,233'.

    Last three digits, then pairs. Used for every rupee amount in prose,
    because a figure written 4,985,233 in an Indian payments document reads as
    foreign.
    """
    digits = str(int(value))
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def lakhs(value: Decimal | int) -> str:
    """Round to lakhs for prose: 4985233 -> '49.9L'."""
    return f"{float(value) / 100_000:.1f}L"


@dataclass(frozen=True)
class Figure:
    """One published number, with every rendering it is quoted in.

    `renderings` is what the documents are searched for. A figure quoted in
    only one format still lists one; the point is that the search never has to
    guess how a given document spells it.
    """

    key: str
    renderings: tuple[str, ...]


def _pct(before: float, after: float) -> str:
    return f"{(after - before) / before:+.1%}"


def compute(params: Params | None = None) -> dict[str, Figure]:
    """Run both arms and return every figure the documents quote."""
    params = params or load()
    _, baseline = run(RazorpayDefault(params.baseline_offsets_days), params)
    cohort, engine = run(ConstraintAwareEngine(), params)

    by_id = {m.id: m for m in cohort}

    def wasted(result: RunResult) -> int:
        return sum(
            c.attempts_used
            for c in result.cycles
            if by_id[c.mandate_id].true_mode in TERMINAL_MODES
        )

    recovered_by_baseline = {c.mandate_id for c in baseline.cycles if c.recovered}
    recovered_by_engine = {c.mandate_id for c in engine.cycles if c.recovered}
    lost = recovered_by_baseline - recovered_by_engine
    gained = recovered_by_engine - recovered_by_baseline
    lost_value = sum((by_id[m].amount for m in lost), Decimal(0))
    gained_value = sum((by_id[m].amount for m in gained), Decimal(0))

    def rupees(value: Decimal | int) -> tuple[str, ...]:
        """Every way a rupee amount is written across the documents."""
        return (indian_grouping(value), f"{int(value):,}", lakhs(value))

    figures = [
        Figure("recovery_rate_baseline", (f"{baseline.recovered_count / baseline.total:.1%}",)),
        Figure("recovery_rate_engine", (f"{engine.recovered_count / engine.total:.1%}",)),
        Figure("recovered_baseline", rupees(baseline.recovered_value)),
        Figure("recovered_engine", rupees(engine.recovered_value)),
        Figure(
            "recovered_delta",
            (_pct(float(baseline.recovered_value), float(engine.recovered_value)),),
        ),
        Figure("mandates_saved_baseline", (f"{baseline.mandates_saved:,}",)),
        Figure("mandates_saved_engine", (f"{engine.mandates_saved:,}",)),
        Figure(
            "mandates_saved_delta",
            (_pct(baseline.mandates_saved, engine.mandates_saved),),
        ),
        Figure("attempts_baseline", (f"{baseline.attempts_spent:,}",)),
        Figure("attempts_engine", (f"{engine.attempts_spent:,}",)),
        Figure("attempts_per_recovery_baseline", (f"{baseline.attempts_per_recovery:.2f}",)),
        Figure("attempts_per_recovery_engine", (f"{engine.attempts_per_recovery:.2f}",)),
        Figure("wasted_baseline", (f"{wasted(baseline):,}",)),
        Figure("wasted_engine", (f"{wasted(engine):,}",)),
        Figure("regressions_count", (f"{len(lost)}",)),
        Figure("regressions_value", rupees(lost_value)),
        Figure("gained_count", (f"{len(gained)}",)),
        Figure("gained_value", rupees(gained_value)),
        Figure(
            "gained_to_lost_ratio",
            (
                f"{float(gained_value / lost_value):.1f}x",
                f"{float(gained_value / lost_value):.1f}×",
            ),
        ),
        Figure(
            "escalated",
            (f"{sum(1 for c in engine.cycles if c.outcome == Outcome.STOPPED_EARLY):,}",),
        ),
    ]
    return {f.key: f for f in figures}


# Which figures each document is expected to quote. A document that stops
# quoting one should be removed from its list deliberately, not by letting the
# check quietly weaken.
EXPECTED: dict[str, tuple[str, ...]] = {
    "README.md": (
        "recovery_rate_baseline",
        "recovery_rate_engine",
        "recovered_baseline",
        "recovered_engine",
        # The deltas are checked too. A stale "+51%" survived a full
        # verification pass once, in prose well away from the results table,
        # precisely because only the absolute figures were pinned.
        "recovered_delta",
        "mandates_saved_delta",
        "mandates_saved_baseline",
        "mandates_saved_engine",
        "attempts_baseline",
        "attempts_engine",
        "attempts_per_recovery_baseline",
        "attempts_per_recovery_engine",
        "wasted_baseline",
        "wasted_engine",
        "regressions_count",
        "regressions_value",
        "gained_count",
        "gained_value",
        "gained_to_lost_ratio",
    ),
    "docs/four-attempts.html": (
        "recovery_rate_baseline",
        "recovery_rate_engine",
        "recovered_baseline",
        "recovered_engine",
        "mandates_saved_baseline",
        "mandates_saved_engine",
        "attempts_baseline",
        "attempts_engine",
        "wasted_baseline",
        "wasted_engine",
        "regressions_count",
        "regressions_value",
        "gained_count",
        "gained_value",
        "gained_to_lost_ratio",
    ),
    # DECISIONS.md was on this list and has been removed on purpose. It used
    # to restate the headline table, and the prose around that table drifted
    # -- it was quoting wasted-attempt and mandates-saved figures from an
    # earlier run while the pinned table beside it stayed current. A second
    # copy of a number is a second thing to keep true. That document now
    # carries no figures at all and points at the benchmark instead, which is
    # a stronger guarantee than checking a duplicate.
}
