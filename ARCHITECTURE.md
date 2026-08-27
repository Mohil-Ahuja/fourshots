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

```mermaid
flowchart LR
    subgraph live["Live service"]
        RZP[Razorpay webhook] -->|raw bytes| VER[verify HMAC]
        VER -->|rejected: no body kept| LOG
        VER -->|verified| PARSE[parse event]
        PARSE --> CLS
    end

    subgraph domain["Domain model — shared"]
        CLS[taxonomy.classify]
        POL[policy.check_legality]
    end

    subgraph bench["Experiment harness"]
        WORLD[(World: ground truth)] -->|decline code only| OBS[Observation]
        OBS --> ENG[engine / baseline]
        ENG -->|proposed attempt| POL
        POL -->|legal instant| WORLD
    end

    CLS --> ENG
    CLS --> LOG[(hash-chained audit log)]
    POL --> LOG
    ENG --> LOG
```

The dashed edge that does **not** exist is the important one: nothing runs from
`World` to `engine`. That absence is the information barrier, and it is
asserted structurally in `tests/test_simulator.py`.

---

## Modules

| Module | Responsibility |
|---|---|
| `taxonomy.py` | Decline codes → retry-relevant classes. Carries provenance per mapping. |
| `policy.py` | The NPCI/RBI constraint lattice. Deterministic, no model, no heuristic. |
| `simulator.py` | The cohort and its ground truth, behind the information barrier. |
| `policies.py` | The baseline arm: Razorpay's documented default. |
| `engine.py` | The constraint-aware arm. |
| `triage.py` | The only place a language model is consulted. |
| `runner.py` | The harness both arms pass through, identically. |
| `audit.py` | Append-only hash-chained decision log. |
| `webhook.py`, `app.py` | Live Razorpay ingestion. |
| `benchmark.py`, `figures.py` | Reproduce and publish the results. |
| `params.py` | Loads and validates the pre-registered cohort parameters. |

---

## The five boundaries that matter

Everything else is ordinary code. These five are where a reviewer should push.

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

The model does one thing a lookup table cannot: read the prose a rail attaches
to a decline code the taxonomy has never seen. It proposes a **classification,
never a schedule**, from a closed set; the engine then does exactly what it
always does with that class.

Four bounds, each tested:

- A verdict naming a class that does not exist is discarded, not coerced to the
  nearest match — a model inventing a category is a reason to distrust it.
- A verdict below the confidence floor is discarded. The prompt offers an
  explicit way to answer "I cannot tell", because a confident wrong answer
  costs an attempt that cannot be recovered.
- Any failure — credentials, network, malformed response — returns nothing and
  the conservative default stands. An outage at the model provider must not
  change when someone's account is debited.
- Verdicts are cached and committed, so benchmark runs stay reproducible and
  every model judgement behind the result can be read and disputed.

With no API key the null triager runs and behaviour is identical to before the
layer existed. **Measured ceiling: +1.76%**, against +49.2% from the
deterministic work.

### 5. The audit log commits to its own history

Each entry carries the hash of its predecessor, so editing a recorded amount,
forging an entry's own digest, or splicing an entry out all fail `verify()`.
Crash-truncation deliberately does not — the surviving prefix is still a valid
chain, and flagging it would cry wolf.

The reason this matters is not tidiness. The headline is a comparison between
two policies, and a reader is entitled to ask whether the losing arm's log was
tidied afterwards. A chain that verifies is the answer.

---

## Verification

Six layers, because each catches what the others miss.

| Layer | What it establishes | What it cannot |
|---|---|---|
| 243 tests | Behaviour matches intent | That the tests assert anything |
| 98% coverage | Lines execute | That a bug in them is caught |
| `tools/mutation_audit.py` | 12 deliberate defects all fail the suite | That untested behaviour exists elsewhere |
| `tests/test_published_numbers.py` | Prose quotes current figures | That the figures are right |
| 20 replications | The effect is not one lucky seed | That the cohort model is right |
| Two sensitivity sweeps | It survives payday and decline-mix assumptions | That other assumptions hold |

The mutation audit is the load-bearing one. A suite at 98% coverage can assert
almost nothing; introducing real defects — a regulatory constant that no longer
matches the circular, signature verification switched off, the engine losing
its terminal-stop — and requiring the suite to fail on each is what shows the
tests bite. Its first run caught 11 of 12, and the miss was the metric most
exposed to challenge.

CI runs all four, plus the benchmark itself, so a result that stops reproducing
breaks the build.

---

## What was got wrong

Recorded because the corrections are more informative than the result, and all
of them are in the git history.

**A payday prior that was being told the answer.** The first engine aimed each
balance retry at a hardcoded Indian-payroll prior and scored well. Shifting the
*world's* payday three days while the prior stayed fixed degraded it to +9.9%.
Spreading attempts evenly needs no payday belief and holds at +46.9% worst
case. The offsets are even thirds of a ~30-day cycle, derived from the cycle
length rather than fitted.

**A metric that flattered us.** `mandates_saved` counted every early stop as a
saved mandate, including mandates whose VPA no longer resolved. Stopping early
there is still right — it saves three wasted attempts — but it does not save a
customer.

**A third of the taxonomy was unreachable.** `classify()` accepted an NPCI
response code with documented mappings for Z9, Z8, U28, U30 and U69, and
nothing ever passed one. Z8 is terminal; the engine could not act on it because
it never saw it.

**Two failure modes were inert.** A probe counting which classes actually
reached the engine found `customer_absent` and `issuer_down` — 17% of the
cohort — never arrived. One was silently reclassified by an ambiguous rail
code; the other cleared on its first attempt unless a rare random outage
happened to land on the exact debit day.

**Two promises the parameter file made and the code did not keep.**
`cohort.yaml` declared 20 independent replications and a decline-mix
sensitivity range, and for a while neither was implemented — the benchmark ran
a single cohort and swept only payday. Both are now real, and the replications
changed what can honestly be claimed: the pre-registered seed's +49.2% sits
above a mean of +38.8%.

**A bug in the measuring instrument.** The sensitivity sweep shifted
day-of-month values modulo 30 across a 1–31 space, folding day 31 onto day 1
even at shift zero. It reported wrong numbers without failing, which is the
worst kind of bug, and it had contaminated every sweep figure published up to
that point.

---

## Known limitations

- **The four-attempt cap may be per cycle or per mandate lifetime.** Sources
  describe it as per mandate "identified by each sequence number", which reads
  as per-cycle. `mandates_saved` depends on that reading and it is not yet
  confirmed against the primary NPCI circular.
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
