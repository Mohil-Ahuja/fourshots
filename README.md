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
| recovery rate | 44.0% | **62.9%** | +42.8% |
| recovered | ₹49,72,579 | **₹75,28,138** | +51.4% |
| mandates saved | 881 | **1,600** | +81.6% |
| attempts spent | 5,842 | **4,058** | −30.5% |
| attempts per recovery | 6.63 | **3.23** | −51.4% |

**More money and more mandates from 30% fewer attempts**, on the same
regulator-capped budget. Attempts spent on debits that could never clear fall
from 1,508 to 453.

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
individual losses. **76 mandates worth ₹2,26,020 were recovered by the baseline
and not by the engine** — mostly customer-absent declines, where the engine
escalates to a person while the baseline retries blindly and sometimes gets
lucky. Against ₹27,81,579 gained on 453 mandates, a 12.3x ratio. Worth taking,
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

**The tests bite.** `python tools/mutation_audit.py` introduces twelve
deliberate defects — regulatory constants that no longer match the circulars,
compliance checks switched off, signature verification disabled, the engine
losing its terminal-stop — and requires the suite to fail on each. 12/12 caught.
Coverage is 98%, but coverage only proves lines ran.

## Two things we got wrong, and how we found out

Both are in the git history, and both matter more than the headline.

**The sensitivity sweep killed our first engine.** It held a prior about Indian
payroll and aimed each balance retry at the next plausible payday. It scored
+41%. Then shifting the *world's* payday distribution three days while the
prior stayed fixed degraded the advantage to **+8.7%** — the engine was being
told the answer more than reading the world. Spreading attempts evenly across
the cycle needs no payday belief at all and holds at **+46.5% worst case**. The
offsets are even thirds of a ~30-day cycle, derived from the cycle length
rather than fitted to the cohort.

**A third of the taxonomy was dead code.** `classify()` accepted an NPCI
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

## Running it

```bash
pip install -e ".[dev]"

pytest -q                          # 192 tests
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

## Layout

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

**Where it earns its place:** reading the natural-language `error_description`
attached to codes the taxonomy cannot map. That gap is measured, not assumed —
unreadable codes currently get one cautious attempt and then stop, and the
results report what that caution costs. It is the one place deterministic code
genuinely cannot help, and it is bounded so it can never widen what the engine
is permitted to do.

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
