"""Plan a turn without calling the model.

Reports use a fixed quarterly template. Every other question is one step.
A free-tier planner call was taking about three minutes, which blew the turn.
"""

from __future__ import annotations

from agent.deps import Deps, status
from agent.reports.period import parse_period, resolve_data_through
from agent.reports.templates import report_steps


def plan_node(state: dict, deps: Deps) -> dict:
    question = state.get("user_text") or ""
    intent = state.get("intent") or "analysis"
    if intent == "report":
        data_through, source = resolve_data_through(
            deps,
            brands=list(state.get("entitled_brands") or []),
            salt=deps.settings.pii_salt,
        )
        status(deps, f"orders run through {data_through.isoformat()} ({source})")
        period = parse_period(question, data_through=data_through, source=source)
        plan = report_steps(period)
        status(deps, f"planning {len(plan)} template steps for {period.label}")
        return {
            "plan": plan,
            "current_step": 0,
            "attempts": 0,
            "last_error": None,
            "sql_fingerprint": None,
            "template_sql": True,
            "period_label": period.label,
            "period_note": period.note,
            "save_report": True,
        }
    status(deps, "planning 1 step")
    return {
        "plan": [{"question": question, "purpose": "question"}],
        "current_step": 0,
        "attempts": 0,
        "last_error": None,
        "sql_fingerprint": None,
        "template_sql": False,
    }
