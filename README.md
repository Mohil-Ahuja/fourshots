# fourshots

**NPCI allows four attempts per mandate cycle. This decides how to spend them.**

Razorpay AI Buildathon 2026 — Track 03, AI Revenue Recovery.

---

## The problem

When a UPI AutoPay debit fails, NPCI allows exactly **four executions per
mandate cycle** — one original plus three retries, in force since 1 August
2025. After the fourth, the cycle is cancelled.

Razorpay's documented retry policy spends them on consecutive days. From the
[Subscriptions documentation](https://razorpay.com/docs/payments/subscriptions/payment-retries/):

> "We automatically retry the payment on the following day."

No variation by failure reason. No merchant configurability.

So consider a debit that fails on the 26th because the account is empty.
Attempts two, three and four land on the 27th, 28th and 29th — every one of
them on a day the account is *still* empty, because salary does not arrive
until the 1st. The budget is gone before the money shows up, and the mandate
is cancelled.

That is the mechanical explanation for a documented industry figure: roughly
**20 million UPI AutoPay revocations per month**, driven by low customer
balances ([Business Standard, Sept 2025](https://www.business-standard.com/finance/news/upi-autopay-revocations-hit-20-mn-monthly-over-low-customer-balances-125090700500_1.html)).

The same blindness wastes attempts at the other end. A mandate whose VPA no
longer resolves, or whose amount breaches a per-transaction cap, announces on
attempt one that no retry can ever succeed. The documented policy retries it
three more times anyway.

![One balance curve, two attempt rows. The documented policy spends all four
attempts on 26–29 September while the account is empty and the cycle is
cancelled; fourshots spends one, waits, and clears on 4 October with two
attempts still in hand.](docs/four-attempts.svg)

The documented policy never reads the decline code, so it cannot tell a balance
shortfall from a dead mandate — and never asks when money might arrive.
fourshots spreads its attempts across the cycle instead. It holds no belief
about payday; even spacing means an attempt lands soon after money arrives,
whenever that is.

## The problem is not "retry smarter"

Four constraints compose into something more interesting than a retry loop:

1. **A hard budget.** Four executions per cycle, then cancellation.
2. **Quantised timing.** AutoPay executes only outside UPI peak hours —
   before 10:00, 13:00–17:00, and after 21:30 IST. Roughly three slots a day.
3. **Commitment delay.** The RBI *Digital Payments — E-mandate Framework, 2026*
   (notified 21 April 2026) requires pre-debit notification at least 24 hours
   ahead, with amount, date and time. **Each attempt must be booked a day
   before it runs — before you know whether the money will be there.**
4. **Threshold authentication.** AFA above ₹15,000 (₹1,00,000 for insurance,
   SIP and credit-card mandates) cannot be satisfied by a silent retry.

That is a constrained sequential decision problem with commitment delay. The
decline code from attempt *N* is the only evidence available for scheduling
attempt *N+1*.

## Result

2000 mandates, seed `20260827`, September 2026:

| | Razorpay documented default | fourshots | delta |
|---|---|---|---|
| recovery rate | 44.1% | **62.8%** | +42.4% |
| recovered | ₹49,85,233 | **₹74,39,249** | +49.2% |
| mandates saved | 883 | **1,601** | +81.3% |
| attempts spent | 5,857 | **4,057** | −30.7% |
| attempts per recovery | 6.63 | **3.23** | −51.3% |

**More money and more mandates from 30% fewer attempts**, on the same
regulator-capped budget. Attempts spent on debits that could never clear fall
from 1,508 to 453.

### Across 20 independent cohorts

One seed cannot distinguish a real effect from a favourable draw, so the
parameter file declares 20 replications. `--replicate` runs them:

| | mean | min | max |
|---|---|---|---|
| baseline recovered | ₹54,18,881 | ₹43,78,967 | ₹61,75,896 |
| fourshots recovered | ₹75,03,378 | ₹61,93,192 | ₹84,42,044 |
| **advantage** | **+38.8%** | +28.0% | +54.1% |

**The engine is ahead in 20 of 20 replications.** Note that the single
pre-registered seed above (+49.2%) sits on the *favourable* side of that
distribution — the mean is the number to argue from, and the 20-of-20 is the
claim that actually settles it.

### Where it is worse

The headline is a net figure, and net figures hide things. Two costs are real,
and `python -m fourshots.benchmark` prints them on every run so the trade-off
travels with the result:

**Cash-flow lag.** The engine deliberately waits — for money to arrive, for an
outage to clear — so recoveries land later.

| days from due date to recovery | median | mean | p90 | max |
|---|---|---|---|---|
| baseline | 0.0 | 0.6 | 1.0 | 3.0 |
| fourshots | 1.0 | **6.0** | **16.0** | **25.0** |

A merchant feels that as delayed cash even when the total is higher. Whether
+51% recovered is worth a six-day mean delay is the merchant's call, not ours
— but it should be made with the number in view.

**Mandate-level regressions.** Aggregate improvement is compatible with
individual losses. **78 mandates worth ₹3,29,169 were recovered by the baseline
and not by the engine** — mostly customer-absent declines, where the engine
escalates to a person while the baseline retries blindly and sometimes gets
lucky. Against ₹27,83,185 gained on 452 mandates, an 8.5x ratio. Worth taking,
and still a real loss.

Reproduce with `python -m fourshots.benchmark` (see below). The seed is fixed,
so these exact numbers should appear on your machine.

## Why you should believe the number

The obvious objection to any result like this is *you built the simulator and
you built the optimizer*. Four answers, all checkable:

**The baseline is real.** The control arm is Razorpay's shipped, documented
policy, read from the pre-registered parameter file with its citation attached
— not a strawman. It spends the full four-attempt budget and passes through the
same legality gate as the engine. It is not handicapped; it simply proposes
worse attempts.

**The engine cannot see the world.** A policy receives an `Observation`
carrying only what a merchant learns from a webhook — decline code, timestamp,
amount, attempts used. It never touches the balance trajectory, payday, or the
true failure mode. `tests/test_simulator.py::test_policy_cannot_reach_ground_truth`
asserts this on the dataclass fields, so a leaky field added later fails the
suite rather than quietly invalidating the benchmark.

**The parameters were pre-registered.** `params/cohort.yaml` was committed
*before* the simulator, both policy arms, and any result existed — see the git
history. Every parameter declares its provenance, and most are marked `assumed`
with reasoning rather than dressed up with an invented citation; no primary
source publishes a decline-reason breakdown for failed recurring debits.

**The assumptions are swept, including the one that hurts.** The parameter file
names the balance share as what the headline is most exposed to, and sweeping
it across the declared 0.35–0.75 range shows exactly that:

| balance share | 0.35 | 0.45 | **0.55** | 0.65 | 0.75 |
|---|---|---|---|---|---|
| advantage | **+8.0%** | +20.3% | **+49.2%** | +54.7% | +65.8% |

At the low end the advantage falls to +8.0%. The engine still wins everywhere
in the range, but the size of the win depends substantially on balance failures
being the dominant mode — which is an assumption, and is labelled as one.

**The tests bite.** `python tools/mutation_audit.py` introduces twelve
deliberate defects — regulatory constants that no longer match the circulars,
compliance checks switched off, signature verification disabled, the engine
losing its terminal-stop — and requires the suite to fail on each. 12/12 caught.
Coverage is 98%, but coverage only proves lines ran.

## What the checks caught

Three defects the verification found before they reached a published number.
They are listed because each is evidence that a specific check works — a claim
that cannot be made by asserting the code is correct, only by showing what the
checks stopped. All are in the git history;
[`ARCHITECTURE.md`](ARCHITECTURE.md#what-the-checks-caught) has the full six.

**The sensitivity sweep killed our first engine.** It held a prior about Indian
payroll and aimed each balance retry at the next plausible payday. It scored
+41%. Then shifting the *world's* payday distribution three days while the
prior stayed fixed degraded the advantage to **+9.9%** — the engine was being
told the answer more than reading the world. Spreading attempts evenly across
the cycle needs no payday belief at all and holds at **+46.9% worst case**. The
offsets are even thirds of a ~30-day cycle, derived from the cycle length
rather than fitted to the cohort.

**A reachability audit found a third of the taxonomy was dead code.**
`classify()` accepted an NPCI
response code with documented mappings for Z9, Z8, U28, U30 and U69. Nothing
ever passed one. That mattered: Z8 states a breached limit, which is terminal,
and the engine could not act on it because it never saw it.

**Two failure modes were inert, and a probe found them.** Instrumenting which
decline classes actually reached the engine showed `customer_absent` and
`issuer_down` — 17% of the declared cohort — never arrived at all. The first
was silently reclassified because the simulator tagged it with NPCI code U69
while the taxonomy read U69 as rail-transient. The second cleared on its first
attempt unless a random 0.8%-per-day outage happened to land on that exact
day, so the mode was effectively decorative. Both are fixed; U69 now yields to
a definite aggregator code, because its own documentation spans two situations
needing opposite responses.

## Live integration

This is not only a simulation. The service ingests real Razorpay webhooks:

- HMAC-SHA256 signature verified against the **raw request bytes** with
  `compare_digest`. Unverified payloads are rejected and never recorded — only
  the fact of rejection is. A missing secret fails closed.
- `payment.failed` is classified through the taxonomy on arrival.
- `payment.downtime.*` converts an inference about issuer outages into an
  observation, which is worth an attempt from a scarce budget.
- Every decision lands in an append-only, hash-chained audit log. Editing an
  entry, forging its digest, or splicing one out all fail `verify()`.

A real test-mode payment is what surfaced our first unmapped code
(`international_transaction_not_allowed`). The system degraded to its
conservative class rather than crashing or guessing — and the synthetic cohort
would never have produced that code.

## The Track 03 bar, clause by clause

Razorpay states the bar for this track as one sentence:

> "Show measured money recovered across a batch, with compliant escalation,
> stopping rules, and an audit trail."

Each clause is a component rather than a paragraph:

| clause | where it lives | what makes it checkable |
|---|---|---|
| **measured money recovered** | `benchmark.py` | Against Razorpay's own documented policy as the control arm, on a fixed seed, with the losses printed too |
| **across a batch** | `params/cohort.yaml` | 2000 mandates, and 20 independent replications so one lucky draw cannot carry the claim |
| **compliant escalation** | `policy.py`, `engine.py` | The NPCI/RBI lattice gates every attempt in both arms; a debit that cannot clear silently escalates to a person instead of burning budget |
| **stopping rules** | `engine.py`, `runner.py` | Terminal decline classes stop the cycle; 470 escalations replace 1,055 attempts on debits that could never clear |
| **audit trail** | `audit.py` | Append-only and hash-chained. Editing an entry, forging its digest, or splicing one out all fail `verify()` |

The one deliberate departure: the track's framing invites an agent that decides
*and acts*. The scheduling decision here is deterministic and the AI layer is
confined to reading prose, for the reason set out below — a model choosing when
to debit a stranger's account is the part of this problem that cannot be
audited, and the measured value of putting one there is +1.76%.

## Running it

```bash
pip install -e ".[dev]"

pytest -q                          # 257 tests
python tools/mutation_audit.py     # 12/12 mutations caught
python -m fourshots.benchmark      # reproduce the headline table
```

To receive live webhooks:

```bash
cp .env.example .env               # add rzp_test_ keys and a webhook secret
uvicorn fourshots.app:app --port 8000
ngrok http 8000                    # point the dashboard at /webhooks/razorpay
```

Test mode only. No real funds, no real PII.

## The rules are written twice

`policy.py` decides whether a debit against someone's account is permitted at
all. Everywhere else in this system a bug shows up as a worse number; there it
shows up as an illegal attempt the benchmark still counts as fine, and no
aggregate metric would reveal it.

So the constraint lattice is implemented a second time in Rust
(`rust/src/lib.rs`), written from the circulars rather than from the Python, and
`tests/test_differential.py` requires the two to return identical answers —
exhaustively across all 1440 minutes of the day, and across 20,000 randomly
generated gate cases skewed toward the boundaries where an off-by-one hides.

**The Rust is not on the shipped path.** It is an oracle, not a faster backend:
the benchmark runs in seconds, so there is no performance problem to solve, and
swapping implementations by environment would mean behaviour differing
depending on whether an extension happened to compile. Python remains the only
execution path; without the toolchain the differential test skips and nothing
else changes.

```bash
cd rust && cargo test --lib          # 7 tests, rules stated against the circulars
maturin build --release
pip install rust/target/wheels/fourshots_rules-*.whl
pytest tests/test_differential.py    # 11 tests, the two implementations agree
```

Two independent implementations agreeing across the whole input space is
evidence. One implementation passing its own tests is an assertion.

## Layout

[`ARCHITECTURE.md`](ARCHITECTURE.md) covers the boundaries and why each sits
where it does — the information barrier, how both arms are kept comparable,
where AI is deliberately absent, and the six defects the checks caught along the
way.

| module | role |
|---|---|
| `taxonomy.py` | decline codes → retry-relevant classes, with provenance per mapping |
| `policy.py` | the NPCI/RBI constraint lattice. Deterministic, no model, no heuristic |
| `simulator.py` | the cohort and its ground truth, behind the information barrier |
| `policies.py` | the baseline arm: Razorpay's documented default |
| `engine.py` | the constraint-aware arm |
| `runner.py` | the harness both arms run through, identically |
| `audit.py` | append-only hash-chained decision log |
| `webhook.py` / `app.py` | live Razorpay ingestion |

## Where AI is used, and where it deliberately is not

**Not in the scheduling path.** Choosing when to debit someone is a money
decision. A language model there cannot be tested exhaustively, cannot be
audited line by line, and fails by producing a plausible-sounding wrong date.
Attempt accounting, window legality, notice periods, AFA thresholds and
terminal detection are all deterministic and hand-checkable.

**Where it earns its place:** reading the natural-language description a rail
attaches to codes the taxonomy cannot map. A lookup table cannot read prose; a
model can. `triage.py` asks Claude to pick from the *closed set* of failure
classes the taxonomy already defines, and the deterministic engine then does
exactly what it always does with that class.

The bounding is the design:

- The model proposes a **classification, never a schedule**. Same attempt
  budget, same execution windows, same notice period, same AFA thresholds.
- A verdict below the confidence floor is discarded, and so is one naming a
  class that does not exist — a model inventing a category is a reason to
  distrust it, not to snap to the nearest match. The prompt gives it an
  explicit way to say "I cannot tell", because a confident wrong answer costs
  an attempt that cannot be recovered.
- Any failure — no credentials, network, malformed response — returns nothing
  and the conservative default stands. An outage at the model provider must not
  change scheduling behaviour.
- Verdicts are **cached to a file meant to be committed**, so a benchmark run is
  reproducible and every model judgement behind a result can be read and
  disputed rather than taken on trust. **This repository ships no cache**, so
  the default run is triage-free and the layer is inert — the benchmark says so
  on every run rather than leaving you to infer it. `python -m fourshots.triage`
  populates it from a live model if you supply `ANTHROPIC_API_KEY`, and
  `tests/test_triage.py::test_shipped_cache_state_matches_what_the_docs_claim`
  fails if this paragraph and the repository ever disagree.

**And it is worth about +1.76%.** We measured the ceiling with an oracle that
reads the prose perfectly: `python -m fourshots.benchmark` prints it every run.
Any real model scores at or below that. The gap is small because unreadable
codes are only ~2% of the cohort.

That number is the honest answer to "how much is the AI doing here": the
deterministic constraint work delivers +51%, the AI layer adds up to +1.8% on
top. Inflating that would have been the easiest claim in the project and the
least defensible one.

## Honest limitations

- **The 4-attempt cap may be per cycle or per mandate lifetime.** Sources
  describe it as per mandate "identified by each sequence number", which reads
  as per-cycle. The `mandates_saved` metric depends on this reading, and it is
  not yet confirmed against the primary NPCI circular.
- **The cohort is synthetic.** No public dataset gives decline-reason
  breakdowns for Indian recurring debits. Distributions are declared, sourced
  where possible, marked `assumed` where not, and swept where they matter.
- **NPCI response codes are usually absent in practice.** Razorpay's
  error-mapping layer translates them into its own `error_reason` before the
  webhook fires. The path is exercised by the simulator and by direct PSP
  integrations; the engine works without it.
- **Balance is modelled as a payday credit decaying monthly**, using a
  30-day modulus. It reproduces the mechanism that matters — a late-cycle debit
  meets a thinner account — but it is an approximation, not a cash-flow model.
- **One execution cycle is simulated, not a subscription lifetime.** Multi-cycle
  effects (a saved mandate recovering next month) are argued, not measured.
