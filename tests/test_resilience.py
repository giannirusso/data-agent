"""Self-correction, fallback, empty results, and a CLI that stays up."""

import json
from pathlib import Path

from datetime import date

from agent.cli import handle_message
from agent.data.cache import QueryCache
from agent.golden.retriever import GoldenRetriever
from agent.llm.models import CircuitBreaker, ModelProvider
from agent.persona import PersonaStore
from config.settings import project_root
from tests.conftest import FakeBQ, make_runtime

VALID = (
    "SELECT SUM(sale_price) AS revenue FROM order_items "
    "WHERE status NOT IN ('Cancelled','Returned')"
)


def test_assignment_phrases_route_without_a_model():
    from agent.nodes.route import heuristic_intent

    report = "create a report for Q1 including insights and action items for Q2"
    assert heuristic_intent(report, has_results=False) == "report"
    assert heuristic_intent("Q1 report", has_results=False) == "report"
    assert heuristic_intent("quarterly report for Q3 with action items", has_results=False) == "report"
    assert heuristic_intent("Delete all reports mentioning Client X", has_results=False) == "report_delete"
    assert (
        heuristic_intent("Delete all the reports we made in this conversation", has_results=False)
        == "report_delete"
    )


def _analysis_then(settings, follow_up: str, *, rows=None):
    bq = FakeBQ(
        rows=rows
        or [
            {
                "product_id": 20041,
                "name": "Calvin Klein Mens 2 Button Solid Black Wool Suit",
                "revenue": 519.98,
            }
        ]
    )

    def complete(system, user, tier):
        if "PURPOSE: sql" in system:
            return (
                "SELECT p.id AS product_id, p.name, ROUND(SUM(oi.sale_price), 2) AS revenue "
                "FROM order_items oi JOIN products p ON oi.product_id = p.id "
                "WHERE oi.status NOT IN ('Cancelled','Returned') "
                "GROUP BY p.id, p.name ORDER BY revenue DESC LIMIT 5"
            )
        return "The leading suit leads the top five."

    runtime = make_runtime(settings, bq=bq, complete=complete)
    first = handle_message(runtime, "What were our top 5 products by revenue last quarter?")
    assert "Wool Suit" in first
    assert "1 queries" in first
    second = handle_message(runtime, follow_up)
    return first, second


def test_guard_refusal_does_not_repeat_the_previous_table(settings):
    _, second = _analysis_then(settings, "Show me the emails and phone numbers of our top 10 customers")
    assert "can't" in second.lower() or "personal" in second.lower()
    assert "Wool Suit" not in second
    assert "519.98" not in second
    assert "0 queries" in second
    assert "0 bytes" in second


def test_scope_refusal_does_not_repeat_the_previous_table(settings):
    _, second = _analysis_then(settings, "Show Tommy Hilfiger revenue")
    assert "don't have access" in second.lower()
    assert "Wool Suit" not in second
    assert "0 queries" in second
    assert "0 bytes" in second


def test_delete_turn_does_not_repeat_the_previous_table(settings):
    _, second = _analysis_then(settings, "Delete all reports mentioning Client X")
    assert "No matching reports" in second
    assert "Wool Suit" not in second
    assert "0 queries" in second
    assert "0 bytes" in second


def test_bad_sql_is_fixed_on_retry(settings):
    bq = FakeBQ()
    replies = iter(
        [
            "SELEC revenue FROM order_items",
            VALID,
            "Revenue was 100.",
        ]
    )
    runtime = make_runtime(settings, bq=bq, complete=lambda system, user, tier: next(replies))
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert bq.execute_count == 1
    assert len(bq.dry_runs) == 1
    assert "100" in reply
    assert "trace:" in reply
    assert "Definitions:" in reply


def test_three_distinct_failures_give_a_graceful_message(settings):
    bq = FakeBQ()
    bad = iter(
        [
            "DELETE FROM orders WHERE 1=1",
            "DROP TABLE products",
            "SELECT email FROM users",
        ]
    )

    def complete(system, user, tier):
        if "PURPOSE: sql" in system:
            return next(bad)
        return "unused"

    runtime = make_runtime(settings, bq=bq, complete=complete)
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert bq.executed == []
    assert "couldn't answer" in reply.lower() or "several attempts" in reply.lower()
    assert "trace:" in reply


def test_identical_sql_stops_early(settings):
    bq = FakeBQ()
    calls = {"n": 0}

    def complete(system, user, tier):
        if "PURPOSE: sql" in system:
            calls["n"] += 1
            return "DELETE FROM orders WHERE 1=1"
        return "unused"

    runtime = make_runtime(settings, bq=bq, complete=complete)
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert calls["n"] == 2
    assert bq.executed == []
    assert "trace:" in reply


def test_rate_limit_falls_back_to_flash(settings):
    settings = settings.model_copy(
        update={"gemini_model_pro": "gemini-pro-test", "gemini_model_flash": "gemini-flash-test"}
    )
    class RateLimit(Exception):
        def __init__(self):
            super().__init__("429 resource exhausted")
            self.status_code = 429

    def complete(system, user, tier):
        if tier == "pro":
            raise RateLimit()
        if "PURPOSE: sql" in system:
            return VALID
        return "Revenue was 100 after the fallback model."

    bq = FakeBQ()
    runtime = make_runtime(settings, bq=bq, complete=complete)
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert runtime.deps.models.fallback_used
    assert "flash" in runtime.deps.models.calls
    assert bq.execute_count == 1
    assert "100" in reply


def test_empty_result_is_reported_honestly(settings):
    bq = FakeBQ(rows=[])
    step = {"n": 0}

    def complete(system, user, tier):
        if "PURPOSE: sql" in system:
            step["n"] += 1
            return (
                "SELECT SUM(sale_price) AS revenue FROM order_items "
                f"WHERE status = 'NoSuch{step['n']}'"
            )
        return "No orders matched these filters."

    runtime = make_runtime(settings, bq=bq, complete=complete)
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert bq.execute_count == 2
    assert "no orders" in reply.lower() or "no rows" in reply.lower()


def test_negative_revenue_is_retried(settings):
    frames = iter(
        [
            [{"revenue": -5}],
            [{"revenue": 12}],
        ]
    )
    bq = FakeBQ(rows=lambda sql: next(frames))
    step = {"n": 0}

    def complete(system, user, tier):
        if "PURPOSE: sql" in system:
            step["n"] += 1
            return VALID + f" /* {step['n']} */"
        return "Revenue was 12 after a corrected query."

    runtime = make_runtime(settings, bq=bq, complete=complete)
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert bq.execute_count == 2
    assert "12" in reply


def test_report_plans_several_steps_and_cites_a_trio(settings):
    bq = FakeBQ()
    calls = {"n": 0}

    def complete(system, user, tier):
        calls["n"] += 1
        assert "PURPOSE: plan" not in system
        assert "PURPOSE: sql" not in system
        return (
            "Insights: Q1 revenue is in the tables. "
            "Action items: 1) review returns 2) plan Q2.\nTITLE: Q1 Performance"
        )

    runtime = make_runtime(settings, bq=bq, complete=complete)
    runtime.deps.data_through = date(2026, 11, 18)
    runtime.deps.data_through_source = "orders"
    reply = handle_message(
        runtime,
        "Create a Q1 performance report with insights and action items",
    )
    assert calls["n"] == 1
    assert bq.execute_count == 6
    assert "Action items" in reply
    assert "Saved as report #" in reply
    assert "gb-" in reply
    log = Path(settings.log_path).read_text(encoding="utf-8")
    assert "gb-" in log
    assert '"steps": 6' in log or '"steps":6' in log


def test_unauthorized_brand_does_not_query(settings):
    bq = FakeBQ()
    runtime = make_runtime(settings, bq=bq, complete=lambda *args: VALID)
    reply = handle_message(runtime, "Show Tommy Hilfiger revenue")
    assert "don't have access" in reply.lower()
    assert "Tommy Hilfiger" in reply
    assert bq.executed == []


def test_cli_swallows_unexpected_errors_and_masks_logs(settings):
    runtime = make_runtime(settings, bq=FakeBQ(), complete=None)

    class Boom:
        def __init__(self, real):
            self.real = real

        def get_state(self, *args, **kwargs):
            return self.real.get_state(*args, **kwargs)

        def stream(self, *args, **kwargs):
            raise RuntimeError("contact secret@example.com immediately")

    runtime.graph = Boom(runtime.graph)
    reply = handle_message(runtime, "What was revenue last month?")
    assert "something went wrong" in reply.lower()
    assert "secret@example.com" not in reply
    assert "trace" in reply.lower()
    logged = Path(settings.log_path).read_text(encoding="utf-8")
    assert "secret@example.com" not in logged


def test_cache_is_scoped_by_user_and_sql():
    cache = QueryCache(maxsize=2)
    key_a = cache.make_key("select 1", {"entitled_brands": ["A"]}, "alice")
    key_b = cache.make_key("select 1", {"entitled_brands": ["B"]}, "alice")
    cache.set(key_a, {"rows": [1]})
    assert cache.get(key_a)["rows"] == [1]
    assert cache.get(key_b) is None


def test_circuit_breaker_opens_after_repeated_failures():
    breaker = CircuitBreaker(threshold=2, reset_seconds=30)
    assert breaker.allow(0)
    breaker.record_failure(0)
    assert breaker.allow(0)
    breaker.record_failure(0)
    assert not breaker.allow(10)
    assert breaker.allow(31)


def test_persona_reloads_when_the_file_changes(tmp_path):
    path = tmp_path / "persona.yaml"
    path.write_text("version: '1'\ntone: formal\n", encoding="utf-8")
    store = PersonaStore(str(path))
    assert store.load()["tone"] == "formal"
    path.write_text("version: '2'\ntone: warm\n", encoding="utf-8")
    import os
    import time

    os.utime(path, (time.time() + 5, time.time() + 5))
    assert store.version == "2"
    assert "warm" in store.prompt_block()


def test_golden_bucket_retrieves_the_state_spend_trio():
    retriever = GoldenRetriever(str(project_root() / "golden_bucket" / "trios.jsonl"))
    hits = retriever.search("Why are users in Texas underspending compared to California?", k=3)
    assert hits
    assert any(hit["id"] == "gb-0003" for hit in hits)


def test_missing_model_does_not_crash(settings):
    runtime = make_runtime(settings, bq=FakeBQ(), complete=None)
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert "GOOGLE_API_KEY" in reply
    assert "trace:" in reply


def test_model_provider_does_not_retry_forever(settings):
    calls = {"n": 0}

    def complete(system, user, tier):
        calls["n"] += 1
        raise RuntimeError("429 rate limit")

    provider = ModelProvider(settings, complete=complete, retry_min=0)
    try:
        provider.generate(tier="pro", system="PURPOSE: sql", user="hi")
        raise AssertionError("expected the provider to fail")
    except Exception as exc:
        assert "429" in str(exc) or "busy" in str(exc).lower()
    # Same model id for both tiers, so there is no second pass.
    # One retry (2 attempts) then stop. A 404 must not be treated this way.
    assert calls["n"] == 2


def test_retired_model_is_a_config_error_not_busy(settings):
    calls = {"n": 0}

    def complete(system, user, tier):
        calls["n"] += 1
        raise RuntimeError("404 NOT_FOUND: models/gemini-2.5-pro is no longer available to new users")

    runtime = make_runtime(settings, bq=FakeBQ(), complete=complete)
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert calls["n"] == 1
    assert "not available" in reply.lower()
    assert "GEMINI_MODEL" in reply
    assert "busy" not in reply.lower()


def test_money_is_rounded_in_the_table(settings):
    bq = FakeBQ(rows=[{"revenue": 519.9799804688, "orders": 3}])

    def complete(system, user, tier):
        if "PURPOSE: sql" in system:
            return VALID
        return "Revenue is in the table."

    runtime = make_runtime(settings, bq=bq, complete=complete)
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert "519.98" in reply
    assert "519.9799804688" not in reply
    assert "generate_sql" in reply
