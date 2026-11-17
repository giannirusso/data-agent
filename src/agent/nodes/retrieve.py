"""Golden Bucket lookup. A failure here degrades to zero-shot; it does not stop the turn."""

from __future__ import annotations

from agent.deps import Deps, status


def retrieve_node(state: dict, deps: Deps) -> dict:
    status(deps, "retrieving analyst patterns")
    try:
        trios = deps.retriever.search(state.get("user_text") or "", k=3)
    except Exception:
        trios = []
    # Drop the bulky fields the checkpoint does not need twice; keep the prompt fields.
    slim = [
        {
            "id": trio.get("id"),
            "question": trio.get("question"),
            "sql": trio.get("sql"),
            "report_md": trio.get("report_md"),
            "metrics": trio.get("metrics") or [],
            "score": trio.get("score"),
        }
        for trio in trios
    ]
    return {"golden_trios": slim}
