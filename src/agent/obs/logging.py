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
            "level": "info" if status == "ok" else "warning",
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


def summarize_session(rows: list[dict]) -> dict:
    traces = {row.get("trace_id") for row in rows if row.get("trace_id")}
    turns = [row for row in rows if row.get("span") == "turn"]
    return {
        "events": len(rows),
        "traces": len(traces),
        "turns": len(turns),
        "errors": sum(1 for row in rows if row.get("status") not in {"ok", None}),
        "pii_hits": sum(int((row.get("details") or {}).get("pii_hits") or 0) for row in rows),
        "guard_blocks": sum(1 for row in rows if row.get("span") == "guard" and row.get("status") == "blocked"),
        "give_ups": sum(1 for row in turns if (row.get("details") or {}).get("status") == "give_up"),
        "deletes": sum(1 for row in rows if row.get("span") == "confirm_delete" and row.get("status") == "deleted"),
        "cancelled_deletes": sum(
            1 for row in rows if row.get("span") == "confirm_delete" and row.get("status") == "cancelled"
        ),
    }
