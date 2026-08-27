"""Mutation audit: break the code on purpose, check the tests notice.

Line coverage says which lines ran. It does not say whether a bug in those
lines would be caught, and a suite can sit at 98% coverage while asserting
almost nothing. This script answers the harder question by introducing
deliberate defects one at a time and requiring the suite to fail on each.

Every mutation here is a defect that would matter: a regulatory constant that
no longer matches the circular, a compliance check that no longer runs, a
security control switched off, or the engine losing the behaviour the headline
result depends on. If the suite passes with one of these applied, the tests are
decorative in that area.

Run:  python tools/mutation_audit.py

The file being mutated is restored immediately after each run, including when
pytest fails, so an interrupted audit cannot leave the tree modified.

Files are read and written in binary mode, and snippets are re-encoded to match
the file's own line-ending convention. Text mode on Windows rewrites LF as CRLF
on the round-trip, leaving every mutated file dirty in `git status` even when
nothing changed in substance -- and a byte-level match written with LF silently
fails to find a multi-line pattern in a CRLF checkout, turning a real mutation
into an untested hole. Both failure modes were hit while writing this.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

# (file, original snippet, replacement, description)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # --- Regulatory constants must match the circulars -------------------
    (
        "src/fourshots/policy.py",
        "MAX_ATTEMPTS_PER_CYCLE = 4",
        "MAX_ATTEMPTS_PER_CYCLE = 5",
        "NPCI attempt cap changed 4 -> 5",
    ),
    (
        "src/fourshots/policy.py",
        'AFA_THRESHOLD_GENERAL = Decimal("15000")',
        'AFA_THRESHOLD_GENERAL = Decimal("25000")',
        "AFA threshold changed 15k -> 25k",
    ),
    (
        "src/fourshots/policy.py",
        "PRE_DEBIT_NOTICE = timedelta(hours=24)",
        "PRE_DEBIT_NOTICE = timedelta(hours=1)",
        "RBI pre-debit notice cut 24h -> 1h",
    ),
    # --- Compliance checks must actually run -----------------------------
    (
        "src/fourshots/policy.py",
        "return not any(start <= local < end for start, end in PEAK_WINDOWS)",
        "return True",
        "peak-window check disabled (all times legal)",
    ),
    # --- Engine behaviour the headline result rests on -------------------
    (
        "src/fourshots/engine.py",
        "if classification.is_terminal:\n            return None",
        "if False:\n            return None",
        "engine no longer stops on terminal codes",
    ),
    (
        "src/fourshots/engine.py",
        "BALANCE_RETRY_OFFSETS_DAYS: tuple[int, ...] = (8, 16, 25)",
        "BALANCE_RETRY_OFFSETS_DAYS: tuple[int, ...] = (1, 2, 3)",
        "engine spread replaced by consecutive days",
    ),
    (
        "src/fourshots/taxonomy.py",
        "min_backoff_hours=24.0,\n    typical_resolution_hours=None,  # event-driven",
        "min_backoff_hours=0.0,\n    typical_resolution_hours=None,  # event-driven",
        "balance-failure 24h floor removed",
    ),
    # --- Security controls -----------------------------------------------
    (
        "src/fourshots/webhook.py",
        "if not hmac.compare_digest(expected, signature.strip()):",
        "if False:",
        "webhook signature verification disabled",
    ),
    (
        "src/fourshots/audit.py",
        "if _digest(entry.prev_hash, entry.body()) != entry.entry_hash:",
        "if False:",
        "audit tamper detection disabled",
    ),
    (
        "src/fourshots/audit.py",
        "if entry.prev_hash != expected_prev:",
        "if False:",
        "audit chain-link check disabled",
    ),
    # --- The benchmark must not be able to cheat -------------------------
    (
        "src/fourshots/simulator.py",
        "return mandate.balance_on(at.date()) >= mandate.amount",
        "return True",
        "simulator: balance failures always clear",
    ),
    (
        "src/fourshots/runner.py",
        "return self.outcome == Outcome.STOPPED_EARLY and self.repairable",
        "return self.outcome == Outcome.STOPPED_EARLY",
        "mandates_saved counts unrepairable mandates again",
    ),
]


def run_suite() -> bool:
    """True if the suite passes. `-x` stops at the first failure for speed."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "--no-header"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def encoder_for(blob: bytes):
    """Return an encoder matching this file's line-ending convention."""
    if b"\r\n" in blob:
        return lambda text: text.encode("utf-8").replace(b"\n", b"\r\n")
    return lambda text: text.encode("utf-8")


def main() -> int:
    caught: list[str] = []
    missed: list[str] = []
    skipped: list[str] = []

    for path, original_snippet, replacement, label in MUTATIONS:
        target = pathlib.Path(path)
        original = target.read_bytes()
        encode = encoder_for(original)
        snippet = encode(original_snippet)

        if snippet not in original:
            # The code moved on and the mutation no longer applies. Loud,
            # because a silently-inapplicable mutation is a hole in the audit.
            skipped.append(label)
            print(f"  SKIP    {label}  (snippet not found -- update this mutation)")
            continue

        target.write_bytes(original.replace(snippet, encode(replacement), 1))
        try:
            survived = run_suite()
        finally:
            target.write_bytes(original)

        if survived:
            missed.append(label)
            print(f"  MISSED  {label}   <-- tests did not notice")
        else:
            caught.append(label)
            print(f"  CAUGHT  {label}")

    total = len(caught) + len(missed)
    print()
    print(f"caught {len(caught)} / {total} mutations")
    if skipped:
        print(f"{len(skipped)} mutation(s) skipped and need updating")

    # A missed mutation is a real gap: behaviour that matters with no test
    # defending it. A skip is also a gap, just a quieter one. Exit non-zero on
    # either so this can gate CI.
    return 1 if missed or skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
