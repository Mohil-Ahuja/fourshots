"""Loader for the pre-registered cohort parameters.

Validation here is not defensive programming, it is part of the methodology.
The submission's headline number rests on a parameter file that was committed
before any result existed; a file that silently accepts a decline mix summing
to 1.4 would undermine that claim quietly. So every invariant the simulator
depends on is checked at load time and fails loudly.

The regulatory section is treated differently from everything else. Those are
rules, not assumptions -- they are never swept, and `check_matches_policy()`
asserts they agree with the constants the engine actually enforces. If someone
edits one and not the other, the tests fail rather than the benchmark quietly
measuring a world with different rules than the code obeys.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "params" / "cohort.yaml"

# Sums are checked to this tolerance -- tight enough to catch a typo, loose
# enough to tolerate decimal representation.
_SUM_TOLERANCE = 1e-9


class ParamsInvalid(Exception):
    """Raised when the parameter file violates an invariant."""


def _require_sums_to_one(values: dict[str, float], label: str) -> None:
    total = sum(values.values())
    if abs(total - 1.0) > _SUM_TOLERANCE:
        raise ParamsInvalid(f"{label} must sum to 1.0, got {total!r}")


@dataclass(frozen=True)
class Params:
    """Parsed parameters, with the raw document retained.

    `raw` is kept so the audit log can record the exact parameter set a run
    used. A result that cannot say which world produced it is not reproducible.
    """

    raw: dict[str, Any]

    # --- cohort ---
    @property
    def size(self) -> int:
        return int(self.raw["cohort"]["size"])

    @property
    def trials(self) -> int:
        return int(self.raw["cohort"]["trials"])

    @property
    def seed(self) -> int:
        return int(self.raw["cohort"]["seed"])

    # --- distributions ---
    @property
    def decline_mix(self) -> dict[str, float]:
        return dict(self.raw["decline_mix"]["values"])

    @property
    def purposes(self) -> dict[str, float]:
        return dict(self.raw["purposes"]["values"])

    @property
    def amount_bands(self) -> list[dict[str, Any]]:
        return list(self.raw["amounts"]["bands"])

    @property
    def salary_weights(self) -> dict[str, float]:
        """Day-of-month weights, normalised.

        Stored unnormalised in the file so a reader can adjust one day without
        rebalancing the rest by hand; normalisation happens here.
        """
        raw = self.raw["salary_credit"]["day_of_month_weights"]
        total = sum(raw.values())
        return {str(k): v / total for k, v in raw.items()}

    # --- regulatory (never swept) ---
    @property
    def max_attempts(self) -> int:
        return int(self.raw["regulatory"]["max_attempts_per_cycle"])

    @property
    def pre_debit_notice_hours(self) -> int:
        return int(self.raw["regulatory"]["pre_debit_notice_hours"])

    @property
    def afa_general(self) -> Decimal:
        return Decimal(str(self.raw["regulatory"]["afa_threshold_general_inr"]))

    @property
    def afa_elevated(self) -> Decimal:
        return Decimal(str(self.raw["regulatory"]["afa_threshold_elevated_inr"]))

    # --- baseline arm ---
    @property
    def baseline_offsets_days(self) -> list[int]:
        return [int(d) for d in self.raw["baseline"]["retry_offsets_days"]]

    # --- provenance reporting ---
    def provenance_summary(self) -> dict[str, list[str]]:
        """Which sections are documented vs assumed.

        Reported alongside the results. A submission claiming honest metrics
        should be able to state, without being asked, how much of its own world
        model is sourced and how much is judgement.
        """
        summary: dict[str, list[str]] = {}
        for section, body in self.raw.items():
            if not isinstance(body, dict):
                continue
            for key, value in body.items():
                if "provenance" in key and isinstance(value, str):
                    summary.setdefault(value, []).append(section)
        return summary

    def validate(self) -> None:
        """Check every invariant the simulator relies on."""
        _require_sums_to_one(self.decline_mix, "decline_mix.values")
        _require_sums_to_one(self.purposes, "purposes.values")
        _require_sums_to_one(
            {str(i): b["weight"] for i, b in enumerate(self.amount_bands)},
            "amounts.bands weights",
        )

        if self.size <= 0 or self.trials <= 0:
            raise ParamsInvalid("cohort size and trials must be positive")

        for band in self.amount_bands:
            if band["min"] > band["max"]:
                raise ParamsInvalid(f"amount band has min > max: {band}")

        # The AFA path must actually be exercised. A benchmark where no mandate
        # ever crosses the threshold would leave that branch untested while
        # appearing to pass.
        general = float(self.afa_general)
        if not any(b["max"] > general for b in self.amount_bands):
            raise ParamsInvalid(
                "no amount band exceeds the general AFA threshold, so the AFA "
                "path would never be exercised"
            )

        if self.max_attempts < 1:
            raise ParamsInvalid("max_attempts_per_cycle must be at least 1")

    def check_matches_policy(self) -> None:
        """Assert the parameter file agrees with the constants the engine enforces.

        The simulator reads its world from this file; the engine reads its rules
        from `policy`. If the two disagree, the benchmark measures a world whose
        rules differ from the ones being obeyed, and the result means nothing.
        """
        from fourshots import policy

        mismatches: list[str] = []

        if self.max_attempts != policy.MAX_ATTEMPTS_PER_CYCLE:
            mismatches.append(
                f"max attempts: params={self.max_attempts} "
                f"policy={policy.MAX_ATTEMPTS_PER_CYCLE}"
            )

        notice = policy.PRE_DEBIT_NOTICE.total_seconds() / 3600
        if self.pre_debit_notice_hours != notice:
            mismatches.append(
                f"pre-debit notice: params={self.pre_debit_notice_hours}h "
                f"policy={notice}h"
            )

        if self.afa_general != policy.AFA_THRESHOLD_GENERAL:
            mismatches.append(
                f"AFA general: params={self.afa_general} "
                f"policy={policy.AFA_THRESHOLD_GENERAL}"
            )

        if self.afa_elevated != policy.AFA_THRESHOLD_ELEVATED:
            mismatches.append(
                f"AFA elevated: params={self.afa_elevated} "
                f"policy={policy.AFA_THRESHOLD_ELEVATED}"
            )

        file_windows = [
            (start, end) for start, end in self.raw["regulatory"]["peak_windows_ist"]
        ]
        code_windows = [
            (s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in policy.PEAK_WINDOWS
        ]
        if file_windows != code_windows:
            mismatches.append(f"peak windows: params={file_windows} policy={code_windows}")

        if mismatches:
            raise ParamsInvalid(
                "parameter file disagrees with enforced policy: " + "; ".join(mismatches)
            )


def load(path: str | Path = DEFAULT_PATH) -> Params:
    """Load, validate, and cross-check the parameter file."""
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    params = Params(document)
    params.validate()
    params.check_matches_policy()
    return params
