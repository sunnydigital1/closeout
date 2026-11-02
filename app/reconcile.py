"""The reconciliation engine.

Deterministic by design: every match, difference and dollar amount is computed
here in plain code. The AI layer only explains these results in plain English
and is never allowed to introduce numbers of its own.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .models import (
    Action,
    Dataset,
    FeeSchedule,
    Invoice,
    Issue,
    LedgerRow,
    Order,
    PayPalTxn,
    ReconResult,
    Summary,
    fmt_money,
    money,
)

ZERO = Decimal("0.00")
PENNY = Decimal("0.01")

ISSUE_PREFIX = {
    "DUPLICATE_PAYMENT": "DUP",
    "UNPAID_INVOICE": "UNPAID",
    "PARTIAL_PAYMENT": "PARTIAL",
    "REFUND_NOT_RECORDED": "REFUND",
    "CHARGEBACK": "CB",
    "FEE_VARIANCE": "FEE",
    "FX_DIFFERENCE": "FX",
    "UNRECORDED_SALE": "NOREC",
    "MISSING_PAYMENT": "MISSING",
    "MISSING_INVOICE": "NOINV",
    "AMOUNT_MISMATCH": "AMT",
}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

OPEN_INVOICE_STATUSES = {"SENT", "UNPAID", "PAYMENT_PENDING", "SCHEDULED"}
PAID_INVOICE_STATUSES = {"PAID", "MARKED_AS_PAID"}


def _journal(memo: str, day, lines: list[tuple[str, Decimal, Decimal]]) -> dict:
    return {
        "date": day.isoformat(),
        "memo": memo,
        "lines": [
            {"account": acct, "debit": f"{dr:.2f}", "credit": f"{cr:.2f}"} for acct, dr, cr in lines
        ],
    }


def _mark_reviewed() -> Action:
    return Action(type="mark_reviewed", label="Mark as reviewed")


class _Engine:
    def __init__(self, ds: Dataset):
        self.ds = ds
        self.meta = ds.meta
        self.sched: FeeSchedule = ds.meta.fee_schedule
        self.ccy = ds.meta.book_currency
        self.payments = [t for t in ds.txns if t.kind == "payment"]
        self.by_ref: dict[str, list[PayPalTxn]] = defaultdict(list)
        self.by_id: dict[str, PayPalTxn] = {t.id: t for t in ds.txns}
        for p in self.payments:
            # Stores pass their order number to PayPal as invoice_id or custom_id.
            for ref in {p.invoice_ref, p.custom} - {None, ""}:
                self.by_ref[ref].append(p)
        self.follow_ups: dict[str, list[PayPalTxn]] = defaultdict(list)
        for t in ds.txns:
            if t.reference_id and t.kind != "payment":
                self.follow_ups[t.reference_id].append(t)
        self.invoices = {inv.number: inv for inv in ds.invoices}
        self.used: set[str] = set()
        self.rows: list[LedgerRow] = []
        self.issues: list[Issue] = []

    # ------------------------------------------------------------------ helpers
    def _issue(self, kind, severity, title, at_risk, row_id, ref, facts, actions) -> Issue:
        issue = Issue(
            id=f"{ISSUE_PREFIX[kind]}-{ref}",
            kind=kind,
            severity=severity,
            title=title,
            amount_at_risk=money(at_risk),
            row_id=row_id,
            facts=facts,
            actions=actions,
        )
        self.issues.append(issue)
        return issue

    def _claim(self, txns: list[PayPalTxn]) -> None:
        for t in txns:
            self.used.add(t.id)

    def _row(self, order: Order, txns: list[PayPalTxn], status: str, issue: Issue | None = None, note=None):
        received = sum((t.gross for t in txns), ZERO) if txns else None
        fee = sum((t.fee for t in txns), ZERO) if txns else None
        net = (received + fee) if txns else None
        row = LedgerRow(
            id=f"ROW-{order.order_id}",
            day=order.day,
            reference=order.reference,
            customer=order.customer_name,
            channel=order.channel,
            currency=txns[0].currency if txns else order.currency,
            expected=order.amount,
            received=received,
            fee=fee,
            net=net,
            status=status,
            issue_id=issue.id if issue else None,
            txn_ids=[t.id for t in txns],
            note=note,
        )
        self.rows.append(row)
        return row

    # ----------------------------------------------------------- web orders
    def web_order(self, order: Order) -> None:
        row_id = f"ROW-{order.order_id}"
        pays = [p for p in self.by_ref.get(order.reference, []) if p.id not in self.used]
        note = None
        if not pays:
            pays = self._fuzzy_match(order)
            if pays:
                note = "Matched by customer email and amount (no order reference on the payment)"
        self._claim(pays)

        if not pays:
            if order.book_status == "paid":
                issue = self._issue(
                    "MISSING_PAYMENT",
                    "high",
                    f"Order {order.reference} is marked paid, but PayPal has no payment",
                    order.amount,
                    row_id,
                    order.reference,
                    {
                        "order": order.reference,
                        "customer": order.customer_name,
                        "order_date": order.day.isoformat(),
                        "order_amount": fmt_money(order.amount, order.currency),
                        "status_in_books": order.book_status,
                        "paypal_payments_found": 0,
                    },
                    [_mark_reviewed()],
                )
                self._row(order, [], "issue", issue)
            else:
                self._row(order, [], "open", note="No payment yet")
            return

        if len(pays) > 1:
            self._duplicate(order, pays, row_id)
            return

        p = pays[0]
        follow = self.follow_ups.get(p.id, [])
        refunds = [f for f in follow if f.kind == "refund"]
        chargebacks = [f for f in follow if f.kind in ("chargeback", "reversal")]
        fx_legs = [f for f in follow if f.kind == "fx"]

        if chargebacks and order.book_status != "refunded":
            self._chargeback(order, p, chargebacks, row_id)
        elif refunds and order.book_status != "refunded":
            self._refund_not_recorded(order, p, refunds, row_id)
        elif refunds:
            total = -sum((r.gross for r in refunds), ZERO)
            if abs(total - order.amount) <= PENNY:
                self._row(order, [p] + refunds, "refunded", note=f"Refunded {fmt_money(total)}")
            else:
                self._amount_mismatch(order, [p] + refunds, row_id, total, label="refund")
        elif p.currency != order.currency:
            self._fx(order, p, fx_legs, row_id)
        elif abs(p.gross - order.amount) > PENNY:
            self._amount_mismatch(order, [p], row_id, p.gross)
        else:
            self._fee_check(order, p, row_id, note)

    def _fuzzy_match(self, order: Order) -> list[PayPalTxn]:
        for p in self.payments:
            if p.id in self.used or p.invoice_ref or p.custom:
                continue
            if not p.payer_email or p.payer_email.lower() != order.customer_email:
                continue
            if p.currency != order.currency or abs(p.gross - order.amount) > PENNY:
                continue
            if abs((p.day - order.day).days) <= 3:
                return [p]
        return []

    def _duplicate(self, order: Order, pays: list[PayPalTxn], row_id: str) -> None:
        pays = sorted(pays, key=lambda t: t.timestamp)
        first, extras = pays[0], pays[1:]
        extra = extras[0]
        dup_total = sum((e.gross for e in extras), ZERO)
        minutes = int((extra.timestamp - first.timestamp).total_seconds() // 60)
        issue = self._issue(
            "DUPLICATE_PAYMENT",
            "high",
            f"Customer paid order {order.reference} twice",
            dup_total,
            row_id,
            order.reference,
            {
                "order": order.reference,
                "customer": order.customer_name,
                "customer_email": order.customer_email,
                "order_amount": fmt_money(order.amount),
                "first_payment_id": first.id,
                "first_payment_time": first.timestamp.strftime("%Y-%m-%d %H:%M UTC"),
                "second_payment_id": extra.id,
                "second_payment_time": extra.timestamp.strftime("%Y-%m-%d %H:%M UTC"),
                "minutes_apart": minutes,
                "duplicate_amount": fmt_money(dup_total),
                "fee_on_duplicate": fmt_money(-extra.fee),
            },
            [
                Action(
                    type="refund_duplicate",
                    label=f"Refund the duplicate {fmt_money(extra.gross)}",
                    moves_money=True,
                    contacts_customer=True,
                    params={
                        "capture_id": extra.id,
                        "amount": f"{extra.gross:.2f}",
                        "currency": extra.currency,
                        "payer_email": extra.payer_email,
                    },
                ),
                _mark_reviewed(),
            ],
        )
        self._row(order, pays, "issue", issue)

    def _chargeback(self, order: Order, p: PayPalTxn, cbs: list[PayPalTxn], row_id: str) -> None:
        cb = cbs[0]
        pulled = -cb.gross
        dispute_fee = -cb.fee if cb.fee < 0 else ZERO
        total_hit = pulled + dispute_fee
        issue = self._issue(
            "CHARGEBACK",
            "high",
            f"Chargeback on order {order.reference}",
            total_hit,
            row_id,
            order.reference,
            {
                "order": order.reference,
                "customer": order.customer_name,
                "order_amount": fmt_money(order.amount),
                "payment_id": p.id,
                "chargeback_id": cb.id,
                "chargeback_date": cb.day.isoformat(),
                "amount_reversed": fmt_money(pulled),
                "dispute_fee": fmt_money(dispute_fee),
                "total_taken_back": fmt_money(total_hit),
                "status_in_books": order.book_status,
            },
            [
                Action(
                    type="review_dispute",
                    label="Review the dispute in PayPal",
                    params={"dispute_txn": cb.id},
                ),
                Action(
                    type="record_adjustment",
                    label="Record the chargeback in your books",
                    params={
                        "entry": _journal(
                            f"Chargeback on order {order.reference} ({cb.id})",
                            cb.day,
                            [
                                ("Chargebacks", pulled, ZERO),
                                ("Payment processing fees", dispute_fee, ZERO),
                                ("PayPal clearing", ZERO, total_hit),
                            ],
                        )
                    },
                ),
                _mark_reviewed(),
            ],
        )
        self._row(order, [p] + cbs, "issue", issue)

    def _refund_not_recorded(self, order: Order, p: PayPalTxn, refunds: list[PayPalTxn], row_id: str) -> None:
        total = -sum((r.gross for r in refunds), ZERO)
        last = max(refunds, key=lambda r: r.timestamp)
        issue = self._issue(
            "REFUND_NOT_RECORDED",
            "medium",
            f"Refund on order {order.reference} isn't in your books",
            total,
            row_id,
            order.reference,
            {
                "order": order.reference,
                "customer": order.customer_name,
                "order_amount": fmt_money(order.amount),
                "refund_amount": fmt_money(total),
                "refund_date": last.day.isoformat(),
                "refund_id": last.id,
                "status_in_books": order.book_status,
            },
            [
                Action(
                    type="record_adjustment",
                    label="Record the refund in your books",
                    params={
                        "entry": _journal(
                            f"Refund on order {order.reference} ({last.id})",
                            last.day,
                            [
                                ("Sales returns & refunds", total, ZERO),
                                ("PayPal clearing", ZERO, total),
                            ],
                        )
                    },
                ),
                _mark_reviewed(),
            ],
        )
        self._row(order, [p] + refunds, "issue", issue)

    def _fx(self, order: Order, p: PayPalTxn, legs: list[PayPalTxn], row_id: str) -> None:
        received = sum((l.gross for l in legs if l.currency == order.currency and l.gross > 0), ZERO)
        foreign_net = p.gross + p.fee
        expected_fee = self.sched.expected_fee(order.amount)
        expected_net = order.amount - expected_fee
        shortfall = expected_net - received
        if received > ZERO and shortfall <= self.sched.tolerance:
            row = self._row(order, [p] + legs, "matched", note=f"Paid in {p.currency}, converted")
            row.currency = order.currency
            row.received, row.fee, row.net = None, None, received
            return
        facts = {
            "order": order.reference,
            "customer": order.customer_name,
            "order_amount": fmt_money(order.amount),
            "buyer_paid": fmt_money(p.gross, p.currency),
            "paypal_fee": fmt_money(-p.fee, p.currency),
            "net_in_foreign_currency": fmt_money(foreign_net, p.currency),
            "converted_to": fmt_money(received) if received else "not converted yet",
            "expected_net": fmt_money(expected_net),
            "shortfall": fmt_money(shortfall) if received else "unknown until converted",
            "payment_currency": p.currency,
        }
        issue = self._issue(
            "FX_DIFFERENCE",
            "low",
            f"Currency conversion shortfall on order {order.reference}",
            shortfall if received else ZERO,
            row_id,
            order.reference,
            facts,
            [
                Action(
                    type="record_adjustment",
                    label="Record the conversion loss",
                    params={
                        "entry": _journal(
                            f"FX loss on order {order.reference} ({p.id})",
                            p.day,
                            [("Foreign exchange losses", shortfall, ZERO), ("PayPal clearing", ZERO, shortfall)],
                        )
                    },
                ),
                _mark_reviewed(),
            ],
        )
        row = self._row(order, [p] + legs, "issue", issue)
        # Gross and fee were in another currency; show what actually landed in book
        # currency and compare it with the net a normal sale would have produced.
        row.currency = order.currency
        row.received = None
        row.fee = None
        row.net = received if received else None
        row.diff = -shortfall if received else None
        row.note = f"Paid {fmt_money(p.gross, p.currency)}, converted to {fmt_money(received)}"

    def _amount_mismatch(self, order: Order, txns: list[PayPalTxn], row_id: str, got: Decimal, label="payment"):
        diff = got - order.amount
        issue = self._issue(
            "AMOUNT_MISMATCH",
            "medium",
            f"Amount mismatch on order {order.reference}",
            abs(diff),
            row_id,
            order.reference,
            {
                "order": order.reference,
                "customer": order.customer_name,
                "order_amount": fmt_money(order.amount),
                f"{label}_amount": fmt_money(got),
                "difference": fmt_money(diff),
            },
            [_mark_reviewed()],
        )
        self._row(order, txns, "issue", issue)

    def _fee_check(self, order: Order, p: PayPalTxn, row_id: str, note: str | None) -> None:
        actual = -p.fee
        expected = self.sched.expected_fee(p.gross)
        over = actual - expected
        if over <= self.sched.tolerance:
            self._row(order, [p], "matched", note=note)
            return
        international = bool(p.payer_country) and p.payer_country != self.sched.home_country
        expected_intl = self.sched.expected_fee(p.gross, international=True)
        facts = {
            "order": order.reference,
            "customer": order.customer_name,
            "sale_amount": fmt_money(p.gross),
            "fee_charged": fmt_money(actual),
            "expected_fee": fmt_money(expected),
            "extra_fee": fmt_money(over),
            "buyer_country": p.payer_country or "unknown",
            "your_fee_schedule": self.sched.describe(),
        }
        if international and abs(actual - expected_intl) <= self.sched.tolerance:
            facts["matches_international_rate"] = "yes"
            facts["international_fee_rate"] = f"{self.sched.intl_percent}%"
        issue = self._issue(
            "FEE_VARIANCE",
            "low",
            f"Higher fee than expected on order {order.reference}",
            over,
            row_id,
            order.reference,
            facts,
            [
                _mark_reviewed(),
                Action(
                    type="record_adjustment",
                    label="Record the extra fee",
                    params={
                        "entry": _journal(
                            f"Additional PayPal fee on order {order.reference} ({p.id})",
                            p.day,
                            [("Payment processing fees", over, ZERO), ("PayPal clearing", ZERO, over)],
                        )
                    },
                ),
            ],
        )
        self._row(order, [p], "issue", issue, note=note)

    # ------------------------------------------------------- invoice orders
    def invoice_order(self, order: Order) -> None:
        row_id = f"ROW-{order.order_id}"
        inv = self.invoices.get(order.reference)
        if inv is None:
            issue = self._issue(
                "MISSING_INVOICE",
                "medium",
                f"Invoice {order.reference} isn't in PayPal",
                order.amount,
                row_id,
                order.reference,
                {
                    "invoice_number": order.reference,
                    "customer": order.customer_name,
                    "amount_in_books": fmt_money(order.amount),
                },
                [_mark_reviewed()],
            )
            self._row(order, [], "issue", issue)
            return

        pays = [p for p in self.by_ref.get(inv.number, []) if p.id not in self.used]
        for pid in inv.payment_ids:
            t = self.by_id.get(pid)
            if t and t.id not in self.used and t not in pays:
                pays.append(t)
        self._claim(pays)
        as_of = self.meta.as_of
        days_overdue = (as_of - inv.due_date).days if inv.due_date else 0

        if inv.status in PAID_INVOICE_STATUSES:
            if abs(inv.paid - order.amount) > PENNY:
                self._amount_mismatch(order, pays, row_id, inv.paid)
            else:
                self._row(order, pays, "matched")
            return

        reminder = Action(
            type="send_reminder",
            label=f"Send a payment reminder for {fmt_money(inv.due)}",
            contacts_customer=True,
            params={
                "invoice_id": inv.id,
                "invoice_number": inv.number,
                "recipient_email": inv.recipient_email,
                "amount_due": f"{inv.due:.2f}",
            },
        )
        base_facts = {
            "invoice_number": inv.number,
            "customer": inv.recipient_name or order.customer_name,
            "customer_email": inv.recipient_email or order.customer_email,
            "invoice_amount": fmt_money(inv.amount),
            "amount_paid": fmt_money(inv.paid),
            "amount_due": fmt_money(inv.due),
            "issued": inv.issue_date.isoformat() if inv.issue_date else "unknown",
            "due_date": inv.due_date.isoformat() if inv.due_date else "none",
            "close_date": as_of.isoformat(),
        }
        if inv.status == "PARTIALLY_PAID":
            facts = dict(base_facts)
            if days_overdue > 0:
                facts["days_overdue"] = days_overdue
            issue = self._issue(
                "PARTIAL_PAYMENT",
                "medium",
                f"Invoice {inv.number} is only partly paid",
                inv.due,
                row_id,
                inv.number,
                facts,
                [reminder, _mark_reviewed()],
            )
            self._row(order, pays, "issue", issue)
            return

        if inv.status in OPEN_INVOICE_STATUSES:
            if inv.due_date and days_overdue > 0:
                facts = dict(base_facts, days_overdue=days_overdue)
                issue = self._issue(
                    "UNPAID_INVOICE",
                    "high" if days_overdue >= 10 else "medium",
                    f"Invoice {inv.number} is {days_overdue} days overdue",
                    inv.due,
                    row_id,
                    inv.number,
                    facts,
                    [reminder, _mark_reviewed()],
                )
                row = self._row(order, pays, "issue", issue)
                if not pays:
                    row.diff = -inv.due  # nothing received yet: the whole amount is outstanding
            else:
                self._row(order, pays, "open", note="Invoice sent, not yet due")
            return

        # Cancelled / refunded invoices etc.: nothing to collect, keep visible.
        self._row(order, pays, "matched", note=f"Invoice status: {inv.status.lower()}")

    # ------------------------------------------------------------- the rest
    def orphans(self) -> None:
        for p in self.payments:
            if p.id in self.used:
                continue
            row_id = f"ROW-{p.id}"
            gross, fee = p.gross, -p.fee
            issue = self._issue(
                "UNRECORDED_SALE",
                "medium",
                "PayPal payment with no matching order",
                gross,
                row_id,
                p.id,
                {
                    "payment_id": p.id,
                    "date": p.day.isoformat(),
                    "payer": p.payer_name or "unknown",
                    "payer_email": p.payer_email or "unknown",
                    "amount": fmt_money(gross, p.currency),
                    "paypal_fee": fmt_money(fee, p.currency),
                    "note_on_payment": p.note or "none",
                    "order_reference_on_payment": p.invoice_ref or "none",
                },
                [
                    Action(
                        type="record_adjustment",
                        label="Add this sale to your books",
                        params={
                            "entry": _journal(
                                f"Unrecorded PayPal sale ({p.id}) from {p.payer_name or 'unknown payer'}",
                                p.day,
                                [
                                    ("PayPal clearing", gross - fee, ZERO),
                                    ("Payment processing fees", fee, ZERO),
                                    ("Sales", ZERO, gross),
                                ],
                            )
                        },
                    ),
                    _mark_reviewed(),
                ],
            )
            self.rows.append(
                LedgerRow(
                    id=row_id,
                    day=p.day,
                    reference="(none)",
                    customer=p.payer_name or p.payer_email or "unknown",
                    channel="paypal-only",
                    currency=p.currency,
                    expected=None,
                    received=p.gross,
                    fee=p.fee,
                    net=p.net,
                    status="issue",
                    issue_id=issue.id,
                    txn_ids=[p.id],
                    note=p.note,
                )
            )

    def transfers(self) -> None:
        for t in self.ds.txns:
            if t.kind != "withdrawal":
                continue
            self.rows.append(
                LedgerRow(
                    id=f"ROW-{t.id}",
                    day=t.day,
                    reference="Bank transfer",
                    customer="Transfer to bank",
                    channel="transfer",
                    currency=t.currency,
                    expected=None,
                    received=t.gross,
                    fee=t.fee,
                    net=t.net,
                    status="transfer",
                    txn_ids=[t.id],
                )
            )

    def summary(self) -> Summary:
        in_ccy = [t for t in self.ds.txns if t.currency == self.ccy]
        sales = sum((t.gross for t in in_ccy if t.kind == "payment"), ZERO)
        fees = sum((t.fee for t in in_ccy), ZERO)
        refunds = sum((t.gross for t in in_ccy if t.kind == "refund"), ZERO)
        chargebacks = sum((t.gross for t in in_ccy if t.kind in ("chargeback", "reversal")), ZERO)
        transfers = sum((t.gross for t in in_ccy if t.kind == "withdrawal"), ZERO)
        recon_rows = [r for r in self.rows if r.status != "transfer"]
        clean = [r for r in recon_rows if r.status in ("matched", "refunded", "open")]
        rate = (Decimal(len(clean)) * 100 / Decimal(len(recon_rows))) if recon_rows else Decimal("100")
        return Summary(
            txn_count=len(self.ds.txns),
            order_count=len(self.ds.orders),
            sales_gross=money(sales),
            fees=money(fees),
            refunds=money(refunds),
            chargebacks=money(chargebacks),
            transfers=money(transfers),
            matched_count=len(clean),
            issue_count=len(self.issues),
            amount_at_risk=money(sum((i.amount_at_risk for i in self.issues), ZERO)),
            match_rate=rate.quantize(Decimal("0.1")),
        )


def reconcile(ds: Dataset) -> ReconResult:
    engine = _Engine(ds)
    for order in sorted(ds.orders, key=lambda o: (o.day, o.order_id)):
        if order.channel == "invoice":
            engine.invoice_order(order)
        else:
            engine.web_order(order)
    engine.orphans()
    engine.transfers()
    engine.rows.sort(key=lambda r: (r.day, r.id))
    engine.issues.sort(key=lambda i: (SEVERITY_ORDER[i.severity], -i.amount_at_risk))
    return ReconResult(meta=ds.meta, rows=engine.rows, issues=engine.issues, summary=engine.summary())
