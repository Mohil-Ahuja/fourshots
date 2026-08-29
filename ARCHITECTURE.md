# Architecture

How the system is put together, and why each boundary sits where it does. The
[README](README.md) covers the problem and the results; this covers the build.

---

## The shape of it

Two independent things share one domain model: a **live service** that ingests
real Razorpay webhooks, and an **experiment harness** that measures two retry
policies against a declared cohort. Neither depends on the other. The domain
model — what a decline code means, and what the regulations permit — is the
only thing they have in common, which is what makes the benchmark a claim about
the shipped logic rather than about a separate research script.

![Three columns. The live service takes a Razorpay webhook through signature
verification, event parsing and a recovery decision, then books a retry or
raises a payment link; a rejected webhook is logged with its body discarded.
The shared domain model in the centre holds the four constraints and is called
identically by both sides. The experiment harness on the right holds ground
truth in World and passes only a decline code, timestamp, amount and attempt
count across a red information barrier into an Observation, which is all the
engine and baseline ever see. Everything lands in one append-only hash-chained
audit log.](docs/architecture.svg)

The line drawn in red is the one worth looking at. Everything a policy could
use to cheat — the balance trajectory, the payday, the true failure mode — sits
above it, and the only things that cross are the four facts a merchant actually
receives from a webhook. Boundary 2 below is that line in detail.

Note also what the centre column is: one implementation of the rules, called by
both sides. The benchmark is therefore a claim about the code that runs in
production, not about a research script that happens to resemble it.

---

## Modules

| Module | Responsibility |
|---|---|
| `taxonomy.py` | Decline codes → retry-relevant classes. Carries provenance per mapping. |
| `policy.py` | The NPCI/RBI constraint lattice. Deterministic, no model, no heuristic. |
| `simulator.py` | The cohort and its ground truth, behind the information barrier. |
| `policies.py` | The baseline arm: Razorpay's documented default. |
| `engine.py` | The constraint-aware arm. |
| `triage.py` | Reads the prose on codes the taxonomy cannot map. |
| `outreach.py` | Writes the escalation copy. The other place a model is consulted. |
| `runner.py` | The harness both arms pass through, identically. |
| `audit.py` | Append-only hash-chained decision log. |
| `webhook.py`, `app.py` | Live Razorpay ingestion. |
| `recovery.py` | Closes the loop: a live decline becomes a recorded, executed action. |
| `razorpay_client.py` | Test-mode REST client. Refuses a live key in code. |
| `rust/src/lib.rs` | Independent reimplementation of the gate, used as a differential oracle. |
| `benchmark.py`, `figures.py` | Reproduce and publish the results. |
| `params.py` | Loads and validates the pre-registered cohort parameters. |

---

## The six boundaries that matter

Everything else is ordinary code. These six are where a reviewer should push.

### 1. Rules are separated from judgement

`policy.py` answers *is this attempt legal?* `engine.py` answers *is it worth
spending?* Those are different kinds of question and they do not share a code
path.

Legality is not a judgement call — NPCI caps a cycle at four executions, blocks
AutoPay during UPI peak hours, and the RBI requires 24 hours' notice with AFA
above ₹15,000. Those are rules, so they are deterministic, exhaustively tested,
and hand-checkable against the circulars. Whether an attempt is *worth* one of
the four remaining is a judgement, and it lives separately.

`check_legality()` returns **every** violation rather than short-circuiting on
the first, so the audit log records the complete reason an attempt was refused.

### 2. The information barrier

A policy receives an `Observation` — mandate id, amount, purpose, attempts
used, and the history of decline codes. That is everything a merchant learns
from a webhook and its own books. It never receives the `World`, the `Mandate`,
the balance trajectory, the payday, or the true failure mode.

This is not a convention. `Observation` and `AttemptResult` are the only types
that cross the boundary, and a test asserts on their dataclass fields that
neither exposes ground truth — so a leaky field added later fails the suite
instead of quietly invalidating the headline number.

The barrier constrains the **policy**, not the **scorer**. Scoring honestly
requires ground truth: `mandates_saved` has to know whether a mandate was
actually repairable, or an early stop on a dead VPA would count as saving a
customer it did not save.

### 3. Both arms run through one harness

The comparison is only worth something if it is fair, so fairness is
structural rather than promised:

- **Same budget.** Both arms get `MAX_ATTEMPTS_PER_CYCLE`, including the
  original execution. The baseline is not handicapped; it spends the full
  regulatory allowance.
- **Same legality treatment.** An illegal proposal is corrected identically for
  either arm — moved to the earliest legal instant — rather than counted as a
  failure for one and forgiven for the other.
- **Same information.** Both receive the same `Observation` type.

The baseline is Razorpay's **documented, shipped** policy, read from the
pre-registered parameter file with its citation attached — not a strawman.
Tests pin that it retries on consecutive days, ignores the decline code, and
spends its whole budget; if any of those stopped being true, the comparison
would be against something we invented.

### 4. Where AI is, and where it deliberately is not

Scheduling a debit is a money decision. A language model there cannot be tested
exhaustively, cannot be audited line by line, and fails by producing a
plausible-sounding wrong date. Attempt accounting, window legality, notice
periods, AFA thresholds and terminal detection are all deterministic.

The model does two things a lookup table cannot, and neither of them is
choosing a moment.

**It reads the prose** a rail attaches to a decline code the taxonomy has never
seen, and proposes a **classification, never a schedule**, from a closed set;
the engine then does exactly what it always does with that class.

Four bounds, each tested:

- A verdict naming a class that does not exist is discarded, not coerced to the
  nearest match — a model inventing a category is a reason to distrust it.
- A verdict below the confidence floor is discarded. The prompt offers an
  explicit way to answer "I cannot tell", because a confident wrong answer
  costs an attempt that cannot be recovered.
- Any failure — credentials, network, malformed response — returns nothing and
  the conservative default stands. An outage at the model provider must not
  change when someone's account is debited.
- Verdicts are cached to a file meant to be committed, so a benchmark run is
  reproducible and every model judgement behind it can be read and disputed.
  **This repository ships no cache**, so the default run is triage-free and
  the layer is inert; the benchmark prints that rather than leaving it to be
  inferred. `python -m fourshots.triage` derives the cohort's unmappable
  codes and fills the cache from a live model. A test asserts the shipped
  state and the documented claim never drift apart.

With no API key the null triager runs and behaviour is identical to before the
layer existed. **Measured ceiling: +1.76%**, against +49.2% from the
deterministic work.

**And it writes the escalation message**, in `outreach.py`, for the ~470
mandates a run the engine refuses to retry. Whether to escalate is already
decided by then; what is open is what to say, which differs by blocker and is
often better said in Hinglish than English.

The bound here is structural rather than behavioural. The model is never given
the amount, so it cannot write a correct one: it returns prose containing
`{merchant}`, `{amount}`, `{link}` and `{deadline}`, and `recovery.py`
substitutes the mandate's own figures afterwards. A draft is rejected for any
digit, any placeholder outside that set, a missing amount or link, or excess
length — and rejection falls back to the reviewed templates, which are what
ships, which every test and benchmark run exercises, and which are held to the
same validator. The audit entry records the template, the rendered message and
which drafter produced it, so wording can be approved once for a whole class
and still be inspected per customer.

This layer is deliberately outside the +1.76%. It changes no schedule, so the
benchmark cannot measure it, and attaching a recovery-rate number to message
quality would be exactly the unfalsifiable claim this document exists to
avoid.

### 5. The regulatory gate is implemented twice

`rust/src/lib.rs` reimplements the constraint lattice from the circulars, and
`tests/test_differential.py` requires it to agree with the Python exhaustively
across the day and across 20,000 randomly generated gate cases.

It is an oracle, not a backend. The benchmark runs in seconds, so a faster
implementation solves nothing, and selecting between implementations at runtime
would mean behaviour depending on whether an extension compiled on a given
machine — a real risk bought for no benefit. Python stays the only execution
path.

The violation names are mapped between the two rather than shared. A common
constant would let one mistake propagate into both and the test would still
pass.

What this cannot establish is that both implementations are not wrong in the
same way — a misread circular produces two agreeing implementations of the
wrong rule. Agreement narrows the space of errors to misunderstanding rather
than mistranslation; it does not close it.

### 6. The audit log commits to its own history

Each entry carries the hash of its predecessor, so editing a recorded amount,
forging an entry's own digest, or splicing an entry out all fail `verify()`.
Crash-truncation deliberately does not — the surviving prefix is still a valid
chain, and flagging it would cry wolf.

The reason this matters is not tidiness. The headline is a comparison between
two policies, and a reader is entitled to ask whether the losing arm's log was
tidied afterwards. A chain that verifies is the answer.

---

## Verification

Seven layers, because each catches what the others miss.

| Layer | What it establishes | What it cannot |
|---|---|---|
| 335 tests | Behaviour matches intent | That the tests assert anything |
| 97% coverage | Lines execute | That a bug in them is caught |
| `tools/mutation_audit.py` | 12 deliberate defects all fail the suite | That untested behaviour exists elsewhere |
| `tests/test_published_numbers.py` | Prose quotes current figures | That the figures are right |
| 20 replications | The effect is not one lucky seed | That the cohort model is right |
| Two sensitivity sweeps | It survives payday and decline-mix assumptions | That other assumptions hold |
| Rust differential oracle | Two independent implementations of the gate agree | That both are not wrong the same way |

The mutation audit is the load-bearing one. A suite at 97% coverage can assert
almost nothing; introducing real defects — a regulatory constant that no longer
matches the circular, signature verification switched off, the engine losing
its terminal-stop — and requiring the suite to fail on each is what shows the
tests bite. Its first run caught 11 of 12, and the miss was the metric most
exposed to challenge.

CI runs the suite, the coverage gate, the mutation audit and the
published-numbers check, plus the benchmark and the Rust differential, so a
result that stops reproducing breaks the build.

---

## What the checks caught

Six defects the verification found before they reached a result. They are
listed because each one is evidence that a specific check works — a claim that
cannot be made by asserting the code is correct, only by showing what the
checks stopped. All of them are in the git history.

**Caught by the sensitivity sweep — a payday prior that was being told the
answer.** The first engine aimed each
balance retry at a hardcoded Indian-payroll prior and scored well. Shifting the
*world's* payday three days while the prior stayed fixed degraded it to +9.9%.
Spreading attempts evenly needs no payday belief and holds at +46.9% worst
case. The offsets are even thirds of a ~30-day cycle, derived from the cycle
length rather than fitted.

**Caught by the mutation audit — a metric that flattered us.**
`mandates_saved` counted every early stop as a
saved mandate, including mandates whose VPA no longer resolved. Stopping early
there is still right — it saves three wasted attempts — but it does not save a
customer.

**Caught by a reachability audit — a third of the taxonomy was dead code.**
`classify()` accepted an NPCI
response code with documented mappings for Z9, Z8, U28, U30 and U69, and
nothing ever passed one. Z8 is terminal; the engine could not act on it because
it never saw it.

**Caught by instrumenting the engine — two failure modes were inert.**
A probe counting which classes actually
reached the engine found `customer_absent` and `issuer_down` — 17% of the
cohort — never arrived. One was silently reclassified by an ambiguous rail
code; the other cleared on its first attempt unless a rare random outage
happened to land on the exact debit day.

**Caught by auditing the parameter file against the code — two promises it
made that nothing kept.** `cohort.yaml` declared 20 independent replications and a decline-mix
sensitivity range, and for a while neither was implemented — the benchmark ran
a single cohort and swept only payday. Both are now real, and the replications
changed what can honestly be claimed: the pre-registered seed's +49.2% sits
above a mean of +38.8%.

**Caught by a cross-check between two paths that should agree — a bug in the
measuring instrument.** The sensitivity sweep shifted
day-of-month values modulo 30 across a 1–31 space, folding day 31 onto day 1
even at shift zero. It reported wrong numbers without failing, which is the
worst kind of bug, and it had contaminated every sweep figure published up to
that point.

None of these six were found by the test suite alone. That is the argument for
the layered verification above: a passing suite tells you the checks you thought
of are satisfied, and every defect here was in something nobody had thought to
check yet.

---

## Known limitations

- **The four-attempt cap is read as per cycle, on corroboration rather than a
  primary citation.** Four independent secondary readings agree and are
  specific: one execution attempt plus three retries per mandate "based on its
  sequence number", a sequence number being the individual execution within a
  recurring series. The primary circular went to NPCI members rather than the
  public circulars page. `mandates_saved` depends on the reading; the
  per-lifetime alternative would flatter this engine rather than damage it,
  since unspent attempts would carry forward.
- **The cohort is synthetic.** No public dataset gives decline-reason
  breakdowns for Indian recurring debits. Distributions are pre-registered,
  sourced where possible, marked `assumed` where not, and swept where they
  matter.
- **NPCI response codes are usually absent in practice.** Razorpay's
  error-mapping layer translates them into its own `error_reason` before the
  webhook fires. The path is exercised by the simulator and by direct PSP
  integrations; the engine works without it.
- **One execution cycle is simulated, not a subscription lifetime.**
  Multi-cycle effects — a saved mandate recovering next month — are argued,
  not measured.
- **The triage descriptions were written by us**, so that measurement
  demonstrates the mechanism rather than field accuracy. The live rail is where
  it gets validated — and it has already produced one real unmapped code with
  a real description.
