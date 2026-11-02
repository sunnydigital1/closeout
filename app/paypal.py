"""Small PayPal REST client covering the APIs Closeout uses.

* OAuth 2.0 client credentials       POST /v1/oauth2/token
* Transaction Search                  GET  /v1/reporting/transactions
* Invoicing v2                        GET  /v2/invoicing/invoices, /{id}
                                      POST /v2/invoicing/invoices/{id}/remind
* Payments v2 (refunds)               POST /v2/payments/captures/{id}/refund
* Disputes                            GET  /v1/customer/disputes
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx

BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}
RESOLUTION_CENTER = {
    "sandbox": "https://www.sandbox.paypal.com/resolutioncenter",
    "live": "https://www.paypal.com/resolutioncenter",
}


class PayPalError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, debug_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.debug_id = debug_id


def _search_time(dt: datetime) -> str:
    """Transaction Search wants RFC 3339 with seconds, e.g. 2026-10-01T00:00:00Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        env: str = "sandbox",
        http: httpx.Client | None = None,
    ):
        if env not in BASE_URLS:
            raise ValueError(f"PAYPAL_ENV must be 'sandbox' or 'live', got {env!r}")
        self.env = env
        self.base = BASE_URLS[env]
        self._auth = (client_id, client_secret)
        self._http = http or httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._token_expires = 0.0

    # ----------------------------------------------------------------- plumbing
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        resp = self._http.post(
            f"{self.base}/v1/oauth2/token",
            auth=self._auth,
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise PayPalError(
                "PayPal rejected the client ID / secret. Check PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.",
                resp.status_code,
            )
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 3000))
        return self._token

    def _request(self, method: str, path: str, *, retry: bool = True, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers.update({"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"})
        resp = self._http.request(method, f"{self.base}{path}", headers=headers, **kwargs)
        if resp.status_code == 401 and retry:
            self._token = None
            return self._request(method, path, retry=False, headers=headers, **kwargs)
        if resp.status_code >= 400:
            debug_id = resp.headers.get("paypal-debug-id")
            try:
                body = resp.json()
                detail = body.get("message") or body.get("error_description") or body.get("name")
                issues = body.get("details") or []
                if issues:
                    detail = f"{detail}: {issues[0].get('issue')} {issues[0].get('description', '')}".strip()
            except ValueError:
                detail = resp.text[:200]
            if resp.status_code == 403 and "/reporting/" in path:
                detail = (
                    "Transaction Search isn't enabled for this app yet. Turn on 'Transaction search' "
                    "under your app's Features in the PayPal developer dashboard (it can take a few hours)."
                )
            raise PayPalError(f"PayPal {method} {path} failed ({resp.status_code}): {detail}", resp.status_code, debug_id)
        return resp

    # ---------------------------------------------------------------- reading
    def list_transactions(self, start: datetime, end: datetime) -> dict:
        """All transactions in [start, end] (max 31 days), every page merged."""
        page, details, first = 1, [], None
        while True:
            resp = self._request(
                "GET",
                "/v1/reporting/transactions",
                params={
                    "start_date": _search_time(start),
                    "end_date": _search_time(end),
                    "fields": "all",
                    "page_size": 500,
                    "page": page,
                },
            )
            body = resp.json()
            first = first or body
            details.extend(body.get("transaction_details", []))
            if page >= int(body.get("total_pages", 1) or 1):
                break
            page += 1
        merged = dict(first or {})
        merged["transaction_details"] = details
        merged["total_items"] = len(details)
        return merged

    def list_invoices(self, with_details: bool = True) -> dict:
        items, page = [], 1
        while True:
            body = self._request(
                "GET",
                "/v2/invoicing/invoices",
                params={"page": page, "page_size": 100, "total_required": "true"},
            ).json()
            items.extend(body.get("items", []))
            if page >= int(body.get("total_pages", 1) or 1):
                break
            page += 1
        if with_details:
            items = [self.get_invoice(item["id"]) for item in items]
        return {"items": items, "total_items": len(items)}

    def get_invoice(self, invoice_id: str) -> dict:
        return self._request("GET", f"/v2/invoicing/invoices/{invoice_id}").json()

    def list_disputes(self) -> dict:
        return self._request("GET", "/v1/customer/disputes", params={"page_size": 50}).json()

    # ---------------------------------------------------------------- writing
    def remind_invoice(self, invoice_id: str, subject: str, note: str) -> None:
        """Sends PayPal's standard reminder email with our note. Returns nothing (HTTP 204)."""
        self._request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/remind",
            json={
                "subject": subject[:100],
                "note": note[:4000],
                "send_to_invoicer": False,
                "send_to_recipient": True,
            },
        )

    def refund_capture(
        self,
        capture_id: str,
        amount: Decimal,
        currency: str,
        note_to_payer: str,
        request_id: str | None = None,
    ) -> dict:
        """Refund (part of) a captured payment. request_id makes retries safe (idempotent)."""
        resp = self._request(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            headers={"PayPal-Request-Id": request_id or str(uuid.uuid4()), "Prefer": "return=representation"},
            json={
                "amount": {"value": f"{amount:.2f}", "currency_code": currency},
                "note_to_payer": note_to_payer[:255],
            },
        )
        return resp.json()

    @property
    def resolution_center_url(self) -> str:
        return RESOLUTION_CENTER[self.env]
