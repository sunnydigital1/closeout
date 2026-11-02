"""Closeout web app: FastAPI backend + static dashboard."""
from __future__ import annotations

import csv
import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import actions as actions_mod
from .ai import Explainer, build_llm, template_explanation
from .config import ROOT, load_settings
from .models import ReconResult
from .paypal import PayPalClient, PayPalError
from .reconcile import reconcile
from .sources import SAMPLE_MONTH, load_paypal, load_sample

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("closeout")

settings = load_settings()
STATIC = ROOT / "static"

app = FastAPI(title="Closeout", description="Month-end reconciliation for PayPal sellers")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_paypal: PayPalClient | None = None


def paypal_client() -> PayPalClient | None:
    global _paypal
    if _paypal is None and settings.paypal_configured:
        _paypal = PayPalClient(settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_env)
    return _paypal


def _build_explainer() -> Explainer:
    try:
        llm = build_llm(settings)
    except Exception as exc:  # missing SDK, bad key format...
        log.warning("AI disabled: %s", exc)
        llm = None
    return Explainer(llm, ROOT / "data" / "cache" / "ai_cache.json")


explainer = _build_explainer()


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.results: dict[tuple[str, str], ReconResult] = {}
        self.explanations: dict[tuple[str, str], dict] = {}
        self.statuses: dict[tuple[str, str, str], str] = {}
        self.activity: list[dict] = []
        self.adjustments: list[dict] = []


STATE = _State()


# ------------------------------------------------------------------ helpers
def _num(value):
    return None if value is None else float(value)


def _key(source: str, month: str | None) -> tuple[str, str]:
    if source not in ("sample", "paypal"):
        raise HTTPException(400, "source must be 'sample' or 'paypal'")
    if source == "sample":
        return source, SAMPLE_MONTH
    return source, month or date.today().strftime("%Y-%m")


def _load(source: str, month: str) -> ReconResult:
    if source == "sample":
        return reconcile(load_sample())
    if not settings.paypal_configured:
        raise HTTPException(400, "PayPal isn't connected. Add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET to .env.")
    try:
        return reconcile(load_paypal(settings, month, paypal_client()))
    except PayPalError as exc:
        raise HTTPException(502, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc


def _result(source: str, month: str, refresh: bool = False) -> ReconResult:
    key = (source, month)
    with STATE.lock:
        cached = STATE.results.get(key)
    if cached and not refresh:
        return cached
    result = _load(source, month)
    with STATE.lock:
        STATE.results[key] = result
        if refresh:
            STATE.explanations.pop(key, None)
    return result


def _serialize(result: ReconResult, key: tuple[str, str]) -> dict:
    m, s = result.meta, result.summary
    by_issue = {i.id: i for i in result.issues}
    status = lambda iid: STATE.statuses.get((*key, iid), "open")  # noqa: E731
    rows = []
    for r in result.rows:
        issue = by_issue.get(r.issue_id) if r.issue_id else None
        rows.append(
            {
                "id": r.id,
                "day": r.day.isoformat(),
                "reference": r.reference,
                "customer": r.customer,
                "channel": r.channel,
                "currency": r.currency,
                "expected": _num(r.expected),
                "received": _num(r.received),
                "fee": _num(r.fee),
                "net": _num(r.net),
                "difference": _num(r.difference),
                "status": r.status,
                "issue_id": r.issue_id,
                "issue_title": issue.title if issue else None,
                "severity": issue.severity if issue else None,
                "issue_status": status(issue.id) if issue else None,
                "txn_ids": r.txn_ids,
                "note": r.note,
            }
        )
    return {
        "source": key[0],
        "month": key[1],
        "meta": {
            "business_name": m.business_name,
            "book_currency": m.book_currency,
            "period_start": m.period_start.isoformat(),
            "period_end": m.period_end.isoformat(),
            "month_label": m.period_start.strftime("%B %Y"),
            "month_name": m.period_start.strftime("%B"),
            "as_of": m.as_of.isoformat(),
            "fee_schedule": m.fee_schedule.describe(),
        },
        "summary": {
            "txn_count": s.txn_count,
            "order_count": s.order_count,
            "sales_gross": _num(s.sales_gross),
            "fees": _num(s.fees),
            "refunds": _num(s.refunds),
            "chargebacks": _num(s.chargebacks),
            "transfers": _num(s.transfers),
            "matched_count": s.matched_count,
            "issue_count": s.issue_count,
            "amount_at_risk": _num(s.amount_at_risk),
            "match_rate": _num(s.match_rate),
        },
        "rows": rows,
        "issues": [
            {
                "id": i.id,
                "kind": i.kind,
                "severity": i.severity,
                "title": i.title,
                "amount_at_risk": _num(i.amount_at_risk),
                "row_id": i.row_id,
                "facts": i.facts,
                "actions": [
                    {
                        "type": a.type,
                        "label": a.label,
                        "moves_money": a.moves_money,
                        "contacts_customer": a.contacts_customer,
                    }
                    for a in i.actions
                ],
                "status": status(i.id),
            }
            for i in result.issues
        ],
    }


# ------------------------------------------------------------------- routes
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config():
    return {
        "sources": [
            {"id": "sample", "label": "Demo month", "month": SAMPLE_MONTH, "available": True},
            {
                "id": "paypal",
                "label": "PayPal sandbox",
                "month": date.today().strftime("%Y-%m"),
                "available": settings.paypal_configured,
            },
        ],
        "ai": explainer.describe(),
        "live_actions": settings.allow_live_actions,
        "paypal_env": settings.paypal_env,
    }


@app.get("/api/reconcile")
def get_reconcile(source: str = "sample", month: str | None = None, refresh: bool = False):
    key = _key(source, month)
    result = _result(*key, refresh=refresh)
    return _serialize(result, key)


class ExplainRequest(BaseModel):
    source: str = "sample"
    month: str | None = None


@app.post("/api/explain")
def explain(req: ExplainRequest):
    key = _key(req.source, req.month)
    with STATE.lock:
        done = STATE.explanations.get(key)
    if done:
        return done
    result = _result(*key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        items_future = pool.submit(explainer.explain_issues, result)
        summary_future = pool.submit(explainer.summarize, result)
        items, error = items_future.result()
        summary, summary_source = summary_future.result()
    payload = {
        "summary": summary,
        "summary_source": summary_source,
        "items": items,
        "error": error,
        "ai": explainer.describe(),
    }
    with STATE.lock:
        STATE.explanations[key] = payload
    return payload


class AskRequest(BaseModel):
    source: str = "sample"
    month: str | None = None
    question: str = Field(min_length=2, max_length=300)


@app.post("/api/ask")
def ask(req: AskRequest):
    key = _key(req.source, req.month)
    return explainer.ask(_result(*key), req.question)


class ActionRequest(BaseModel):
    source: str = "sample"
    month: str | None = None
    issue_id: str
    action: str
    approved: bool = False
    message: str | None = None


def _issue_and_action(req: ActionRequest):
    key = _key(req.source, req.month)
    result = _result(*key)
    issue = next((i for i in result.issues if i.id == req.issue_id), None)
    if issue is None:
        raise HTTPException(404, f"No issue {req.issue_id} in this month")
    try:
        action = actions_mod.find_action(issue, req.action)
    except actions_mod.ActionError as exc:
        raise HTTPException(400, str(exc)) from exc
    mode = actions_mod.Mode.for_source(key[0], settings.allow_live_actions, paypal_client() is not None)
    return key, result, issue, action, mode


@app.post("/api/actions/preview")
def action_preview(req: ActionRequest):
    key, result, issue, action, mode = _issue_and_action(req)
    with STATE.lock:
        exp = (STATE.explanations.get(key) or {}).get("items", {}).get(issue.id)
    message = (exp or {}).get("customer_message") or template_explanation(issue, result.meta.business_name)[
        "customer_message"
    ]
    return actions_mod.preview(issue, action, mode, message)


@app.post("/api/actions/execute")
def action_execute(req: ActionRequest):
    if not req.approved:
        raise HTTPException(400, "Actions only run after you approve them.")
    key, result, issue, action, mode = _issue_and_action(req)
    status_key = (*key, issue.id)
    with STATE.lock:
        current = STATE.statuses.get(status_key, "open")
    if action.moves_money and current == "resolved":
        raise HTTPException(409, "This was already done. Closeout won't move money twice.")
    try:
        outcome = actions_mod.execute(issue, action, mode, req.message, paypal_client() if mode.live else None)
    except PayPalError as exc:
        raise HTTPException(502, str(exc)) from exc
    entry = {**outcome, "issue_title": issue.title, "source": key[0], "month": key[1]}
    with STATE.lock:
        if outcome.get("issue_status"):
            STATE.statuses[status_key] = outcome["issue_status"]
        STATE.activity.insert(0, entry)
        if outcome.get("journal"):
            STATE.adjustments.append(outcome["journal"])
    return {**entry, "issue_status": STATE.statuses.get(status_key, "open")}


@app.get("/api/activity")
def activity():
    with STATE.lock:
        return STATE.activity[:100]


@app.get("/api/adjustments.csv")
def adjustments_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "memo", "account", "debit", "credit"])
    with STATE.lock:
        entries = list(STATE.adjustments)
    for entry in entries:
        for line in entry["lines"]:
            writer.writerow([entry["date"], entry["memo"], line["account"], line["debit"], line["credit"]])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=closeout-adjustments.csv"},
    )


@app.exception_handler(Exception)
def unhandled(_, exc: Exception):  # pragma: no cover - last-resort safety net
    log.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"detail": f"Something went wrong: {exc}"})
