"""Treat an empty or suspicious frame as a hypothesis, then either retry or accept it."""

from __future__ import annotations

from agent.deps import Deps, status


def check_result_node(state: dict, deps: Deps) -> dict:
    frame = dict(state.get("pending_frame") or {})
    if state.get("template_sql"):
        if int(frame.get("row_count") or 0) == 0:
            frame["legitimately_empty"] = True
            frame["empty"] = True
        accepted = _accept(state, frame)
        if _budget_spent(deps):
            accepted["partial"] = True
        return accepted
    reason = suspicious_reason(frame)
    row_count = int(frame.get("row_count") or 0)
    attempts = int(state.get("attempts") or 0)
    max_attempts = deps.settings.max_sql_attempts

    if row_count == 0 and not frame.get("legitimately_empty"):
        if not state.get("empty_retried") and attempts < max_attempts:
            status(deps, "no rows, refining filters")
            return {
                "empty_retried": True,
                "last_error": (
                    "Query returned no rows. Check string casing (state values look like 'Texas'), "
                    "whether the date range exists, and the status filter "
                    "(revenue excludes Cancelled and Returned)."
                ),
                "retry_execute": False,
                "cache_hit": False,
                "stop_retry": False,
            }
        frame = dict(frame)
        frame["legitimately_empty"] = True
        frame["empty"] = True
        return _accept(state, frame)

    if reason:
        if not state.get("suspicious_retried") and attempts < max_attempts:
            status(deps, "result looked off, retrying")
            return {
                "suspicious_retried": True,
                "last_error": f"Suspicious result: {reason} Rewrite the aggregation or the join.",
                "retry_execute": False,
                "cache_hit": False,
                "stop_retry": False,
            }
        return {
            "last_error": f"I stopped because the result looked unreliable ({reason}).",
            "stop_retry": True,
            "retry_execute": False,
            "cache_hit": False,
        }

    return _accept(state, frame)


def _budget_spent(deps: Deps) -> bool:
    import time

    started = getattr(deps, "turn_started", 0) or 0
    budget = getattr(deps.settings, "turn_budget_seconds", 0) or 0
    if not started or not budget:
        return False
    return (time.perf_counter() - float(started)) > budget


def suspicious_reason(frame: dict) -> str | None:
    rows = frame.get("rows") or []
    if int(frame.get("row_count") or 0) > 1000:
        return "row count is above the limit, which suggests a join fan-out"
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value < 0 and any(token in str(key).lower() for token in ("revenue", "margin", "profit", "sale")):
                return f"negative value in {key}"
    if rows and all(isinstance(row, dict) and all(value is None for value in row.values()) for row in rows):
        return "every value is null"
    return None


def _accept(state: dict, frame: dict) -> dict:
    results = list(state.get("results") or [])
    results.append(frame)
    scanned = int(frame.get("bytes") or 0)
    if state.get("cache_hit"):
        scanned = 0
    return {
        "results": results,
        "pending_frame": None,
        "last_error": None,
        "current_step": int(state.get("current_step") or 0) + 1,
        "attempts": 0,
        "sql": None,
        "rewritten_sql": None,
        "sql_fingerprint": None,
        "empty_retried": False,
        "suspicious_retried": False,
        "stop_retry": False,
        "fatal": None,
        "retry_execute": False,
        "retry_dry": False,
        "cache_hit": False,
        "transient_tries": 0,
        "bytes_total": int(state.get("bytes_total") or 0) + scanned,
    }
