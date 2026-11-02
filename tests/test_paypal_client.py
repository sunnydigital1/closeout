"""PayPal client tests against a fake PayPal (httpx MockTransport). No network needed."""
import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.paypal import PayPalClient, PayPalError


def make_client(handler):
    return PayPalClient("id", "secret", "sandbox", http=httpx.Client(transport=httpx.MockTransport(handler)))


def token_response():
    return httpx.Response(200, json={"access_token": "TOKEN", "expires_in": 32400})


def test_transaction_search_merges_pages_and_formats_dates():
    seen = []

    def handler(request: httpx.Request):
        if request.url.path == "/v1/oauth2/token":
            return token_response()
        seen.append(request.url.params)
        assert request.headers["Authorization"] == "Bearer TOKEN"
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "transaction_details": [{"transaction_info": {"transaction_id": f"P{page}"}}],
                "total_pages": 2,
                "page": page,
            },
        )

    client = make_client(handler)
    data = client.list_transactions(
        datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
    )
    assert [d["transaction_info"]["transaction_id"] for d in data["transaction_details"]] == ["P1", "P2"]
    assert seen[0]["start_date"] == "2026-10-01T00:00:00Z"
    assert seen[0]["end_date"] == "2026-10-31T23:59:59Z"
    assert seen[0]["fields"] == "all"


def test_refund_sends_idempotency_key_and_amount():
    captured = {}

    def handler(request: httpx.Request):
        if request.url.path == "/v1/oauth2/token":
            return token_response()
        captured["path"] = request.url.path
        captured["request_id"] = request.headers.get("PayPal-Request-Id")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "REFUND123", "status": "COMPLETED"})

    client = make_client(handler)
    result = client.refund_capture("CAP1", Decimal("42.00"), "USD", "Duplicate charge refunded", request_id="req-1")
    assert result["status"] == "COMPLETED"
    assert captured["path"] == "/v2/payments/captures/CAP1/refund"
    assert captured["request_id"] == "req-1"
    assert captured["body"]["amount"] == {"value": "42.00", "currency_code": "USD"}


def test_remind_invoice_posts_note():
    captured = {}

    def handler(request: httpx.Request):
        if request.url.path == "/v1/oauth2/token":
            return token_response()
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    make_client(handler).remind_invoice("INV2-AAAA", "Friendly reminder", "Hi there")
    assert captured["path"] == "/v2/invoicing/invoices/INV2-AAAA/remind"
    assert captured["body"]["send_to_recipient"] is True


def test_missing_transaction_search_permission_gives_helpful_error():
    def handler(request: httpx.Request):
        if request.url.path == "/v1/oauth2/token":
            return token_response()
        return httpx.Response(403, json={"name": "NOT_AUTHORIZED", "message": "Authorization failed"})

    with pytest.raises(PayPalError) as err:
        make_client(handler).list_transactions(datetime(2026, 10, 1), datetime(2026, 10, 2))
    assert "Transaction search" in str(err.value)


def test_bad_credentials_give_helpful_error():
    def handler(request: httpx.Request):
        return httpx.Response(401, json={"error": "invalid_client"})

    with pytest.raises(PayPalError) as err:
        make_client(handler).get_invoice("X")
    assert "PAYPAL_CLIENT_ID" in str(err.value)
