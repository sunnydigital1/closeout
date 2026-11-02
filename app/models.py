"""Core data types.

Money is always Decimal. Signs follow PayPal's convention: money into the
PayPal balance is positive, money out (refunds, fees, chargebacks, transfers)
is negative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def money(value) -> Decimal:
    """Parse anything money-like into a 2-decimal Decimal."""
    if value is None or value == "":
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal | None, currency: str = "USD") -> str:
    """Human format used everywhere text is shown to the user or the AI."""
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    amount = f"{abs(value):,.2f}"
    if currency == "USD":
        return f"{sign}${amount}"
    return f"{sign}{amount} {currency}"


@dataclass
class PayPalTxn:
    """One row from the PayPal Transaction Search API, normalized."""

    id: str
    event_code: str
    kind: str  # payment | refund | reversal | chargeback | chargeback_reversal |
    #            dispute_hold | dispute_release | withdrawal | fx | fee | other
    timestamp: datetime
    gross: Decimal
    fee: Decimal
    currency: str
    status: str
    invoice_ref: str | None = None  # transaction_info.invoice_id
    custom: str | None = None
    note: str | None = None
    payer_email: str | None = None
    payer_name: str | None = None
    payer_country: str | None = None
    reference_id: str | None = None  # paypal_reference_id (refund -> original payment)

    @property
    def net(self) -> Decimal:
        return self.gross + self.fee

    @property
    def day(self) -> date:
        return self.timestamp.date()


@dataclass
class Invoice:
    """A PayPal invoice (Invoicing API v2), normalized."""

    id: str
    number: str
    status: str
    recipient_name: str | None
    recipient_email: str | None
    issue_date: date | None
    due_date: date | None
    amount: Decimal
    paid: Decimal
    currency: str
    payment_ids: list[str] = field(default_factory=list)

    @property
    def due(self) -> Decimal:
        return self.amount - self.paid


@dataclass
class Order:
    """One record from the seller's own books (store export / spreadsheet)."""

    order_id: str
    day: date
    customer_name: str
    customer_email: str
    channel: str  # "web" (PayPal checkout) | "invoice" (PayPal invoice)
    reference: str  # web: the order number sent to PayPal; invoice: invoice number
    amount: Decimal
    currency: str
    book_status: str  # paid | invoiced | refunded


@dataclass
class FeeSchedule:
    percent: Decimal = Decimal("3.49")
    fixed: Decimal = Decimal("0.49")
    intl_percent: Decimal = Decimal("1.50")
    home_country: str = "US"
    tolerance: Decimal = Decimal("0.05")

    def expected_fee(self, amount: Decimal, international: bool = False) -> Decimal:
        pct = self.percent + (self.intl_percent if international else Decimal("0"))
        return money(abs(amount) * pct / Decimal("100") + self.fixed)

    def describe(self) -> str:
        return (
            f"{self.percent}% + ${self.fixed} per domestic sale; "
            f"international sales add {self.intl_percent}%"
        )


@dataclass
class Meta:
    business_name: str
    book_currency: str
    period_start: date
    period_end: date
    as_of: date  # the "close date" used for overdue math
    fee_schedule: FeeSchedule
    source: str  # sample | paypal
    # How current PayPal's transaction search data is (its last_refreshed_datetime).
    # PayPal can take up to 3 hours to report new payments.
    data_through: datetime | None = None


@dataclass
class Dataset:
    meta: Meta
    orders: list[Order]
    txns: list[PayPalTxn]
    invoices: list[Invoice]


@dataclass
class Action:
    """Something the user can approve. Nothing runs without approval."""

    type: str  # send_reminder | refund_duplicate | record_adjustment | review_dispute | mark_reviewed
    label: str
    moves_money: bool = False
    contacts_customer: bool = False
    params: dict = field(default_factory=dict)


@dataclass
class Issue:
    id: str
    kind: str
    severity: str  # high | medium | low
    title: str
    amount_at_risk: Decimal
    row_id: str
    facts: dict
    actions: list[Action]
    status: str = "open"  # open | resolved | reviewed


@dataclass
class LedgerRow:
    id: str
    day: date
    reference: str
    customer: str
    channel: str  # web | invoice | paypal-only | transfer
    currency: str
    expected: Decimal | None
    received: Decimal | None
    fee: Decimal | None
    net: Decimal | None
    status: str  # matched | refunded | open | issue | transfer
    issue_id: str | None = None
    txn_ids: list[str] = field(default_factory=list)
    note: str | None = None
    diff: Decimal | None = None  # set when received/expected don't tell the story (e.g. FX)

    @property
    def difference(self) -> Decimal | None:
        if self.diff is not None:
            return self.diff
        if self.expected is None or self.received is None:
            return None
        return self.received - self.expected


@dataclass
class Summary:
    txn_count: int
    order_count: int
    sales_gross: Decimal
    fees: Decimal
    refunds: Decimal
    chargebacks: Decimal
    transfers: Decimal
    matched_count: int
    issue_count: int
    amount_at_risk: Decimal
    match_rate: Decimal  # percent
    waiting_count: int = 0  # sales PayPal hasn't reported yet


@dataclass
class ReconResult:
    meta: Meta
    rows: list[LedgerRow]
    issues: list[Issue]
    summary: Summary
