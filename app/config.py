"""Settings, read from environment variables (and a local .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else default


@dataclass(frozen=True)
class Settings:
    ai_provider: str
    gemini_api_key: str
    gemini_model: str
    anthropic_api_key: str
    claude_model: str
    paypal_env: str
    paypal_client_id: str
    paypal_client_secret: str
    books_csv: Path
    business_name: str
    home_country: str
    book_currency: str
    fee_percent: Decimal
    fee_fixed: Decimal
    intl_fee_percent: Decimal
    allow_live_actions: bool
    store_enabled: bool

    @property
    def paypal_configured(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def ai_configured(self) -> bool:
        if self.ai_provider == "gemini":
            return bool(self.gemini_api_key)
        if self.ai_provider == "claude":
            return bool(self.anthropic_api_key)
        return False


def load_settings() -> Settings:
    books = Path(_env("BOOKS_CSV", "data/sandbox/orders.csv"))
    if not books.is_absolute():
        books = ROOT / books
    return Settings(
        ai_provider=_env("AI_PROVIDER", "gemini").lower(),
        gemini_api_key=_env("GEMINI_API_KEY"),
        gemini_model=_env("GEMINI_MODEL"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        claude_model=_env("CLAUDE_MODEL", "claude-haiku-5-5"),
        paypal_env=_env("PAYPAL_ENV", "sandbox").lower(),
        paypal_client_id=_env("PAYPAL_CLIENT_ID"),
        paypal_client_secret=_env("PAYPAL_CLIENT_SECRET"),
        books_csv=books,
        business_name=_env("BUSINESS_NAME", "Larkspur Candle Co."),
        home_country=_env("HOME_COUNTRY", "US").upper(),
        book_currency=_env("BOOK_CURRENCY", "USD").upper(),
        fee_percent=Decimal(_env("FEE_PERCENT", "3.49")),
        fee_fixed=Decimal(_env("FEE_FIXED", "0.49")),
        intl_fee_percent=Decimal(_env("INTL_FEE_PERCENT", "1.50")),
        # Hosted demos can switch real sandbox actions off and simulate instead.
        allow_live_actions=_env("ALLOW_LIVE_ACTIONS", "true").lower() in ("1", "true", "yes"),
        # The sandbox test store creates PayPal sandbox activity. Off on hosted demos.
        store_enabled=_env("ENABLE_TEST_STORE", "true").lower() in ("1", "true", "yes"),
    )
