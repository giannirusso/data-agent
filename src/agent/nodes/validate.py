"""Run the sqlglot gate. Brand values are attached later, as query parameters."""

from __future__ import annotations

from agent.deps import Deps, status
from agent.safety.sql_validator import validate_and_rewrite


def validate_node(state: dict, deps: Deps) -> dict:
    status(deps, "validating SQL")
    if not state.get("entitled_brands"):
        return {
            "fatal": "no_scope",
            "stop_retry": True,
            "last_error": "This user has no product scope configured.",
            "rewritten_sql": None,
        }
    result = validate_and_rewrite(state.get("sql") or "", max_rows=deps.settings.max_rows)
    if not result.ok:
        return {
            "last_error": result.error,
            "rewritten_sql": None,
            "stop_retry": False,
            "retry_execute": False,
        }
    return {
        "rewritten_sql": result.sql,
        "last_error": None,
        "stop_retry": False,
        "query_params": {
            "entitled_brands": list(state.get("entitled_brands") or []),
            "pii_salt": deps.settings.pii_salt,
            **dict(state.get("step_params") or {}),
        },
    }
