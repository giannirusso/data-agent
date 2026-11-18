"""Ask Gemini for one SELECT. The previous error is fed back on retry."""

from __future__ import annotations

import hashlib

from agent.deps import Deps, status
from agent.llm.models import (
    LLMConfigError,
    LLMFatalError,
    LLMRateLimitError,
    LLMTimeoutError,
    timeout_message,
)
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
    if sub.get("template") and sub.get("sql"):
        return _template_step(state, sub, attempts)
    status(deps, f"writing query {step + 1}/{len(plan)} (attempt {attempts})")
    if _turn_expired(deps) and state.get("results"):
        return {"partial": True, "stop_retry": True, "fatal": None, "last_error": None, "attempts": attempts}
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
        # Thinking level comes from GEMINI_THINKING_LEVEL (default low).
        # gemini-3.8-flash rejects "minimal". 2048 leaves room for thinking tokens plus SQL.
        raw = deps.models.generate(
            tier="pro",
            system=SQL_SYSTEM,
            user=user,
            max_output_tokens=2048,
        )
    except LLMRateLimitError:
        return _model_failure(attempts, "429", "model_outage", "429 rate limit. The model is busy right now.")
    except LLMTimeoutError:
        return _model_failure(
            attempts,
            "timeout",
            "model_timeout",
            timeout_message(deps.settings.gemini_timeout_seconds),
        )
    except LLMConfigError as exc:
        return _model_failure(attempts, "other", "model_config", str(exc))
    except LLMFatalError as exc:
        text = str(exc)
        kind = "429" if "429" in text or "rate limit" in text.lower() else "other"
        return _model_failure(attempts, kind, "model_outage", text or "The model is busy right now.")
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


def _model_failure(attempts: int, kind: str, fatal: str, message: str) -> dict:
    return {
        "fatal": fatal,
        "stop_retry": True,
        "last_error": message,
        "error_kind": kind,
        "attempts": attempts,
        "retry_execute": False,
    }


def _template_step(state: dict, sub: dict, attempts: int) -> dict:
    """Use the pre-written statement. A second pass with the same error gives up."""
    sql = str(sub.get("sql") or "").strip()
    template_id = sub.get("golden_template") or state.get("golden_template")
    if state.get("last_error"):
        return {
            "template_sql": True,
            "golden_template": template_id,
            "stop_retry": True,
            "fatal": None,
            "last_error": state.get("last_error"),
            "error_kind": "other",
            "attempts": attempts,
            "sql": sql,
        }
    fingerprint = hashlib.sha256(sql.encode()).hexdigest()
    return {
        "sql": sql,
        "template_sql": True,
        "golden_template": template_id,
        "step_params": dict(sub.get("params") or {}),
        "attempts": attempts,
        "stop_retry": False,
        "fatal": None,
        "last_error": None,
        "sql_fingerprint": fingerprint,
        "retry_execute": False,
        "retry_dry": False,
        "cache_hit": False,
    }


def _turn_expired(deps: Deps) -> bool:
    started = getattr(deps, "turn_started", 0) or 0
    budget = getattr(deps.settings, "turn_budget_seconds", 0) or 0
    if not started or not budget:
        return False
    import time

    return (time.perf_counter() - started) > budget


def _metrics(deps: Deps) -> str:
    lines = []
    for name, spec in (deps.metrics or {}).items():
        if isinstance(spec, dict):
            lines.append(f"- {name}: {spec.get('definition', '')}")
    return "\n".join(lines) or "- revenue excludes Cancelled and Returned"
