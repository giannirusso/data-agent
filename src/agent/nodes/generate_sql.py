"""Ask Gemini for one SELECT. The previous error is fed back on retry."""

from __future__ import annotations

import hashlib

from agent.deps import Deps, status
from agent.llm.models import LLMConfigError, LLMFatalError
from agent.llm.parsing import extract_sql
from agent.llm.prompts import SQL_SYSTEM
from agent.safety.safe_schema import sql_prompt_schema


def generate_sql_node(state: dict, deps: Deps) -> dict:
    attempts = int(state.get("attempts") or 0) + 1
    plan = state.get("plan") or []
    step = int(state.get("current_step") or 0)
    if step >= len(plan):
        return {
            "fatal": "plan",
            "stop_retry": True,
            "last_error": "There is no sub-question left to query.",
            "attempts": attempts,
        }
    sub = plan[step]
    status(deps, f"writing query {step + 1}/{len(plan)} (attempt {attempts})")
    if not deps.models.available():
        return {
            "fatal": "no_model",
            "stop_retry": True,
            "last_error": "GOOGLE_API_KEY is not set.",
            "attempts": attempts,
            "retry_execute": False,
        }

    shown = (state.get("golden_trios") or [])[:1]
    trios = "\n\n".join(
        f"Example {trio.get('id')}: {trio.get('question')}\n{(trio.get('sql') or '')[:400]}"
        for trio in shown
    )
    previous_sql = state.get("sql") or ""
    user = "\n\n".join(
        [
            f"Sub-question: {sub.get('question')}",
            f"Original question: {state.get('user_text')}",
            "Schema:\n" + sql_prompt_schema(),
            "Metric notes:\n" + _metrics(deps),
            "Analyst example:\n" + (trios or "(none)"),
            "Previous SQL:\n" + (previous_sql[:500] or "(none)"),
            "Previous error:\n" + (state.get("last_error") or "(none)"),
        ]
    )
    try:
        raw = deps.models.generate(tier="pro", system=SQL_SYSTEM, user=user)
    except LLMConfigError as exc:
        return {
            "fatal": "model_config",
            "stop_retry": True,
            "last_error": str(exc),
            "attempts": attempts,
            "retry_execute": False,
        }
    except LLMFatalError as exc:
        return {
            "fatal": "model_outage",
            "stop_retry": True,
            "last_error": str(exc),
            "attempts": attempts,
            "retry_execute": False,
        }
    sql = extract_sql(raw)
    fingerprint = hashlib.sha256(sql.strip().encode()).hexdigest() if sql else "empty"
    repeated = bool(state.get("last_error")) and fingerprint == state.get("sql_fingerprint")
    return {
        "sql": sql,
        "sql_fingerprint": fingerprint,
        "attempts": attempts,
        "stop_retry": repeated,
        "fatal": None,
        "retry_execute": False,
        "retry_dry": False,
        "cache_hit": False,
        "last_error": None if sql and not repeated else state.get("last_error"),
    }


def _metrics(deps: Deps) -> str:
    lines = []
    for name, spec in (deps.metrics or {}).items():
        if isinstance(spec, dict):
            lines.append(f"- {name}: {spec.get('definition', '')}")
    return "\n".join(lines) or "- revenue excludes Cancelled and Returned"
