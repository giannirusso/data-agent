"""Known questions skip the SQL model and fill the template parameters."""

import json
from datetime import date
from pathlib import Path

from agent.cli import handle_message
from agent.golden.retriever import GoldenRetriever
from agent.golden.templates import match_template
from agent.safety.sql_validator import validate_and_rewrite
from config.settings import project_root
from tests.conftest import FakeBQ, make_runtime

AS_OF = date(2024, 12, 31)


def _hits(question: str):
    retriever = GoldenRetriever(str(project_root() / "golden_bucket" / "trios.jsonl"))
    return retriever.search(question, k=3)


def test_top_products_fills_limit_and_last_quarter():
    question = "What were our top 5 products by revenue last quarter?"
    filled = match_template(question, _hits(question), today=AS_OF)
    assert filled is not None
    assert filled.trio_id == "gb-0002"
    assert filled.kind == "top_products"
    assert "LIMIT 5" in filled.sql
    assert filled.params["period_start"].startswith("2024-10-01")
    assert filled.params["period_end"].startswith("2025-01-01")
    assert "2024-10-01" not in filled.sql
    checked = validate_and_rewrite(filled.sql)
    assert checked.ok, checked.error
    assert "@entitled_brands" in checked.sql


def test_monthly_revenue_uses_the_named_year():
    question = "Monthly revenue in 2023"
    filled = match_template(question, _hits(question), today=AS_OF)
    assert filled is not None
    assert filled.trio_id == "gb-0001"
    assert filled.params["period_start"].startswith("2023-01-01")
    assert filled.params["period_end"].startswith("2024-01-01")
    assert "GROUP BY month" in filled.sql


def test_category_customers_returns_and_product_compare():
    category = match_template("Revenue by category last quarter", _hits("Revenue by category last quarter"), today=AS_OF)
    assert category is not None and category.trio_id == "gb-0012"
    assert category.params["period_start"].startswith("2024-10-01")

    customers = match_template("Top 10 customers by spend", _hits("Top 10 customers by spend"), today=AS_OF)
    assert customers is not None and customers.trio_id == "gb-0013"
    assert "customer_key" in customers.sql
    assert "LIMIT 10" in customers.sql
    assert "email" not in customers.sql.lower()

    returns = match_template(
        "What is the return rate by category?",
        _hits("What is the return rate by category?"),
        today=AS_OF,
    )
    assert returns is not None and returns.trio_id == "gb-0005"
    assert "return_rate" in returns.sql

    question = "Compare the wool suit vs the denim jacket"
    compared = match_template(question, _hits(question), today=AS_OF)
    assert compared is not None and compared.trio_id == "gb-0014"
    assert compared.params["product_names"] == ["wool suit", "denim jacket"]
    assert "wool suit" not in compared.sql
    assert "UNNEST(@product_names)" in compared.sql
    for filled in (category, customers, returns, compared):
        checked = validate_and_rewrite(filled.sql)
        assert checked.ok, checked.error


def test_novel_and_single_month_questions_stay_on_the_model():
    for question in (
        "Why did margin fall in the Northeast last year?",
        "What was revenue last month?",
        "Why are users in Texas underspending compared to California?",
    ):
        assert match_template(question, _hits(question), today=AS_OF) is None


def test_fast_path_skips_sql_generation_and_records_the_template(settings):
    seen = []

    def complete(system, user, tier):
        seen.append(system.splitlines()[0])
        if "PURPOSE: sql" in system:
            raise AssertionError("golden template should not ask the model for SQL")
        return "The leading product carried the quarter."

    runtime = make_runtime(
        settings,
        bq=FakeBQ(rows=[{"name": "Wool Suit", "revenue": 519.98, "orders": 3}]),
        complete=complete,
    )
    runtime.deps.data_through = AS_OF
    runtime.deps.data_through_source = "orders"
    reply = handle_message(runtime, "What were our top 5 products by revenue last quarter?")
    assert seen == ["PURPOSE: analyze"]
    assert runtime.deps.bq.execute_count == 1
    sql = runtime.deps.bq.executed[0]["sql"]
    params = runtime.deps.bq.executed[0]["params"]
    assert "@entitled_brands" in sql
    assert "LIMIT 5" in sql
    assert params["period_start"].startswith("2024-10-01")
    assert params["entitled_brands"] == ["Calvin Klein", "Allegra K"]
    assert "Wool Suit" in reply
    trace = reply.split("trace: ", 1)[1].split("·", 1)[0].strip()
    shown = handle_message(runtime, f"/trace {trace}")
    assert "sql: golden template gb-0002" in shown
    log = Path(settings.log_path).read_text(encoding="utf-8")
    assert "sql: golden template gb-0002" in log or '"golden_template": "gb-0002"' in log


def test_fallback_bullet_drops_the_name_prefix():
    from agent.nodes.analyze import _golden_fallback

    text = _golden_fallback(
        {"period_label": "Q4 2024"},
        [
            {
                "rows": [
                    {
                        "name": "Tommy Hilfiger Men's Houndstooth Trim Fit Suit",
                        "category": "Suits & Sport Coats",
                        "revenue": 519.98,
                        "orders": 2,
                    }
                ]
            }
        ],
        None,
    )
    assert "Tommy Hilfiger Men's Houndstooth Trim Fit Suit (Suits & Sport Coats): $519.98, 2 orders" in text
    assert "name Tommy" not in text
    assert "\n\nRevenue excludes Cancelled and Returned." in text


def test_empty_narrative_logs_the_raw_reason(settings):
    def complete(system, user, tier):
        if "PURPOSE: analyze" in system:
            return "   "
        return "SELECT 1"

    runtime = make_runtime(
        settings,
        bq=FakeBQ(rows=[{"name": "Wool Suit", "category": "Suits", "revenue": 10, "orders": 1}]),
        complete=complete,
    )
    runtime.deps.data_through = AS_OF
    runtime.deps.data_through_source = "orders"
    reply = handle_message(runtime, "What were our top 5 products by revenue last quarter?")
    assert "Narrative skipped: the model returned no usable narrative" in reply
    rows = [json.loads(line) for line in Path(settings.log_path).read_text(encoding="utf-8").splitlines()]
    analyze = [row for row in rows if row.get("span") == "analyze"]
    assert analyze
    assert analyze[-1]["details"]["narrative_detail"] == "whitespace only"


def test_parse_failure_is_logged_on_the_analyze_span(settings):
    def complete(system, user, tier):
        if "PURPOSE: analyze" in system:
            raise RuntimeError("parse failure: model content had no text parts")
        return "SELECT 1"

    runtime = make_runtime(
        settings,
        bq=FakeBQ(rows=[{"name": "Wool Suit", "revenue": 10, "orders": 1}]),
        complete=complete,
    )
    runtime.deps.data_through = AS_OF
    runtime.deps.data_through_source = "orders"
    handle_message(runtime, "What were our top 5 products by revenue last quarter?")
    rows = [json.loads(line) for line in Path(settings.log_path).read_text(encoding="utf-8").splitlines()]
    analyze = [row for row in rows if row.get("span") == "analyze"]
    assert analyze
    detail = analyze[-1]["details"]["narrative_detail"]
    assert "parse failure" in detail
    assert "no text parts" in detail


def test_analyze_failure_uses_a_row_summary(settings):
    def complete(system, user, tier):
        raise TimeoutError("The read timed out")

    runtime = make_runtime(
        settings,
        bq=FakeBQ(rows=[{"name": "Wool Suit", "revenue": 2155.83, "orders": 4}]),
        complete=complete,
    )
    runtime.deps.data_through = AS_OF
    runtime.deps.data_through_source = "orders"
    reply = handle_message(runtime, "What were our top 5 products by revenue last quarter?")
    assert "Wool Suit" in reply
    assert "$2,155.83" in reply
    assert "Narrative skipped: Gemini timed out" in reply
    assert "busy" not in reply.lower()
    assert runtime.deps.bq.execute_count == 1


def test_rate_limit_is_logged_as_429_not_only_busy(settings):
    def complete(system, user, tier):
        raise RuntimeError("429 resource exhausted")

    runtime = make_runtime(settings, bq=FakeBQ(), complete=complete)
    reply = handle_message(runtime, "Why did margin fall in the Northeast last year?")
    assert "busy" in reply.lower()
    rows = [json.loads(line) for line in Path(settings.log_path).read_text(encoding="utf-8").splitlines()]
    sql_rows = [row for row in rows if row.get("span") == "generate_sql" and row.get("status") == "error"]
    assert sql_rows
    assert sql_rows[-1]["details"]["error_kind"] == "429"
    assert "429" in sql_rows[-1]["details"]["last_error"]
    assert sql_rows[-1]["details"]["attempts"] >= 1
