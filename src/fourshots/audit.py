"""Append-only, hash-chained audit log.

The track bar asks for an audit trail. A log you can silently edit after the
fact is not one, so every entry commits to the entire history before it: each
record carries the hash of its predecessor, and `verify()` recomputes the whole
chain. Change one rupee in entry 3 and entries 3..N all fail verification.

This matters beyond tidiness. The headline result of this project is a
comparison between two scheduling policies, and a reader is entitled to ask
whether the losing arm's log was tidied up afterwards. A chain that verifies
is the answer.

Format is JSON Lines: one entry per line, appended, never rewritten. That keeps
it greppable and diffable, and means a crash mid-run truncates at a line
boundary rather than corrupting the file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS = "0" * 64
"""Hash the first entry chains from. Fixed, so a chain cannot be re-rooted."""


def _canonical(payload: dict[str, Any]) -> bytes:
    """Deterministic serialisation for hashing.

    Sorted keys and no incidental whitespace, so the same logical entry always
    produces the same bytes regardless of dict insertion order. `default=str`
    keeps Decimals and datetimes hashable without silently coercing them to
    floats, which would lose paise.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _digest(prev_hash: str, body: dict[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + _canonical(body)).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    """One recorded decision or observation.

    `kind` is a coarse category ("decline_observed", "attempt_scheduled",
    "attempt_rejected", "mandate_halted"). `data` carries whatever that kind
    needs -- deliberately open, because forcing every event into one schema
    would push detail out of the log, and detail is the point.
    """

    seq: int
    at: datetime
    kind: str
    mandate_id: str | None
    data: dict[str, Any]
    prev_hash: str
    entry_hash: str

    def body(self) -> dict[str, Any]:
        """The fields the hash commits to. Excludes `entry_hash` itself."""
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "kind": self.kind,
            "mandate_id": self.mandate_id,
            "data": self.data,
        }

    def to_json(self) -> str:
        record = self.body()
        record["prev_hash"] = self.prev_hash
        record["entry_hash"] = self.entry_hash
        return json.dumps(record, sort_keys=True, default=str)

    @classmethod
    def from_json(cls, line: str) -> "AuditEntry":
        raw = json.loads(line)
        return cls(
            seq=raw["seq"],
            at=datetime.fromisoformat(raw["at"]),
            kind=raw["kind"],
            mandate_id=raw["mandate_id"],
            data=raw["data"],
            prev_hash=raw["prev_hash"],
            entry_hash=raw["entry_hash"],
        )


class ChainBroken(Exception):
    """Raised when the log does not verify. Carries the first bad sequence."""

    def __init__(self, seq: int, reason: str) -> None:
        super().__init__(f"audit chain broken at seq={seq}: {reason}")
        self.seq = seq
        self.reason = reason


class AuditLog:
    """A hash-chained JSONL log.

    Opening an existing log reads its tail to recover the chain head, so an
    interrupted run resumes the same chain rather than starting a second one.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq, self._head = self._recover()

    def _recover(self) -> tuple[int, str]:
        """Read the last entry to find where the chain left off."""
        if not self.path.exists():
            return 0, GENESIS
        last: AuditEntry | None = None
        for entry in self.read():
            last = entry
        if last is None:
            return 0, GENESIS
        return last.seq + 1, last.entry_hash

    def append(
        self,
        kind: str,
        data: dict[str, Any],
        *,
        mandate_id: str | None = None,
        at: datetime | None = None,
    ) -> AuditEntry:
        """Append one entry and return it.

        The timestamp is injectable so the simulator can write logs in
        simulated time; production callers leave it and get wall-clock UTC.
        """
        entry_at = at or datetime.now(timezone.utc)
        body = {
            "seq": self._seq,
            "at": entry_at.isoformat(),
            "kind": kind,
            "mandate_id": mandate_id,
            "data": data,
        }
        entry = AuditEntry(
            seq=self._seq,
            at=entry_at,
            kind=kind,
            mandate_id=mandate_id,
            data=data,
            prev_hash=self._head,
            entry_hash=_digest(self._head, body),
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.to_json() + "\n")
        self._seq += 1
        self._head = entry.entry_hash
        return entry

    def read(self) -> Iterator[AuditEntry]:
        """Yield every entry in order. Blank lines are skipped."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield AuditEntry.from_json(line)

    def verify(self) -> int:
        """Recompute the whole chain. Returns the number of entries verified.

        Raises ChainBroken on the first entry whose sequence, link or digest
        does not hold. Checking all three matters: a tamperer who recomputes
        digests but forgets the links, or splices entries out and renumbers,
        should still be caught.
        """
        expected_prev = GENESIS
        expected_seq = 0
        count = 0

        for entry in self.read():
            if entry.seq != expected_seq:
                raise ChainBroken(entry.seq, f"expected seq={expected_seq}")
            if entry.prev_hash != expected_prev:
                raise ChainBroken(entry.seq, "prev_hash does not match predecessor")
            if _digest(entry.prev_hash, entry.body()) != entry.entry_hash:
                raise ChainBroken(entry.seq, "contents do not match entry_hash")
            expected_prev = entry.entry_hash
            expected_seq += 1
            count += 1

        return count

    @property
    def head(self) -> str:
        """Current chain head. Publishing this pins the log's entire contents."""
        return self._head
