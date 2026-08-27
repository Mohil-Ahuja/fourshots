# Decisions

Standing decisions for the build, recorded so they survive the schedule and
can be defended in the panel. Updated as things change; nothing here is
retconned after a result lands.

## Track

**Track 03 — AI Revenue Recovery.** One submission is allowed, so track
crowding is a first-order variable, not a tiebreaker. Track 01 (Agentic
Commerce) draws the most entries and its strongest submissions converge on the
same researched answer: a policy gateway with scoped mandates and an audit log,
which is a scaled-down clone of Visa TAP and Mastercard Agent Pay.

Track 03's bar reads as a spec for this project:

> "Show measured money recovered across a batch, with compliant escalation,
> stopping rules, and an audit trail."

Each clause maps to a component: money recovered → the benchmark; stopping
rules → the attempt budget; compliant escalation → the RBI/NPCI constraint
lattice; audit trail → the hash chain.

## The thesis

NPCI allows **four executions per mandate cycle** (1 original + 3 retries,
in force 2025-08-01). Razorpay's documented default spends them on consecutive
days: *"We automatically retry the payment on the following day."* No variation
by failure reason, no merchant configurability.

So a debit that fails on the 26th for insufficient funds burns all four
attempts by the 29th — every one on a day the account is provably empty —
and the cycle is cancelled before salary lands on the 1st.

This is the mechanical explanation for a documented industry number: roughly
**20 million UPI AutoPay revocations per month** driven by low balances
(Business Standard, Sept 2025).

The project is therefore **attempt-budget allocation under regulatory
constraint**, not "smart retries". Four shots, each committed 24 hours ahead
under the RBI pre-debit notification rule, each landing in one of three daily
non-peak windows, with the decline code from attempt N the only evidence for
scheduling attempt N+1. A constrained sequential decision problem with
commitment delay.

## Where AI goes, and where it does not

Deliberate and defensible in the panel:

- **Deterministic, never a model:** attempt-budget accounting, execution-window
  legality, 24h notice, AFA thresholds, terminal-code detection, opt-out.
  Putting an LLM in the debit-scheduling path would be indefensible.
- **Agentic:** diagnosing failure class and choosing which remaining attempt to
  spend, and when.
- **LLM-only:** triaging the residual unresolved exceptions at the end.

## Methodology — defending the headline number

The predictable attack is *"you built the simulator and the optimizer."*
Four answers, all cheap:

1. **Information barrier.** The engine sees only what a real merchant sees from
   a webhook: decline code, timestamp, amount, attempts remaining. It never
   touches the simulator's balance trajectory. Enforced by module boundary.
2. **Pre-registration.** Cohort parameters are committed with sources *before*
   any result exists. The git history is the evidence.
3. **Sensitivity sweep.** The win is reported across a range of assumptions,
   not one tuned setting.
4. **Quotable baseline.** The control arm is Razorpay's own documented policy,
   not a strawman we invented.

## Metrics to report

- Rupees recovered vs the baseline arm, on the same 4-attempt budget
- **Mandates saved** — cycles that survived instead of being cancelled. Ties to
  the 20M/month revocation figure and reframes recovery as retention.
- Recovery rate by failure class
- Attempts consumed per success
- Compliance violations (target: 0, against a baseline that would violate)
- An honest unresolved-exception list
- Taxonomy coverage: how much of the domain model is documented vs inferred

## Video — beats in order

Five minutes. Open on the problem, not the architecture.

1. **The calendar image.** Two rows of a month. Row 1: four attempts stacked on
   the 26th–29th, every one on a flat-balance day, mandate dies. Row 2: attempt
   held, placed in the post-salary window, recovered. Understandable in three
   seconds without knowing a single NPCI rule. This is the pitch.
2. **The number.** ~20M revocations/month on low balance.
3. **The constraint that nobody knows.** Four attempts, three daily windows,
   24h commitment. State that most retry systems do not model any of it.
4. **The baseline is Razorpay's own documented policy** — quote the docs line.
   Framed as a measurable improvement to a documented default, never as
   "Razorpay's retry is dumb". We are applying to work there.
5. **The design we killed.** The first engine aimed each balance retry at a
   hardcoded Indian-payroll prior and scored +41%. The sensitivity sweep
   shifted the world's payday by three days, the prior stayed fixed, and the
   advantage collapsed to +2.2% -- it was being told the answer, not reading
   the world. Spreading attempts evenly across the cycle needs no payday belief
   and holds at +32.3% worst case. Say this out loud in the video: building the
   clever version, testing it honestly, and shipping the robust one is the
   whole argument for the methodology.
6. **The live failure.** A real test-mode payment returned
   `international_transaction_not_allowed`, a code the taxonomy had never seen.
   It degraded to the conservative class with a 24h floor rather than crashing
   or guessing, and recorded that its own mapping confidence was `inferred`.
   Found by pointing the receiver at the real rail — the synthetic cohort would
   never have produced that code. This is the "one failure handled gracefully".
7. **The headline result** + the sensitivity sweep behind it.
8. **Audit chain verification live** — edit one rupee in the log, watch verify
   fail, on camera.

## Result so far (D5)

2000 mandates, seed 20260827, September 2026:

| | baseline | engine | delta |
|---|---|---|---|
| recovery rate | 45.6% | 63.5% | +39.3% |
| recovered | INR 56.2L | INR 81.3L | +44.8% |
| mandates saved | 912 | 1,610 | +76.5% |
| attempts spent | 5,508 | 3,875 | -29.6% |
| attempts per recovery | 6.04 | 3.05 | -49.5% |

More money and more mandates from fewer attempts. Attempts spent on debits
that could never clear fall from 1,540 to 598.

Two corrections made before reporting, both worth telling: the payday prior
was killed by the sweep (above), and `mandates_saved` originally counted an
early stop on a dead mandate as a saved customer, which it is not -- fixing it
cut the engine's figure from 1,721 to 1,610.

## Rust

**Scope: the constraint engine only.** Pure, no I/O, tight API, ~300 lines,
already exhaustively tested — the tests become the port spec. It is also the
component where the claim means something, because it is the money-decision
gate.

**Not** the receiver (done, working), the decisioning layer (talks to an LLM),
or the analysis.

**Shape:** Python stays the shipped reference implementation; Rust is an
optional accelerated backend, imported if the extension is present and silently
skipped if not. A judge who clones without a Rust toolchain must still get a
working repo — a failed `pip install` is worse than no Rust at all.

**The part worth showing** is not "we used Rust" but the differential test: a
property test generates random attempts and asserts both implementations return
identical legality verdicts. A reimplementation is a risk requiring evidence,
not a flex.

**Timing: D8, after the headline result exists.** Rust on an unfinished result
adds nothing. Drop it without loss if the schedule slips.

**Trigger to do it earlier:** if the sensitivity sweep exceeds a couple of
minutes, the simulation kernel becomes a legitimate performance target.
"We ported it because the sweep took 40 minutes" beats "we ported it because
Rust."

## Open verification items

- [ ] **Is the NPCI 4-attempt cap per execution cycle or per mandate lifetime?**
      Sources say "per mandate, identified by each sequence number", which reads
      as per-cycle. The `mandates_saved` metric depends on it. No headline claim
      may rest on it until checked against the primary circular.
- [ ] **Is 5 September the application deadline or the submission deadline?**
      Changes the calendar by weeks.

## Known debt

- `app.py` builds its `AuditLog` at import time, so tests clear the module
  cache to isolate logs. Works, but should become a factory before the repo
  goes public.
- UPI QR and Intent cannot be tested in test mode (Razorpay docs); only UPI
  Collect works. A local checkout page using Checkout.js is needed at D6 to
  drive `failure@razorpay` and control the payment method for the demo.
