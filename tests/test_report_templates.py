"""Quarterly reports use fixed SQL and still finish if the model times out."""

import json
from datetime import date
from pathlib import Path

from agent.cli import handle_message
from agent.reports.period import parse_period
from agent.reports.templates import report_steps
from agent.safety.sql_validator import validate_and_rewrite
from tests.conftest import FakeBQ, make_runtime

NOV_2026 = date(2026, 11, 18)


def test_q1_without_a_year_uses_the_latest_complete_q1_before_the_max_order():
    period = parse_period(
        "create a report for Q1 including insights and action items for Q2",
        data_through=NOV_2026,
        source="orders",
    )
    assert (period.quarter, period.year) == (1, 2026)
    assert period.start.isoformat().startswith("2026-01-01")
    assert period.end.isoformat().startswith("2026-04-01")
    assert "2026-11-18" in period.note
    assert "latest order date" in period.note
    assert "No year was given" in period.note


def test_older_max_date_still_selects_q1_of_that_window():
    period = parse_period("Q1 report", data_through=date(2024, 12, 31), source="orders")
    assert (period.quarter, period.year) == (1, 2024)


def test_quarter_phrases():
    q1 = parse_period("Q1 report", data_through=NOV_2026, source="orders")
    assert (q1.quarter, q1.year) == (1, 2026)
    q3 = parse_period("quarterly report for Q3 with action items", data_through=NOV_2026, source="orders")
    assert (q3.quarter, q3.year) == (3, 2026)
    named = parse_period("create a report for Q1 2023", data_through=NOV_2026, source="orders")
    assert (named.quarter, named.year) == (1, 2023)


def test_report_templates_are_single_selects_with_date_parameters():
    period = parse_period("Q1 report", data_through=NOV_2026, source="orders")
    steps = report_steps(period)
    assert len(steps) == 6
    purposes = [step["purpose"] for step in steps]
    assert purposes == ["monthly", "products", "categories", "returns", "customers", "qoq"]
    for step in steps:
        result = validate_and_rewrite(step["sql"])
        assert result.ok, result.error
        assert "@entitled_brands" in result.sql
        assert "@period_start" in step["sql"]
        assert "2026-01-01" not in step["sql"]
        assert step["params"]["period_start"].startswith("2026-01-01")


def test_sql_timeout_is_not_called_busy(settings):
    def complete(system, user, tier):
        raise TimeoutError("The read timed out")

    runtime = make_runtime(settings, bq=FakeBQ(), complete=complete)
    reply = handle_message(runtime, "Why did margin fall in the Northeast last year?")
    assert "Gemini timed out after 150s (free tier is slow)." in reply
    assert "Try /retry or raise GEMINI_TIMEOUT_SECONDS" in reply
    assert "busy" not in reply.lower()
    rows = [json.loads(line) for line in Path(settings.log_path).read_text(encoding="utf-8").splitlines()]
    sql_rows = [row for row in rows if row.get("span") == "generate_sql"]
    assert sql_rows
    assert sql_rows[-1]["status"] == "error"
    assert sql_rows[-1]["details"]["error_kind"] == "timeout"
    assert sql_rows[-1]["details"]["attempts"] >= 1
    assert "timed out" in sql_rows[-1]["details"]["last_error"].lower()


def test_timeout_still_saves_a_template_report(settings):
    calls = {"n": 0}

    def complete(system, user, tier):
        calls["n"] += 1
        raise TimeoutError("The read timed out")

    runtime = make_runtime(settings, bq=FakeBQ(rows=[{"revenue": 2012.96, "orders": 40}]), complete=complete)
    runtime.deps.data_through = date(2026, 11, 18)
    runtime.deps.data_through_source = "orders"
    reply = handle_message(
        runtime,
        "create a report for Q1 including insights and action items for Q2",
    )
    assert calls["n"] == 1
    assert "Saved as report #" in reply
    assert "Action items" in reply
    assert "template-based" in reply
    assert "Q1 2026" in reply
    assert "Narrative skipped: Gemini timed out" in reply
    assert "revenue=" not in reply
    assert runtime.deps.bq.execute_count == 6
    assert "busy" not in reply.lower()
    log = Path(settings.log_path).read_text(encoding="utf-8")
    assert "Narrative skipped: Gemini timed out" in log


def test_latest_order_date_is_queried_once_per_session(settings):
    import pandas as pd

    from agent.reports.period import resolve_data_through

    class MaxBQ(FakeBQ):
        def execute(self, sql, params=None, maximum_bytes_billed=None, timeout=None):
            self.execute_count += 1
            self.executed.append({"sql": sql, "params": params, "maximum_bytes_billed": maximum_bytes_billed})
            return pd.DataFrame([{"latest_order": "2026-11-10T08:00:00+00:00"}])

    runtime = make_runtime(settings, bq=MaxBQ(), complete=None)
    first, source = resolve_data_through(runtime.deps, brands=runtime.brands, salt=settings.pii_salt)
    again, source_again = resolve_data_through(runtime.deps, brands=runtime.brands, salt=settings.pii_salt)
    assert first == date(2026, 11, 10)
    assert again == first
    assert source == source_again == "orders"
    assert runtime.deps.bq.execute_count == 1
    assert runtime.deps.bq.dry_runs
    sql = runtime.deps.bq.executed[0]["sql"]
    assert "@entitled_brands" in sql
    assert "safe_orders" in sql
    assert runtime.deps.bq.executed[0]["params"]["entitled_brands"] == ["Calvin Klein", "Allegra K"]
    assert runtime.deps.bq.executed[0]["maximum_bytes_billed"] == settings.bq_max_bytes


def test_latest_order_lookup_falls_back_to_today(settings):
    from agent.reports.period import resolve_data_through

    class Down(FakeBQ):
        def execute(self, sql, params=None, maximum_bytes_billed=None, timeout=None):
            self.execute_count += 1
            raise RuntimeError("warehouse down")

    runtime = make_runtime(settings, bq=Down(), complete=None)
    found, source = resolve_data_through(runtime.deps, brands=runtime.brands, salt=settings.pii_salt)
    assert source == "today"
    assert found == date.today()
    again, _ = resolve_data_through(runtime.deps, brands=runtime.brands, salt=settings.pii_salt)
    assert again == found
    assert runtime.deps.bq.execute_count == 1


def test_template_summary_is_executive_prose_with_deltas():
    from agent.reports.templates import template_summary

    period = parse_period("Q1 report", data_through=NOV_2026, source="orders")
    frames = [
        {
            "purpose": "qoq",
            "rows": [
                {"period": "current", "revenue": 7194.11, "orders": 216},
                {"period": "previous", "revenue": 6673.57, "orders": 223},
            ],
        },
        {"purpose": "categories", "rows": [{"category": "Outerwear & Coats", "revenue": 1529.05}]},
        {"purpose": "returns", "rows": [{"returned_items": 33, "items": 311, "return_rate": 0.1061}]},
        {"purpose": "customers", "rows": [{"new_customers": 208, "returning_customers": 8}]},
    ]
    text = template_summary(period, frames, skip_hint="Narrative skipped: Gemini rate limit (429)")
    lines = text.splitlines()
    assert lines[0].startswith("TITLE:")
    assert lines[1] == ""
    assert lines[2] == "Narrative skipped: Gemini rate limit (429)"
    assert lines[3] == ""
    assert lines[4].startswith("No year was given")
    assert "$7,194.11" in text
    assert "+7.8%" in text
    assert "216 orders" in text
    assert "-3.1%" in text
    assert "$33.31" in text
    assert "Top category: Outerwear & Coats, $1,529.05" in text
    assert "Return rate 10.6% (33 of 311 items)" in text
    assert "Customers: 208 new, 8 returning" in text
    assert "template-based" in text
    assert "retention" in text.lower()
    assert "revenue=" not in text


def test_rate_limit_retries_once_then_records_the_skip(settings):
    calls = {"n": 0}

    class RateLimit(Exception):
        def __init__(self):
            super().__init__("429 rate limit")
            self.status_code = 429
            self.retry_after = 0

    def complete(system, user, tier):
        calls["n"] += 1
        raise RateLimit()

    runtime = make_runtime(settings, bq=FakeBQ(rows=[{"revenue": 100.0, "orders": 4}]), complete=complete)
    runtime.deps.data_through = NOV_2026
    runtime.deps.data_through_source = "orders"
    reply = handle_message(
        runtime,
        "create a report for Q1 including insights and action items for Q2",
    )
    assert calls["n"] == 2
    assert "Narrative skipped: Gemini rate limit (429)" in reply
    assert "template-based" in reply
    assert reply.splitlines()[0].startswith("TITLE:")
    assert reply.splitlines()[1] == ""
    log = Path(settings.log_path).read_text(encoding="utf-8")
    assert "Gemini rate limit (429)" in log
    assert all(json.loads(line) for line in log.splitlines() if line.strip())
