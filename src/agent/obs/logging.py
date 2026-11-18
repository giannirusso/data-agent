"""JSON-lines trace log. Every payload is masked before it is written."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent.safety.pii_masker import PiiMasker


class AgentLogger:
    def __init__(self, path: str, masker: PiiMasker) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.masker = masker
        self._lock = threading.Lock()

    def span(
        self,
        *,
        trace_id: str,
        span: str,
        duration_ms: float,
        status: str,
        user_id: str | None = None,
        session_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": "info" if status in _INFO_STATUSES else "warning",
            "trace_id": trace_id,
            "span": span,
            "duration_ms": round(float(duration_ms), 2),
            "status": status,
            "user_id": user_id,
            "session_id": session_id,
            "details": details or {},
        }
        line = json.dumps(self.masker.mask_obj(event), default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def read_trace(self, trace_id: str) -> list[dict]:
        return [row for row in self._read_all() if row.get("trace_id") == trace_id]

    def session_rows(self, session_id: str) -> list[dict]:
        return [row for row in self._read_all() if row.get("session_id") == session_id]

    def all_rows(self) -> list[dict]:
        return self._read_all()

    def _read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


# Confirmations, refusals, and pauses are outcomes, not failures.
_INFO_STATUSES = {"ok", "blocked", "cancelled", "deleted", "waiting", "interrupted", None}
_FAILURE_SPANS = {"generate_sql", "validate_sql", "dry_run", "execute"}


def summarize_session(rows: list[dict]) -> dict:
    """Rates over one set of log rows (a session, or the whole file)."""
    turns = [row for row in rows if row.get("span") == "turn"]
    turn_count = len(turns)
    traces = {row.get("trace_id") for row in rows if row.get("trace_id")}
    guard_blocks = sum(1 for row in rows if row.get("span") == "guard" and row.get("status") == "blocked")
    give_ups = sum(1 for row in turns if _is_give_up(row))
    failure_events = sum(1 for row in rows if row.get("span") in _FAILURE_SPANS and row.get("status") == "error")
    retried = _retried_turns(rows)
    corrected = sum(1 for trace_id in retried if not _trace_gave_up(rows, trace_id))
    pii_hits = sum(int((row.get("details") or {}).get("pii_hits") or 0) for row in turns)
    bytes_scanned = sum(int((row.get("details") or {}).get("bytes") or 0) for row in turns)
    latencies = _trace_latencies(rows)
    return {
        "turns": turn_count,
        "traces": len(traces),
        "errors": failure_events + give_ups,
        "guard_blocks": guard_blocks,
        "guard_block_rate": _rate(guard_blocks, turn_count),
        "self_corrections": corrected,
        "self_correction_attempts": len(retried),
        "self_correction_rate": _rate(corrected, len(retried)),
        "give_ups": give_ups,
        "give_up_rate": _rate(give_ups, turn_count),
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "bytes_scanned": bytes_scanned,
        "pii_hits": pii_hits,
        "deletes": sum(1 for row in rows if row.get("span") == "confirm_delete" and row.get("status") == "deleted"),
        "cancelled_deletes": sum(
            1 for row in rows if row.get("span") == "confirm_delete" and row.get("status") == "cancelled"
        ),
    }


def format_stats(session: dict, overall: dict) -> str:
    return "\n\n".join(
        [
            "This session\n" + _block(session),
            "All time (from logs/agent.jsonl)\n" + _block(overall),
        ]
    )


def _block(summary: dict) -> str:
    lines = [
        f"- turns: {summary['turns']}",
        f"- errors: {summary['errors']}",
        f"- guard block rate: {summary['guard_block_rate']}",
        f"- self-correction rate: {summary['self_correction_rate']}",
        f"- give-up rate: {summary['give_up_rate']}",
        f"- latency p50: {_latency(summary['latency_p50_ms'])}",
        f"- latency p95: {_latency(summary['latency_p95_ms'])}",
        f"- bytes scanned: {summary['bytes_scanned']}",
        f"- pii hits: {summary['pii_hits']}",
        f"- deletes: {summary['deletes']}",
        f"- cancelled deletes: {summary['cancelled_deletes']}",
    ]
    return "\n".join(lines)


def _is_give_up(row: dict) -> bool:
    if row.get("status") == "give_up":
        return True
    return (row.get("details") or {}).get("status") == "give_up"


def _retried_turns(rows: list[dict]) -> set[str]:
    retried = set()
    for row in rows:
        if row.get("span") not in _FAILURE_SPANS and row.get("span") != "generate_sql":
            continue
        trace_id = row.get("trace_id")
        if not trace_id:
            continue
        attempts = int((row.get("details") or {}).get("attempts") or 0)
        if row.get("status") == "error" or attempts > 1:
            retried.add(trace_id)
    return retried


def _trace_gave_up(rows: list[dict], trace_id: str) -> bool:
    return any(row.get("trace_id") == trace_id and row.get("span") == "turn" and _is_give_up(row) for row in rows)


def _trace_latencies(rows: list[dict]) -> list[float]:
    totals: dict[str, float] = {}
    for row in rows:
        if row.get("span") == "turn":
            continue
        trace_id = row.get("trace_id")
        if not trace_id:
            continue
        totals[trace_id] = totals.get(trace_id, 0.0) + float(row.get("duration_ms") or 0)
    return list(totals.values())


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * fraction
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _rate(numerator: int, denominator: int) -> str:
    if not denominator:
        return "n/a"
    return f"{100.0 * numerator / denominator:.1f}% ({numerator}/{denominator})"


def _latency(millis: float | None) -> str:
    if millis is None:
        return "n/a"
    if millis >= 1000:
        return f"{millis / 1000:.1f}s"
    return f"{millis:.0f}ms"
