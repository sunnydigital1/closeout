"""Where a month of data comes from: the bundled demo month, or a live PayPal account."""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path

from .config import ROOT, Settings
from .models import Dataset, FeeSchedule, Meta
from .normalize import invoices_from_api, orders_from_csv, transactions_from_api
from .paypal import PayPalClient

SAMPLE_DIR = ROOT / "data" / "sample"
SAMPLE_MONTH = "2026-10"


def _fee_schedule(settings: Settings | None, raw: dict | None = None) -> FeeSchedule:
    if raw:
        return FeeSchedule(
            percent=Decimal(raw["percent"]),
            fixed=Decimal(raw["fixed"]),
            intl_percent=Decimal(raw["intl_percent"]),
            home_country=raw.get("home_country", "US"),
        )
    return FeeSchedule(
        percent=settings.fee_percent,
        fixed=settings.fee_fixed,
        intl_percent=settings.intl_fee_percent,
        home_country=settings.home_country,
    )


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = (int(x) for x in month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return date(year, mon, 1), date(year, mon, last)


def load_sample(sample_dir: Path = SAMPLE_DIR) -> Dataset:
    meta_raw = json.loads((sample_dir / "meta.json").read_text())
    meta = Meta(
        business_name=meta_raw["business_name"],
        book_currency=meta_raw["book_currency"],
        period_start=date.fromisoformat(meta_raw["period_start"]),
        period_end=date.fromisoformat(meta_raw["period_end"]),
        as_of=date.fromisoformat(meta_raw["as_of"]),
        fee_schedule=_fee_schedule(None, meta_raw["fee_schedule"]),
        source="sample",
    )
    txns = transactions_from_api(json.loads((sample_dir / "transactions.json").read_text()))
    invoices = invoices_from_api(json.loads((sample_dir / "invoices.json").read_text()))
    orders = orders_from_csv(sample_dir / "orders.csv")
    return Dataset(meta=meta, orders=orders, txns=txns, invoices=invoices)


def load_paypal(settings: Settings, month: str, client: PayPalClient | None = None) -> Dataset:
    """Pull one calendar month from the PayPal account and read the seller's books CSV."""
    start, end = month_bounds(month)
    today = date.today()
    client = client or PayPalClient(settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_env)
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end, time(23, 59, 59), tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if end_dt > now:
        end_dt = now
    txns = transactions_from_api(client.list_transactions(start_dt, end_dt))
    invoices = invoices_from_api(client.list_invoices())
    if not settings.books_csv.exists():
        raise FileNotFoundError(
            f"Books file not found at {settings.books_csv}. Run scripts/seed_sandbox.py or set BOOKS_CSV."
        )
    orders = [o for o in orders_from_csv(settings.books_csv) if start <= o.day <= end]
    meta = Meta(
        business_name=settings.business_name,
        book_currency=settings.book_currency,
        period_start=start,
        period_end=end,
        as_of=today,
        fee_schedule=_fee_schedule(settings),
        source="paypal",
    )
    return Dataset(meta=meta, orders=orders, txns=txns, invoices=invoices)
