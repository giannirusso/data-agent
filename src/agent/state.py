"""LangGraph state. Entitlements are copied in by the CLI, never by the model."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    user_id: str
    role: str
    entitled_brands: list[str]
    session_id: str
    messages: Annotated[list[Any], add_messages]
    user_text: str
    intent: str
    golden_trios: list[dict]
    plan: list[dict]
    current_step: int
    sql: str | None
    rewritten_sql: str | None
    attempts: int
    last_error: str | None
    sql_fingerprint: str | None
    stop_retry: bool
    fatal: str | None
    results: list[dict]
    prior_results: list[dict]
    pending_frame: dict | None
    pending_delete: dict | None
    trace_id: str
    response: str
    refusal: bool
    save_report: bool
    empty_retried: bool
    suspicious_retried: bool
    retry_execute: bool
    retry_dry: bool
    cache_hit: bool
    transient_tries: int
    status: str
    last_bytes: int
    bytes_total: int
    pii_hits: int
    last_narrative: str
    scope_note: str
    persona_version: str
    query_params: dict
    step_params: dict
    template_sql: bool
    partial: bool
    period_label: str
    period_note: str
    narrative_skip: str
    golden_template: str | None
    error_kind: str | None
