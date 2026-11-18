"""Write the narrative from result frames only, then optionally save a report."""

from __future__ import annotations

import re

from agent.deps import Deps, status
from agent.llm.prompts import ANALYZE_SYSTEM
from agent.reports.period import parse_period
from agent.reports.templates import template_summary
from agent.safety.pii_masker import collect_pii_values


def analyze_node(state: dict, deps: Deps) -> dict:
    status(deps, "writing the answer")
    persona = deps.persona.load()
    prefs = deps.reports.preferences(state.get("user_id") or "")
    frames = _frames(state)
    narrative, skip = _narrative(state, deps, persona, prefs, frames)
    masked = deps.masker.mask(narrative, extra_names=collect_pii_values(frames)).text
    title = _title(masked, state.get("user_text") or "Analysis")
    update = {
        "response": masked,
        "last_narrative": masked,
        "persona_version": str(persona.get("version", "0")),
        "status": "ok",
        "fatal": None,
    }
    if skip:
        update["narrative_skip"] = skip
    if state.get("results"):
        update["prior_results"] = list(state["results"])
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


def _frames(state: dict) -> list:
    return list(state.get("results") or state.get("prior_results") or [])


def _narrative(state: dict, deps: Deps, persona: dict, prefs: dict, frames: list | None = None) -> tuple[str, str | None]:
    frames = list(frames if frames is not None else _frames(state))
    trios = state.get("golden_trios") or []
    is_report = state.get("intent") == "report" or state.get("save_report")
    if is_report:
        text, skip = _report_narrative(state, deps, persona, prefs, frames, trios)
        return _with_citation(text, trios), skip
    if state.get("golden_template"):
        text, skip = _golden_narrative(state, deps, persona, prefs, frames, trios)
        return _with_citation(text, trios), skip
    if deps.models.available() and frames and _llm_allowed(deps):
        user = _analyze_prompt(state, persona, prefs, frames, trios)
        try:
            text = deps.models.generate(tier="pro", system=ANALYZE_SYSTEM, user=user)
            if text and text.strip():
                return _with_citation(text.strip(), trios), None
        except Exception:
            pass
    return _with_citation(_fallback(state, frames), trios), None


def _golden_narrative(state, deps, persona, prefs, frames, trios) -> tuple[str, str | None]:
    """One analysis call. A failure becomes a short summary of the rows."""
    if not frames:
        return _golden_fallback(state, frames, None), None
    if not deps.models.available() or not _llm_allowed(deps):
        hint = (
            "Narrative skipped: Gemini is not configured"
            if not deps.models.available()
            else "Narrative skipped: no time left in this turn"
        )
        return _golden_fallback(state, frames, hint), hint
    user = _analyze_prompt(state, persona, prefs, frames, trios)
    try:
        text = deps.models.generate(
            tier="pro",
            system=ANALYZE_SYSTEM,
            user=user,
            max_output_tokens=2048,
            max_attempts=1,
        )
    except Exception as exc:
        hint = _skip_hint(exc)
        return _golden_fallback(state, frames, hint), hint
    if not text or not text.strip():
        hint = "Narrative skipped: the model returned no usable narrative"
        return _golden_fallback(state, frames, hint), hint
    return text.strip(), None


def _golden_fallback(state: dict, frames: list, hint: str | None) -> str:
    label = state.get("period_label") or "the requested period"
    lines = [f"Results for {label}."]
    if hint:
        lines.append(hint + ".")
    for frame in frames or []:
        rows = frame.get("rows") or []
        if not rows:
            lines.append("No rows matched these filters.")
            continue
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            bits = []
            for key, value in row.items():
                bits.append(f"{key} {_format_fact(key, value)}")
            if bits:
                lines.append("- " + ", ".join(bits))
    lines.append("Revenue excludes Cancelled and Returned. Customers are shown only as customer_key.")
    return "\n".join(lines)


def _format_fact(key: str, value) -> str:
    if value is None:
        return "n/a"
    name = str(key).lower()
    if name == "return_rate" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value) * 100:.1f}%"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if any(token in name for token in ("revenue", "spend", "margin", "aov", "price", "cost")):
            return f"${float(value):,.2f}"
        if float(value).is_integer():
            return f"{int(value):,}"
    return str(value)


def _report_narrative(state, deps, persona, prefs, frames, trios) -> tuple[str, str | None]:
    """One model call, plus one 429 retry if it still fits the turn budget."""
    period = parse_period(
        state.get("user_text") or "",
        data_through=getattr(deps, "data_through", None),
        source=getattr(deps, "data_through_source", "today"),
    )
    note = state.get("period_note") or period.note
    if not frames:
        return _fallback_report(period, frames, note, None), None
    if not deps.models.available():
        hint = "Narrative skipped: Gemini is not configured"
        return _fallback_report(period, frames, note, hint), hint
    if not _llm_allowed(deps):
        hint = "Narrative skipped: no time left in this turn"
        return _fallback_report(period, frames, note, hint), hint
    user = _analyze_prompt(state, persona, prefs, frames, trios)
    user += (
        f"\n\nThis is a saved quarterly report for {period.label}. "
        "Include insights and numbered action items for the following quarter. "
        f"State this assumption: {note}"
    )
    try:
        text = _one_narrative_call(deps, user)
    except Exception as exc:
        from agent.llm.models import LLMRateLimitError

        if isinstance(exc, LLMRateLimitError) and _retry_fits(deps, exc.retry_after):
            _sleep(exc.retry_after or 1.0)
            try:
                text = _one_narrative_call(deps, user)
            except Exception as second:
                hint = _skip_hint(second)
                return _fallback_report(period, frames, note, hint), hint
        else:
            hint = _skip_hint(exc)
            return _fallback_report(period, frames, note, hint), hint
    if not text or not text.strip():
        hint = "Narrative skipped: the model returned no usable narrative"
        return _fallback_report(period, frames, note, hint), hint
    body = text.strip()
    if note not in body:
        body += "\n\n" + note
    return body, None


def _one_narrative_call(deps: Deps, user: str) -> str:
    return deps.models.generate(
        tier="pro",
        system=ANALYZE_SYSTEM,
        user=user,
        max_output_tokens=2048,
        max_attempts=1,
    )


def _fallback_report(period, frames, note: str, hint: str | None) -> str:
    body = template_summary(period, frames, skip_hint=hint)
    if note and note not in body:
        body = body.replace(period.note, note, 1) if period.note in body else f"{body}\n\n{note}"
    return body


def _retry_fits(deps: Deps, retry_after: float | None) -> bool:
    import time

    wait = 1.0 if retry_after is None else max(0.0, float(retry_after))
    started = getattr(deps, "turn_started", 0) or 0
    budget = getattr(deps.settings, "turn_budget_seconds", 0) or 0
    if not started or not budget:
        return True
    remaining = budget - (time.perf_counter() - float(started))
    return wait <= remaining


def _sleep(seconds: float) -> None:
    import time

    if seconds > 0:
        time.sleep(seconds)


def _skip_hint(exc: Exception) -> str:
    from agent.llm.models import LLMRateLimitError, LLMTimeoutError, _is_rate_limit, _is_timeout

    if isinstance(exc, LLMRateLimitError) or _is_rate_limit(exc):
        return "Narrative skipped: Gemini rate limit (429)"
    if isinstance(exc, LLMTimeoutError) or _is_timeout(exc):
        return "Narrative skipped: Gemini timed out"
    text = str(exc).lower()
    if "404" in text or "not found" in text or "no longer available" in text:
        return "Narrative skipped: model not found (404)"
    if not str(exc).strip():
        return "Narrative skipped: the model returned no usable narrative"
    return "Narrative skipped: the model returned no usable narrative"


def _llm_allowed(deps: Deps) -> bool:
    import time

    started = getattr(deps, "turn_started", 0) or 0
    budget = getattr(deps.settings, "turn_budget_seconds", 0) or 0
    if started and budget and (time.perf_counter() - float(started)) > budget:
        return False
    return True


def _analyze_prompt(state, persona, prefs, frames, trios) -> str:
    import json

    profile = ", ".join(f"{key}={value}" for key, value in prefs.items()) or "none"
    tone = str(persona.get("tone") or "").strip().split("\n")[0][:240]
    example = ""
    if trios:
        example = f"{trios[0].get('id')}: {(trios[0].get('report_md') or '')[:280]}"
    payload = []
    for frame in frames[:4]:
        payload.append(
            {
                "question": frame.get("question"),
                "columns": frame.get("columns"),
                "rows": (frame.get("rows") or [])[:8],
                "row_count": frame.get("row_count"),
                "empty": frame.get("empty"),
            }
        )
    return "\n\n".join(
        [
            f"Tone: {tone}",
            f"Format: {profile}",
            f"Question: {state.get('user_text')}",
            f"Brands: {', '.join(state.get('entitled_brands') or [])}",
            "Style: " + (example or "(none)"),
            "Results:\n" + json.dumps(payload, default=str),
        ]
    )


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
