"""BigQuery executor.

Compatible with the assignment starter ``bq_client.py`` by Alex Freidman.
The constructor signature is unchanged. This prototype adds ``dry_run`` and
``maximum_bytes_billed`` so the agent can reject an expensive query before it
is billed. The client is created lazily: importing or constructing the runner
does not require credentials, and a missing project or key becomes a
``BigQueryConfigError`` at query time rather than a crash at startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import pandas as pd


class BigQueryRunnerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        outage: bool = False,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.outage = outage
        self.code = code


class BigQueryConfigError(BigQueryRunnerError):
    """Missing project or credentials. Not retried as a SQL problem."""


@dataclass
class DryRunResult:
    total_bytes_processed: int
    statement_type: str | None = None
    referenced_tables: list[str] = field(default_factory=list)


class BigQueryRunner:
    def __init__(
        self,
        project_id: Optional[str] = None,
        dataset_id: Optional[str] = "bigquery-public-data.thelook_ecommerce",
    ) -> None:
        self.project_id = project_id or os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        self.dataset_id = dataset_id
        self._client: Any = None

    def query(
        self,
        sql: str,
        *,
        params: dict[str, Any] | None = None,
        maximum_bytes_billed: int | None = None,
        timeout: int | None = 120,
    ) -> pd.DataFrame:
        """Execute SQL and return a DataFrame. `execute` is the same method."""
        return self.execute(
            sql,
            params=params,
            maximum_bytes_billed=maximum_bytes_billed,
            timeout=timeout,
        )

    def run(self, sql: str, **kwargs: Any) -> pd.DataFrame:
        return self.query(sql, **kwargs)

    def execute(
        self,
        sql: str,
        *,
        params: dict[str, Any] | None = None,
        maximum_bytes_billed: int | None = None,
        timeout: int | None = 120,
    ) -> pd.DataFrame:
        from google.cloud import bigquery

        client = self._get_client()
        job_config = bigquery.QueryJobConfig(
            query_parameters=_query_parameters(params),
            use_query_cache=True,
        )
        if maximum_bytes_billed is not None:
            job_config.maximum_bytes_billed = int(maximum_bytes_billed)
        try:
            job = client.query(sql, job_config=job_config)
            result = job.result(timeout=timeout)
        except Exception as exc:
            raise _wrap(exc) from exc
        try:
            return result.to_dataframe(create_bqstorage_client=False)
        except Exception:
            rows = [dict(row.items()) for row in result]
            return pd.DataFrame(rows)

    def dry_run(self, sql: str, *, params: dict[str, Any] | None = None) -> DryRunResult:
        """Validate SQL and estimate bytes. Dry runs are not billed."""
        from google.cloud import bigquery

        client = self._get_client()
        job_config = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
            query_parameters=_query_parameters(params),
        )
        try:
            job = client.query(sql, job_config=job_config)
        except Exception as exc:
            raise _wrap(exc) from exc
        tables = []
        for ref in job.referenced_tables or []:
            tables.append(f"{ref.project}.{ref.dataset_id}.{ref.table_id}")
        return DryRunResult(
            total_bytes_processed=int(job.total_bytes_processed or 0),
            statement_type=job.statement_type,
            referenced_tables=tables,
        )

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self.project_id:
            raise BigQueryConfigError(
                "GCP_PROJECT is not set. BigQuery needs a billing project even for public data."
            )
        try:
            from google.cloud import bigquery
        except ImportError as exc:
            raise BigQueryConfigError("google-cloud-bigquery is not installed.") from exc
        try:
            self._client = bigquery.Client(project=self.project_id, location="US")
        except Exception as exc:
            raise _wrap(exc) from exc
        return self._client


def _query_parameters(params: dict[str, Any] | None):
    from google.cloud import bigquery

    bound = []
    for key, value in (params or {}).items():
        if isinstance(value, list):
            bound.append(bigquery.ArrayQueryParameter(key, "STRING", [str(v) for v in value]))
        elif isinstance(value, bool):
            bound.append(bigquery.ScalarQueryParameter(key, "BOOL", value))
        elif isinstance(value, int) and not isinstance(value, bool):
            bound.append(bigquery.ScalarQueryParameter(key, "INT64", value))
        elif isinstance(value, float):
            bound.append(bigquery.ScalarQueryParameter(key, "FLOAT64", value))
        elif key in {"period_start", "period_end", "previous_start"}:
            bound.append(bigquery.ScalarQueryParameter(key, "TIMESTAMP", _as_timestamp(value)))
        else:
            bound.append(bigquery.ScalarQueryParameter(key, "STRING", "" if value is None else str(value)))
    return bound


def _as_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _wrap(exc: Exception) -> BigQueryRunnerError:
    if isinstance(exc, BigQueryRunnerError):
        return exc
    name = type(exc).__name__
    text = str(exc)
    lowered = text.lower()
    if name in {"DefaultCredentialsError", "RefreshError"} or "could not automatically determine credentials" in lowered:
        return BigQueryConfigError(
            "BigQuery credentials are missing. Run `gcloud auth application-default login` "
            "or set GOOGLE_APPLICATION_CREDENTIALS."
        )
    if name in {"PermissionDenied", "Unauthenticated", "Forbidden"}:
        return BigQueryConfigError(f"BigQuery access denied: {text}")
    transient_names = {
        "ServiceUnavailable",
        "InternalServerError",
        "TooManyRequests",
        "DeadlineExceeded",
        "GatewayTimeout",
        "Aborted",
    }
    if name in transient_names or any(token in lowered for token in ("503", "500", "429", "timeout", "unavailable")):
        return BigQueryRunnerError(text, transient=True, outage=True, code=name)
    if name in {"BadRequest", "NotFound", "InvalidArgument"}:
        return BigQueryRunnerError(text, transient=False, outage=False, code=name)
    return BigQueryRunnerError(text or name, code=name)
