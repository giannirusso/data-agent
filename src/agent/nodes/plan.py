"""Plan a turn without calling the model.

Reports use a fixed quarterly template. Every other question is one step.
A free-tier planner call was taking about three minutes, which blew the turn.
"""

from __future__ import annotations

from datetime import date

from agent.deps import Deps, status
from agent.golden.templates import classify, match_template, needs_data_window
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
            "golden_template": None,
            "period_label": period.label,
            "period_note": period.note,
            "save_report": True,
        }
    filled = _golden(question, state, deps)
    if filled is not None:
        status(deps, f"sql: golden template {filled.trio_id}")
        return {
            "plan": [
                {
                    "question": question,
                    "purpose": filled.kind,
                    "template": True,
                    "sql": filled.sql,
                    "params": filled.params,
                    "golden_template": filled.trio_id,
                }
            ],
            "current_step": 0,
            "attempts": 0,
            "last_error": None,
            "sql_fingerprint": None,
            "template_sql": True,
            "golden_template": filled.trio_id,
            "period_label": filled.period_label,
        }
    status(deps, "planning 1 step")
    return {
        "plan": [{"question": question, "purpose": "question"}],
        "current_step": 0,
        "attempts": 0,
        "last_error": None,
        "sql_fingerprint": None,
        "template_sql": False,
        "golden_template": None,
    }


def _golden(question: str, state: dict, deps: Deps):
    trios = state.get("golden_trios") or []
    if not trios or classify(question) is None:
        return None
    today = _as_of(question, state, deps)
    return match_template(question, trios, today=today)


def _as_of(question: str, state: dict, deps: Deps) -> date:
    cached = getattr(deps, "data_through", None)
    if isinstance(cached, date):
        return cached
    if not needs_data_window(question):
        return date.today()
    found, _source = resolve_data_through(
        deps,
        brands=list(state.get("entitled_brands") or []),
        salt=deps.settings.pii_salt,
    )
    return found
