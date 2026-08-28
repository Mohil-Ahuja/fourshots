"""The one place a language model earns its keep.

Where this sits
---------------
The engine never asks a model when to debit someone. That is a money decision:
it must be testable exhaustively, auditable line by line, and its failure mode
must not be a plausible-sounding wrong date. Scheduling stays deterministic.

What a model can do that deterministic code cannot is read prose. When a rail
returns a code the taxonomy has never seen, it usually also returns a sentence
saying what went wrong -- "the account has been frozen by the issuing bank",
"the mandate was revoked by the customer". A lookup table cannot read that. A
model can, and the gap is measured rather than assumed: unreadable codes
currently receive one cautious attempt and then stop.

The bounding, which is the point
--------------------------------
The model proposes a *classification*, never a schedule. It picks from the
closed set of failure classes the taxonomy already defines. Whatever it
returns, the deterministic engine then does exactly what it always does with
that class -- same attempt budget, same execution windows, same notice period,
same AFA thresholds. The model can narrow uncertainty; it cannot widen
authority.

Three further limits:

- A verdict below `min_confidence` is discarded and the conservative default
  stands. Declining to answer is a valid outcome.
- A class name outside the known set is discarded, not coerced to the nearest
  match. A model inventing a category is a signal to distrust the verdict.
- Verdicts are cached by code and committed to the repository, so a benchmark
  run is reproducible and every model judgement the result depends on can be
  read, disputed, and re-run.

Without an API key, `NullTriager` is used and behaviour is exactly what it was
before this module existed. The AI layer is an enhancement, never a dependency.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from fourshots.taxonomy import ALL_CLASSES, FailureClass

MODEL = "claude-opus-5"

DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "params" / "triage_cache.json"

# The closed set the model must choose from. UNCLASSIFIED is included so the
# model has an explicit way to say "I cannot tell" rather than being forced
# into a guess -- an escape hatch that removes the incentive to confabulate.
_CLASS_BY_NAME: dict[str, FailureClass] = {c.name: c for c in ALL_CLASSES}

SYSTEM_PROMPT = """\
You classify payment decline messages from Indian payment rails (UPI, cards, \
NACH) so a retry scheduler can decide whether spending one of its four \
regulator-capped attempts could ever succeed.

You are given a decline code and the human-readable description a rail \
returned with it. Decide which failure class it belongs to.

The classes, and what each asserts about a future retry:

- insufficient_balance: the account lacked funds. A retry can succeed once \
money arrives, which is driven by payroll timing, not elapsed hours.
- issuer_down: the customer's bank was unavailable. Clears in hours.
- psp_transient: the payment rail or aggregator had a transient fault. Clears \
in minutes to hours.
- customer_absent: the customer had to act and did not (an expired collect \
request, an abandoned approval). A silent retry has nobody to answer it.
- auth_required: additional authentication is needed. A silent retry cannot \
supply it.
- mandate_dead: the mandate or its identifier no longer exists or is revoked. \
No retry can succeed.
- limit_breach: the amount exceeds a per-transaction or periodic cap. The same \
amount can never clear, however long you wait.
- instrument_rejected: the card or account is unusable at this merchant (wrong \
geography, unsupported type). The same instrument can never clear.
- unclassified: you cannot tell with confidence.

Rules:
- Choose `unclassified` whenever the description is vague, generic, or spans \
several of the above. Saying you do not know is correct and useful; a wrong \
confident answer costs a real attempt that cannot be recovered.
- Judge only from the code and description given. Do not infer from what would \
be convenient.
- Confidence is your probability that the class is right, from 0 to 1.\
"""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "failure_class": {"type": "string", "enum": sorted(_CLASS_BY_NAME)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string", "maxLength": 400},
    },
    "required": ["failure_class", "confidence", "reasoning"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class TriageVerdict:
    """One model judgement about one decline code.

    Persisted verbatim, including the reasoning and the model that produced it,
    so a reader can audit the judgements the headline number rests on instead
    of taking them on trust.
    """

    code: str
    failure_class: str
    confidence: float
    reasoning: str
    model: str

    def resolved(self) -> FailureClass | None:
        """The class this verdict names, or None if it names nothing known."""
        return _CLASS_BY_NAME.get(self.failure_class)


class Triager(Protocol):
    """Reads a decline description and proposes a class, or declines to."""

    def triage(self, code: str, description: str | None) -> TriageVerdict | None:
        ...


class NullTriager:
    """The offline default: never proposes anything.

    With this in place the engine behaves exactly as it did before the AI layer
    existed -- unreadable codes get one cautious attempt and stop. That is what
    makes the layer an enhancement rather than a dependency, and it is what
    runs when no API key is configured.
    """

    name = "null"

    def triage(self, code: str, description: str | None) -> TriageVerdict | None:
        return None


class CachedTriager:
    """Serves verdicts from a committed cache; never calls out.

    This is what the benchmark uses. A live API call inside a benchmark would
    make the headline number irreproducible and would spend money to re-derive
    an answer that has not changed. Populate the cache with `refresh_cache()`,
    commit it, and every subsequent run is deterministic.
    """

    name = "cached"

    def __init__(self, cache_path: Path | str = DEFAULT_CACHE, min_confidence: float = 0.7):
        self.cache_path = Path(cache_path)
        self.min_confidence = min_confidence
        self._verdicts: dict[str, TriageVerdict] = {}
        if self.cache_path.exists():
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            for entry in raw.get("verdicts", []):
                verdict = TriageVerdict(**entry)
                self._verdicts[verdict.code] = verdict

    def triage(self, code: str, description: str | None) -> TriageVerdict | None:
        verdict = self._verdicts.get(code)
        if verdict is None:
            return None
        return verdict if _acceptable(verdict, self.min_confidence) else None

    def __len__(self) -> int:
        return len(self._verdicts)


class ClaudeTriager:
    """Asks Claude to classify a decline description.

    Used to populate the cache, not inside the benchmark. Any failure -- no
    credentials, a network error, a malformed response -- returns None and the
    conservative default stands. A triager that raises would make the AI layer
    a dependency, which is exactly what it must not be.
    """

    name = "claude"

    def __init__(self, client=None, min_confidence: float = 0.7, model: str = MODEL):
        self.min_confidence = min_confidence
        self.model = model
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def triage(self, code: str, description: str | None) -> TriageVerdict | None:
        if not description:
            # Nothing to read. A model given only a code it has never seen is
            # guessing from the string, which is not what this layer is for.
            return None

        try:
            response = self._ensure_client().messages.create(
                model=self.model,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": _RESPONSE_SCHEMA,
                    },
                    "effort": "low",
                },
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Decline code: {code}\n"
                            f"Description returned by the rail: {description}"
                        ),
                    }
                ],
            )
        except Exception:
            # Deliberately broad. Whatever went wrong -- auth, network, schema,
            # rate limit -- the answer is the same: fall back to conservative
            # handling rather than let an outage change scheduling behaviour.
            return None

        payload = _first_json_object(response)
        if payload is None:
            return None

        verdict = TriageVerdict(
            code=code,
            failure_class=str(payload.get("failure_class", "")),
            confidence=float(payload.get("confidence", 0.0)),
            reasoning=str(payload.get("reasoning", ""))[:400],
            model=self.model,
        )
        return verdict if _acceptable(verdict, self.min_confidence) else None


def _first_json_object(response) -> dict | None:
    """Pull the structured payload out of a response, tolerating shape drift."""
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) != "text":
            continue
        try:
            parsed = json.loads(block.text)
        except (ValueError, AttributeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _acceptable(verdict: TriageVerdict, min_confidence: float) -> bool:
    """Whether a verdict may influence scheduling.

    Three ways to be rejected, and all of them leave the conservative default
    in place: an unknown class name (a model inventing a category is a reason
    to distrust it, not to guess at the nearest match), an explicit
    `unclassified`, or confidence below the floor.
    """
    if verdict.resolved() is None:
        return False
    if verdict.failure_class == "unclassified":
        return False
    return verdict.confidence >= min_confidence


def refresh_cache(
    codes: dict[str, str],
    cache_path: Path | str = DEFAULT_CACHE,
    triager: Triager | None = None,
) -> int:
    """Classify each `code -> description` and write the verdicts to disk.

    Run this deliberately, with credentials, when new unmapped codes appear.
    The output is committed so benchmark runs stay reproducible and every model
    judgement is auditable. Returns the number of verdicts written.
    """
    engine = triager or ClaudeTriager()
    verdicts: list[TriageVerdict] = []

    for code, description in sorted(codes.items()):
        verdict = engine.triage(code, description)
        if verdict is not None:
            verdicts.append(verdict)

    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "note": (
                    "Model verdicts on decline codes the taxonomy cannot map. "
                    "Committed so benchmark runs are reproducible and every "
                    "judgement the result depends on can be audited. "
                    "Regenerate with fourshots.triage.refresh_cache()."
                ),
                "model": getattr(engine, "model", MODEL),
                "verdicts": [asdict(v) for v in verdicts],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(verdicts)


def default_triager() -> Triager:
    """Cached verdicts when any exist, otherwise the offline no-op."""
    cached = CachedTriager()
    return cached if len(cached) else NullTriager()


def credentials_available() -> bool:
    """Whether a live triager could authenticate.

    Only checks the environment variable; the SDK resolves other sources too,
    so a False here is a hint, not a guarantee of failure.
    """
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def unmappable_codes() -> dict[str, str]:
    """The decline codes in the pre-registered cohort that the taxonomy cannot
    map, paired with the prose the rail returned alongside them.

    This is exactly the input the triage layer exists to handle, so deriving it
    from the cohort rather than hand-listing it keeps the cache honest: if a
    later taxonomy change makes a code readable, it stops appearing here and the
    stale verdict can be dropped.
    """
    import random

    from fourshots.params import load
    from fourshots.simulator import World, build_cohort
    from fourshots.taxonomy import UNCLASSIFIED, classify
    from fourshots.policy import IST
    from datetime import datetime

    params = load()
    rng = random.Random(params.seed)
    cohort = build_cohort(params, rng)
    world = World(params, rng)
    at = datetime(2026, 9, 26, 9, 0, tzinfo=IST)

    found: dict[str, str] = {}
    for mandate in cohort:
        result = world.attempt(mandate, at, at)
        if result.cleared:
            continue
        if classify(result.razorpay_code, result.npci_code).failure_class is UNCLASSIFIED:
            found.setdefault(result.razorpay_code, result.description or "")
    return found


def _main() -> int:
    """Populate the committed verdict cache.

    Deliberately a separate command rather than something the benchmark does on
    its own. A benchmark that silently calls a model is a benchmark whose number
    depends on the weather at the provider; this one only ever reads a file
    somebody chose to commit.
    """
    import argparse

    parser = argparse.ArgumentParser(description=_main.__doc__)
    parser.add_argument(
        "--codes",
        type=Path,
        help="JSON object of code -> description. Defaults to the codes the "
        "pre-registered cohort produces that the taxonomy cannot map.",
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args()

    if not credentials_available():
        print(
            "ANTHROPIC_API_KEY is not set, so there is nothing to refresh.\n"
            "Exiting non-zero because the refresh you asked for did not happen "
            "-- not because anything is broken. Without a key the engine runs "
            "the null triager and behaves exactly as it does with no AI layer."
        )
        return 1

    codes = (
        json.loads(args.codes.read_text(encoding="utf-8"))
        if args.codes
        else unmappable_codes()
    )
    if not codes:
        print("No unmappable codes found. Nothing to cache.")
        return 0

    written = refresh_cache(codes, cache_path=args.cache)
    print(f"Wrote {written} verdict(s) to {args.cache}. Commit the file.")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(_main())
