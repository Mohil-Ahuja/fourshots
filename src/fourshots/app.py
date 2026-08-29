"""HTTP surface: receives Razorpay webhooks, classifies declines, logs decisions.

Kept thin on purpose. Everything that constitutes a judgement -- how a decline
is classified, whether an attempt is legal -- lives in modules that need no
server to test. This file does transport: verify, parse, record, respond.

The app is built by a factory rather than assembled at import time, so a test
can hand it its own audit log instead of clearing the module cache to get an
isolated one. `app` at the bottom is the module-level instance uvicorn loads.

Run it with:
    uvicorn fourshots.app:app --port 8000
then expose it with `ngrok http 8000` and point the dashboard webhook at
https://<subdomain>.ngrok-free.dev/webhooks/razorpay
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response, status

from fourshots.audit import AuditLog, ChainBroken
from fourshots.config import load_env, optional, require
from fourshots.razorpay_client import client_or_none
from fourshots.recovery import RecoveryService, decisions_from
from fourshots.webhook import (
    SIGNATURE_HEADER,
    DeclineObserved,
    DowntimeObserved,
    SignatureInvalid,
    SubscriptionStateChanged,
    parse_event,
    verify_signature,
)


def create_app(
    audit: AuditLog | None = None,
    webhook_secret: str | None = None,
    recovery: RecoveryService | None = None,
) -> FastAPI:
    """Build the application.

    Every dependency is injectable. The secret is resolved lazily, per
    request, when not supplied -- so importing this module never requires a
    configured environment, but a running server still fails loudly on the
    first webhook if the secret is missing rather than degrading verification
    into a formality.

    The recovery service is built with whatever Razorpay test keys are
    configured, and with none if there are none. A live key is refused outright
    rather than being quietly downgraded to no client at all: running this
    against a real account is not a mode, it is a mistake.
    """
    load_env()
    audit_log = audit or AuditLog(optional("AUDIT_LOG_PATH", "out/audit.jsonl"))
    recovery_service = recovery or RecoveryService(
        audit_log,
        client=client_or_none(),
        merchant_name=optional("MERCHANT_NAME", "your merchant"),
        language=optional("OUTREACH_LANGUAGE", "english"),
    )

    application = FastAPI(
        title="fourshots",
        description=(
            "NPCI allows four executions per mandate cycle. This decides how "
            "to spend them."
        ),
    )
    # Exposed for tests and for introspection; the handlers close over the
    # local, so reassigning this does not silently change behaviour.
    application.state.audit = audit_log

    def secret() -> str:
        return webhook_secret if webhook_secret is not None else require(
            "RAZORPAY_WEBHOOK_SECRET"
        )

    @application.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "audit_head": audit_log.head}

    @application.get("/audit/verify")
    def audit_verify() -> dict[str, Any]:
        """Recompute the audit chain end to end.

        Exposed over HTTP because it is the claim worth demonstrating live: the
        decision log has not been edited since it was written.
        """
        try:
            verified = audit_log.verify()
        except ChainBroken as broken:
            return {
                "verified": False,
                "broken_at_seq": broken.seq,
                "reason": broken.reason,
            }
        return {"verified": True, "entries": verified, "head": audit_log.head}

    @application.get("/recovery/actions")
    def recovery_actions() -> dict[str, Any]:
        """Every recovery action taken, read back out of the audit log.

        Read from the chain rather than from memory on purpose: this endpoint
        and `/audit/verify` are then two views of the same bytes, so a schedule
        shown here cannot disagree with a log that verifies.
        """
        actions = decisions_from(audit_log.read())
        return {"count": len(actions), "actions": actions}

    @application.post("/webhooks/razorpay")
    async def razorpay_webhook(request: Request, response: Response) -> dict[str, Any]:
        """Receive a Razorpay webhook.

        Verification happens against the raw bytes before anything else touches
        the payload -- an unverified body is not data, it is input from a
        stranger.
        """
        raw_body = await request.body()

        try:
            verify_signature(
                raw_body, request.headers.get(SIGNATURE_HEADER), secret()
            )
        except SignatureInvalid as bad:
            # Log the rejection but never the body: an unverified payload is
            # attacker-controlled and does not belong in the decision record.
            audit_log.append("webhook_rejected", {"reason": str(bad)})
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return {"accepted": False, "reason": str(bad)}

        payload = await request.json()
        event_name = payload.get("event", "<unknown>")
        parsed = parse_event(payload)

        if isinstance(parsed, DeclineObserved):
            cls = parsed.classification
            audit_log.append(
                "decline_observed",
                {
                    "event": event_name,
                    "payment_id": parsed.payment_id,
                    "amount_inr": str(parsed.amount),
                    "method": parsed.method,
                    # Retain what the rail said alongside how we read it, so a
                    # later taxonomy correction can be applied to historic logs.
                    "raw_code": parsed.raw_code,
                    "raw_reason": parsed.raw_reason,
                    "npci_code": parsed.npci_code,
                    "raw_description": parsed.raw_description,
                    "failure_class": cls.failure_class.name,
                    "blocker": cls.failure_class.blocker.value,
                    "terminal": cls.is_terminal,
                    "silently_retryable": cls.failure_class.silently_retryable,
                    "min_backoff_hours": cls.failure_class.min_backoff_hours,
                    "mapping_confidence": cls.confidence.value,
                    "mapping_note": cls.note,
                },
                mandate_id=parsed.subscription_id,
                at=parsed.at,
            )
            # Recorded first, decided second: the entry above is part of the
            # history the four-attempt budget is counted from, so deciding
            # before writing it would let a redelivered webhook spend a fifth.
            decision = recovery_service.handle(parsed)
            return {
                "accepted": True,
                "event": event_name,
                "failure_class": cls.failure_class.name,
                "terminal": cls.is_terminal,
                "decision": decision.to_dict(),
            }

        if isinstance(parsed, DowntimeObserved):
            audit_log.append(
                "downtime_observed",
                {
                    "event": event_name,
                    "downtime_id": parsed.downtime_id,
                    "method": parsed.method,
                    "issuer": parsed.issuer,
                    "status": parsed.status,
                    "severity": parsed.severity,
                    "resolved": parsed.resolved,
                },
                at=parsed.at,
            )
            return {"accepted": True, "event": event_name, "resolved": parsed.resolved}

        if isinstance(parsed, SubscriptionStateChanged):
            audit_log.append(
                "subscription_state",
                {
                    "event": event_name,
                    "state": parsed.state,
                    "paid_count": parsed.paid_count,
                    "remaining_count": parsed.remaining_count,
                },
                mandate_id=parsed.subscription_id,
                at=parsed.at,
            )
            return {"accepted": True, "event": event_name, "state": parsed.state}

        # Subscribed but not modelled (successful charges, lifecycle noise).
        # Still recorded, so the log is a complete account of what was seen.
        audit_log.append("event_ignored", {"event": event_name})
        return {"accepted": True, "event": event_name, "handled": False}

    return application


app = create_app()
