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
from datetime import date, datetime
from decimal import Decimal
from statistics import mean, median

from fourshots.engine import ConstraintAwareEngine
from fourshots.params import Params, load
from fourshots.policies import RazorpayDefault
from fourshots.runner import Outcome, RunResult, run_cohort
from fourshots.policy import IST
from fourshots.simulator import (
    _MODE_TO_DESCRIPTION,
    TERMINAL_MODES,
    Mandate,
    World,
    build_cohort,
)
from fourshots.triage import NullTriager, TriageVerdict, default_triager

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



def print_costs(cohort: list[Mandate], baseline: RunResult, engine: RunResult) -> None:
    """Report where the engine is WORSE than the documented default.

    The headline is a net figure and net figures hide things. Two costs are
    real and neither is visible in the summary table:

    Cash-flow lag. The engine deliberately waits -- for money to arrive, for an
    outage to clear -- so recoveries land later. A merchant feels that as
    delayed cash even when the total is higher, and a reader is entitled to
    know the size of the delay before accepting the trade.

    Mandate-level regressions. Aggregate improvement is compatible with
    individual losses. Some mandates the baseline recovered, the engine does
    not -- mostly customer-absent declines, where the engine escalates to a
    person while the baseline retries blindly and occasionally gets lucky.

    Printed every run, so the trade-off travels with the headline instead of
    being something a reader has to think to ask for.
    """
    by_id = {m.id: m for m in cohort}

    def lags(result: RunResult) -> list[float]:
        out = []
        for cycle in result.cycles:
            if cycle.recovered_at is None:
                continue
            mandate = by_id[cycle.mandate_id]
            due = datetime(MONTH.year, MONTH.month, mandate.debit_day, 9, 0, tzinfo=IST)
            out.append((cycle.recovered_at - due).total_seconds() / 86400)
        return sorted(out)

    baseline_lag, engine_lag = lags(baseline), lags(engine)

    def p90(values: list[float]) -> float:
        return values[int(0.9 * (len(values) - 1))] if values else 0.0

    print()
    print("WHERE THE ENGINE IS WORSE")
    print()
    print("Cash-flow lag -- days from due date to recovery")
    print(f"{'':12s} {'median':>8s} {'mean':>8s} {'p90':>8s} {'max':>8s}")
    for label, values in (("baseline", baseline_lag), ("fourshots", engine_lag)):
        print(f"{label:12s} {median(values):>8.1f} {mean(values):>8.1f} "
              f"{p90(values):>8.1f} {max(values):>8.1f}")

    recovered_by_baseline = {c.mandate_id for c in baseline.cycles if c.recovered}
    recovered_by_engine = {c.mandate_id for c in engine.cycles if c.recovered}
    lost = recovered_by_baseline - recovered_by_engine
    gained = recovered_by_engine - recovered_by_baseline

    lost_value = sum((by_id[m].amount for m in lost), Decimal(0))
    gained_value = sum((by_id[m].amount for m in gained), Decimal(0))

    print()
    print("Mandate-level regressions")
    print(f"  lost   (baseline recovered, engine did not): {len(lost):>4}  "
          f"INR {_rupees(lost_value)}")
    print(f"  gained (engine recovered, baseline did not): {len(gained):>4}  "
          f"INR {_rupees(gained_value)}")
    if lost_value:
        print(f"  gained-to-lost value ratio: {float(gained_value / lost_value):.1f}x")

    causes: dict[str, int] = {}
    for mandate_id in lost:
        mode = by_id[mandate_id].true_mode.value
        causes[mode] = causes.get(mode, 0) + 1
    if causes:
        listed = ", ".join(f"{k} {v}" for k, v in sorted(causes.items(), key=lambda kv: -kv[1]))
        print(f"  regression causes: {listed}")


class _OracleTriager:
    """Reads the rail's prose perfectly. Not a model -- an upper bound.

    Answers the only question worth asking before trusting an AI layer: if it
    were flawless, how much would it be worth? Any real model scores at or
    below this.
    """

    name = "oracle"
    _BY_PROSE = {prose: mode.value for mode, prose in _MODE_TO_DESCRIPTION.items()}

    def triage(self, code: str, description: str | None):
        resolved = self._BY_PROSE.get(description or "")
        if resolved is None:
            return None
        return TriageVerdict(code, resolved, 1.0, "oracle reading", "oracle")


def print_ai_layer(params: Params) -> None:
    """Size the AI layer honestly, including when the answer is 'not much'.

    The model reads prose attached to decline codes the taxonomy cannot map.
    That gap is real but small, so the headroom is small, and reporting it as
    small is the point: the deterministic constraint work does the heavy
    lifting, and inflating the AI's contribution would be the easiest and least
    defensible claim in the project.
    """
    _, without = run(ConstraintAwareEngine(NullTriager()), params)
    _, perfect = run(ConstraintAwareEngine(_OracleTriager()), params)

    active = default_triager()
    cached = getattr(active, "__len__", lambda: 0)()

    print()
    print("AI LAYER -- triage of unreadable decline codes")
    print()
    print(f"{'':26s} {'recovered':>12s} {'attempts':>10s}")
    print(f"{'no triage (default)':26s} {_rupees(without.recovered_value):>12s} "
          f"{without.attempts_spent:>10,}")
    print(f"{'perfect triage (oracle)':26s} {_rupees(perfect.recovered_value):>12s} "
          f"{perfect.attempts_spent:>10,}")

    headroom = perfect.recovered_value - without.recovered_value
    share = float(headroom / without.recovered_value) if without.recovered_value else 0.0
    print()
    print(f"Ceiling on what any triage layer can add here: "
          f"INR {_rupees(headroom)} ({share:+.2%}).")
    print(f"Active triager: {getattr(active, 'name', '?')} "
          f"({cached} cached verdict(s)).")
    if not cached:
        print("No verdicts cached, so triage is inert in this run -- the engine "
              "behaves exactly as it does without the layer.")
        print("Populate with fourshots.triage.refresh_cache() and commit the result.")


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
    baseline, engine = print_headline(params)
    cohort, _ = run(RazorpayDefault(params.baseline_offsets_days), params)
    print_costs(cohort, baseline, engine)
    print_ai_layer(params)
    if args.sweep:
        print_sweep(params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
