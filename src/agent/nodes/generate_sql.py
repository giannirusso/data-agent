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

    parts = [
        f"Sub-question: {sub.get('question')}",
        f"Original question: {state.get('user_text')}",
        "Schema:\n" + sql_prompt_schema(),
        "Metrics:\n" + _metrics(deps),
    ]
    shown = (state.get("golden_trios") or [])[:1]
    if shown:
        trio = shown[0]
        parts.append(
            "Analyst example "
            f"{trio.get('id')}: {trio.get('question')}\n{(trio.get('sql') or '')[:240]}"
        )
    previous_error = state.get("last_error") or ""
    if previous_error:
        parts.append("Previous SQL:\n" + (state.get("sql") or "")[:500])
        parts.append("Previous error:\n" + previous_error)
    user = "\n\n".join(parts)
    try:
        # SQL is the first model call and the slow one on the free tier.
        # "minimal" is below the default "low" used for the short analysis call.
        raw = deps.models.generate(
            tier="pro",
            system=SQL_SYSTEM,
            user=user,
            thinking_level="minimal",
            max_output_tokens=512,
        )
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
        "prompt_chars": len(SQL_SYSTEM) + len(user),
    }


def _metrics(deps: Deps) -> str:
    lines = []
    for name, spec in (deps.metrics or {}).items():
        if isinstance(spec, dict):
            lines.append(f"- {name}: {spec.get('definition', '')}")
    return "\n".join(lines) or "- revenue excludes Cancelled and Returned"
