"""Write the narrative from result frames only, then optionally save a report."""

from __future__ import annotations

import re

from agent.deps import Deps, status
from agent.llm.prompts import ANALYZE_SYSTEM


def analyze_node(state: dict, deps: Deps) -> dict:
    status(deps, "writing the answer")
    persona = deps.persona.load()
    prefs = deps.reports.preferences(state.get("user_id") or "")
    narrative = _narrative(state, deps, persona, prefs)
    masked = deps.masker.mask(narrative).text
    title = _title(masked, state.get("user_text") or "Analysis")
    update = {
        "response": masked,
        "last_narrative": masked,
        "persona_version": str(persona.get("version", "0")),
        "status": "ok",
        "fatal": None,
    }
    if state.get("save_report") or state.get("intent") == "report":
        report_id = deps.reports.save(
            state.get("user_id") or "",
            state.get("session_id") or "",
            title,
            masked,
            tags=state.get("intent") or "",
        )
        update["response"] = masked + f"\n\nSaved as report #{report_id} \"{title}\"."
        update["last_narrative"] = update["response"]
    return update


def save_last_node(state: dict, deps: Deps) -> dict:
    body = state.get("last_narrative") or ""
    if not body:
        return {"response": "There's no analysis to save yet. Ask a question first.", "status": "ok"}
    masked = deps.masker.mask(body).text
    title = _title(masked, "Saved analysis")
    report_id = deps.reports.save(
        state.get("user_id") or "",
        state.get("session_id") or "",
        title,
        masked,
    )
    return {"response": f"Saved as report #{report_id} \"{title}\".", "status": "ok"}


def _narrative(state: dict, deps: Deps, persona: dict, prefs: dict) -> str:
    frames = state.get("results") or []
    trios = state.get("golden_trios") or []
    if deps.models.available() and frames:
        user = _analyze_prompt(state, persona, prefs, frames, trios)
        try:
            text = deps.models.generate(tier="pro", system=ANALYZE_SYSTEM, user=user)
            if text and text.strip():
                return _with_citation(text.strip(), trios)
        except Exception:
            pass
    return _with_citation(_fallback(state, frames), trios)


def _analyze_prompt(state, persona, prefs, frames, trios) -> str:
    import json

    profile = ", ".join(f"{key}={value}" for key, value in prefs.items()) or "none"
    examples = "\n".join(f"{trio.get('id')}: {trio.get('report_md')}" for trio in trios)
    payload = []
    for frame in frames:
        payload.append(
            {
                "question": frame.get("question"),
                "columns": frame.get("columns"),
                "rows": (frame.get("rows") or [])[:20],
                "row_count": frame.get("row_count"),
                "empty": frame.get("empty"),
            }
        )
    return "\n\n".join(
        [
            f"Persona:\n{deps_persona(persona)}",
            f"User format profile: {profile}",
            f"Question: {state.get('user_text')}",
            f"Scope note: {state.get('scope_note') or 'results are limited to the user brands'}",
            f"Brands: {', '.join(state.get('entitled_brands') or [])}",
            "Analyst style notes:\n" + (examples or "(none)"),
            "Result frames:\n" + json.dumps(payload, default=str),
        ]
    )


def deps_persona(persona: dict) -> str:
    return "\n".join(f"{key}: {value}" for key, value in persona.items())


def _fallback(state: dict, frames: list[dict]) -> str:
    if not frames:
        return "I don't have a result to discuss yet. Ask a question about revenue, products, or customers."
    lines = ["Here is what the data shows for your brands."]
    if state.get("scope_note"):
        lines.append(state["scope_note"])
    for frame in frames:
        count = frame.get("row_count")
        question = frame.get("question") or "Result"
        if frame.get("empty") or count == 0:
            lines.append(f"{question}: no rows matched these filters.")
            continue
        lines.append(f"{question}: {count} row(s).")
    lines.append(
        "Period: only the filters that were asked for were applied. "
        "I did not invent a date range."
    )
    if state.get("intent") == "report" or state.get("save_report"):
        lines.append("Action items: 1) Review the table with the commercial team. 2) Ask a follow-up on the largest gap.")
        lines.append("TITLE: Analysis report")
    return "\n".join(lines)


def _with_citation(text: str, trios: list[dict]) -> str:
    if not trios:
        return text
    ids = ", ".join(str(trio.get("id")) for trio in trios if trio.get("id"))
    if not ids or ids in text:
        return text
    return text.rstrip() + f"\n\nApproach based on analyst report {ids}."


def _title(text: str, fallback: str) -> str:
    match = re.search(r"^TITLE:\s*(.+)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip().strip('"')[:120]
    compact = " ".join(fallback.split())
    return (compact[:80] or "Analysis").strip()
