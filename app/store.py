"""The sandbox test store.

Makes real PayPal sandbox activity for Closeout to reconcile, including the
messy cases on purpose. It plays the part of a shop's order system: every
normal sale is written to the books CSV, exactly as a real store would.
The messy cases are the ones that never make it into the books:

* the customer pays twice          -> a second capture for the same order
* a sale that skips your books     -> a payment with no order reference
* a refund done only in PayPal     -> refunded through the API, books untouched
* wholesale invoices               -> paid, partly paid, due tomorrow, not yet due

Sandbox only. Turn it off with ENABLE_TEST_STORE=false (e.g. on a hosted demo).
"""
from __future__ import annotations

import csv
import json
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .config import ROOT, Settings
from .models import FeeSchedule, fmt_money
from .normalize import BOOKS_COLUMNS
from .paypal import PayPalClient

PRODUCTS = [
    {"id": "cedar", "name": "Cedar & Smoke candle", "price": "24.00"},
    {"id": "lavender", "name": "Lavender Field candle", "price": "28.00"},
    {"id": "seasalt", "name": "Sea Salt & Driftwood candle", "price": "32.00"},
    {"id": "trio", "name": "Gift set of three candles", "price": "64.00"},
    {"id": "trimmer", "name": "Brass wick trimmer", "price": "18.00"},
]
CUSTOM_ORDER = {"name": "Custom candle set (deposit)", "price": "75.00"}

# shop, email, amount, kind, days until due, amount recorded as paid
INVOICE_SPECS = [
    ("Fernwood General Store", "fernwood.orders@example.com", "380.00", "paid", 14, "380.00"),
    ("Cedar & Sage Boutique", "cedar.sage@example.com", "450.00", "partial", 14, "300.00"),
    ("Hollow Oak Gift Shop", "hollow.oak@example.com", "640.00", "overdue", 1, None),
    ("Harborlight Home", "harborlight.buying@example.com", "520.00", "open", 30, None),
]


class StoreError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SandboxStore:
    def __init__(self, settings: Settings, client: PayPalClient):
        self.settings = settings
        self.client = client
        self.books = settings.books_csv
        self.state_path = self.books.parent / "store.json"
        self.lock = threading.Lock()
        self.pending: dict[str, dict] = {}  # PayPal order id -> what it is for
        self.state = self._load()
        self.fees = FeeSchedule(
            percent=settings.fee_percent,
            fixed=settings.fee_fixed,
            intl_percent=settings.intl_fee_percent,
            home_country=settings.home_country,
        )

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except json.JSONDecodeError:
                pass
        return {"orders": {}, "offbook": [], "invoices": [], "log": [], "next_number": 1}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=1))

    def _append_books(self, row: dict) -> None:
        self.books.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.books.exists() or self.books.stat().st_size == 0
        with open(self.books, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=BOOKS_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    def _log(self, text: str) -> None:
        self.state["log"].insert(0, {"at": _now(), "text": text})
        del self.state["log"][60:]

    # ------------------------------------------------------------------ views
    def summary(self) -> dict:
        try:
            books_path = str(self.books.relative_to(ROOT))
        except ValueError:
            books_path = str(self.books)
        return {
            "client_id": self.settings.paypal_client_id,
            "env": self.settings.paypal_env,
            "currency": "USD",
            "products": PRODUCTS,
            "custom_order": CUSTOM_ORDER,
            "orders": [
                {
                    "ref": ref,
                    "amount": o["amount"],
                    "customer": o.get("customer"),
                    "description": o.get("description"),
                    "charges": len(o.get("captures", [])),
                    "refunded_in_paypal_only": o.get("refunded", False),
                }
                for ref, o in sorted(self.state["orders"].items())
            ],
            "offbook": self.state["offbook"],
            "invoices": self.state["invoices"],
            "log": self.state["log"],
            "books_path": books_path,
        }

    # --------------------------------------------------------------- checkout
    def create_order(self, scenario: str, product_id: str | None, quantity: int, ref: str | None) -> dict:
        quantity = max(1, min(int(quantity or 1), 5))
        with self.lock:
            if scenario == "normal":
                product = next((p for p in PRODUCTS if p["id"] == product_id), None)
                if not product:
                    raise StoreError("Pick a product first.")
                amount = Decimal(product["price"]) * quantity
                ref = f"LC-S{self.state['next_number']:03d}"
                self.state["next_number"] += 1
                self._save()
                description = f"{quantity} x {product['name']}"
                custom_id = ref
            elif scenario == "repeat":
                order = self.state["orders"].get(ref or "")
                if not order:
                    raise StoreError("Pick a completed order to charge again.")
                amount = Decimal(order["amount"])
                description = order["description"]
                custom_id = ref
            elif scenario == "offbook":
                amount = Decimal(CUSTOM_ORDER["price"])
                description = CUSTOM_ORDER["name"]
                custom_id = None
                ref = None
            else:
                raise StoreError(f"Unknown scenario {scenario!r}")
        # Normal sales carry the order number as invoice_id too (what most shops send).
        # A repeat charge can't reuse it: PayPal blocks duplicate invoice IDs by default.
        invoice_id = ref if scenario == "normal" else None
        created = self.client.create_order(amount, "USD", description, custom_id, invoice_id=invoice_id)
        self.pending[created["id"]] = {
            "scenario": scenario,
            "ref": ref,
            "amount": f"{amount:.2f}",
            "description": description,
        }
        return {"id": created["id"]}

    def capture(self, paypal_order_id: str) -> dict:
        pending = self.pending.pop(paypal_order_id, None)
        if pending is None:
            raise StoreError("Closeout doesn't know this checkout. Reload the store page and try again.")
        result = self.client.capture_order(paypal_order_id)
        capture = result["purchase_units"][0]["payments"]["captures"][0]
        payer = result.get("payer") or {}
        name = " ".join(
            p for p in [(payer.get("name") or {}).get("given_name"), (payer.get("name") or {}).get("surname")] if p
        ) or "Sandbox buyer"
        email = (payer.get("email_address") or "buyer@example.com").lower()
        fee = ((capture.get("seller_receivable_breakdown") or {}).get("paypal_fee") or {}).get("value")
        amount = Decimal(pending["amount"])
        ref = pending["ref"]

        with self.lock:
            if pending["scenario"] == "normal":
                self.state["orders"][ref] = {
                    "amount": pending["amount"],
                    "description": pending["description"],
                    "customer": name,
                    "email": email,
                    "captures": [capture["id"]],
                    "refunded": False,
                    "day": date.today().isoformat(),
                }
                self._append_books(
                    {
                        "order_id": ref,
                        "date": date.today().isoformat(),
                        "customer_name": name,
                        "customer_email": email,
                        "channel": "web",
                        "reference": ref,
                        "amount": pending["amount"],
                        "currency": "USD",
                        "status": "paid",
                    }
                )
                text = f"Sold {ref} ({fmt_money(amount)}) to {name} and recorded it in your books."
            elif pending["scenario"] == "repeat":
                self.state["orders"][ref]["captures"].append(capture["id"])
                text = f"Charged {ref} a second time ({fmt_money(amount)}). Your books still show one sale."
            else:
                self.state["offbook"].append(
                    {"capture_id": capture["id"], "amount": pending["amount"], "customer": name, "at": _now()}
                )
                text = f"Took a {fmt_money(amount)} payment from {name} that never reached your books."
            if fee:
                text += f" PayPal fee: {fmt_money(Decimal(fee))}."
            self._log(text)
            self._save()

        warning = None
        if fee and pending["scenario"] != "offbook":
            expected = self.fees.expected_fee(amount)
            if abs(Decimal(fee) - expected) > Decimal("0.05"):
                warning = (
                    f"PayPal charged {fmt_money(Decimal(fee))} on this sale, but your fee schedule expects "
                    f"{fmt_money(expected)}. Set FEE_PERCENT and FEE_FIXED in .env to your sandbox account's "
                    f"rate so Closeout doesn't flag every sale."
                )
        return {"ref": ref, "capture_id": capture["id"], "status": capture.get("status"), "message": text,
                "warning": warning}

    # ---------------------------------------------------------------- refunds
    def refund_in_paypal_only(self, ref: str) -> dict:
        with self.lock:
            order = self.state["orders"].get(ref or "")
            if not order:
                raise StoreError("Pick a completed order to refund.")
            if order.get("refunded"):
                raise StoreError(f"{ref} was already refunded.")
        result = self.client.refund_capture(
            order["captures"][0],
            Decimal(order["amount"]),
            "USD",
            "Refund issued from the Closeout test store",
            request_id=f"closeout-store-refund-{ref}",
        )
        with self.lock:
            order["refunded"] = True
            text = f"Refunded {ref} ({fmt_money(Decimal(order['amount']))}) in PayPal only. Your books still say it's paid."
            self._log(text)
            self._save()
        return {"ref": ref, "refund_id": result.get("id"), "status": result.get("status"), "message": text}

    # --------------------------------------------------------------- invoices
    def create_invoices(self) -> dict:
        with self.lock:
            if self.state["invoices"]:
                raise StoreError("The test invoices already exist.")
        today = date.today()
        stamp = datetime.now().strftime("%m%d%H%M")
        created = []
        for i, (shop, email, amount, kind, due_in, paid) in enumerate(INVOICE_SPECS, start=1):
            number = f"INV-{stamp}-{i}"
            body = {
                "detail": {
                    "invoice_number": number,
                    "invoice_date": today.isoformat(),
                    "currency_code": "USD",
                    "note": "Wholesale candle order",
                    "payment_term": {
                        "term_type": "DUE_ON_DATE_SPECIFIED",
                        "due_date": (today + timedelta(days=due_in)).isoformat(),
                    },
                },
                "primary_recipients": [{"billing_info": {"business_name": shop, "email_address": email}}],
                "items": [
                    {
                        "name": "Wholesale candles",
                        "quantity": "1",
                        "unit_amount": {"currency_code": "USD", "value": amount},
                    }
                ],
            }
            invoice = self.client.create_invoice(body)
            invoice_id = invoice.get("id") or (invoice.get("href") or "").rstrip("/").rsplit("/", 1)[-1]
            if not invoice_id:
                raise StoreError(f"PayPal didn't return an ID for invoice {number}.")
            self.client.send_invoice(invoice_id)
            if paid:
                self.client.record_invoice_payment(invoice_id, Decimal(paid), "USD", today.isoformat())
            self._append_books(
                {
                    "order_id": number,
                    "date": today.isoformat(),
                    "customer_name": shop,
                    "customer_email": email,
                    "channel": "invoice",
                    "reference": number,
                    "amount": amount,
                    "currency": "USD",
                    "status": "paid" if kind == "paid" else "invoiced",
                }
            )
            created.append(
                {
                    "number": number,
                    "id": invoice_id,
                    "shop": shop,
                    "amount": amount,
                    "kind": kind,
                    "due": (today + timedelta(days=due_in)).isoformat(),
                    "paid": paid or "0.00",
                }
            )
        with self.lock:
            self.state["invoices"] = created
            self._log(
                f"Created {len(created)} wholesale invoices in PayPal: one paid, one partly paid, "
                f"one due tomorrow and one not due yet."
            )
            self._save()
        return {"invoices": created}
