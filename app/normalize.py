"""Turn raw PayPal API responses and the seller's books into Closeout models.

The sample dataset is stored in the exact shape the PayPal APIs return, so the
same code path handles demo data and live sandbox data.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Invoice, Order, PayPalTxn, money

_TZ_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_paypal_datetime(value: str) -> datetime:
    """PayPal returns e.g. '2026-10-03T14:22:11+0000' or '...Z'."""
    value = value.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    value = _TZ_NO_COLON.sub(r"\1:\2", value)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def classify_event(code: str) -> str:
    """Map a PayPal transaction event code to a Closeout transaction kind."""
    code = (code or "").upper()
    if code.startswith("T00"):
        return "payment"
    if code == "T1107":
        return "refund"
    if code == "T1106":
        return "reversal"
    if code == "T1201":
        return "chargeback"
    if code in ("T1202", "T1205", "T1208"):
        return "chargeback_reversal"
    if code == "T1110":
        return "dispute_hold"
    if code == "T1111":
        return "dispute_release"
    if code.startswith("T04"):
        return "withdrawal"
    if code.startswith("T02"):
        return "fx"
    if code.startswith("T01"):
        return "fee"
    return "other"


def _name(payer_info: dict) -> str | None:
    name = payer_info.get("payer_name") or {}
    full = name.get("alternate_full_name")
    if full:
        return full
    parts = [name.get("given_name"), name.get("surname")]
    joined = " ".join(p for p in parts if p)
    return joined or None


def transactions_from_api(payload: dict) -> list[PayPalTxn]:
    """Normalize a /v1/reporting/transactions response (any number of pages merged)."""
    txns: list[PayPalTxn] = []
    for detail in payload.get("transaction_details", []):
        info = detail.get("transaction_info", {})
        payer = detail.get("payer_info", {}) or {}
        amount = info.get("transaction_amount") or {}
        fee = info.get("fee_amount") or {}
        txns.append(
            PayPalTxn(
                id=info["transaction_id"],
                event_code=info.get("transaction_event_code", ""),
                kind=classify_event(info.get("transaction_event_code", "")),
                timestamp=parse_paypal_datetime(info["transaction_initiation_date"]),
                gross=money(amount.get("value")),
                fee=money(fee.get("value")),
                currency=amount.get("currency_code", "USD"),
                status=info.get("transaction_status", ""),
                invoice_ref=info.get("invoice_id") or None,
                custom=info.get("custom_field") or None,
                note=info.get("transaction_note") or info.get("transaction_subject") or None,
                payer_email=payer.get("email_address"),
                payer_name=_name(payer),
                payer_country=payer.get("country_code"),
                reference_id=info.get("paypal_reference_id") or None,
            )
        )
    txns.sort(key=lambda t: t.timestamp)
    return txns


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def invoices_from_api(payload: dict) -> list[Invoice]:
    """Normalize Invoicing API v2 invoices (list response with details merged in)."""
    invoices: list[Invoice] = []
    for item in payload.get("items", []):
        detail = item.get("detail", {})
        amount = money((item.get("amount") or {}).get("value"))
        currency = (item.get("amount") or {}).get("currency_code") or detail.get("currency_code", "USD")
        payments = item.get("payments") or {}
        if payments.get("paid_amount"):
            paid = money(payments["paid_amount"].get("value"))
        elif item.get("due_amount"):
            paid = amount - money(item["due_amount"].get("value"))
        else:
            paid = amount if item.get("status") in ("PAID", "MARKED_AS_PAID") else money(0)
        recipients = item.get("primary_recipients") or [{}]
        billing = recipients[0].get("billing_info", {}) if recipients else {}
        bname = billing.get("name") or {}
        recipient_name = (
            billing.get("business_name")
            or bname.get("full_name")
            or " ".join(p for p in [bname.get("given_name"), bname.get("surname")] if p)
            or None
        )
        invoices.append(
            Invoice(
                id=item["id"],
                number=detail.get("invoice_number", item["id"]),
                status=item.get("status", ""),
                recipient_name=recipient_name,
                recipient_email=billing.get("email_address"),
                issue_date=_parse_day(detail.get("invoice_date")),
                due_date=_parse_day((detail.get("payment_term") or {}).get("due_date")),
                amount=amount,
                paid=paid,
                currency=currency,
                payment_ids=[
                    p.get("payment_id") for p in payments.get("transactions", []) if p.get("payment_id")
                ],
            )
        )
    return invoices


BOOKS_COLUMNS = [
    "order_id",
    "date",
    "customer_name",
    "customer_email",
    "channel",
    "reference",
    "amount",
    "currency",
    "status",
]


def orders_from_csv(path: str | Path) -> list[Order]:
    """Read the seller's books. Columns: see BOOKS_COLUMNS."""
    orders: list[Order] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            orders.append(
                Order(
                    order_id=row["order_id"].strip(),
                    day=date.fromisoformat(row["date"].strip()),
                    customer_name=row["customer_name"].strip(),
                    customer_email=row["customer_email"].strip().lower(),
                    channel=row["channel"].strip().lower(),
                    reference=row["reference"].strip(),
                    amount=money(row["amount"]),
                    currency=row.get("currency", "USD").strip().upper() or "USD",
                    book_status=row["status"].strip().lower(),
                )
            )
    return orders
