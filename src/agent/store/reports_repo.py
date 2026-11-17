"""Saved reports, soft delete, and the audit log.

Every read and write is scoped by owner_user_id. Deletion updates deleted_at and
writes the audit row in one transaction. The purge of expired rows is a production
job; the prototype only soft-deletes and restores.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime | None = None) -> str:
    return (moment or utcnow()).isoformat()


class ReportsRepo:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reports (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              owner_user_id TEXT NOT NULL,
              session_id TEXT,
              title TEXT NOT NULL,
              body_md TEXT NOT NULL,
              tags TEXT,
              created_at TEXT NOT NULL,
              deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              actor_user_id TEXT NOT NULL,
              action TEXT NOT NULL,
              report_ids TEXT NOT NULL,
              request_text TEXT,
              trace_id TEXT,
              ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_preferences (
              user_id TEXT NOT NULL,
              key TEXT NOT NULL,
              value TEXT NOT NULL,
              source TEXT NOT NULL,
              confidence REAL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (user_id, key)
            );
            """
        )
        self.conn.commit()

    def save(
        self,
        owner_user_id: str,
        session_id: str,
        title: str,
        body_md: str,
        tags: str = "",
    ) -> int:
        now = iso()
        cur = self.conn.execute(
            """
            INSERT INTO reports (owner_user_id, session_id, title, body_md, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (owner_user_id, session_id, title, body_md, tags, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get(self, owner_user_id: str, report_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM reports WHERE id = ? AND owner_user_id = ?",
            (report_id, owner_user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_reports(
        self,
        owner_user_id: str,
        *,
        session_id: str | None = None,
        mention: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict]:
        sql = "SELECT * FROM reports WHERE owner_user_id = ?"
        args: list[object] = [owner_user_id]
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        if session_id is not None:
            sql += " AND session_id = ?"
            args.append(session_id)
        sql += " ORDER BY created_at ASC, id ASC"
        rows = [dict(row) for row in self.conn.execute(sql, args).fetchall()]
        if mention:
            needle = mention.lower()
            rows = [row for row in rows if needle in f"{row['title']}\n{row['body_md']}".lower()]
        return rows

    def soft_delete(
        self,
        owner_user_id: str,
        report_ids: list[int],
        *,
        request_text: str,
        trace_id: str,
    ) -> list[int]:
        """Soft-delete exactly these owned ids and write one audit row. Others are ignored."""
        now = iso()
        deleted: list[int] = []
        with self.conn:
            for report_id in report_ids:
                cur = self.conn.execute(
                    """
                    UPDATE reports
                    SET deleted_at = ?
                    WHERE id = ? AND owner_user_id = ? AND deleted_at IS NULL
                    """,
                    (now, int(report_id), owner_user_id),
                )
                if cur.rowcount:
                    deleted.append(int(report_id))
            if deleted:
                self.conn.execute(
                    """
                    INSERT INTO audit_log (actor_user_id, action, report_ids, request_text, trace_id, ts)
                    VALUES (?, 'delete', ?, ?, ?, ?)
                    """,
                    (owner_user_id, json.dumps(deleted), request_text, trace_id, now),
                )
        return deleted

    def restore_last(self, owner_user_id: str, *, within_days: int = 30) -> list[int]:
        row = self.conn.execute(
            """
            SELECT report_ids, ts FROM audit_log
            WHERE actor_user_id = ? AND action = 'delete'
            ORDER BY id DESC LIMIT 1
            """,
            (owner_user_id,),
        ).fetchone()
        if row is None:
            return []
        deleted_at = datetime.fromisoformat(row["ts"])
        if deleted_at.tzinfo is None:
            deleted_at = deleted_at.replace(tzinfo=timezone.utc)
        if utcnow() - deleted_at > timedelta(days=within_days):
            return []
        return self.restore_ids(owner_user_id, json.loads(row["report_ids"]))

    def restore_ids(self, owner_user_id: str, report_ids: list[int]) -> list[int]:
        now = iso()
        restored: list[int] = []
        with self.conn:
            for report_id in report_ids:
                cur = self.conn.execute(
                    """
                    UPDATE reports
                    SET deleted_at = NULL
                    WHERE id = ? AND owner_user_id = ? AND deleted_at IS NOT NULL
                    """,
                    (int(report_id), owner_user_id),
                )
                if cur.rowcount:
                    restored.append(int(report_id))
            if restored:
                self.conn.execute(
                    """
                    INSERT INTO audit_log (actor_user_id, action, report_ids, request_text, trace_id, ts)
                    VALUES (?, 'restore', ?, '', '', ?)
                    """,
                    (owner_user_id, json.dumps(restored), now),
                )
        return restored

    def audit_rows(self, owner_user_id: str | None = None) -> list[dict]:
        if owner_user_id is None:
            rows = self.conn.execute("SELECT * FROM audit_log ORDER BY id ASC").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_log WHERE actor_user_id = ? ORDER BY id ASC",
                (owner_user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_preference(
        self,
        user_id: str,
        key: str,
        value: str,
        *,
        source: str = "explicit",
        confidence: float = 1.0,
    ) -> None:
        allowed = {"format", "depth", "charts", "default_period"}
        if key not in allowed:
            return
        now = iso()
        self.conn.execute(
            """
            INSERT INTO user_preferences (user_id, key, value, source, confidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET
              value = excluded.value,
              source = excluded.source,
              confidence = excluded.confidence,
              updated_at = excluded.updated_at
            """,
            (user_id, key, value, source, confidence, now),
        )
        self.conn.commit()

    def preferences(self, user_id: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT key, value FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def clear_preferences(self, user_id: str) -> None:
        self.conn.execute("DELETE FROM user_preferences WHERE user_id = ?", (user_id,))
        self.conn.commit()
