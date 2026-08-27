"""The cohort and the world it lives in.

The information barrier
-----------------------
This module holds ground truth: balance trajectories, when salary lands, which
issuers are down, and why each mandate actually failed. **None of it is visible
to a retry policy.** A policy receives an `Observation`, which carries only what
a real merchant learns from a webhook -- decline code, timestamp, amount,
attempts used -- and gets back an `AttemptResult`, which carries only whether
the debit cleared and what code came back.

That barrier is the answer to the obvious attack on this project: *you built
the simulator and you built the optimizer.* The engine cannot be exploiting
knowledge of the balance curve, because the type it receives has no path to it.
`test_simulator.py` asserts this structurally rather than trusting the claim.

Modelling the unmapped code
---------------------------
`unclassified` is not a failure mode. It is a mandate whose true failure mode is
one of the real ones, but whose decline code the taxonomy cannot map. So the
world knows the real reason and the policy does not -- which is what makes the
conservative fallback cost something measurable instead of being free.
"""

from __future__ import annotations

import enum
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from fourshots.params import Params
from fourshots.policy import IST, MandatePurpose


class FailureMode(enum.Enum):
    """Why a mandate's debit actually fails. Ground truth -- never observable.

    Distinct from `FailureClass` in the taxonomy, which is a policy's *reading*
    of a decline code. The gap between the two is where this project lives.
    """

    # Values match the keys in params/cohort.yaml exactly, so the parameter
    # file and this enum cannot drift apart silently. See
    # test_every_declared_mode_is_a_real_failure_mode.
    BALANCE = "insufficient_balance"    # clears when money arrives
    ISSUER_DOWN = "issuer_down"         # clears when the outage ends
    PSP_TRANSIENT = "psp_transient"     # clears within hours
    CUSTOMER_ABSENT = "customer_absent" # needs the customer to respond
    AUTH_REQUIRED = "auth_required"     # needs AFA; never clears silently
    MANDATE_DEAD = "mandate_dead"       # never clears
    LIMIT_BREACH = "limit_breach"       # never clears at this amount
    INSTRUMENT_REJECTED = "instrument_rejected"  # never clears on this instrument
    NONE = "none"                       # would have succeeded


# Ground-truth mode -> the decline code the rail actually emits. The policy sees
# only the string, and must map it back through the taxonomy.
_MODE_TO_CODE: dict[FailureMode, str] = {
    FailureMode.BALANCE: "insufficient_funds",
    FailureMode.ISSUER_DOWN: "bank_technical_error",
    FailureMode.PSP_TRANSIENT: "partner_bank_downtime",
    FailureMode.CUSTOMER_ABSENT: "payment_collect_request_expired",
    FailureMode.AUTH_REQUIRED: "payment_declined",
    FailureMode.MANDATE_DEAD: "invalid_vpa",
    FailureMode.LIMIT_BREACH: "payment_declined",
    FailureMode.INSTRUMENT_REJECTED: "international_transaction_not_allowed",
}

# Ground-truth mode -> the NPCI response code the rail returns underneath.
#
# Razorpay's own error-mapping layer translates these into its `error_reason`
# strings before they reach a merchant webhook, so in a standard Razorpay
# integration only the translation is visible. Merchants with direct PSP or
# bank connectivity see the raw code, and it is strictly more specific -- Z8
# says "limit breached", which is terminal, where Razorpay's rendering may
# only say "payment_declined". The engine uses it when present and works
# without it when absent.
_MODE_TO_NPCI: dict[FailureMode, str] = {
    FailureMode.BALANCE: "Z9",
    FailureMode.ISSUER_DOWN: "U28",
    FailureMode.PSP_TRANSIENT: "U69",
    FailureMode.LIMIT_BREACH: "Z8",
    FailureMode.CUSTOMER_ABSENT: "U69",
}

# Codes the taxonomy has no mapping for. Real rails emit these; a cohort
# without them would flatter the engine.
_UNMAPPABLE_CODES = (
    "issuer_risk_hold",
    "npci_reject_u91",
    "acquirer_policy_decline",
)

# Modes that no amount of waiting or retrying will resolve.
TERMINAL_MODES = frozenset(
    {
        FailureMode.MANDATE_DEAD,
        FailureMode.LIMIT_BREACH,
        FailureMode.INSTRUMENT_REJECTED,
        FailureMode.AUTH_REQUIRED,
    }
)


@dataclass(frozen=True)
class Mandate:
    """One recurring mandate in the cohort.

    Only `id`, `amount`, `purpose` and `debit_day` are ever shown to a policy.
    Everything else on this object is ground truth.
    """

    id: str
    amount: Decimal
    purpose: MandatePurpose
    debit_day: int              # day of month the debit is scheduled

    # --- ground truth below this line ---
    salary_day: int
    opening_multiple: float     # balance at salary credit, as a multiple of amount
    daily_burn: float           # fraction of opening balance spent per day
    true_mode: FailureMode
    code_is_unmappable: bool    # rail emits a code the taxonomy cannot read

    def balance_on(self, when: date) -> Decimal:
        """Ground-truth account balance, as a multiple of the debit amount.

        Money lands on `salary_day` and decays through the month. A mandate
        debiting late in the cycle is therefore more likely to meet an empty
        account -- which is the entire mechanism the project exploits.
        """
        days_since_salary = (when.day - self.salary_day) % 30
        remaining = self.opening_multiple * (1.0 - self.daily_burn) ** days_since_salary
        return self.amount * Decimal(str(max(0.0, remaining)))


@dataclass(frozen=True)
class DeclineRecord:
    """One observed decline: when, and what the rail called it.

    Both codes are carried because they are different views of the same event.
    `razorpay_code` is the aggregator's translation and is always present;
    `npci_code` is the rail's own response code, more specific, and available
    only to integrations that see it. The taxonomy prefers the NPCI code where
    it is both present and definite.
    """

    at: datetime
    razorpay_code: str | None
    npci_code: str | None = None


@dataclass(frozen=True)
class AttemptResult:
    """What a policy learns from spending an attempt.

    Deliberately narrow: cleared or not, and the codes the rail returned. No
    balance, no mode, no hint about when the blocker will clear. This is the
    information barrier made concrete.
    """

    cleared: bool
    at: datetime
    razorpay_code: str | None
    npci_code: str | None = None


@dataclass(frozen=True)
class Observation:
    """Everything a policy is allowed to know when it decides.

    Mirrors what a merchant actually has after a webhook: the mandate's public
    facts, how many attempts are gone, and the history of codes seen so far.
    """

    mandate_id: str
    amount: Decimal
    purpose: MandatePurpose
    now: datetime
    attempts_used: int
    history: tuple[DeclineRecord, ...] = ()

    @property
    def last_decline(self) -> DeclineRecord | None:
        return self.history[-1] if self.history else None


class World:
    """Ground truth for one simulation run.

    Owns the issuer-downtime schedule and resolves whether a given attempt
    clears. Policies never hold a reference to this object; the runner does.
    """

    def __init__(self, params: Params, rng: random.Random) -> None:
        self._params = params
        self._rng = rng
        self._downtime: dict[date, tuple[datetime, datetime]] = {}
        self._downtime_prob = float(params.raw["issuer_downtime"]["daily_probability"])
        self._downtime_hours = float(
            params.raw["issuer_downtime"]["mean_duration_hours"]
        )

    def _outage_on(self, day: date) -> tuple[datetime, datetime] | None:
        """Whether the issuer is down on `day`, memoised so it stays consistent
        across repeated queries within a run."""
        if day not in self._downtime:
            if self._rng.random() < self._downtime_prob:
                start_hour = self._rng.uniform(0, 24 - self._downtime_hours)
                start = datetime.combine(day, datetime.min.time(), tzinfo=IST) + timedelta(
                    hours=start_hour
                )
                self._downtime[day] = (start, start + timedelta(hours=self._downtime_hours))
            else:
                self._downtime[day] = None  # type: ignore[assignment]
        return self._downtime[day]

    def attempt(self, mandate: Mandate, at: datetime) -> AttemptResult:
        """Resolve one debit attempt against ground truth.

        Returns only what the rail would tell a merchant.
        """
        mode = mandate.true_mode

        if mode is FailureMode.NONE:
            return AttemptResult(True, at, None, None)

        cleared = self._would_clear(mandate, at, mode)
        if cleared:
            return AttemptResult(True, at, None, None)

        if mandate.code_is_unmappable:
            # An unreadable aggregator code, and no rail code to fall back on.
            return AttemptResult(False, at, self._rng.choice(_UNMAPPABLE_CODES), None)

        return AttemptResult(False, at, _MODE_TO_CODE[mode], _MODE_TO_NPCI.get(mode))

    def _would_clear(self, mandate: Mandate, at: datetime, mode: FailureMode) -> bool:
        """Whether the blocker behind `mode` has resolved by `at`."""
        if mode in TERMINAL_MODES:
            return False

        if mode is FailureMode.BALANCE:
            return mandate.balance_on(at.date()) >= mandate.amount

        if mode is FailureMode.ISSUER_DOWN:
            outage = self._outage_on(at.date())
            return not (outage and outage[0] <= at < outage[1])

        if mode is FailureMode.PSP_TRANSIENT:
            # Rail congestion clears quickly; any attempt in a later window
            # is very likely to succeed.
            return self._rng.random() < 0.85

        if mode is FailureMode.CUSTOMER_ABSENT:
            # Needs a human to answer a collect request. Some do on a later try.
            return self._rng.random() < 0.35

        return False


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _draw_amount(rng: random.Random, params: Params) -> Decimal:
    bands = params.amount_bands
    band = rng.choices(bands, weights=[b["weight"] for b in bands], k=1)[0]
    return Decimal(rng.randint(int(band["min"]), int(band["max"])))


def _draw_salary_day(rng: random.Random, params: Params) -> int:
    """Pick a payday. The `other` bucket spreads across mid-month days that
    carry no explicit weight."""
    pick = _weighted_choice(rng, params.salary_weights)
    if pick == "other":
        return rng.choice([4, 5, 6, 8, 9, 11, 12, 13, 14, 15, 20, 25])
    return int(pick)


def build_cohort(params: Params, rng: random.Random) -> list[Mandate]:
    """Generate the mandate population declared in the parameter file.

    `unclassified` in the decline mix is handled as described in the module
    docstring: the mandate gets a real underlying failure mode, but the rail
    reports a code the taxonomy cannot map.
    """
    mix = params.decline_mix
    purposes = params.purposes
    balance_cfg = params.raw["balance"]

    # The real modes an unmappable code could be hiding, renormalised.
    real_modes = {k: v for k, v in mix.items() if k != "unclassified"}
    total = sum(real_modes.values())
    real_modes = {k: v / total for k, v in real_modes.items()}

    cohort: list[Mandate] = []
    for index in range(params.size):
        drawn = _weighted_choice(rng, mix)
        unmappable = drawn == "unclassified"
        mode_name = _weighted_choice(rng, real_modes) if unmappable else drawn

        opening = max(
            0.0,
            rng.gauss(
                float(balance_cfg["opening_multiple_of_debit"]["mean"]),
                float(balance_cfg["opening_multiple_of_debit"]["sd"]),
            ),
        )
        burn = min(
            0.9,
            max(
                0.0,
                rng.gauss(
                    float(balance_cfg["daily_burn_fraction"]["mean"]),
                    float(balance_cfg["daily_burn_fraction"]["sd"]),
                ),
            ),
        )

        cohort.append(
            Mandate(
                id=f"mand_{index:05d}",
                amount=_draw_amount(rng, params),
                purpose=MandatePurpose(_weighted_choice(rng, purposes)),
                debit_day=rng.randint(1, 28),
                salary_day=_draw_salary_day(rng, params),
                opening_multiple=opening,
                daily_burn=burn,
                true_mode=FailureMode(mode_name),
                code_is_unmappable=unmappable,
            )
        )

    return cohort
