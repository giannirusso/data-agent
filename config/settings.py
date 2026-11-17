"""Runtime settings. Environment variables and an optional .env file override defaults."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def project_root() -> Path:
    env = os.environ.get("AGENT_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1]


_ROOT = project_root()


class Settings(BaseSettings):
    """Configuration for the retail analytics agent.

    Model ids, the BigQuery byte budget, and the PII salt are all environment-driven
    so a reviewer can point the prototype at a Google AI Studio key without code changes.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    google_api_key: str = ""
    gcp_project: str = ""
    gemini_model_pro: str = "gemini-2.5-pro"
    gemini_model_flash: str = "gemini-2.5-flash"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    pii_salt: str = "dev-only-change-me"
    bq_max_bytes: int = 1_000_000_000
    bq_dataset: str = "bigquery-public-data.thelook_ecommerce"
    bq_timeout_seconds: int = 120
    max_sql_attempts: int = 3
    max_rows: int = 1000
    max_subquestions: int = 6
    delete_ttl_seconds: int = 600
    restore_days: int = 30
    large_delete_threshold: int = 20
    llm_guard: str = "suspicious"
    llm_route: str = "heuristic"
    log_path: str = str(_ROOT / "logs" / "agent.jsonl")
    sqlite_path: str = str(_ROOT / "var" / "agent.sqlite")
    checkpoint_path: str = str(_ROOT / "var" / "checkpoints.sqlite")
    persona_path: str = str(_ROOT / "config" / "persona.yaml")
    entitlements_path: str = str(_ROOT / "config" / "entitlements.yaml")
    metrics_path: str = str(_ROOT / "config" / "metrics.yaml")
    golden_path: str = str(_ROOT / "golden_bucket" / "trios.jsonl")


def load_settings() -> Settings:
    env_file = project_root() / ".env"
    if env_file.exists():
        return Settings(_env_file=env_file)
    return Settings(_env_file=None)
