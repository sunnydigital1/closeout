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
