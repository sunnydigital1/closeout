"""AI layer tests with a fake model: guardrails, fallbacks, caching."""
import json

import pytest

from app.ai import Explainer, LLMError, invented_numbers, allowed_numbers, template_explanation
from app.reconcile import reconcile
from app.sources import load_sample


class FakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def complete_json(self, system, prompt):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str) else json.dumps(item)


@pytest.fixture(scope="module")
def october():
    return reconcile(load_sample())


def good_item(issue):
    return {
        "id": issue.id,
        "explanation": f"Here is what happened with {issue.title}.",
        "likely_cause": "Something ordinary.",
        "recommended_action": issue.actions[0].type,
        "customer_message": None,
    }


def test_guardrail_spots_invented_numbers():
    allowed = allowed_numbers({"amount_due": "$640.00", "days_overdue": 13})
    assert invented_numbers("Invoice is 13 days late and $640.00 is due.", allowed) == []
    assert invented_numbers("You are owed $650.00.", allowed) == ["650.00"]


def test_valid_ai_output_is_used(october):
    llm = FakeLLM([{"explanations": [good_item(i) for i in october.issues]}])
    out, error = Explainer(llm).explain_issues(october)
    assert error is None
    assert all(v["source"] == "ai" for v in out.values())
    assert llm.calls == 1


def test_invented_number_is_retried_then_falls_back(october):
    target = october.issues[0]
    bad = [good_item(i) for i in october.issues]
    bad[0]["explanation"] = "You lost $9,999.99 on this."
    still_bad = {"explanations": [dict(bad[0])]}
    llm = FakeLLM([{"explanations": bad}, still_bad])
    out, _ = Explainer(llm).explain_issues(october)
    assert out[target.id]["source"] == "rules"  # never shows the invented figure
    assert "9,999.99" not in out[target.id]["explanation"]
    assert sum(1 for v in out.values() if v["source"] == "ai") == len(october.issues) - 1
    assert llm.calls == 2


def test_unknown_action_is_replaced_with_an_allowed_one(october):
    items = [good_item(i) for i in october.issues]
    items[0]["recommended_action"] = "wire_money_somewhere"
    out, _ = Explainer(FakeLLM([{"explanations": items}])).explain_issues(october)
    allowed = {a.type for a in october.issues[0].actions}
    assert out[october.issues[0].id]["recommended_action"] in allowed


def test_provider_failure_falls_back_to_rules(october):
    out, error = Explainer(FakeLLM([LLMError("429 quota")])).explain_issues(october)
    assert "429" in error
    assert all(v["source"] == "rules" for v in out.values())


def test_responses_are_cached(october, tmp_path):
    payload = {"explanations": [good_item(i) for i in october.issues]}
    cache = tmp_path / "cache.json"
    first = FakeLLM([payload])
    Explainer(first, cache).explain_issues(october)
    second = FakeLLM([])  # would raise if called
    out, _ = Explainer(second, cache).explain_issues(october)
    assert second.calls == 0 and all(v["source"] == "ai" for v in out.values())


def test_summary_guardrail(october):
    llm = FakeLLM([{"summary": "You made $1,000,000 this month."}, {"summary": "Mostly clean month."}])
    text, source = Explainer(llm).summarize(october)
    assert source == "ai" and text == "Mostly clean month."


def test_every_issue_kind_has_a_readable_template(october):
    for issue in october.issues:
        t = template_explanation(issue, "Larkspur Candle Co.")
        assert t["explanation"] and "{" not in t["explanation"]
        allowed = allowed_numbers(issue.facts, issue.title)
        assert invented_numbers(t["explanation"], allowed) == [], issue.id


def test_ask_without_ai_is_honest(october):
    answer = Explainer(None).ask(october, "Why is October short?")
    assert "turned off" in answer["answer"]


# --------------------------------------------------------- Gemini fallbacks

def _gemini_with(responses):
    """A GeminiLLM whose network client is replaced by scripted responses per model."""
    from types import SimpleNamespace

    from app.ai import GeminiLLM

    calls = []

    def generate_content(model, contents, config):
        calls.append(model)
        outcome = responses[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)

    llm = GeminiLLM.__new__(GeminiLLM)
    llm._client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    llm._models = list(responses)
    llm.model = llm._models[0]
    llm.backoff = 0
    return llm, calls


def _busy(code=503):
    from google.genai import errors

    return errors.ServerError(code, {"error": {"code": code, "message": "high demand", "status": "UNAVAILABLE"}})


def test_gemini_retries_then_moves_to_next_model_when_overloaded():
    llm, calls = _gemini_with({"model-a": [_busy(), _busy()], "model-b": ['{"ok": true}']})
    assert llm.complete_json("sys", "prompt") == '{"ok": true}'
    assert calls == ["model-a", "model-a", "model-b"]
    assert llm.model == "model-b"  # remembered for the next call


def test_gemini_recovers_on_retry_of_same_model():
    llm, calls = _gemini_with({"model-a": [_busy(), '{"ok": 1}'], "model-b": []})
    assert llm.complete_json("sys", "prompt") == '{"ok": 1}'
    assert calls == ["model-a", "model-a"]


def test_gemini_all_models_busy_raises_clear_error():
    from google.genai import errors

    quota = errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    llm, _ = _gemini_with({"model-a": [_busy(), _busy()], "model-b": [quota]})
    with pytest.raises(LLMError) as err:
        llm.complete_json("sys", "prompt")
    assert "busy" in str(err.value) and "model-b 429" in str(err.value)


def test_summary_mentions_sales_waiting_for_paypal():
    from datetime import date, datetime, timezone
    from decimal import Decimal

    from app.ai import _summary_facts, template_summary
    from app.models import Dataset, FeeSchedule, Meta, Order

    meta = Meta("Test Shop", "USD", date(2026, 11, 1), date(2026, 11, 30), date(2026, 11, 2), FeeSchedule(),
                "paypal", data_through=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))
    order = Order("O-1", date(2026, 11, 2), "A Buyer", "a@example.com", "web", "O-1", Decimal("24.00"), "USD", "paid")
    result = reconcile(Dataset(meta, [order], [], []))
    text = template_summary(result)
    assert "1 recent sale is still waiting for PayPal" in text
    assert "ready to close" not in text
    assert _summary_facts(result)["recent_sales_waiting_for_paypal_to_report"] == 1
    # The demo month has nothing waiting, so its prompt (and cached AI answer) is unchanged.
    assert "recent_sales_waiting_for_paypal_to_report" not in _summary_facts(reconcile(load_sample()))
