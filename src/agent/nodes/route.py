"""Intent routing. Destructive intents come only from these rules, not from the model."""

from __future__ import annotations

import re

from agent.deps import Deps, status
from agent.llm.parsing import extract_json
from agent.llm.prompts import ROUTE_SYSTEM

_PREF_PATTERNS = (
    (re.compile(r"prefer tables|use tables|as a table", re.IGNORECASE), "format", "table"),
    (re.compile(r"prefer bullets|bullet points", re.IGNORECASE), "format", "bullets"),
    (re.compile(r"always shorter|keep it short|be brief", re.IGNORECASE), "depth", "summary"),
    (re.compile(r"more detail|deep dive|go deeper", re.IGNORECASE), "depth", "deep"),
)


def heuristic_intent(text: str, *, has_results: bool) -> str:
    raw = (text or "").strip()
    lowered = raw.lower()
    if re.search(r"\b(forget (my )?preferences|what do you remember)\b", lowered):
        return "preferences"
    if re.search(r"\brestore\b", lowered) and re.search(r"\b(reports?|deleted|undo)\b", lowered):
        return "report_restore"
    if re.search(r"\b(delete|remove|erase)\b", lowered) and re.search(
        r"\b(reports?|saved|conversation)\b", lowered
    ):
        return "report_delete"
    if re.search(r"\b(list|show)\b.*\breports?\b|\bmy reports\b", lowered):
        return "report_list"
    if has_results and re.search(r"\bsave (this|that|it|a report|the report)\b", lowered):
        return "save_last"
    if re.search(
        r"\b(schema|what data|which tables|what tables|available data|data dictionary|what do we have)\b",
        lowered,
    ):
        return "schema"
    if re.search(
        r"\b(create|write|make|build|prepare)\b.*\breports?\b|\breports?\b.*\b(insight|action)\b",
        lowered,
    ):
        return "report"
    if lowered in {"hi", "hello", "hey", "thanks", "thank you", "help"} or lowered.startswith("what can you"):
        return "chitchat"
    if has_results and re.match(
        r"^(and |also |explain|why is that|summarize|tell me more|so what|what does that mean)",
        lowered,
    ):
        return "follow_up"
    return "analysis"


def explicit_preferences(text: str) -> dict[str, str]:
    found = {}
    for pattern, key, value in _PREF_PATTERNS:
        if pattern.search(text or ""):
            found[key] = value
    return found


def unauthorized_brands(text: str, entitled: list[str], known: list[str]) -> list[str]:
    lowered = (text or "").lower()
    entitled_l = {brand.lower() for brand in entitled}
    mentioned = [brand for brand in known if brand.lower() in lowered]
    if not mentioned:
        return []
    allowed = [brand for brand in mentioned if brand.lower() in entitled_l]
    denied = [brand for brand in mentioned if brand.lower() not in entitled_l]
    if denied and not allowed:
        return denied
    return []


def route_node(state: dict, deps: Deps) -> dict:
    status(deps, "routing")
    text = state.get("user_text") or ""
    has_results = bool(state.get("results"))
    intent = heuristic_intent(text, has_results=has_results)
    if (
        intent == "analysis"
        and deps.settings.llm_route == "always"
        and deps.models.available()
    ):
        intent = _llm_intent(deps, text, has_results) or intent
    if intent == "follow_up" and not has_results:
        intent = "analysis"

    for key, value in explicit_preferences(text).items():
        deps.reports.upsert_preference(state.get("user_id") or "", key, value)

    update: dict = {
        "intent": intent,
        "save_report": intent == "report",
        "scope_note": "",
        "refusal": False,
    }
    if intent in {"analysis", "report", "schema"}:
        update["results"] = []
        update["current_step"] = 0
        update["attempts"] = 0
        update["bytes_total"] = 0
        update["plan"] = []

    if intent in {"analysis", "report"}:
        denied = unauthorized_brands(text, state.get("entitled_brands") or [], deps.known_brands)
        if denied:
            brands = ", ".join(state.get("entitled_brands") or []) or "nothing"
            update.update(
                {
                    "intent": "scope_denied",
                    "save_report": False,
                    "response": (
                        f"You don't have access to {', '.join(denied)}. "
                        f"Your scope is {brands}."
                    ),
                    "status": "refused",
                }
            )
    return update


def _llm_intent(deps: Deps, text: str, has_results: bool) -> str | None:
    try:
        raw = deps.models.generate(
            tier="flash",
            system=ROUTE_SYSTEM,
            user=f"has_previous_result={has_results}\n{text}",
        )
    except Exception:
        return None
    data = extract_json(raw)
    if not data:
        return None
    intent = str(data.get("intent") or "")
    if intent in {"schema", "analysis", "report", "follow_up", "chitchat"}:
        return intent
    return None
