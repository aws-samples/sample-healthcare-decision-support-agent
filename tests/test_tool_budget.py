"""Tests for request-scoped external tool limits."""

from medical_nudging.tools.tool_budget import CallBudget


def test_call_budget_stops_after_configured_limit():
    budget = CallBudget("search_guidelines", 2)

    assert budget.consume() == (True, 1)
    assert budget.consume() == (True, 0)
    assert budget.consume() == (False, 0)
    assert "BUDGET_EXHAUSTED" in budget.exhausted_message()


def test_call_budget_can_be_unbounded():
    budget = CallBudget("search_guidelines", None)

    assert budget.consume() == (True, None)
    assert budget.consume() == (True, None)
