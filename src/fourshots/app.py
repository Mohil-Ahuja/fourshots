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

With `CONSOLE_ENABLED=true`, http://localhost:8000/console serves a page that
drives this same endpoint against synthetic declines. It is a window onto the
running service, not a second implementation of it: it can only reach the
system through the webhook route, signature and all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import FileResponse

from fourshots import console as demo
from fourshots.audit import AuditLog, ChainBroken
from fourshots.config import load_env, optional, require
from fourshots.engine import ConstraintAwareEngine
from fourshots.outreach import default_drafter
from fourshots.razorpay_client import client_or_none
from fourshots.recovery import RecoveryService, decisions_from
from fourshots.triage import default_triager
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
    # Both model-backed layers are wired in here and nowhere deeper, so the
    # live service reads unmappable declines and drafts escalation copy with
    # exactly the configuration the benchmark reports. Each resolves to its
    # offline default when no key is present, and `/console/status` says which
    # one is in force rather than leaving a viewer to assume.
    recovery_service = recovery or RecoveryService(
        audit_log,
        engine=ConstraintAwareEngine(default_triager()),
        client=client_or_none(),
        drafter=default_drafter(),
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

    # --- demo console ----------------------------------------------------
    #
    # Three small routes, all inert unless CONSOLE_ENABLED is set. They serve a
    # page, describe what this deployment can do, and sign a synthetic webhook
    # so the browser can post it. None of them decide anything: the decision
    # still happens in the webhook handler below, reached over HTTP like any
    # other caller.

    def console_guard() -> dict[str, Any] | None:
        if demo.console_enabled():
            return None
        return {
            "enabled": False,
            "reason": (
                "Set CONSOLE_ENABLED=true in .env to switch the demo console "
                "on. It is off by default because it can write entries into "
                "the decision log."
            ),
        }

    @application.get("/console")
    def console_page(response: Response) -> Any:
        page = Path(__file__).resolve().parents[2] / "docs" / "console.html"
        if not page.exists():
            response.status_code = status.HTTP_404_NOT_FOUND
            return {"error": "docs/console.html is missing"}
        return FileResponse(page, media_type="text/html")

    @application.get("/console/status")
    def console_status(response: Response) -> dict[str, Any]:
        """What this deployment will actually do, plus the demo scenarios.

        Served even when the console is disabled, so the page can explain why
        it is inert instead of failing silently.
        """
        blocked = console_guard()
        if blocked is not None:
            return blocked
        return {
            "enabled": True,
            "scenarios": demo.scenario_catalogue(),
            "service": recovery_service.status(),
            "audit_head": audit_log.head,
        }

    @application.post("/console/new-mandate")
    def console_new_mandate(response: Response) -> dict[str, Any]:
        """A fresh demo mandate id, and therefore a fresh attempt budget.

        Nothing is deleted to produce it. The log is append-only and the point
        of the demo is that it stays that way.
        """
        blocked = console_guard()
        if blocked is not None:
            response.status_code = status.HTTP_403_FORBIDDEN
            return blocked
        return {"mandate_id": demo.new_mandate_id()}

    @application.post("/console/sign")
    async def console_sign(request: Request, response: Response) -> dict[str, Any]:
        """Build one demo webhook and sign it. Never signs bytes it was given.

        The body comes back as a string rather than an object because the
        browser has to post these exact bytes: a signature commits to bytes,
        and re-encoding a parsed object is free to reorder keys and break it.
        """
        blocked = console_guard()
        if blocked is not None:
            response.status_code = status.HTTP_403_FORBIDDEN
            return blocked

        asked = await request.json()
        try:
            payload = demo.build_payload(
                mandate_id=asked.get("mandate_id", ""),
                scenario_key=asked.get("scenario", ""),
                amount=asked.get("amount", "0"),
            )
            body = demo.canonical_body(payload)
            signature = demo.sign(body, secret())
        except demo.ScenarioRejected as rejected:
            response.status_code = status.HTTP_400_BAD_REQUEST
            return {"error": str(rejected)}

        return {
            "body": body.decode("utf-8"),
            "signature": signature,
            "header": SIGNATURE_HEADER,
        }

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
