"""Reproduce the headline comparison.

    python -m fourshots.benchmark
    python -m fourshots.benchmark --sweep     # payday sensitivity as well

Runs both arms against the pre-registered cohort and prints the table from the
README. The seed is fixed in `params/cohort.yaml`, so the numbers are the same
on every machine -- a result nobody else can reproduce is not a result.
"""

from __future__ import annotations

import argparse
import copy
import random
from datetime import date
from decimal import Decimal

from fourshots.engine import ConstraintAwareEngine
from fourshots.params import Params, load
from fourshots.policies import RazorpayDefault
from fourshots.runner import Outcome, RunResult, run_cohort
from fourshots.simulator import TERMINAL_MODES, World, build_cohort

MONTH = date(2026, 9, 1)

DAYS_IN_MONTH_SPACE = 31
"""Day-of-month values run 1..31, so shifts wrap modulo 31, not 30."""


def run(policy, params: Params):
    """Run one arm. Cohort and world are rebuilt from the same seed for each
    arm, so both face an identical world rather than a shared, drifting one."""
    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)
    return cohort, run_cohort(cohort, World(params, rng), policy, MONTH)


def _rupees(value: Decimal | int | float) -> str:
    return f"{int(value):,}"


def _delta(before: float, after: float) -> str:
    if before == 0:
        return "     n/a"
    return f"{(after - before) / before:>+7.1%}"


def print_headline(params: Params) -> tuple[RunResult, RunResult]:
    _, baseline = run(RazorpayDefault(params.baseline_offsets_days), params)
    cohort, engine = run(ConstraintAwareEngine(), params)

    print(f"Cohort: {params.size} mandates, seed {params.seed}, {MONTH:%B %Y}")
    print()
    print(f"{'':24s} {'BASELINE':>13s} {'FOURSHOTS':>13s} {'DELTA':>9s}")
    print("-" * 62)

    rows = [
        ("recovery rate", baseline.recovered_count / baseline.total,
         engine.recovered_count / engine.total, lambda v: f"{v:.1%}"),
        ("recovered (INR)", baseline.recovered_value, engine.recovered_value, _rupees),
        ("mandates saved", baseline.mandates_saved, engine.mandates_saved, _rupees),
        ("attempts spent", baseline.attempts_spent, engine.attempts_spent, _rupees),
        ("attempts per recovery", baseline.attempts_per_recovery,
         engine.attempts_per_recovery, lambda v: f"{v:.2f}"),
    ]
    for label, before, after, fmt in rows:
        print(f"{label:24s} {fmt(before):>13s} {fmt(after):>13s} "
              f"{_delta(float(before), float(after))}")

    print("-" * 62)

    by_id = {m.id: m for m in cohort}

    def wasted(result: RunResult) -> int:
        return sum(
            c.attempts_used for c in result.cycles
            if by_id[c.mandate_id].true_mode in TERMINAL_MODES
        )

    print(f"{'attempts on hopeless debits':24s} {wasted(baseline):>13,} {wasted(engine):>13,}")
    stopped = sum(1 for c in engine.cycles if c.outcome == Outcome.STOPPED_EARLY)
    print(f"{'escalated instead of burnt':24s} {0:>13,} {stopped:>13,}")
    print(f"{'exposed (INR)':24s} {_rupees(baseline.exposed_value):>13s}")
    print()

    provenance = params.provenance_summary()
    documented = len(provenance.get("documented", []))
    assumed = len(provenance.get("assumed", []))
    print(f"Parameter provenance: {documented} documented section(s), "
          f"{assumed} assumed. See params/cohort.yaml.")

    return baseline, engine


def _shift_payday(params: Params, days: int) -> Params:
    """Move the world's payday distribution without touching the engine.

    The engine holds no payday belief, so this tests whether the advantage
    depends on the cohort happening to be shaped conveniently.
    """
    document = copy.deepcopy(params.raw)
    weights = document["salary_credit"]["day_of_month_weights"]
    shifted: dict = {}
    for key, weight in weights.items():
        if key == "other":
            shifted["other"] = weight
            continue
        # Modulus 31, matching the 1..31 day-of-month space. A modulus of 30
        # silently folds day 31 onto day 1 -- including at shift zero, which
        # made the "unshifted" column disagree with the headline. Caught by
        # exactly that disagreement; DAYS_IN_MONTH_SPACE keeps it explicit.
        moved = ((int(key) - 1 + days) % DAYS_IN_MONTH_SPACE) + 1
        shifted[moved] = shifted.get(moved, 0) + weight
    document["salary_credit"]["day_of_month_weights"] = shifted
    return Params(document)


def print_sweep(params: Params) -> None:
    print()
    print("Payday sensitivity -- the world moves, the engine's logic does not.")
    print()
    print(f"{'shift':>6s} {'baseline INR':>14s} {'fourshots INR':>15s} {'delta':>9s}")
    print("-" * 48)

    worst = None
    for shift in range(-3, 4):
        shifted = _shift_payday(params, shift)
        _, baseline = run(RazorpayDefault(shifted.baseline_offsets_days), shifted)
        _, engine = run(ConstraintAwareEngine(), shifted)
        delta = float(
            (engine.recovered_value - baseline.recovered_value) / baseline.recovered_value
        )
        worst = delta if worst is None else min(worst, delta)
        print(f"{shift:>+6d} {_rupees(baseline.recovered_value):>14s} "
              f"{_rupees(engine.recovered_value):>15s} {delta:>+8.1%}")

    print("-" * 48)
    print(f"worst case across the sweep: {worst:+.1%}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sweep", action="store_true",
        help="also run the payday sensitivity sweep",
    )
    args = parser.parse_args()

    params = load()
    print_headline(params)
    if args.sweep:
        print_sweep(params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
