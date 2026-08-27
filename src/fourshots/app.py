"""HTTP surface: receives Razorpay webhooks, classifies declines, logs decisions.

Kept thin on purpose. Everything that constitutes a judgement -- how a decline
is classified, whether an attempt is legal -- lives in modules that need no
server to test. This file does transport: verify, parse, record, respond.

Run it with:
    uvicorn fourshots.app:app --port 8000
then expose it with `ngrok http 8000` and point the dashboard webhook at
https://<subdomain>.ngrok-free.app/webhooks/razorpay
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response, status

from fourshots.audit import AuditLog, ChainBroken
from fourshots.config import load_env, optional, require
from fourshots.webhook import (
    SIGNATURE_HEADER,
    DeclineObserved,
    DowntimeObserved,
    SignatureInvalid,
    SubscriptionStateChanged,
    parse_event,
    verify_signature,
)

load_env()

app = FastAPI(
    title="fourshots",
    description=(
        "NPCI allows four executions per mandate cycle. This decides how to "
        "spend them."
    ),
)

audit = AuditLog(optional("AUDIT_LOG_PATH", "out/audit.jsonl"))


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "audit_head": audit.head}


@app.get("/audit/verify")
def audit_verify() -> dict[str, Any]:
    """Recompute the audit chain end to end.

    Exposed over HTTP because it is the claim worth demonstrating live: the
    decision log has not been edited since it was written.
    """
    try:
        verified = audit.verify()
    except ChainBroken as broken:
        return {
            "verified": False,
            "broken_at_seq": broken.seq,
            "reason": broken.reason,
        }
    return {"verified": True, "entries": verified, "head": audit.head}


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, response: Response) -> dict[str, Any]:
    """Receive a Razorpay webhook.

    Verification happens against the raw bytes before anything else touches the
    payload -- an unverified body is not data, it is input from a stranger.
    """
    raw_body = await request.body()

    try:
        verify_signature(
            raw_body,
            request.headers.get(SIGNATURE_HEADER),
            require("RAZORPAY_WEBHOOK_SECRET"),
        )
    except SignatureInvalid as bad:
        # Log the rejection but never the body: an unverified payload is
        # attacker-controlled and does not belong in the decision record.
        audit.append("webhook_rejected", {"reason": str(bad)})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"accepted": False, "reason": str(bad)}

    payload = await request.json()
    event_name = payload.get("event", "<unknown>")
    parsed = parse_event(payload)

    if isinstance(parsed, DeclineObserved):
        cls = parsed.classification
        audit.append(
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
        return {
            "accepted": True,
            "event": event_name,
            "failure_class": cls.failure_class.name,
            "terminal": cls.is_terminal,
        }

    if isinstance(parsed, DowntimeObserved):
        audit.append(
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
        audit.append(
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

    # Subscribed but not modelled (successful charges, lifecycle noise). Still
    # recorded, so the log is a complete account of what the endpoint saw.
    audit.append("event_ignored", {"event": event_name})
    return {"accepted": True, "event": event_name, "handled": False}
