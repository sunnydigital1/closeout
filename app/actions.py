"""Approval-gated actions.

Nothing here runs on its own. The UI first asks for a preview (exactly what
will happen, including the PayPal API call), the owner approves, and only then
is the action executed. In the demo month every action is simulated; against
the PayPal sandbox the real API is called with test money.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .models import Action, Issue, fmt_money
from .paypal import PayPalClient


class ActionError(RuntimeError):
    pass


@dataclass
class Mode:
    live: bool  # True: call PayPal sandbox for real. False: simulate.
    label: str

    @classmethod
    def for_source(cls, source: str, allow_live: bool, client_available: bool) -> "Mode":
        if source == "paypal" and allow_live and client_available:
            return cls(True, "PayPal sandbox: this calls the real PayPal API with test money.")
        if source == "paypal":
            return cls(False, "Live actions are switched off on this server, so this is simulated.")
        return cls(False, "Demo month: nothing is sent to PayPal.")


def find_action(issue: Issue, action_type: str) -> Action:
    for action in issue.actions:
        if action.type == action_type:
            return action
    raise ActionError(f"'{action_type}' isn't an allowed action for {issue.id}")


def _who(issue: Issue) -> str:
    return str(issue.facts.get("customer") or issue.facts.get("payer") or "the customer")


def preview(issue: Issue, action: Action, mode: Mode, default_message: str | None) -> dict:
    p = action.params
    out = {
        "issue_id": issue.id,
        "action": action.type,
        "mode": mode.label,
        "live": mode.live,
        "moves_money": action.moves_money,
        "contacts_customer": action.contacts_customer,
        "message": None,
        "message_limit": None,
        "api_call": None,
        "journal": None,
    }
    if action.type == "refund_duplicate":
        amount = fmt_money(Decimal(p["amount"]), p["currency"])
        out.update(
            title=f"Refund {amount} to {_who(issue)}?",
            confirm_label=f"Refund {amount}",
            done_label=f"Refunded {amount}",
            lines=[
                f"PayPal returns {amount} from the duplicate payment {p['capture_id']} to {p.get('payer_email') or _who(issue)}.",
                "The first payment for this order stays in place.",
                "Your note below is shown to the customer with the refund.",
            ],
            api_call=f"POST /v2/payments/captures/{p['capture_id']}/refund",
            message=default_message,
            message_limit=255,
        )
    elif action.type == "send_reminder":
        due = fmt_money(Decimal(p["amount_due"]))
        out.update(
            title=f"Send a payment reminder to {_who(issue)}?",
            confirm_label="Send reminder",
            done_label="Reminder sent",
            lines=[
                f"PayPal emails {p.get('recipient_email') or _who(issue)} a reminder for invoice "
                f"{p['invoice_number']} with {due} due, including your note below.",
                "The email comes from PayPal and includes the link to pay.",
            ],
            api_call=f"POST /v2/invoicing/invoices/{p['invoice_id']}/remind",
            message=default_message,
            message_limit=4000,
        )
    elif action.type == "record_adjustment":
        out.update(
            title="Record this in your books?",
            confirm_label="Record in books",
            done_label="Recorded in books",
            lines=[
                "Closeout adds this entry to your adjustments file for your bookkeeper.",
                "Nothing changes in PayPal.",
            ],
            journal=p["entry"],
        )
    elif action.type == "review_dispute":
        out.update(
            title="Open the dispute in PayPal?",
            confirm_label="Open PayPal",
            done_label="Opened in PayPal",
            lines=[
                "Opens PayPal's Resolution Center, where you can see the buyer's claim and respond with evidence.",
                f"Chargeback reference: {p.get('dispute_txn')}.",
            ],
        )
    elif action.type == "mark_reviewed":
        out.update(
            title="Mark as reviewed?",
            confirm_label="Mark as reviewed",
            done_label="Marked as reviewed",
            lines=["Moves this item off your list. Nothing changes in PayPal or in your books."],
        )
    else:
        raise ActionError(f"Unknown action {action.type}")
    return out


def execute(
    issue: Issue,
    action: Action,
    mode: Mode,
    message: str | None,
    client: PayPalClient | None,
) -> dict:
    """Run an approved action. Returns what happened, for the UI and the activity log."""
    p = action.params
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = {"at": now, "issue_id": issue.id, "action": action.type, "live": mode.live, "paypal_id": None}

    if action.type == "refund_duplicate":
        amount = Decimal(p["amount"])
        note = (message or "Refund for a duplicate payment.").strip()[:255]
        if mode.live:
            resp = client.refund_capture(  # type: ignore[union-attr]
                p["capture_id"], amount, p["currency"], note, request_id=f"closeout-{issue.id}"
            )
            result["paypal_id"] = resp.get("id")
            result["paypal_status"] = resp.get("status")
        result.update(issue_status="resolved", summary=f"Refunded {fmt_money(amount, p['currency'])} to {_who(issue)}")
    elif action.type == "send_reminder":
        note = (message or "").strip()[:4000] or f"Friendly reminder that invoice {p['invoice_number']} is due."
        if mode.live:
            client.remind_invoice(  # type: ignore[union-attr]
                p["invoice_id"], f"Reminder: invoice {p['invoice_number']}", note
            )
        result.update(issue_status="resolved", summary=f"Reminder sent for invoice {p['invoice_number']}")
    elif action.type == "record_adjustment":
        result.update(issue_status="resolved", summary="Recorded in books", journal=p["entry"])
    elif action.type == "review_dispute":
        url = client.resolution_center_url if client else "https://www.sandbox.paypal.com/resolutioncenter"
        result.update(issue_status=None, summary="Opened the dispute in PayPal", open_url=url)
    elif action.type == "mark_reviewed":
        result.update(issue_status="reviewed", summary="Marked as reviewed")
    else:
        raise ActionError(f"Unknown action {action.type}")
    if not mode.live and action.type in ("refund_duplicate", "send_reminder"):
        result["summary"] += " (simulated)"
    return result
