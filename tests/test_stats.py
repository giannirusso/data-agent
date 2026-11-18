""" /stats counts real failures, not delete confirmations, and reports rates."""

from agent.cli import handle_message
from agent.obs.logging import format_stats, summarize_session
from tests.conftest import FakeBQ, make_runtime


def _row(span, status, trace, session="s", duration=100, **details):
    return {
        "span": span,
        "status": status,
        "trace_id": trace,
        "session_id": session,
        "duration_ms": duration,
        "details": details,
    }


def test_delete_outcomes_are_not_errors():
    rows = [
        _row("find_delete", "ok", "t1", duration=2),
        _row("confirm_delete", "waiting", "t1", duration=1),
        _row("confirm_delete", "cancelled", "t2", duration=3),
        _row("turn", "cancelled", "t2", duration=0, bytes=0, pii_hits=0),
        _row("confirm_delete", "waiting", "t3", duration=1),
        _row("confirm_delete", "deleted", "t4", duration=4),
        _row("turn", "deleted", "t4", duration=0, bytes=0, pii_hits=0),
    ]
    summary = summarize_session(rows)
    assert summary["errors"] == 0
    assert summary["cancelled_deletes"] == 1
    assert summary["deletes"] == 1
    assert summary["give_ups"] == 0
    assert summary["guard_blocks"] == 0


def test_rates_count_only_real_failures():
    rows = [
        _row("validate_sql", "error", "bad", duration=20, attempts=1),
        _row("generate_sql", "ok", "bad", duration=30, attempts=2),
        _row("turn", "give_up", "bad", duration=0, bytes=100, pii_hits=0),
        _row("guard", "blocked", "no", duration=1),
        _row("turn", "blocked", "no", duration=0, bytes=0, pii_hits=1),
        _row("validate_sql", "error", "fixed", duration=10, attempts=1),
        _row("generate_sql", "ok", "fixed", duration=40, attempts=2),
        _row("execute", "ok", "fixed", duration=50, last_bytes=500),
        _row("turn", "ok", "fixed", duration=0, bytes=500, pii_hits=0),
    ]
    summary = summarize_session(rows)
    assert summary["errors"] == 3  # two validation errors + one give-up
    assert summary["give_ups"] == 1
    assert summary["give_up_rate"] == "33.3% (1/3)"
    assert summary["guard_block_rate"] == "33.3% (1/3)"
    assert summary["self_correction_rate"] == "50.0% (1/2)"
    assert summary["bytes_scanned"] == 600
    assert summary["pii_hits"] == 1
    assert summary["latency_p50_ms"] is not None
    text = format_stats(summary, summary)
    assert "This session" in text
    assert "All time" in text
    assert "guard block rate" in text
    assert "self-correction rate" in text
    assert "latency p50" in text
    assert "latency p95" in text


def test_live_delete_session_stats_stay_at_zero_errors(settings):
    runtime = make_runtime(settings, bq=FakeBQ(), complete=None)
    runtime.deps.reports.save("alice", runtime.session_id, "Client X note", "mentions Client X", "")
    handle_message(runtime, "Delete all reports mentioning Client X")
    handle_message(runtime, "no")
    handle_message(runtime, "Delete all reports mentioning Client X")
    deleted = handle_message(runtime, "yes")
    assert "Deleted 1" in deleted
    restored = handle_message(runtime, "/restore")
    assert "Restored" in restored
    stats = handle_message(runtime, "/stats")
    assert "This session" in stats
    assert "All time" in stats
    assert "errors: 0" in stats
    assert "cancelled deletes: 1" in stats
    assert "deletes: 1" in stats
