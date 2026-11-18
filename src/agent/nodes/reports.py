"""Non-destructive report operations and the small preference hook."""

from __future__ import annotations

from agent.deps import Deps, status
from agent.safety.safe_schema import catalog_markdown


def describe_schema_node(state: dict, deps: Deps) -> dict:
    status(deps, "describing the schema")
    brands = ", ".join(state.get("entitled_brands") or []) or "no brands"
    text = catalog_markdown() + f"\n\nYour current scope: {brands}."
    return {"response": text, "status": "ok"}


def chitchat_node(state: dict, deps: Deps) -> dict:
    return {
        "response": (
            "I can answer questions about customers, products, and sales for your brands, "
            "explain what data is available, and save a report with insights and action items."
        ),
        "status": "ok",
    }


def preferences_node(state: dict, deps: Deps) -> dict:
    user_id = state.get("user_id") or ""
    text = (state.get("user_text") or "").lower()
    if "forget" in text:
        deps.reports.clear_preferences(user_id)
        return {"response": "I forgot your saved preferences.", "status": "ok"}
    prefs = deps.reports.preferences(user_id)
    if not prefs:
        return {
            "response": "I don't have any saved preferences yet. You can say \"I prefer tables\" or \"keep it short\".",
            "status": "ok",
        }
    lines = ["Here is what I remember about how you like answers:"]
    for key, value in sorted(prefs.items()):
        lines.append(f"- {key}: {value}")
    lines.append("Say \"forget my preferences\" to clear this. I don't store personal data here.")
    return {"response": "\n".join(lines), "status": "ok"}


def report_ops_node(state: dict, deps: Deps) -> dict:
    status(deps, "looking at saved reports")
    user_id = state.get("user_id") or ""
    intent = state.get("intent")
    if intent == "report_restore":
        restored = deps.reports.restore_last(user_id, within_days=deps.settings.restore_days)
        if not restored:
            return {"response": "There is nothing recent to restore.", "status": "ok"}
        listed = ", ".join(f"#{item}" for item in restored)
        return {"response": f"Restored {len(restored)} report(s): {listed}.", "status": "ok"}
    rows = deps.reports.list_reports(user_id)
    if not rows:
        return {"response": "You have no saved reports.", "status": "ok"}
    return {"response": format_report_list(rows), "status": "ok"}


def give_up_node(state: dict, deps: Deps) -> dict:
    fatal = state.get("fatal")
    if fatal == "no_model":
        message = (
            "I can't reach Gemini because GOOGLE_API_KEY is not set. "
            "Add it to .env and ask again. Schema questions and saved reports still work."
        )
    elif fatal == "no_bq":
        message = (
            "BigQuery isn't configured. Set GCP_PROJECT and run "
            "`gcloud auth application-default login`. "
            "I can still describe the schema and manage reports."
        )
    elif fatal == "bq_outage":
        message = "The data warehouse is temporarily unavailable; your question is saved, try `/retry`."
    elif fatal == "model_config":
        message = state.get("last_error") or (
            "The configured model is not available. Set GEMINI_MODEL_PRO and GEMINI_MODEL_FLASH in .env."
        )
    elif fatal == "model_outage":
        message = "The model is busy right now. Try again in a minute with `/retry`."
    elif fatal == "no_scope":
        message = "Your user has no product brands assigned, so I can't run an analysis."
    else:
        reason = state.get("last_error") or "I couldn't produce a trustworthy query."
        message = (
            "I couldn't answer that reliably after several attempts. "
            f"Last issue: {reason} "
            "Try one metric and a clear time range."
        )
    return {"response": message, "status": "give_up"}


def format_report_list(rows: list[dict]) -> str:
    lines = [f"You have {len(rows)} saved report(s):"]
    for row in rows:
        created = str(row.get("created_at") or "")[:16]
        lines.append(f"- #{row['id']} \"{row['title']}\" ({created})")
    return "\n".join(lines)
