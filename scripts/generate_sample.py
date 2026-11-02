"""Generate the demo month for Closeout.

A fictional candle shop ("Larkspur Candle Co.") for October 2026:
  * 279 web orders paid through PayPal checkout
  * 24 wholesale orders billed with PayPal invoices
  * weekly transfers to the bank, a few refunds, one chargeback, one CAD sale
  * nine planted problems that a month-end close should catch

Everything is written in the same JSON shape the PayPal APIs return
(Transaction Search v1, Invoicing v2), so the demo exercises exactly the
same parsing and matching code as a live sandbox account.

Run:  python scripts/generate_sample.py
"""
from __future__ import annotations

import csv
import json
import random
import string
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "sample"
SEED = 20261031
YEAR, MONTH = 2026, 10
PERIOD_START = datetime(2026, 10, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
AS_OF = date(2026, 11, 2)

FEE_PCT = Decimal("3.49")
FEE_FIXED = Decimal("0.49")
INTL_PCT = Decimal("1.50")
CENT = Decimal("0.01")

rng = random.Random(SEED)
ID_CHARS = string.digits + "ABCDEFGHJKLMNPRSTUVWXY"


def d(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def txn_id() -> str:
    return "".join(rng.choice(ID_CHARS) for _ in range(17))


def inv_id() -> str:
    return "INV2-" + "-".join("".join(rng.choice(ID_CHARS) for _ in range(4)) for _ in range(4))


def fee_for(amount: Decimal, intl: bool = False, fixed: Decimal = FEE_FIXED) -> Decimal:
    pct = FEE_PCT + (INTL_PCT if intl else 0)
    return d(amount * pct / 100 + fixed)


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+0000")


FIRST = """Ava Liam Noah Emma Olivia Mia Ethan Lucas Amelia Harper Mason Logan Ella Grace Chloe Jack Leo
Nora Zoe Owen Caleb Ruby Isaac Hazel Wyatt Violet Elena Miles Iris Jonah Clara Theo Naomi Felix Lena
Sofia Julian Maya Adrian Rosa Dylan Tessa Marcus Priyanka Kenji Aisha Mateo Ingrid""".split()
LAST = """Garcia Smith Johnson Lee Patel Nguyen Brown Davis Miller Wilson Moore Clark Lewis Walker Hall Young
King Wright Lopez Hill Green Adams Baker Nelson Carter Mitchell Perez Roberts Turner Phillips Campbell
Parker Evans Edwards Collins Stewart Morris Reed Cook Morgan Bell Murphy Bailey Rivera Cooper Howard""".split()

PRICES = [18, 22, 24, 26, 28, 32, 34, 36, 38, 42, 44, 48, 54, 58, 64]


def random_basket() -> Decimal:
    items = rng.choice([1, 1, 1, 2, 2, 3])
    subtotal = sum(rng.choice(PRICES) for _ in range(items))
    shipping = Decimal("6.95") if subtotal < 50 else Decimal("0")
    return d(Decimal(subtotal) + shipping)


def random_time(day_from: int = 1, day_to: int = 31) -> datetime:
    day = rng.randint(day_from, day_to)
    # US daytime and evening, expressed in UTC.
    hour = rng.choice([13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2])
    return datetime(YEAR, MONTH, day, hour, rng.randint(0, 59), rng.randint(0, 59), tzinfo=timezone.utc)


def payer(name: str, email: str, country: str = "US") -> dict:
    given, _, surname = name.partition(" ")
    return {
        "account_id": "".join(rng.choice(ID_CHARS) for _ in range(13)),
        "email_address": email,
        "address_status": "Y",
        "payer_status": "Y",
        "payer_name": {"given_name": given, "surname": surname, "alternate_full_name": name},
        "country_code": country,
    }


def txn(
    code: str,
    when: datetime,
    amount: Decimal,
    fee: Decimal,
    currency: str = "USD",
    invoice_id: str | None = None,
    ref: str | None = None,
    who: dict | None = None,
    note: str | None = None,
) -> dict:
    info = {
        "paypal_account_id": "LARKSPURSAMPLE",
        "transaction_id": txn_id(),
        "transaction_event_code": code,
        "transaction_initiation_date": ts(when),
        "transaction_updated_date": ts(when + timedelta(seconds=rng.randint(5, 90))),
        "transaction_amount": {"currency_code": currency, "value": f"{amount:.2f}"},
        "fee_amount": {"currency_code": currency, "value": f"{fee:.2f}"},
        "transaction_status": "S",
        "protection_eligibility": "01" if code.startswith("T00") else "02",
    }
    if invoice_id:
        info["invoice_id"] = invoice_id
    if ref:
        info["paypal_reference_id"] = ref
        info["paypal_reference_id_type"] = "TXN"
    if note:
        info["transaction_note"] = note
    detail = {"transaction_info": info, "payer_info": who or {}}
    return detail


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    txns: list[dict] = []
    books: list[dict] = []
    invoices: list[dict] = []

    # ------------------------------------------------------------------ web orders
    times = sorted(random_time() for _ in range(279))
    special_people = {
        "LC-1159": ("Derek Walsh", "derek.walsh@example.com", "US"),
        "LC-1187": ("Megan Torres", "megan.torres@example.com", "US"),
        "LC-1203": ("Hannah Brooks", "hannah.brooks@example.com", "US"),
        "LC-1221": ("Oliver Bennett", "oliver.bennett@example.co.uk", "GB"),
        "LC-1234": ("Chloe Tremblay", "chloe.tremblay@example.ca", "CA"),
    }
    special_amounts = {
        "LC-1159": d(58),
        "LC-1187": d(42),
        "LC-1203": d("36.50"),
        "LC-1221": d(74),
        "LC-1234": d(40),
    }
    recorded_refunds = {"LC-1042", "LC-1098", "LC-1144"}
    payments_by_ref: dict[str, dict] = {}

    for i, when in enumerate(times):
        ref = f"LC-{1001 + i}"
        if ref in special_people:
            name, email, country = special_people[ref]
        else:
            first, last = rng.choice(FIRST), rng.choice(LAST)
            name = f"{first} {last}"
            email = f"{first.lower()}.{last.lower()}{rng.randint(1, 99)}@example.com"
            country = "US"
        amount = special_amounts.get(ref) or random_basket()
        status = "refunded" if ref in recorded_refunds else "paid"
        books.append(
            {
                "order_id": ref,
                "date": when.date().isoformat(),
                "customer_name": name,
                "customer_email": email,
                "channel": "web",
                "reference": ref,
                "amount": f"{amount:.2f}",
                "currency": "USD",
                "status": status,
            }
        )
        who = payer(name, email, country)

        if ref == "LC-1234":
            # Planted: buyer paid in Canadian dollars; PayPal converted to USD.
            cad = d(54)
            cad_fee = fee_for(cad, intl=True, fixed=Decimal("0.59"))
            pay = txn("T0006", when, cad, -cad_fee, "CAD", invoice_id=ref, who=who)
            txns.append(pay)
            pid = pay["transaction_info"]["transaction_id"]
            net_cad = cad - cad_fee
            usd = d(net_cad * Decimal("0.7124"))
            conv_time = when + timedelta(minutes=1)
            txns.append(txn("T0200", conv_time, -net_cad, d(0), "CAD", ref=pid))
            txns.append(txn("T0200", conv_time, usd, d(0), "USD", ref=pid))
            continue

        intl = country != "US"
        pay = txn("T0006", when, amount, -fee_for(amount, intl=intl), "USD", invoice_id=ref, who=who)
        txns.append(pay)
        payments_by_ref[ref] = pay

        if ref == "LC-1187":
            # Planted: double-click at checkout, customer charged twice.
            txns.append(
                txn("T0006", when + timedelta(minutes=4), amount, -fee_for(amount), "USD", invoice_id=ref, who=who)
            )

    def refund_of(ref: str, hours: int) -> None:
        pay = payments_by_ref[ref]
        info = pay["transaction_info"]
        when = datetime.strptime(info["transaction_initiation_date"], "%Y-%m-%dT%H:%M:%S+0000").replace(
            tzinfo=timezone.utc
        )
        when = min(when + timedelta(hours=hours), PERIOD_END - timedelta(hours=2))
        amount = Decimal(info["transaction_amount"]["value"])
        txns.append(
            txn("T1107", when, -amount, d(0), "USD", invoice_id=ref, ref=info["transaction_id"], who=pay["payer_info"])
        )

    for ref in sorted(recorded_refunds):
        refund_of(ref, hours=rng.randint(20, 90))
    refund_of("LC-1203", hours=26)  # Planted: refunded in PayPal, never recorded in the books.

    # Planted: chargeback on LC-1159 with a dispute fee.
    cb_pay = payments_by_ref["LC-1159"]["transaction_info"]
    cb_when = datetime.strptime(cb_pay["transaction_initiation_date"], "%Y-%m-%dT%H:%M:%S+0000").replace(
        tzinfo=timezone.utc
    )
    cb_when = min(cb_when + timedelta(days=9), PERIOD_END - timedelta(hours=3))
    txns.append(
        txn(
            "T1201",
            cb_when,
            -Decimal(cb_pay["transaction_amount"]["value"]),
            d("-15.00"),
            "USD",
            invoice_id="LC-1159",
            ref=cb_pay["transaction_id"],
            note="Chargeback: buyer reported item not received",
        )
    )

    # Planted: a payment with no order in the books (custom order paid by PayPal link).
    orphan_amount = d(75)
    txns.append(
        txn(
            "T0000",
            datetime(2026, 10, 24, 19, 12, 40, tzinfo=timezone.utc),
            orphan_amount,
            -fee_for(orphan_amount),
            "USD",
            who=payer("Priya Nair", "priya.nair@example.com"),
            note="Custom candle set - deposit",
        )
    )

    # -------------------------------------------------------------- invoices
    shops = [
        ("Fernwood General Store", "orders@fernwood.example"),
        ("Little Wren Books & Gifts", "hello@littlewren.example"),
        ("Harborlight Home", "buying@harborlight.example"),
        ("Tamarack Trading Post", "orders@tamarack.example"),
        ("Saltbox Studio", "studio@saltbox.example"),
        ("Willowbend Florist", "orders@willowbend.example"),
        ("Northfield Co-op", "purchasing@northfield.example"),
        ("Maple Row Mercantile", "orders@maplerow.example"),
        ("Brightwater Spa", "front@brightwater.example"),
        ("Quill & Ember Stationers", "orders@quillember.example"),
    ]
    invoice_amounts = [240, 280, 315, 360, 395, 420, 465, 510, 540, 585, 620, 680, 720, 790, 860]
    planted_invoices = {
        29: ("Cedar & Sage Boutique", "orders@cedarsage.example", d(450), date(2026, 10, 10), 15, "PARTIAL"),
        31: ("Hollow Oak Gift Shop", "owner@hollowoak.example", d(640), date(2026, 10, 5), 15, "UNPAID"),
        34: ("Bluebell Market", "accounts@bluebell.example", d(385), date(2026, 10, 13), 15, "UNPAID"),
        35: ("Fernwood General Store", "orders@fernwood.example", d(310), date(2026, 10, 27), 15, "OPEN"),
        36: ("Harborlight Home", "buying@harborlight.example", d(720), date(2026, 10, 29), 30, "OPEN"),
    }
    for n in range(13, 37):
        number = f"INV-2026-{n:03d}"
        if n in planted_invoices:
            shop, email, amount, issued, terms, kind = planted_invoices[n]
        else:
            shop, email = rng.choice(shops)
            amount = d(rng.choice(invoice_amounts))
            issued = date(2026, 10, rng.randint(1, 20))
            terms = rng.choice([15, 30])
            kind = "PAID"
        due = issued + timedelta(days=terms)
        status = {"PAID": "PAID", "PARTIAL": "PARTIALLY_PAID", "UNPAID": "SENT", "OPEN": "SENT"}[kind]
        contact_first, contact_last = rng.choice(FIRST), rng.choice(LAST)
        inv = {
            "id": inv_id(),
            "status": status,
            "detail": {
                "invoice_number": number,
                "invoice_date": issued.isoformat(),
                "currency_code": "USD",
                "payment_term": {"term_type": f"NET_{terms}", "due_date": due.isoformat()},
                "memo": "Wholesale candle order",
            },
            "invoicer": {"business_name": "Larkspur Candle Co.", "email_address": "sales@larkspur.example"},
            "primary_recipients": [
                {
                    "billing_info": {
                        "business_name": shop,
                        "name": {"given_name": contact_first, "surname": contact_last},
                        "email_address": email,
                    }
                }
            ],
            "amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
        }
        paid = d(0)
        pay_txns = []
        if kind in ("PAID", "PARTIAL"):
            paid = amount if kind == "PAID" else d(300)
            if kind == "PARTIAL":
                pay_day = date(2026, 10, 18)
            else:
                pay_day = min(issued + timedelta(days=rng.randint(2, 20)), date(2026, 10, 31))
            when = datetime(2026, 10, pay_day.day, rng.randint(14, 22), rng.randint(0, 59), 0, tzinfo=timezone.utc)
            who = payer(f"{contact_first} {contact_last}", email)
            who["payer_name"]["alternate_full_name"] = shop
            p = txn("T0006", when, paid, -fee_for(paid), "USD", invoice_id=number, who=who)
            txns.append(p)
            pay_txns.append(
                {
                    "payment_id": p["transaction_info"]["transaction_id"],
                    "payment_date": pay_day.isoformat(),
                    "method": "PAYPAL",
                    "type": "PAYPAL",
                    "amount": {"currency_code": "USD", "value": f"{paid:.2f}"},
                }
            )
        inv["due_amount"] = {"currency_code": "USD", "value": f"{amount - paid:.2f}"}
        if pay_txns:
            inv["payments"] = {
                "paid_amount": {"currency_code": "USD", "value": f"{paid:.2f}"},
                "transactions": pay_txns,
            }
        invoices.append(inv)
        books.append(
            {
                "order_id": number,
                "date": issued.isoformat(),
                "customer_name": shop,
                "customer_email": email,
                "channel": "invoice",
                "reference": number,
                "amount": f"{amount:.2f}",
                "currency": "USD",
                "status": "paid" if kind == "PAID" else "invoiced",
            }
        )

    # ------------------------------------------------------- weekly bank transfers
    txns.sort(key=lambda t: t["transaction_info"]["transaction_initiation_date"])
    balance = d("412.38")  # carried over from September
    out: list[dict] = []
    transfer_days = [5, 12, 19, 26]
    pending = [datetime(2026, 10, day, 10, 0, 0, tzinfo=timezone.utc) for day in transfer_days]
    for t in txns:
        when = t["transaction_info"]["transaction_initiation_date"]
        while pending and ts(pending[0]) <= when:
            amount = Decimal(int(balance - 250))
            if amount > 0:
                out.append(txn("T0400", pending[0], -amount, d(0), "USD", note="Transfer to bank account ending 2291"))
                balance -= amount
            pending.pop(0)
        out.append(t)
        info = t["transaction_info"]
        if info["transaction_amount"]["currency_code"] == "USD":
            balance += Decimal(info["transaction_amount"]["value"]) + Decimal(info["fee_amount"]["value"])
    txns = out

    payload = {
        "transaction_details": txns,
        "account_number": "LARKSPURSAMPLE",
        "start_date": ts(PERIOD_START),
        "end_date": ts(PERIOD_END),
        "last_refreshed_datetime": "2026-11-02T08:00:00+0000",
        "page": 1,
        "total_items": len(txns),
        "total_pages": 1,
        "links": [],
    }
    (OUT / "transactions.json").write_text(json.dumps(payload, indent=1))
    (OUT / "invoices.json").write_text(
        json.dumps({"items": invoices, "total_items": len(invoices), "total_pages": 1}, indent=1)
    )
    books.sort(key=lambda b: (b["date"], b["order_id"]))
    with open(OUT / "orders.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(books[0].keys()))
        writer.writeheader()
        writer.writerows(books)
    meta = {
        "business_name": "Larkspur Candle Co.",
        "book_currency": "USD",
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
        "as_of": AS_OF.isoformat(),
        "fee_schedule": {
            "percent": str(FEE_PCT),
            "fixed": str(FEE_FIXED),
            "intl_percent": str(INTL_PCT),
            "home_country": "US",
        },
        "note": "Fictional business. All names, emails and IDs are made up.",
    }
    (OUT / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"transactions: {len(txns)}  invoices: {len(invoices)}  book records: {len(books)}")


if __name__ == "__main__":
    main()
