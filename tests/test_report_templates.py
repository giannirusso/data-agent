"""Quarterly reports use fixed SQL and still finish if the model times out."""

from datetime import date

from agent.cli import handle_message
from agent.reports.period import parse_period
from agent.reports.templates import report_steps
from agent.safety.sql_validator import validate_and_rewrite
from tests.conftest import FakeBQ, make_runtime

TODAY = date(2026, 11, 18)


def test_q1_without_a_year_uses_the_latest_complete_q1_in_the_data_window():
    period = parse_period(
        "create a report for Q1 including insights and action items for Q2",
        today=TODAY,
    )
    assert (period.quarter, period.year) == (1, 2024)
    assert period.start.isoformat().startswith("2024-01-01")
    assert period.end.isoformat().startswith("2024-04-01")
    assert "2024" in period.note
    assert "No year was given" in period.note


def test_quarter_phrases():
    q1 = parse_period("Q1 report", today=TODAY)
    assert (q1.quarter, q1.year) == (1, 2024)
    q3 = parse_period("quarterly report for Q3 with action items", today=TODAY)
    assert (q3.quarter, q3.year) == (3, 2024)
    named = parse_period("create a report for Q1 2023", today=TODAY)
    assert (named.quarter, named.year) == (1, 2023)


def test_report_templates_are_single_selects_with_date_parameters():
    period = parse_period("Q1 report", today=TODAY)
    steps = report_steps(period)
    assert len(steps) == 6
    purposes = [step["purpose"] for step in steps]
    assert purposes == ["monthly", "products", "categories", "returns", "customers", "qoq"]
    for step in steps:
        result = validate_and_rewrite(step["sql"])
        assert result.ok, result.error
        assert "@entitled_brands" in result.sql
        assert "@period_start" in step["sql"]
        assert "2024-01-01" not in step["sql"]
        assert step["params"]["period_start"].startswith("2024-01-01")


def test_sql_timeout_is_not_called_busy(settings):
    def complete(system, user, tier):
        raise TimeoutError("The read timed out")

    runtime = make_runtime(settings, bq=FakeBQ(), complete=complete)
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert "timed out" in reply.lower()
    assert "/retry" in reply
    assert "busy" not in reply.lower()


def test_timeout_still_saves_a_template_report(settings):
    calls = {"n": 0}

    def complete(system, user, tier):
        calls["n"] += 1
        raise TimeoutError("The read timed out")

    runtime = make_runtime(settings, bq=FakeBQ(rows=[{"revenue": 2012.96, "orders": 40}]), complete=complete)
    reply = handle_message(
        runtime,
        "create a report for Q1 including insights and action items for Q2",
    )
    assert calls["n"] == 1
    assert "Saved as report #" in reply
    assert "Action items" in reply
    assert "template-based" in reply
    assert "Q1 2024" in reply
    assert "timed out" not in reply.lower() or "template-based" in reply
    assert runtime.deps.bq.execute_count == 6
    assert "busy" not in reply.lower()
