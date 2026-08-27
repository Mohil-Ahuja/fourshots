"""Tests for the hash-chained audit log.

The property under test is tamper-evidence, so most of these deliberately
corrupt a written log and assert that verification notices. A chain that only
passes on untouched files proves nothing.
"""

import json
from datetime import datetime, timezone

import pytest

from fourshots.audit import GENESIS, AuditLog, ChainBroken


@pytest.fixture()
def log(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def test_empty_log_verifies_and_starts_at_genesis(log) -> None:
    assert log.verify() == 0
    assert log.head == GENESIS


def test_entries_chain_and_verify(log) -> None:
    log.append("decline_observed", {"reason": "insufficient_funds"}, mandate_id="m1")
    log.append("attempt_scheduled", {"at": "2026-09-01T08:00:00+05:30"}, mandate_id="m1")
    log.append("attempt_rejected", {"violation": "peak_window"}, mandate_id="m1")
    assert log.verify() == 3


def test_each_entry_links_to_its_predecessor(log) -> None:
    first = log.append("a", {})
    second = log.append("b", {})
    assert first.prev_hash == GENESIS
    assert second.prev_hash == first.entry_hash


def test_head_advances_with_each_append(log) -> None:
    before = log.head
    entry = log.append("a", {})
    assert log.head == entry.entry_hash != before


def test_reopening_resumes_the_same_chain(tmp_path) -> None:
    """A crashed run must not silently start a second chain."""
    path = tmp_path / "audit.jsonl"
    first = AuditLog(path)
    first.append("a", {})
    first.append("b", {})

    resumed = AuditLog(path)
    resumed.append("c", {})

    assert resumed.verify() == 3
    assert [e.seq for e in resumed.read()] == [0, 1, 2]


# --- Tamper detection ------------------------------------------------------

def _rewrite(path, line_no: int, mutate) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[line_no])
    mutate(record)
    lines[line_no] = json.dumps(record, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_editing_an_entry_breaks_verification(tmp_path) -> None:
    """The headline claim: you cannot change a recorded amount after the fact."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("attempt", {"amount_inr": "499"})
    log.append("attempt", {"amount_inr": "999"})
    log.append("attempt", {"amount_inr": "1499"})

    _rewrite(path, 1, lambda r: r["data"].__setitem__("amount_inr", "1"))

    with pytest.raises(ChainBroken) as caught:
        AuditLog(path).verify()
    assert caught.value.seq == 1
    assert "entry_hash" in caught.value.reason


def test_recomputing_the_digest_still_breaks_the_link(tmp_path) -> None:
    """A tamperer who fixes the entry's own hash still breaks the chain.

    This is the whole reason entries commit to their predecessor rather than
    just to themselves.
    """
    from fourshots.audit import _digest

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("attempt", {"amount_inr": "499"})
    log.append("attempt", {"amount_inr": "999"})

    def forge(record):
        record["data"]["amount_inr"] = "1"
        body = {k: record[k] for k in ("seq", "at", "kind", "mandate_id", "data")}
        record["entry_hash"] = _digest(record["prev_hash"], body)

    _rewrite(path, 0, forge)

    # Entry 0 now self-verifies, but entry 1 still points at the old hash.
    with pytest.raises(ChainBroken) as caught:
        AuditLog(path).verify()
    assert caught.value.seq == 1
    assert "prev_hash" in caught.value.reason


def test_deleting_an_entry_is_detected(tmp_path) -> None:
    """Splicing out an inconvenient decision must not go unnoticed."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(4):
        log.append("attempt", {"n": i})

    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[2]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ChainBroken) as caught:
        AuditLog(path).verify()
    assert caught.value.seq == 3


def test_truncation_is_not_flagged_as_tampering(tmp_path) -> None:
    """A crash mid-run truncates at a line boundary. The surviving prefix is
    still a valid chain, and treating that as corruption would cry wolf."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(4):
        log.append("attempt", {"n": i})

    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")

    assert AuditLog(path).verify() == 2


# --- Serialisation ---------------------------------------------------------

def test_key_order_does_not_affect_the_digest(log) -> None:
    """Canonical serialisation: the same logical entry hashes identically
    however the dict happened to be built."""
    a = log.append("k", {"alpha": 1, "beta": 2})
    b = log.append("k", {"beta": 2, "alpha": 1})
    # Different positions in the chain, so different hashes -- but both verify,
    # which is what proves the serialisation is order-independent.
    assert a.entry_hash != b.entry_hash
    assert log.verify() == 2


def test_simulated_timestamps_are_accepted(log) -> None:
    """The simulator writes logs in simulated time, not wall clock."""
    when = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    entry = log.append("attempt", {}, at=when)
    assert entry.at == when
    assert log.verify() == 1
