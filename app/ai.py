"""The AI layer: plain-English explanations, a month-end summary, and Q&A.

Design rule: the engine owns the numbers, the model owns the words.
Every number in AI output must already appear in the facts the model was
given. Output that breaks the rule is retried once, then replaced by a
rule-based explanation, so a wrong figure never reaches the user.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from .models import Issue, ReconResult, fmt_money

log = logging.getLogger("closeout.ai")

DEFAULT_GEMINI_MODELS = [
    "gemini-flash-latest",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
]


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    provider: str
    model: str

    def complete_json(self, system: str, prompt: str) -> str: ...


# --------------------------------------------------------------------- providers
class GeminiLLM:
    provider = "gemini"

    def __init__(self, api_key: str, model: str = ""):
        from google import genai  # imported lazily so the app runs without the SDK

        self._client = genai.Client(api_key=api_key)
        self._models = ([model] if model else []) + [m for m in DEFAULT_GEMINI_MODELS if m != model]
        self.model = self._models[0]

    def complete_json(self, system: str, prompt: str) -> str:
        from google.genai import errors, types

        last_error = None
        for model in list(self._models):
            try:
                resp = self._client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        temperature=0.2,
                    ),
                )
                self.model = model
                if not resp.text:
                    raise LLMError("Gemini returned an empty response")
                return resp.text
            except errors.APIError as exc:
                last_error = exc
                # 404: model name not available; 429: this model's free quota is used up.
                # Free-tier quotas are per model, so try the next model either way.
                if exc.code in (404, 429):
                    log.warning("Gemini model %s unavailable (%s), trying next", model, exc.code)
                    continue
                raise LLMError(f"Gemini error {exc.code}: {exc.message}") from exc
        raise LLMError(f"No Gemini model available ({last_error})")


class ClaudeLLM:
    provider = "claude"

    def __init__(self, api_key: str, model: str = "claude-haiku-5-5"):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model or "claude-haiku-5-5"

    def complete_json(self, system: str, prompt: str) -> str:
        import anthropic

        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=2000,
                system=system + "\nRespond with a single JSON object and nothing else.",
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude error {exc.status_code}: {exc.message}") from exc
        return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")


def build_llm(settings) -> LLM | None:
    if settings.ai_provider == "gemini" and settings.gemini_api_key:
        return GeminiLLM(settings.gemini_api_key, settings.gemini_model)
    if settings.ai_provider == "claude" and settings.anthropic_api_key:
        return ClaudeLLM(settings.anthropic_api_key, settings.claude_model)
    return None


# -------------------------------------------------------------- number guardrail
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
ALWAYS_ALLOWED = {Decimal(n) for n in range(0, 4)}


def numbers_in(text: str) -> list[Decimal]:
    found = []
    for match in _NUMBER.finditer(text or ""):
        token = match.group().rstrip(",.").replace(",", "")
        try:
            found.append(Decimal(token))
        except InvalidOperation:
            continue
    return found


def allowed_numbers(*sources) -> set[Decimal]:
    allowed = set(ALWAYS_ALLOWED)
    for src in sources:
        text = src if isinstance(src, str) else json.dumps(src, default=str)
        allowed.update(numbers_in(text))
    return allowed


def invented_numbers(text: str, allowed: set[Decimal]) -> list[str]:
    """Numbers in `text` that do not appear in the allowed set."""
    return sorted({str(n) for n in numbers_in(text) if n not in allowed})


def parse_json(raw: str) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


# ------------------------------------------------------------------- templates
_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _f(issue: Issue, key: str, default: str = "") -> str:
    value = str(issue.facts.get(key, default))
    day = _ISO_DAY.match(value)
    if day:  # 2026-10-20 -> Oct 20, 2026 (same numbers, so the guardrail still passes)
        year, month, dom = day.groups()
        return f"{_MONTHS[int(month) - 1]} {int(dom)}, {year}"
    return value


def template_explanation(issue: Issue, business: str) -> dict:
    k = issue.kind
    f = lambda key, default="": _f(issue, key, default)  # noqa: E731
    message = None
    if k == "DUPLICATE_PAYMENT":
        text = (
            f"{f('customer')} paid order {f('order')} twice, {f('minutes_apart')} minutes apart, so you "
            f"collected {f('duplicate_amount')} extra. Refunding the second payment fixes your books and the customer's card."
        )
        cause = "A double-click or a slow checkout page sent the payment twice."
        message = (
            f"Hi {f('customer').split(' ')[0]}, thanks for your order {f('order')}! It looks like your payment went "
            f"through twice, so we've refunded the duplicate {f('duplicate_amount')}. It should appear on your "
            f"statement within a few days. Sorry for the mix-up! - {business}"
        )
    elif k == "UNPAID_INVOICE":
        text = (
            f"Invoice {f('invoice_number')} for {f('customer')} ({f('amount_due')}) was due on {f('due_date')} "
            f"and is now {f('days_overdue')} days overdue with nothing paid."
        )
        cause = "The invoice was probably missed or is stuck in the customer's approval process."
        message = (
            f"Hi {f('customer')} team, a quick reminder that invoice {f('invoice_number')} for "
            f"{f('amount_due')} was due on {f('due_date')}. You can pay it securely through the PayPal link in "
            f"this email. If it's already on its way, thank you, and please ignore this note! - {business}"
        )
    elif k == "PARTIAL_PAYMENT":
        text = (
            f"{f('customer')} paid {f('amount_paid')} of invoice {f('invoice_number')} ({f('invoice_amount')}), "
            f"so {f('amount_due')} is still owed."
        )
        cause = "The customer may have paid an older quote or split the payment."
        message = (
            f"Hi {f('customer')} team, thanks for your payment of {f('amount_paid')} on invoice "
            f"{f('invoice_number')}. There's a remaining balance of {f('amount_due')}, which you can pay through "
            f"the PayPal link in this email. Let us know if you have any questions! - {business}"
        )
    elif k == "REFUND_NOT_RECORDED":
        text = (
            f"PayPal refunded {f('refund_amount')} to {f('customer')} for order {f('order')} on "
            f"{f('refund_date')}, but your books still show the order as {f('status_in_books')}. Your sales are "
            f"overstated until you record it."
        )
        cause = "The refund was issued in PayPal directly, so it never reached your order records."
    elif k == "CHARGEBACK":
        text = (
            f"{f('customer')} disputed order {f('order')} and PayPal took back {f('amount_reversed')} plus a "
            f"{f('dispute_fee')} dispute fee ({f('total_taken_back')} total) on {f('chargeback_date')}. "
            f"Your books still count it as a sale."
        )
        cause = "The buyer filed a chargeback with their card issuer."
    elif k == "FX_DIFFERENCE":
        text = (
            f"{f('customer')} paid {f('buyer_paid')} for order {f('order')}. After PayPal's fee and currency "
            f"conversion you received {f('converted_to')}, compared with the {f('expected_net')} you'd expect "
            f"from a {f('order_amount')} sale."
        )
        cause = "The buyer paid in another currency and PayPal converted it at its own exchange rate."
    elif k == "FEE_VARIANCE":
        text = (
            f"PayPal charged {f('fee_charged')} on {f('customer')}'s {f('sale_amount')} order, which is "
            f"{f('extra_fee')} more than the {f('expected_fee')} your fee schedule predicts."
        )
        if issue.facts.get("matches_international_rate") == "yes":
            text += (
                f" The buyer is in {f('buyer_country')}, and the fee matches PayPal's extra "
                f"{f('international_fee_rate')} for international sales."
            )
        cause = "Cross-border sales carry an extra fee that your fee schedule doesn't include."
    elif k == "UNRECORDED_SALE":
        text = (
            f"You received {f('amount')} from {f('payer')} on {f('date')}, but there's no matching order in "
            f"your books. The note on the payment says \"{f('note_on_payment')}\"."
        )
        cause = "The payment came in outside your store checkout, for example through a PayPal payment link."
    elif k == "MISSING_PAYMENT":
        text = (
            f"Order {f('order')} for {f('customer')} ({f('order_amount')}) is marked paid in your books, "
            f"but PayPal has no payment for it."
        )
        cause = "The order may have been marked paid by mistake or paid some other way."
    elif k == "MISSING_INVOICE":
        text = (
            f"Your books list invoice {f('invoice_number')} for {f('customer')} ({f('amount_in_books')}), "
            f"but it doesn't exist in PayPal."
        )
        cause = "The invoice may have been created in another tool or deleted in PayPal."
    else:  # AMOUNT_MISMATCH and anything new
        text = f"{issue.title}. {'; '.join(f'{k2}: {v}' for k2, v in issue.facts.items())}."
        cause = "The amount paid doesn't match the amount in your books."
    return {
        "explanation": text,
        "likely_cause": cause,
        "recommended_action": issue.actions[0].type if issue.actions else "mark_reviewed",
        "customer_message": message,
        "source": "rules",
    }


def template_summary(result: ReconResult) -> str:
    s, m = result.summary, result.meta
    month = m.period_start.strftime("%B %Y")
    text = (
        f"{m.business_name} has {s.txn_count} PayPal transactions in {month}, checked against "
        f"{s.order_count} orders. {s.match_rate}% reconciled cleanly. "
    )
    if result.issues:
        top = max(result.issues, key=lambda i: i.amount_at_risk)
        text += (
            f"{s.issue_count} items need attention with {fmt_money(s.amount_at_risk)} at stake; "
            f"start with \"{top.title}\" ({fmt_money(top.amount_at_risk)})."
        )
    else:
        text += "Nothing needs attention. You're ready to close the month."
    return text


# --------------------------------------------------------------------- prompts
SYSTEM = """You are the assistant inside Closeout, a month-end reconciliation tool for small businesses that sell through PayPal. You explain problems to a busy owner who is not an accountant.

Hard rules:
1. Use ONLY numbers, amounts, dates and IDs that appear in the FACTS you are given. Never calculate, estimate, round or total anything yourself. If you would need a number that isn't in FACTS, describe it in words.
2. Plain English, second person ("you"). No accounting jargon unless you explain it in a few words.
3. Be specific and calm. No exclamation marks in explanations.
4. Output JSON only, exactly matching the requested shape."""


def _issue_payload(issue: Issue) -> dict:
    return {
        "id": issue.id,
        "kind": issue.kind,
        "severity": issue.severity,
        "title": issue.title,
        "facts": issue.facts,
        "allowed_actions": [{"type": a.type, "label": a.label} for a in issue.actions],
    }


def _context(result: ReconResult) -> str:
    m = result.meta
    return (
        f"BUSINESS: {m.business_name}. Books are kept in {m.book_currency}. "
        f"Month being closed: {m.period_start.isoformat()} to {m.period_end.isoformat()}. "
        f"Close date: {m.as_of.isoformat()}."
    )


def explanations_prompt(result: ReconResult, issues: list[Issue], feedback: str = "") -> str:
    payload = [_issue_payload(i) for i in issues]
    return f"""{_context(result)}

For EACH issue below, write:
- "explanation": 2-3 sentences (max 60 words): what happened and why it matters to the owner.
- "likely_cause": one short sentence.
- "recommended_action": exactly one "type" from that issue's allowed_actions.
- "customer_message": ONLY if the recommended action is "send_reminder" or "refund_duplicate": a friendly 50-90 word note to the customer, signed "{result.meta.business_name}", no placeholders. Otherwise null.

ISSUES (FACTS):
{json.dumps(payload, indent=1, default=str)}
{feedback}
Return: {{"explanations": [{{"id": "...", "explanation": "...", "likely_cause": "...", "recommended_action": "...", "customer_message": null}}]}}"""


def _summary_facts(result: ReconResult) -> dict:
    s = result.summary
    return {
        "month": result.meta.period_start.strftime("%B %Y"),
        "paypal_transactions": s.txn_count,
        "orders_in_books": s.order_count,
        "reconciled_cleanly_percent": f"{s.match_rate}%",
        "gross_sales": fmt_money(s.sales_gross),
        "paypal_fees": fmt_money(-s.fees),
        "refunds": fmt_money(-s.refunds),
        "chargebacks": fmt_money(-s.chargebacks),
        "transferred_to_bank": fmt_money(-s.transfers),
        "items_needing_attention": s.issue_count,
        "total_at_stake": fmt_money(s.amount_at_risk),
        "issues": [
            {"title": i.title, "severity": i.severity, "amount": fmt_money(i.amount_at_risk)} for i in result.issues
        ],
    }


def summary_prompt(result: ReconResult, feedback: str = "") -> str:
    return f"""{_context(result)}

Write the month-end summary an owner reads first: max 3 sentences, max 70 words. Say how clean the month is, name the biggest risks by amount, and say what to do first.

FACTS:
{json.dumps(_summary_facts(result), indent=1)}
{feedback}
Return: {{"summary": "..."}}"""


def ask_prompt(result: ReconResult, question: str, feedback: str = "") -> str:
    facts = _summary_facts(result)
    facts["issue_details"] = [_issue_payload(i) for i in result.issues]
    return f"""{_context(result)}

The owner asks: "{question}"

Answer in max 80 words using only FACTS. If FACTS can't answer it, say so plainly and say what data would be needed.

FACTS:
{json.dumps(facts, indent=1, default=str)}
{feedback}
Return: {{"answer": "..."}}"""


# -------------------------------------------------------------------- explainer
class Explainer:
    def __init__(self, llm: LLM | None, cache_path: Path | None = None):
        self.llm = llm
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        if cache_path and cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                self._cache = {}

    @property
    def enabled(self) -> bool:
        return self.llm is not None

    def describe(self) -> dict:
        if not self.llm:
            return {"enabled": False, "provider": "none", "model": None}
        return {"enabled": True, "provider": self.llm.provider, "model": self.llm.model}

    # ------------------------------------------------------------- plumbing
    def _call(self, prompt: str) -> dict:
        assert self.llm is not None
        key = hashlib.sha256(f"{self.llm.provider}|{SYSTEM}|{prompt}".encode()).hexdigest()
        with self._lock:
            cached = self._cache.get(key)
        if cached is None:
            started = time.time()
            cached = self.llm.complete_json(SYSTEM, prompt)
            log.info("%s call took %.1fs", self.llm.provider, time.time() - started)
            with self._lock:
                self._cache[key] = cached
                if self.cache_path:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self.cache_path.write_text(json.dumps(self._cache))
        return parse_json(cached)

    # -------------------------------------------------------- explanations
    def explain_issues(self, result: ReconResult) -> tuple[dict[str, dict], str | None]:
        business = result.meta.business_name
        rules = {i.id: template_explanation(i, business) for i in result.issues}
        if not self.llm or not result.issues:
            return rules, None
        by_id = {i.id: i for i in result.issues}
        context_numbers = allowed_numbers(_context(result))
        out: dict[str, dict] = {}
        pending = list(result.issues)
        feedback = ""
        error = None
        for attempt in range(2):
            try:
                data = self._call(explanations_prompt(result, pending, feedback))
            except Exception as exc:  # network, quota, bad JSON...
                log.warning("AI explanations failed: %s", exc)
                error = str(exc)
                break
            problems = []
            for item in data.get("explanations", []):
                issue = by_id.get(item.get("id"))
                if not issue or issue.id in out:
                    continue
                checked = self._validate(item, issue, context_numbers, business)
                if checked is None:
                    problems.append(issue)
                    continue
                out[issue.id] = checked
            pending = [i for i in result.issues if i.id not in out]
            if not pending:
                break
            bad = ", ".join(i.id for i in pending)
            feedback = (
                f"\nYOUR PREVIOUS ANSWER FOR {bad} WAS REJECTED: it was missing or used numbers that are not in "
                f"FACTS. Use only the exact figures given.\n"
            )
        for issue in result.issues:
            out.setdefault(issue.id, rules[issue.id])
        return out, error

    def _validate(self, item: dict, issue: Issue, context_numbers: set[Decimal], business: str) -> dict | None:
        allowed = allowed_numbers(issue.facts, issue.title, [a.label for a in issue.actions]) | context_numbers
        explanation = (item.get("explanation") or "").strip()
        cause = (item.get("likely_cause") or "").strip()
        if not explanation or invented_numbers(explanation + " " + cause, allowed):
            log.info("Rejected AI text for %s: %s", issue.id, invented_numbers(explanation + " " + cause, allowed))
            return None
        action_types = [a.type for a in issue.actions]
        action = item.get("recommended_action")
        if action not in action_types:
            action = action_types[0] if action_types else "mark_reviewed"
        message = item.get("customer_message")
        contacts = {a.type for a in issue.actions if a.contacts_customer}
        if action not in contacts:
            message = None
        elif not message or invented_numbers(message, allowed):
            message = template_explanation(issue, business)["customer_message"]  # safe fallback
        return {
            "explanation": explanation,
            "likely_cause": cause,
            "recommended_action": action,
            "customer_message": message,
            "source": "ai",
        }

    # -------------------------------------------------------------- summary
    def summarize(self, result: ReconResult) -> tuple[str, str]:
        fallback = template_summary(result)
        if not self.llm:
            return fallback, "rules"
        allowed = allowed_numbers(_summary_facts(result), _context(result))
        feedback = ""
        for _ in range(2):
            try:
                text = (self._call(summary_prompt(result, feedback)).get("summary") or "").strip()
            except Exception as exc:
                log.warning("AI summary failed: %s", exc)
                return fallback, "rules"
            bad = invented_numbers(text, allowed)
            if text and not bad:
                return text, "ai"
            feedback = f"\nREJECTED: you used numbers not in FACTS ({', '.join(bad)}). Use only exact figures.\n"
        return fallback, "rules"

    # ------------------------------------------------------------------ Q&A
    def ask(self, result: ReconResult, question: str) -> dict:
        question = question.strip()[:300]
        if not self.llm:
            return {
                "answer": "AI is turned off, so I can't answer free-form questions. Add a GEMINI_API_KEY to enable it.",
                "source": "rules",
            }
        facts_for_check = [_summary_facts(result), _context(result), question] + [
            [i.facts, i.title] for i in result.issues
        ]
        allowed = allowed_numbers(*facts_for_check)
        feedback = ""
        for _ in range(2):
            try:
                text = (self._call(ask_prompt(result, question, feedback)).get("answer") or "").strip()
            except Exception as exc:
                return {"answer": f"The AI service isn't reachable right now ({exc}).", "source": "error"}
            bad = invented_numbers(text, allowed)
            if text and not bad:
                return {"answer": text, "source": "ai"}
            feedback = f"\nREJECTED: you used numbers not in FACTS ({', '.join(bad)}). Use only exact figures.\n"
        return {
            "answer": "I couldn't answer that using only the numbers in this month's data, so I won't guess.",
            "source": "guardrail",
        }
