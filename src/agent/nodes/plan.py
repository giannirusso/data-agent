"""Turn a question into 1–N sub-questions. Simple questions skip the planner call."""

from __future__ import annotations

import re

from agent.deps import Deps, status
from agent.llm.parsing import extract_json
from agent.llm.prompts import PLAN_SYSTEM


def needs_planner(text: str, intent: str) -> bool:
    if intent == "report":
        return True
    return bool(re.search(r"\b(why|compare|versus|vs\.?|breakdown|action items)\b", text or "", re.IGNORECASE))


def plan_node(state: dict, deps: Deps) -> dict:
    question = state.get("user_text") or ""
    intent = state.get("intent") or "analysis"
    if not needs_planner(question, intent) or not deps.models.available():
        status(deps, "planning 1 step")
        return {"plan": [{"question": question, "purpose": "question"}], "current_step": 0, "attempts": 0}

    status(deps, "planning")
    trios = state.get("golden_trios") or []
    examples = "\n".join(
        f"- {trio.get('id')}: {trio.get('question')} (metrics: {', '.join(trio.get('metrics') or [])})"
        for trio in trios
    )
    user = f"Question: {question}\n\nAnalyst examples:\n{examples or '(none)'}"
    subquestions = None
    try:
        raw = deps.models.generate(tier="flash", system=PLAN_SYSTEM, user=user)
        data = extract_json(raw)
        if not data:
            raw = deps.models.generate(
                tier="flash",
                system=PLAN_SYSTEM,
                user=user + "\nReturn JSON only.",
            )
            data = extract_json(raw)
        if data:
            subquestions = data.get("subquestions")
    except Exception:
        subquestions = None

    plan = []
    if isinstance(subquestions, list):
        for item in subquestions:
            if isinstance(item, dict) and item.get("question"):
                plan.append({"question": str(item["question"]), "purpose": str(item.get("purpose") or "step")})
            elif isinstance(item, str) and item.strip():
                plan.append({"question": item.strip(), "purpose": "step"})
    if not plan:
        plan = [{"question": question, "purpose": "question"}]
    plan = plan[: deps.settings.max_subquestions]
    status(deps, f"planning {len(plan)} step{'s' if len(plan) != 1 else ''}")
    return {"plan": plan, "current_step": 0, "attempts": 0, "last_error": None, "sql_fingerprint": None}
