# Decisions

The choices behind this build, and the reasoning that has to survive being
questioned. The [README](README.md) has the problem and the results;
[ARCHITECTURE.md](ARCHITECTURE.md) has the boundaries. This has the *why*,
including for the things that were reversed.

Nothing here is written after the fact to fit a result. Where a decision was
wrong, it says so and says what replaced it.

---

## 1. Treat this as budget allocation, not "smarter retries"

The framing decided everything downstream.

NPCI allows four executions per mandate cycle — one original plus three
retries, in force 1 August 2025. Razorpay's documented default spends them on
consecutive days: *"We automatically retry the payment on the following day."*
No variation by failure reason, no merchant configurability.

A debit that fails on the 26th for insufficient funds therefore burns all four
attempts by the 29th, every one on a day the account is provably empty, and the
cycle is cancelled before salary lands on the 1st. That is the mechanical
explanation for a documented industry number: roughly 20 million UPI AutoPay
revocations a month, driven by low balances.

So the problem is **attempt-budget allocation under regulatory constraint**.
Four shots, each committed 24 hours ahead under the RBI pre-debit notification
rule, each landing in one of three daily non-peak windows, with the decline
code from attempt *N* the only evidence available for scheduling attempt *N+1*.
A constrained sequential decision problem with commitment delay.

Calling it "smart retries" would have produced a heuristic. Calling it budget
allocation produced a constraint lattice, a taxonomy with provenance, and a
measurable comparison.

## 2. Make the control arm Razorpay's own documented policy

The predictable objection to any result like this is *you built the simulator
and you built the optimizer*. The cheapest defence is a baseline nobody can
call a strawman.

The control arm is read from the pre-registered parameter file with its
citation attached, and tests pin its three defining properties: it retries on
consecutive days, it ignores the decline code, and it spends its whole budget.
If any of those stopped being true, the comparison would be against something
invented here.

It is not handicapped in any other way either — same budget, same legality
gate, same `Observation` type. It simply proposes worse attempts.

The framing that follows from this matters: the result is a **measurable
improvement to a documented default**, not a claim that anyone's retry logic is
bad.

## 3. Pre-register the cohort, then do not touch it

`params/cohort.yaml` was committed before the simulator, both policy arms, and
any result existed. The git history is the evidence, and the file has
deliberately not been edited since — not even to update a prose caveat that has
since been overtaken, because an extra commit against it costs more than the
stale sentence it would fix.

Every parameter declares its provenance. Most are marked `assumed` with
reasoning rather than dressed up with an invented citation, because no primary
source publishes a decline-reason breakdown for failed recurring debits in
India. The ones marked `assumed` are exactly what the sensitivity sweeps exist
to test.

## 4. Keep the model out of the money decision

Deliberate, and the line is drawn in code rather than in prose.

- **Deterministic, never a model:** attempt-budget accounting,
  execution-window legality, the 24-hour notice, AFA thresholds, terminal-code
  detection, opt-out. A language model choosing when to debit a stranger's
  account cannot be tested exhaustively, cannot be audited line by line, and
  fails by producing a plausible-sounding wrong date.
- **Deterministic and interesting:** diagnosing the failure class and choosing
  which remaining attempt to spend, and when. This is where the result comes
  from.
- **Model-only:** reading the prose a rail attaches to codes the taxonomy
  cannot map, and writing the escalation copy for the mandates the engine
  refuses to retry. Both are language jobs that a lookup table genuinely cannot
  do.

Neither model-backed layer can change a schedule. The triager picks from a
closed set of classes the taxonomy already defines; the drafter is never given
a figure, so any draft containing a digit is rejected before it can be sent.
With no API key at all, both fall back and behaviour is identical.

The measured ceiling on the triage layer is published rather than estimated,
and it is small. Inflating it would have been the easiest claim in the project
and the least defensible.

## 5. Report the losses next to the win

Every number in the README is produced by `python -m fourshots.benchmark` on a
fixed seed, and the benchmark prints the costs on the same run as the gains:
the cash-flow lag the engine's patience creates, and the mandates the baseline
recovered that the engine did not.

A net figure that hides its components invites exactly one question, and it is
better to have answered it already.

Figures are deliberately not restated in this document. They live in the README
and are pinned there by `tests/test_published_numbers.py`, which fails the build
if the prose and a live run disagree. Restating them here would create a second
copy to drift — which is what happened to an earlier version of this file.

## 6. Implement the regulatory gate twice

`policy.py` decides whether a debit against someone's account is permitted at
all. Everywhere else in this system a bug surfaces as a worse number; there it
surfaces as an illegal attempt that the benchmark still scores as fine, and no
aggregate metric would reveal it.

So the lattice is reimplemented in Rust from the circulars rather than from the
Python, and a differential test requires the two to agree exhaustively.

**It is an oracle, not a backend.** The benchmark runs in seconds, so a faster
implementation solves nothing, and selecting between implementations at runtime
would mean behaviour depending on whether an extension compiled on a given
machine — a real risk bought for no benefit. Python remains the only execution
path; without the toolchain the differential test skips and nothing else
changes.

The part worth showing is not "we used Rust" but the differential test itself.
A reimplementation is a risk that requires evidence, not a flourish.

## 7. Close the loop, and say exactly how far it closes

A live `payment.failed` webhook is decided, gated, and acted on — not merely
classified and filed.

The honest part is the boundary. Razorpay exposes no call that re-attempts a
mandate on a date of your choosing; the subscription's schedule belongs to the
gateway. So a retryable decline is **booked and notified**, and its audit entry
records `executed_against_rail: false`. An escalation, by contrast, is
**executed**: a Payment Link raised against the test-mode API, with its id and
URL hash-chained into the log.

It would have been easy to log both as "scheduled" and let a reader assume the
rail was touched. What is not executed is disclosed rather than implied.

---

## Decisions that were reversed

### The payday prior, killed by our own sensitivity sweep

The first engine held a belief about Indian payroll — salaries near the 1st,
a cluster around the 7th — and aimed each balance retry at the next plausible
payday. It performed well.

Then the sweep shifted the *world's* payday distribution by three days while
the prior stayed fixed, and the advantage collapsed. The engine was being told
the answer more than it was reading the world.

Spreading attempts evenly across the cycle needs no payday belief at all and
holds up far better across the whole range. The offsets are even thirds of a
roughly thirty-day cycle, derived from the cycle length rather than fitted to
the cohort — a fitted constant would have reintroduced exactly the overfitting
the sweep had just removed.

Building the clever version, testing it honestly, and shipping the robust one
is the whole argument for the methodology.

### A retention metric that flattered us

`mandates_saved` originally counted every early stop as a saved mandate,
including mandates whose VPA no longer resolved. Stopping early there is still
the right call — it saves three wasted attempts — but it does not save a
customer, and counting it as one inflated the metric most exposed to challenge.
It was caught by the mutation audit and corrected downward.

[`ARCHITECTURE.md`](ARCHITECTURE.md#what-the-checks-caught) lists all six
defects the verification caught, and which check caught each.

---

## Open items

- [ ] **Cite the primary circular for the four-attempt cap.** The reading is
      settled — per execution cycle, not per mandate lifetime; four independent
      secondary readings agree and are specific, describing one execution
      attempt plus three retries per mandate "based on its sequence number",
      a sequence number being the individual execution within a recurring
      series. What is missing is the citation: *Guidelines on usage of UPI and
      API*, notified 21 May 2025 and in force 1 August 2025, went to NPCI
      members rather than the public circulars page. `UPI_OC_No_223`
      (*Enhancement of UPI AutoPay*) is a different circular and does not carry
      the cap. Resolving this needs the member circular from someone with NPCI
      or PSP access.

## Known limits of the live path

- Razorpay exposes no API call that re-attempts a mandate on a chosen date, so
  a booked retry is scheduled and notified but not submitted to the rail.
  Closing that last inch needs either a Subscriptions-side schedule API or the
  merchant's own debit connectivity.
- UPI QR and Intent cannot be exercised in test mode; only UPI Collect works.
  Driving a controlled failure end to end therefore goes through a local
  checkout page rather than the hosted flow.
