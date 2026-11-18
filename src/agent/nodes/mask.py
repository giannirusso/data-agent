"""Last gate before the user (and before the turn summary) sees text."""

from __future__ import annotations

from langchain_core.messages import AIMessage

from agent.deps import Deps, status
from agent.safety.pii_masker import collect_pii_values


def mask_node(state: dict, deps: Deps) -> dict:
    status(deps, "masking")
    raw = state.get("response") or ""
    masked = deps.masker.mask(raw, extra_names=collect_pii_values(state.get("results")))
    deps.logger.span(
        trace_id=state.get("trace_id") or "",
        span="turn",
        duration_ms=0,
        status=state.get("status") or "ok",
        user_id=state.get("user_id"),
        session_id=state.get("session_id"),
        details={
            "intent": state.get("intent"),
            "status": state.get("status") or "ok",
            "pii_hits": masked.hits,
            "queries": len(state.get("results") or []),
            "bytes": state.get("bytes_total") or 0,
            "attempts": state.get("attempts") or 0,
            "persona_version": state.get("persona_version"),
            "golden_ids": [trio.get("id") for trio in state.get("golden_trios") or []],
            "fatal": state.get("fatal"),
            "narrative_skip": state.get("narrative_skip"),
            "error_kind": state.get("error_kind"),
            "golden_template": state.get("golden_template"),
            "last_error": state.get("last_error"),
        },
    )
    return {
        "response": masked.text,
        "pii_hits": masked.hits,
        "messages": [AIMessage(content=masked.text)],
    }
