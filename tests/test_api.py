"""End-to-end API tests (FastAPI TestClient, demo month, AI off)."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
A = {"X-Closeout-Session": "session-a"}
B = {"X-Closeout-Session": "session-b"}


def test_config_lists_sources():
    body = client.get("/api/config").json()
    assert [s["id"] for s in body["sources"]] == ["sample", "paypal"]


def test_reconcile_demo_month():
    body = client.get("/api/reconcile?source=sample", headers=A).json()
    assert body["summary"]["txn_count"] == 312
    assert body["summary"]["issue_count"] == 9
    fx = next(r for r in body["rows"] if r["reference"] == "LC-1234")
    assert fx["net"] == 36.13 and fx["difference"] == -1.98
    unpaid = next(r for r in body["rows"] if r["reference"] == "INV-2026-031")
    assert unpaid["difference"] == -640.0


def test_explain_without_ai_uses_rules():
    body = client.post("/api/explain", json={"source": "sample"}).json()
    assert body["summary_source"] == "rules"
    assert len(body["items"]) == 9


def test_actions_need_approval_and_are_scoped_per_session():
    payload = {"source": "sample", "issue_id": "PARTIAL-INV-2026-029", "action": "send_reminder"}
    preview = client.post("/api/actions/preview", json=payload, headers=A).json()
    assert preview["api_call"].startswith("POST /v2/invoicing/invoices/")
    assert "nothing is sent" in preview["mode"]

    refused = client.post("/api/actions/execute", json=payload, headers=A)
    assert refused.status_code == 400  # not approved

    done = client.post("/api/actions/execute", json={**payload, "approved": True}, headers=A).json()
    assert done["issue_status"] == "resolved"

    mine = client.get("/api/reconcile?source=sample", headers=A).json()
    theirs = client.get("/api/reconcile?source=sample", headers=B).json()
    status = lambda body: next(i["status"] for i in body["issues"] if i["id"] == "PARTIAL-INV-2026-029")  # noqa: E731
    assert status(mine) == "resolved"
    assert status(theirs) == "open"


def test_money_never_moves_twice():
    payload = {"source": "sample", "issue_id": "DUP-LC-1187", "action": "refund_duplicate", "approved": True}
    first = client.post("/api/actions/execute", json=payload, headers=A)
    second = client.post("/api/actions/execute", json=payload, headers=A)
    assert first.status_code == 200
    assert second.status_code == 409


def test_adjustments_csv_contains_recorded_entries():
    payload = {"source": "sample", "issue_id": "REFUND-LC-1203", "action": "record_adjustment", "approved": True}
    client.post("/api/actions/execute", json=payload, headers=A)
    csv_text = client.get("/api/adjustments.csv?session=session-a").text
    assert "Sales returns & refunds" in csv_text
    assert "Sales returns & refunds" not in client.get("/api/adjustments.csv?session=session-b").text


def test_paypal_source_without_keys_explains_itself():
    resp = client.get("/api/reconcile?source=paypal")
    assert resp.status_code == 400
    assert "PAYPAL_CLIENT_ID" in resp.json()["detail"]
