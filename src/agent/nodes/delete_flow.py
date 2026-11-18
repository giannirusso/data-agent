"""High-stakes delete. The model never chooses the ids that get deleted.

Candidates are loaded with owner_user_id = the authenticated user. The graph
pauses on interrupt() with that exact list. Resume deletes only the stored ids
whose digest still matches, inside one transaction with an audit row.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from langgraph.types import interrupt

from agent.deps import Deps, status


def parse_delete_request(text: str) -> dict:
    session_only = bool(re.search(r"this conversation|this session", text or "", re.IGNORECASE))
    mention = None
    match = re.search(r"mentioning\s+[\"']([^\"']+)[\"']", text or "", re.IGNORECASE)
    if not match:
        match = re.search(r"mentioning\s+(.+?)(?:\?|$)", text or "", re.IGNORECASE)
    if not match:
        match = re.search(r"\babout\s+[\"']([^\"']+)[\"']", text or "", re.IGNORECASE)
    if match:
        mention = match.group(1).strip().strip(" .")
    return {"session_only": session_only, "mention": mention}


def intersect_candidate_ids(candidates: list[int], proposed: list[int]) -> list[int]:
    """Keep only ids that were already in the owner-scoped candidate set."""
    allowed = set(candidates)
    chosen: list[int] = []
    for item in proposed:
        if item in allowed and item not in chosen:
            chosen.append(item)
    return chosen


def digest_ids(user_id: str, report_ids: list[int]) -> str:
    raw = f"{user_id}|{','.join(str(item) for item in report_ids)}".encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass
class DeleteDecision:
    action: str
    ids: list[int]
    message: str


def resolve_delete_answer(pending: dict, answer: str, *, now: float) -> DeleteDecision:
    ids = [int(item) for item in pending.get("ids") or []]
    if not ids:
        return DeleteDecision("cancel", [], "No matching reports. Nothing was deleted.")
    if now > float(pending.get("expires_at") or 0):
        return DeleteDecision("cancel", [], "Deletion cancelled (confirmation expired). Nothing was deleted.")
    if digest_ids(pending.get("user_id") or "", ids) != pending.get("digest"):
        return DeleteDecision(
            "cancel",
            [],
            "Deletion cancelled (confirmation did not match the listed reports). Nothing was deleted.",
        )
    text = (answer or "").strip().lower()
    threshold = int(pending.get("large_threshold") or 20)
    if text in {"no", "n", "cancel", "stop", "nope"}:
        return DeleteDecision("cancel", [], "Deletion cancelled. Nothing was deleted.")
    if len(ids) > threshold:
        match = re.fullmatch(r"delete\s+(\d+)", text)
        if match and int(match.group(1)) == len(ids):
            return DeleteDecision("delete", ids, "confirmed")
        return DeleteDecision("cancel", [], "Deletion cancelled. Nothing was deleted.")
    if text in {"yes", "y", "all", "ok", "confirm"}:
        return DeleteDecision("delete", ids, "confirmed")
    if re.fullmatch(r"[0-9,\s]+", text or ""):
        numbers = [int(item) for item in re.findall(r"\d+", text)]
        if not numbers or any(number < 1 or number > len(ids) for number in numbers):
            return DeleteDecision("cancel", [], "Deletion cancelled. Nothing was deleted.")
        chosen: list[int] = []
        for number in numbers:
            report_id = ids[number - 1]
            if report_id not in chosen:
                chosen.append(report_id)
        return DeleteDecision("delete", chosen, "confirmed")
    return DeleteDecision("cancel", [], "Deletion cancelled. Nothing was deleted.")


def format_confirmation(rows: list[dict], *, session_scoped: bool, large_threshold: int) -> str:
    noun = "report" if len(rows) == 1 else "reports"
    scope = " from this conversation" if session_scoped else ""
    lines = [f"Found {len(rows)} {noun}{scope}:"]
    for index, row in enumerate(rows, start=1):
        created = str(row.get("created_at") or "")[:16]
        snippet = " ".join(str(row.get("body_md") or "").split())[:80]
        lines.append(f"  {index}) \"{row.get('title')}\" ({created}) — {snippet}")
    if len(rows) > large_threshold:
        lines.append(f'This will delete {len(rows)} reports. Type "delete {len(rows)}" to confirm, or "no" to cancel.')
    else:
        lines.append("Delete them? [yes / numbers e.g. 1,3 / no]")
    return "\n".join(lines)


def find_delete_node(state: dict, deps: Deps) -> dict:
    status(deps, "finding your reports")
    parsed = parse_delete_request(state.get("user_text") or "")
    user_id = state.get("user_id") or ""
    rows = deps.reports.list_reports(
        user_id,
        session_id=state.get("session_id") if parsed["session_only"] else None,
        mention=parsed["mention"],
    )
    if not rows:
        return {
            "pending_delete": None,
            "response": "No matching reports.",
            "status": "ok",
            "results": [],
            "bytes_total": 0,
            "pending_frame": None,
        }
    ids = [int(row["id"]) for row in rows]
    prompt = format_confirmation(
        rows,
        session_scoped=parsed["session_only"],
        large_threshold=deps.settings.large_delete_threshold,
    )
    prompt = deps.masker.mask(prompt).text
    pending = {
        "ids": ids,
        "user_id": user_id,
        "digest": digest_ids(user_id, ids),
        "expires_at": deps.clock() + deps.settings.delete_ttl_seconds,
        "prompt": prompt,
        "request_text": state.get("user_text") or "",
        "large_threshold": deps.settings.large_delete_threshold,
        "session_scoped": parsed["session_only"],
    }
    return {"pending_delete": pending, "response": "", **_clear_turn_display()}


def confirm_delete_node(state: dict, deps: Deps) -> dict:
    pending = state.get("pending_delete") or None
    if not pending:
        return {"response": "No matching reports.", "status": "ok", "pending_delete": None, **_clear_turn_display()}
    answer = interrupt(
        {
            "type": "confirm_delete",
            "prompt": pending["prompt"],
            "ids": pending["ids"],
            "count": len(pending["ids"]),
        }
    )
    decision = resolve_delete_answer(pending, str(answer), now=deps.clock())
    if decision.action != "delete":
        return {
            "response": decision.message,
            "status": "cancelled",
            "pending_delete": None,
            **_clear_turn_display(),
        }
    deleted = deps.reports.soft_delete(
        state.get("user_id") or "",
        decision.ids,
        request_text=pending.get("request_text") or "",
        trace_id=state.get("trace_id") or "",
    )
    if not deleted:
        return {
            "response": "Nothing was deleted. Those reports were no longer active.",
            "status": "cancelled",
            "pending_delete": None,
            **_clear_turn_display(),
        }
    noun = "report" if len(deleted) == 1 else "reports"
    return {
        "response": (
            f"Deleted {len(deleted)} {noun}. "
            f"Type /restore within {deps.settings.restore_days} days to undo."
        ),
        "status": "deleted",
        "pending_delete": None,
        **_clear_turn_display(),
    }


def _clear_turn_display() -> dict:
    return {
        "results": [],
        "bytes_total": 0,
        "pending_frame": None,
        "sql": None,
        "rewritten_sql": None,
        "last_error": None,
        "fatal": None,
    }
