"""Shared fixtures. Nothing in this suite calls Gemini or BigQuery."""

from __future__ import annotations

import pandas as pd
import pytest

from agent.data.bq_client import DryRunResult
from agent.runtime import build_runtime
from config.settings import Settings


@pytest.fixture(autouse=True)
def _no_live_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        google_api_key="",
        gcp_project="",
        pii_salt="test-salt",
        bq_max_bytes=1_000_000_000,
        log_path=str(tmp_path / "agent.jsonl"),
        sqlite_path=str(tmp_path / "agent.sqlite"),
        checkpoint_path=str(tmp_path / "checkpoints.sqlite"),
        delete_ttl_seconds=600,
        max_sql_attempts=3,
        llm_guard="off",
        llm_route="heuristic",
    )


class FakeBQ:
    def __init__(self, rows=None, scanned=1000, over_budget=False):
        self.rows = [{"revenue": 100, "state": "Texas"}] if rows is None else rows
        self.scanned = scanned
        self.over_budget = over_budget
        self.executed = []
        self.dry_runs = []
        self.execute_count = 0

    def dry_run(self, sql, params=None):
        self.dry_runs.append({"sql": sql, "params": params})
        scanned = 10**12 if self.over_budget else self.scanned
        return DryRunResult(total_bytes_processed=scanned)

    def execute(self, sql, params=None, maximum_bytes_billed=None, timeout=None):
        self.execute_count += 1
        self.executed.append(
            {
                "sql": sql,
                "params": params,
                "maximum_bytes_billed": maximum_bytes_billed,
                "timeout": timeout,
            }
        )
        rows = self.rows(sql) if callable(self.rows) else self.rows
        return pd.DataFrame(rows)


def make_runtime(settings, *, user="alice", session_id="sess-test", bq=None, complete=None, **kwargs):
    return build_runtime(
        user=user,
        session_id=session_id,
        settings=settings,
        bq=bq if bq is not None else FakeBQ(),
        complete=complete,
        retry_min=0,
        **kwargs,
    )
