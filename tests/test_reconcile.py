from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.models import Dataset, FeeSchedule, Invoice, Meta, Order, PayPalTxn
from app.normalize import classify_event, parse_paypal_datetime
from app.reconcile import reconcile
from app.sources import load_sample


@pytest.fixture(scope="module")
def october():
    return reconcile(load_sample())


def test_sample_month_counts(october):
    s = october.summary
    assert s.txn_count == 312
    assert s.order_count == 303
    assert s.issue_count == 9


def test_sample_month_finds_every_planted_issue(october):
    found = {i.id: (i.kind, i.amount_at_risk) for i in october.issues}
    expected = {
        "DUP-LC-1187": ("DUPLICATE_PAYMENT", Decimal("42.00")),
        "UNPAID-INV-2026-031": ("UNPAID_INVOICE", Decimal("640.00")),
        "UNPAID-INV-2026-034": ("UNPAID_INVOICE", Decimal("385.00")),
        "PARTIAL-INV-2026-029": ("PARTIAL_PAYMENT", Decimal("150.00")),
        "REFUND-LC-1203": ("REFUND_NOT_RECORDED", Decimal("36.50")),
        "CB-LC-1159": ("CHARGEBACK", Decimal("73.00")),
        "FX-LC-1234": ("FX_DIFFERENCE", Decimal("1.98")),
        "FEE-LC-1221": ("FEE_VARIANCE", Decimal("1.11")),
    }
    for issue_id, value in expected.items():
        assert found[issue_id] == value, issue_id
    orphans = [i for i in october.issues if i.kind == "UNRECORDED_SALE"]
    assert len(orphans) == 1 and orphans[0].amount_at_risk == Decimal("75.00")


def test_overdue_days_use_close_date(october):
    issue = next(i for i in october.issues if i.id == "UNPAID-INV-2026-031")
    assert issue.facts["days_overdue"] == 13
    assert issue.severity == "high"


def test_every_money_moving_action_is_flagged(october):
    dup = next(i for i in october.issues if i.kind == "DUPLICATE_PAYMENT")
    refund = next(a for a in dup.actions if a.type == "refund_duplicate")
    assert refund.moves_money and refund.contacts_customer
    assert refund.params["amount"] == "42.00"


def test_recorded_refunds_are_not_issues(october):
    refunded = [r for r in october.rows if r.status == "refunded"]
    assert len(refunded) == 3


def test_open_invoices_not_yet_due_are_not_issues(october):
    open_rows = [r for r in october.rows if r.status == "open"]
    assert {r.reference for r in open_rows} == {"INV-2026-035", "INV-2026-036"}


# ----------------------------------------------------------------- unit level

def _meta(**kw):
    return Meta(
        business_name="Test Shop",
        book_currency="USD",
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        as_of=kw.get("as_of", date(2026, 11, 2)),
        fee_schedule=FeeSchedule(),
        source="sample",
        data_through=kw.get("data_through"),
    )


def _pay(tid, amount, ref=None, email="a@example.com", day=5, fee=None, country="US"):
    amount = Decimal(amount)
    fee = FeeSchedule().expected_fee(amount) if fee is None else Decimal(fee)
    return PayPalTxn(
        id=tid,
        event_code="T0006",
        kind="payment",
        timestamp=datetime(2026, 10, day, 15, 0, tzinfo=timezone.utc),
        gross=amount,
        fee=-fee,
        currency="USD",
        status="S",
        invoice_ref=ref,
        payer_email=email,
        payer_name="A Buyer",
        payer_country=country,
    )


def _order(ref, amount, email="a@example.com", day=5, status="paid", channel="web"):
    return Order(ref, date(2026, 10, day), "A Buyer", email, channel, ref, Decimal(amount), "USD", status)


def test_clean_order_matches():
    ds = Dataset(_meta(), [_order("O-1", "20.00")], [_pay("T1", "20.00", "O-1")], [])
    result = reconcile(ds)
    assert result.issues == []
    assert result.rows[0].status == "matched"


def test_payment_without_reference_matches_by_email_and_amount():
    ds = Dataset(_meta(), [_order("O-1", "20.00", day=5)], [_pay("T1", "20.00", None, day=6)], [])
    result = reconcile(ds)
    assert result.issues == []
    assert "email" in (result.rows[0].note or "")


def test_missing_payment_is_flagged():
    ds = Dataset(_meta(), [_order("O-1", "20.00")], [], [])
    result = reconcile(ds)
    assert [i.kind for i in result.issues] == ["MISSING_PAYMENT"]


def _lagging_month():
    # PayPal's transaction search lags new payments (up to 3 hours). Here its data
    # runs through midday Oct 20, so a sale booked on Oct 20 may not be reported yet.
    meta = _meta(data_through=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))
    orders = [_order("O-OK", "25.00", day=8), _order("O-OLD", "20.00", day=12), _order("O-NEW", "30.00", day=20)]
    return Dataset(meta, orders, [_pay("T1", "25.00", "O-OK", day=8)], [])


def test_sale_paypal_has_not_reported_yet_is_waiting_not_missing():
    result = reconcile(_lagging_month())
    status = {r.reference: r.status for r in result.rows}
    assert status == {"O-OK": "matched", "O-OLD": "issue", "O-NEW": "waiting"}
    # Only the sale PayPal's data already covers is flagged.
    assert [(i.kind, i.id) for i in result.issues] == [("MISSING_PAYMENT", "MISSING-O-OLD")]
    assert result.summary.waiting_count == 1


def test_waiting_sales_are_left_out_of_the_match_rate():
    s = reconcile(_lagging_month()).summary
    assert s.matched_count == 1
    assert s.match_rate == Decimal("50.0")  # 1 clean of the 2 records PayPal can confirm


def test_without_refresh_time_missing_payments_are_still_flagged():
    ds = _lagging_month()
    ds.meta.data_through = None
    result = reconcile(ds)
    assert sorted(i.id for i in result.issues) == ["MISSING-O-NEW", "MISSING-O-OLD"]
    assert result.summary.waiting_count == 0


def test_amount_mismatch_is_flagged():
    ds = Dataset(_meta(), [_order("O-1", "20.00")], [_pay("T1", "18.00", "O-1")], [])
    issue = reconcile(ds).issues[0]
    assert issue.kind == "AMOUNT_MISMATCH" and issue.amount_at_risk == Decimal("2.00")


def test_invoice_not_yet_due_is_open():
    inv = Invoice("INV2-X", "INV-1", "SENT", "Shop", "s@example.com", date(2026, 10, 20), date(2026, 11, 20),
                  Decimal("100.00"), Decimal("0.00"), "USD")
    ds = Dataset(_meta(), [_order("INV-1", "100.00", channel="invoice", status="invoiced")], [], [inv])
    result = reconcile(ds)
    assert result.issues == [] and result.rows[0].status == "open"


def test_fee_schedule_math():
    s = FeeSchedule()
    assert s.expected_fee(Decimal("74.00")) == Decimal("3.07")
    assert s.expected_fee(Decimal("74.00"), international=True) == Decimal("4.18")


def test_event_code_classification():
    assert classify_event("T0006") == "payment"
    assert classify_event("T1107") == "refund"
    assert classify_event("T1201") == "chargeback"
    assert classify_event("T0400") == "withdrawal"
    assert classify_event("T0200") == "fx"
    assert classify_event("T9999") == "other"


@pytest.mark.parametrize(
    "raw",
    ["2026-10-03T14:22:11+0000", "2026-10-03T14:22:11Z", "2026-10-03T14:22:11+00:00"],
)
def test_paypal_datetime_formats(raw):
    assert parse_paypal_datetime(raw) == datetime(2026, 10, 3, 14, 22, 11, tzinfo=timezone.utc)


def _partial_invoice(due):
    inv = Invoice("INV2-P", "INV-P", "PARTIALLY_PAID", "Cedar & Sage", "c@example.com", date(2026, 10, 20), due,
                  Decimal("450.00"), Decimal("300.00"), "USD")
    order = _order("INV-P", "450.00", channel="invoice", status="invoiced")
    return reconcile(Dataset(_meta(), [order], [], [inv])).issues[0]


def test_partial_payment_not_yet_due_is_low_priority():
    issue = _partial_invoice(date(2026, 11, 16))  # close date is Nov 2
    assert (issue.kind, issue.severity) == ("PARTIAL_PAYMENT", "low")
    assert issue.facts["balance_status"] == "Not due yet" and "days_overdue" not in issue.facts


def test_overdue_partial_payment_is_medium_priority():
    issue = _partial_invoice(date(2026, 10, 25))
    assert issue.severity == "medium" and issue.facts["balance_status"] == "Overdue"
    assert issue.facts["days_overdue"] == 8
