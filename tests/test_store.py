"""Sandbox test store against a fake PayPal, plus the engine on store-shaped data."""
import csv
import dataclasses
import itertools
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.config import load_settings
from app.models import Dataset, FeeSchedule, Meta, Order, PayPalTxn
from app.paypal import PayPalClient
from app.reconcile import reconcile
from app.store import StoreError, SandboxStore


class FakePayPal:
    def __init__(self):
        self.ids = itertools.count(1)
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        if path == "/v2/checkout/orders":
            body = json.loads(request.content)
            assert body["intent"] == "CAPTURE"
            return httpx.Response(201, json={"id": f"ORDER-{next(self.ids)}", "status": "CREATED"})
        if path.endswith("/capture"):
            return httpx.Response(
                201,
                json={
                    "id": path.split("/")[-2],
                    "status": "COMPLETED",
                    "payer": {"name": {"given_name": "Sam", "surname": "Buyer"}, "email_address": "sam@example.com"},
                    "purchase_units": [
                        {
                            "payments": {
                                "captures": [
                                    {
                                        "id": f"CAP-{next(self.ids)}",
                                        "status": "COMPLETED",
                                        "seller_receivable_breakdown": {"paypal_fee": {"value": "2.17"}},
                                    }
                                ]
                            }
                        }
                    ],
                },
            )
        if path.endswith("/refund"):
            return httpx.Response(201, json={"id": "REFUND-1", "status": "COMPLETED"})
        if path == "/v2/invoicing/invoices":
            return httpx.Response(201, json={"id": f"INV2-{next(self.ids)}", "status": "DRAFT"})
        if path.endswith("/send"):
            return httpx.Response(202, json={})
        if path.endswith("/payments"):
            return httpx.Response(200, json={"payment_id": "EXTR-1"})
        return httpx.Response(404, json={"message": f"unexpected {path}"})


@pytest.fixture
def store(tmp_path):
    fake = FakePayPal()
    settings = dataclasses.replace(
        load_settings(),
        paypal_client_id="client",
        paypal_client_secret="secret",
        books_csv=tmp_path / "orders.csv",
    )
    client = PayPalClient("client", "secret", "sandbox", http=httpx.Client(transport=httpx.MockTransport(fake)))
    s = SandboxStore(settings, client)
    s.fake = fake
    return s


def books(store):
    with open(store.books, newline="") as fh:
        return list(csv.DictReader(fh))


def test_normal_sale_is_recorded_in_books(store):
    order = store.create_order("normal", "cedar", 2, None)
    result = store.capture(order["id"])
    assert result["ref"] == "LC-S001"
    assert result["warning"] is None  # $2.17 matches the default fee schedule on $48.00
    rows = books(store)
    assert rows == [
        {
            "order_id": "LC-S001", "date": date.today().isoformat(), "customer_name": "Sam Buyer",
            "customer_email": "sam@example.com", "channel": "web", "reference": "LC-S001",
            "amount": "48.00", "currency": "USD", "status": "paid",
        }
    ]


def test_mistakes_never_reach_the_books(store):
    store.capture(store.create_order("normal", "trio", 1, None)["id"])
    store.capture(store.create_order("repeat", None, 1, "LC-S001")["id"])
    store.capture(store.create_order("offbook", None, 1, None)["id"])
    store.refund_in_paypal_only("LC-S001")
    assert len(books(store)) == 1  # only the real sale
    summary = store.summary()
    assert summary["orders"][0]["charges"] == 2
    assert summary["orders"][0]["refunded_in_paypal_only"] is True
    assert len(summary["offbook"]) == 1
    with pytest.raises(StoreError):
        store.refund_in_paypal_only("LC-S001")


def test_capture_of_unknown_checkout_is_refused(store):
    with pytest.raises(StoreError):
        store.capture("ORDER-NOPE")


def test_invoices_are_created_sent_and_paid(store):
    result = store.create_invoices()
    kinds = [i["kind"] for i in result["invoices"]]
    assert kinds == ["paid", "partial", "overdue", "open"]
    sends = [c for c in store.fake.calls if c[1].endswith("/send")]
    payments = [c for c in store.fake.calls if c[1].endswith("/payments")]
    assert len(sends) == 4 and len(payments) == 2
    statuses = [r["status"] for r in books(store)]
    assert statuses == ["paid", "invoiced", "invoiced", "invoiced"]
    with pytest.raises(StoreError):
        store.create_invoices()


def test_fee_warning_when_sandbox_rate_differs(store, monkeypatch):
    store.fees = FeeSchedule(percent=Decimal("2.00"), fixed=Decimal("0.10"))
    result = store.capture(store.create_order("normal", "cedar", 2, None)["id"])
    assert "FEE_PERCENT" in result["warning"]


# -------------------------------------------------- engine on store-shaped data

def _txn(tid, kind, gross, custom=None, ref=None, code="T0006"):
    return PayPalTxn(
        id=tid, event_code=code, kind=kind,
        timestamp=datetime(2026, 11, 3, 15, int(tid[-1]), tzinfo=timezone.utc),
        gross=Decimal(gross), fee=FeeSchedule().expected_fee(Decimal(gross)) * -1 if kind == "payment" else Decimal("0"),
        currency="USD", status="S", custom=custom, payer_email="sam@example.com", payer_name="Sam Buyer",
        payer_country="US", reference_id=ref,
    )


def test_engine_matches_store_payments_by_custom_id():
    meta = Meta("Test", "USD", date(2026, 11, 1), date(2026, 11, 30), date(2026, 11, 9), FeeSchedule(), "paypal")
    orders = [
        Order("LC-S001", date(2026, 11, 3), "Sam Buyer", "sam@example.com", "web", "LC-S001", Decimal("64.00"), "USD", "paid"),
        Order("LC-S002", date(2026, 11, 3), "Sam Buyer", "sam@example.com", "web", "LC-S002", Decimal("28.00"), "USD", "paid"),
        Order("LC-S003", date(2026, 11, 3), "Sam Buyer", "sam@example.com", "web", "LC-S003", Decimal("24.00"), "USD", "paid"),
    ]
    txns = [
        _txn("CAP1", "payment", "64.00", custom="LC-S001"),
        _txn("CAP2", "payment", "64.00", custom="LC-S001"),  # charged twice
        _txn("CAP3", "payment", "28.00", custom="LC-S002"),
        _txn("CAP4", "refund", "-28.00", ref="CAP3", code="T1107"),  # refunded in PayPal only
        _txn("CAP5", "payment", "24.00", custom="LC-S003"),
        _txn("CAP6", "payment", "75.00"),  # no order behind it
    ]
    kinds = sorted(i.kind for i in reconcile(Dataset(meta, orders, txns, [])).issues)
    assert kinds == ["DUPLICATE_PAYMENT", "REFUND_NOT_RECORDED", "UNRECORDED_SALE"]
