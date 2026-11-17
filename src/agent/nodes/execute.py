"""Dry-run, then execute. Both steps are skipped when the result is already cached."""

from __future__ import annotations

import json

import pandas as pd

from agent.data.bq_client import BigQueryConfigError, BigQueryRunnerError
from agent.data.cache import QueryCache
from agent.deps import Deps, status


def _params(state: dict, deps: Deps) -> dict:
    existing = state.get("query_params")
    if existing:
        return existing
    return {
        "entitled_brands": list(state.get("entitled_brands") or []),
        "pii_salt": deps.settings.pii_salt,
    }


def _cache_key(state: dict, deps: Deps, params: dict) -> str:
    return QueryCache.make_key(state.get("rewritten_sql") or "", params, state.get("user_id") or "")


def dry_run_node(state: dict, deps: Deps) -> dict:
    params = _params(state, deps)
    sql = state.get("rewritten_sql") or ""
    if not sql:
        return {"last_error": state.get("last_error") or "No SQL to dry-run.", "cache_hit": False, "retry_dry": False}
    cached = deps.cache.get(_cache_key(state, deps, params))
    if cached is not None:
        status(deps, "using a cached result")
        return {
            "pending_frame": cached,
            "cache_hit": True,
            "last_error": None,
            "retry_dry": False,
            "retry_execute": False,
            "query_params": params,
            "fatal": None,
        }

    status(deps, "estimating query cost")
    try:
        estimate = deps.bq.dry_run(sql, params=params)
    except BigQueryConfigError as exc:
        return {
            "fatal": "no_bq",
            "stop_retry": True,
            "last_error": str(exc),
            "cache_hit": False,
            "retry_dry": False,
        }
    except BigQueryRunnerError as exc:
        return _warehouse_error(state, exc, flag="retry_dry")
    except Exception as exc:
        return {"last_error": f"Dry run failed: {exc}", "cache_hit": False, "retry_dry": False}

    scanned = int(estimate.total_bytes_processed)
    if scanned > deps.settings.bq_max_bytes:
        return {
            "last_error": (
                f"Query would scan {scanned} bytes, over the budget of {deps.settings.bq_max_bytes}. "
                "Add a date filter, fewer columns, or a tighter aggregation."
            ),
            "last_bytes": scanned,
            "cache_hit": False,
            "retry_dry": False,
            "fatal": None,
            "stop_retry": False,
        }
    return {
        "last_error": None,
        "last_bytes": scanned,
        "cache_hit": False,
        "retry_dry": False,
        "fatal": None,
        "stop_retry": False,
        "transient_tries": 0,
        "query_params": params,
    }


def execute_node(state: dict, deps: Deps) -> dict:
    if state.get("cache_hit") and state.get("pending_frame"):
        return {"retry_execute": False, "last_error": None}
    params = _params(state, deps)
    sql = state.get("rewritten_sql") or ""
    status(deps, "querying BigQuery")
    try:
        frame_df = deps.bq.execute(
            sql,
            params=params,
            maximum_bytes_billed=deps.settings.bq_max_bytes,
            timeout=deps.settings.bq_timeout_seconds,
        )
    except BigQueryConfigError as exc:
        return {
            "fatal": "no_bq",
            "stop_retry": True,
            "last_error": str(exc),
            "retry_execute": False,
        }
    except BigQueryRunnerError as exc:
        return _warehouse_error(state, exc, flag="retry_execute")
    except Exception as exc:
        return {"last_error": f"BigQuery error: {exc}", "retry_execute": False}

    if not isinstance(frame_df, pd.DataFrame):
        frame_df = pd.DataFrame(frame_df)
    question = ""
    plan = state.get("plan") or []
    step = int(state.get("current_step") or 0)
    if step < len(plan):
        question = plan[step].get("question") or ""
    frame = frame_from_df(frame_df, question, sql, state.get("last_bytes") or 0)
    deps.cache.set(_cache_key(state, deps, params), frame)
    return {
        "pending_frame": frame,
        "last_error": None,
        "retry_execute": False,
        "fatal": None,
        "stop_retry": False,
        "transient_tries": 0,
    }


def frame_from_df(df: pd.DataFrame, question: str, sql: str, scanned_bytes: int) -> dict:
    view = df.head(50)
    try:
        rows = json.loads(view.to_json(orient="records", date_format="iso"))
    except Exception:
        rows = []
        for record in view.to_dict(orient="records"):
            rows.append({key: _plain(value) for key, value in record.items()})
    return {
        "question": question,
        "sql": sql,
        "columns": [str(column) for column in df.columns],
        "rows": rows,
        "row_count": int(len(df)),
        "truncated": len(df) > 50,
        "bytes": int(scanned_bytes or 0),
        "empty": len(df) == 0,
    }


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _warehouse_error(state: dict, exc: BigQueryRunnerError, *, flag: str) -> dict:
    if exc.transient or exc.outage:
        tries = int(state.get("transient_tries") or 0) + 1
        if tries <= 2:
            return {
                flag: True,
                "transient_tries": tries,
                "last_error": str(exc),
                "cache_hit": False,
                "fatal": None,
            }
        return {
            "fatal": "bq_outage",
            "stop_retry": True,
            "retry_execute": False,
            "retry_dry": False,
            "cache_hit": False,
            "last_error": "The data warehouse is temporarily unavailable.",
        }
    return {
        "last_error": f"BigQuery error: {exc}",
        "retry_execute": False,
        "retry_dry": False,
        "cache_hit": False,
        "fatal": None,
        "stop_retry": False,
    }
