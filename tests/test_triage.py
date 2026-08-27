"""Tests for the model-backed triage layer.

The layer's value is small and its risk is not, so almost every test here is
about the bounding rather than the capability: a model verdict must be able to
narrow a classification and must never be able to widen what the engine is
permitted to do, bypass a regulatory constraint, or turn an outage into a
scheduling change.

No test calls the API. `ClaudeTriager` is exercised through an injected stub,
which is what lets the suite run offline and in CI without credentials.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fourshots.engine import ConstraintAwareEngine
from fourshots.policy import IST, MandatePurpose, PRE_DEBIT_NOTICE, is_in_execution_window
from fourshots.simulator import DeclineRecord, Observation
from fourshots.taxonomy import (
    INSUFFICIENT_BALANCE,
    MANDATE_DEAD,
    UNCLASSIFIED,
    classify,
)
from fourshots.triage import (
    CachedTriager,
    ClaudeTriager,
    NullTriager,
    TriageVerdict,
    refresh_cache,
)

FIRST = datetime(2026, 9, 26, 9, 0, tzinfo=IST)
UNKNOWN_CODE = "bank_internal_5023"
BALANCE_PROSE = "The account did not have sufficient funds when the debit was presented."


def observation(code: str, description: str | None = None, attempts_used: int = 1):
    return Observation(
        mandate_id="mand_test",
        amount=Decimal("1000"),
        purpose=MandatePurpose.GENERAL,
        now=FIRST,
        attempts_used=attempts_used,
        history=tuple(
            DeclineRecord(FIRST + timedelta(days=i), code, None, description)
            for i in range(attempts_used)
        ),
    )


class StubTriager:
    """Returns a fixed verdict. Stands in for the model in every test here."""

    name = "stub"

    def __init__(self, verdict: TriageVerdict | None):
        self.verdict = verdict
        self.calls: list[tuple[str, str | None]] = []

    def triage(self, code: str, description: str | None):
        self.calls.append((code, description))
        return self.verdict


def verdict(failure_class: str, confidence: float = 0.95) -> TriageVerdict:
    return TriageVerdict(UNKNOWN_CODE, failure_class, confidence, "because", "stub-model")


# --- The layer only engages where the table cannot ------------------------

def test_triager_is_not_consulted_when_the_code_is_known() -> None:
    """A model must not second-guess a documented mapping. Spending a call --
    and accepting a model's opinion -- where a lookup already answers is pure
    added risk."""
    stub = StubTriager(verdict("insufficient_balance"))
    ConstraintAwareEngine(stub).propose(observation("invalid_vpa", BALANCE_PROSE))
    assert stub.calls == []


def test_triager_is_consulted_for_an_unreadable_code() -> None:
    stub = StubTriager(verdict("mandate_dead"))
    ConstraintAwareEngine(stub).propose(observation(UNKNOWN_CODE, BALANCE_PROSE))
    assert stub.calls == [(UNKNOWN_CODE, BALANCE_PROSE)]


def test_default_engine_consults_nothing() -> None:
    """Without a triager the engine behaves exactly as it did before this
    layer existed. The AI is an enhancement, never a dependency."""
    engine = ConstraintAwareEngine()
    with_triage = ConstraintAwareEngine(NullTriager())
    obs = observation(UNKNOWN_CODE, BALANCE_PROSE)
    assert engine.propose(obs) == with_triage.propose(obs)


# --- A verdict can narrow, and that is all --------------------------------

def test_a_terminal_verdict_stops_the_cycle() -> None:
    """The clearest win: prose says the registration is dead, so the engine
    stops instead of spending its one cautious attempt."""
    engine = ConstraintAwareEngine(StubTriager(verdict("mandate_dead")))
    assert engine.propose(observation(UNKNOWN_CODE, "registration no longer valid")) is None


def test_a_balance_verdict_schedules_like_a_balance_failure() -> None:
    """Once classified, the decline is handled by exactly the same code path as
    a code the table could read. The model chose the class; it did not choose
    the schedule."""
    triaged = ConstraintAwareEngine(StubTriager(verdict("insufficient_balance"))).propose(
        observation(UNKNOWN_CODE, BALANCE_PROSE)
    )
    known = ConstraintAwareEngine().propose(observation("insufficient_funds"))
    assert triaged == known


def test_a_verdict_cannot_bypass_the_notice_period() -> None:
    """Whatever the model says, the RBI 24-hour commitment still binds."""
    for cls in ("insufficient_balance", "psp_transient", "issuer_down"):
        proposed = ConstraintAwareEngine(StubTriager(verdict(cls))).propose(
            observation(UNKNOWN_CODE, "something went wrong")
        )
        assert proposed is not None
        assert proposed - FIRST >= PRE_DEBIT_NOTICE


def test_a_verdict_cannot_bypass_the_execution_window() -> None:
    for cls in ("insufficient_balance", "psp_transient", "issuer_down"):
        proposed = ConstraintAwareEngine(StubTriager(verdict(cls))).propose(
            observation(UNKNOWN_CODE, "something went wrong")
        )
        assert is_in_execution_window(proposed)


def test_a_verdict_cannot_extend_the_attempt_budget() -> None:
    """The most important bound. No classification buys a fifth attempt."""
    engine = ConstraintAwareEngine(StubTriager(verdict("insufficient_balance")))
    assert engine.propose(observation(UNKNOWN_CODE, BALANCE_PROSE, attempts_used=4)) is None


# --- Bad verdicts must be inert -------------------------------------------

def test_low_confidence_verdict_is_discarded() -> None:
    """Declining to answer is a valid outcome, and the conservative default is
    what a declined answer falls back to."""
    triager = CachedTriager.__new__(CachedTriager)
    triager._verdicts = {UNKNOWN_CODE: verdict("mandate_dead", confidence=0.4)}
    triager.min_confidence = 0.7
    assert triager.triage(UNKNOWN_CODE, BALANCE_PROSE) is None


def test_unknown_class_name_is_discarded_not_coerced() -> None:
    """A model inventing a category is a reason to distrust the verdict, not
    to snap it to the nearest known class."""
    triager = CachedTriager.__new__(CachedTriager)
    triager._verdicts = {UNKNOWN_CODE: verdict("bank_is_sad")}
    triager.min_confidence = 0.7
    assert triager.triage(UNKNOWN_CODE, BALANCE_PROSE) is None


def test_explicit_unclassified_verdict_changes_nothing() -> None:
    engine = ConstraintAwareEngine(StubTriager(verdict("unclassified")))
    triaged = engine.propose(observation(UNKNOWN_CODE, "vague message"))
    untriaged = ConstraintAwareEngine().propose(observation(UNKNOWN_CODE, "vague message"))
    assert triaged == untriaged


def test_a_triager_that_returns_nothing_leaves_the_default_standing() -> None:
    engine = ConstraintAwareEngine(StubTriager(None))
    assert engine.propose(observation(UNKNOWN_CODE, BALANCE_PROSE)) is not None


def test_verdict_resolves_only_known_classes() -> None:
    assert verdict("insufficient_balance").resolved() is INSUFFICIENT_BALANCE
    assert verdict("mandate_dead").resolved() is MANDATE_DEAD
    assert verdict("not_a_real_class").resolved() is None


# --- Failure of the model must not be failure of the system ---------------

class ExplodingClient:
    class messages:
        @staticmethod
        def create(**kwargs):
            raise RuntimeError("network is down")


def test_api_failure_degrades_to_the_conservative_default() -> None:
    """An outage at the model provider must not change scheduling behaviour.
    This is why the layer catches broadly and returns None."""
    triager = ClaudeTriager(client=ExplodingClient())
    assert triager.triage(UNKNOWN_CODE, BALANCE_PROSE) is None


def test_missing_description_is_not_sent_to_the_model() -> None:
    """With no prose there is nothing to read, and a model given only an
    unfamiliar code string would be guessing from its spelling."""
    triager = ClaudeTriager(client=ExplodingClient())
    assert triager.triage(UNKNOWN_CODE, None) is None


def test_malformed_response_is_discarded() -> None:
    class Garbage:
        class messages:
            @staticmethod
            def create(**kwargs):
                class R:
                    content = [type("B", (), {"type": "text", "text": "not json"})()]

                return R()

    assert ClaudeTriager(client=Garbage()).triage(UNKNOWN_CODE, BALANCE_PROSE) is None


# --- Cache behaviour -------------------------------------------------------

def test_cache_round_trips(tmp_path) -> None:
    """Verdicts are committed so a benchmark run is reproducible and every
    model judgement behind the result can be read and disputed."""
    path = tmp_path / "triage_cache.json"
    written = refresh_cache(
        {UNKNOWN_CODE: BALANCE_PROSE},
        cache_path=path,
        triager=StubTriager(verdict("insufficient_balance")),
    )
    assert written == 1

    loaded = CachedTriager(path)
    assert len(loaded) == 1
    restored = loaded.triage(UNKNOWN_CODE, BALANCE_PROSE)
    assert restored is not None
    assert restored.resolved() is INSUFFICIENT_BALANCE
    assert restored.reasoning  # the judgement is auditable, not just its verdict


def test_cache_records_the_model_that_judged(tmp_path) -> None:
    path = tmp_path / "triage_cache.json"
    refresh_cache(
        {UNKNOWN_CODE: BALANCE_PROSE},
        cache_path=path,
        triager=StubTriager(verdict("insufficient_balance")),
    )
    assert CachedTriager(path).triage(UNKNOWN_CODE, BALANCE_PROSE).model == "stub-model"


def test_missing_cache_file_is_empty_not_an_error(tmp_path) -> None:
    triager = CachedTriager(tmp_path / "absent.json")
    assert len(triager) == 0
    assert triager.triage(UNKNOWN_CODE, BALANCE_PROSE) is None


def test_refresh_skips_codes_the_triager_declines(tmp_path) -> None:
    path = tmp_path / "triage_cache.json"
    assert refresh_cache({UNKNOWN_CODE: BALANCE_PROSE}, path, StubTriager(None)) == 0
    assert len(CachedTriager(path)) == 0


# --- The prompt's own honesty ---------------------------------------------

def test_prompt_offers_an_explicit_way_to_decline() -> None:
    """Without an "I don't know" option a model is forced to guess, and a
    confident wrong answer costs an attempt that cannot be recovered."""
    from fourshots.triage import SYSTEM_PROMPT, _RESPONSE_SCHEMA

    assert "unclassified" in _RESPONSE_SCHEMA["properties"]["failure_class"]["enum"]
    assert "unclassified" in SYSTEM_PROMPT
    assert "do not know" in SYSTEM_PROMPT.lower()


def test_schema_is_closed() -> None:
    """The model chooses from the taxonomy's classes and cannot invent one."""
    from fourshots.taxonomy import ALL_CLASSES
    from fourshots.triage import _RESPONSE_SCHEMA

    assert _RESPONSE_SCHEMA["additionalProperties"] is False
    assert set(_RESPONSE_SCHEMA["properties"]["failure_class"]["enum"]) == {
        c.name for c in ALL_CLASSES
    }
