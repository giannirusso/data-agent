"""Build a ready-to-chat runtime: settings, stores, graph, and checkpointer."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.data.bq_client import BigQueryRunner
from agent.data.cache import QueryCache
from agent.deps import Deps
from agent.golden.retriever import GoldenRetriever
from agent.graph import build_graph
from agent.llm.models import ModelProvider
from agent.obs.logging import AgentLogger
from agent.obs.tracing import langfuse_callbacks
from agent.persona import PersonaStore
from agent.safety.pii_masker import PiiMasker
from agent.store.reports_repo import ReportsRepo
from config.settings import Settings, load_settings


@dataclass
class Runtime:
    settings: Settings
    deps: Deps
    graph: Any
    user_id: str
    display_name: str
    role: str
    brands: list[str]
    session_id: str
    verbose: bool = False
    callbacks: list = field(default_factory=list)
    last_question: str = ""
    last_narrative: str = ""
    trace_id: str = ""
    _checkpoint_conn: Any = None

    def config(self) -> dict:
        cfg: dict[str, Any] = {
            "configurable": {"thread_id": self.session_id},
            "recursion_limit": 150,
        }
        if self.callbacks:
            cfg["callbacks"] = self.callbacks
        return cfg


def build_runtime(
    *,
    user: str = "alice",
    session_id: str | None = None,
    settings: Settings | None = None,
    bq=None,
    complete: Callable | None = None,
    verbose: bool = False,
    model: str | None = None,
    retry_min: float = 0.05,
    clock: Callable[[], float] | None = None,
) -> Runtime:
    settings = settings or load_settings()
    if model:
        settings = settings.model_copy(update={"gemini_model_pro": model})
    users = _load_users(settings.entitlements_path)
    if user not in users:
        known = ", ".join(sorted(users)) or "(none)"
        raise KeyError(f"Unknown user '{user}'. Known demo users: {known}")
    record = users[user]
    brands = list(record.get("brands") or [])
    known_brands = sorted({brand for item in users.values() for brand in (item.get("brands") or [])})
    masker = PiiMasker(protected_bigrams={brand.lower() for brand in known_brands if " " in brand})
    logger = AgentLogger(settings.log_path, masker)
    reports = ReportsRepo(settings.sqlite_path)
    models = ModelProvider(settings, complete=complete, retry_min=retry_min)
    deps = Deps(
        settings=settings,
        bq=bq
        if bq is not None
        else BigQueryRunner(project_id=settings.gcp_project or None, dataset_id=settings.bq_dataset),
        reports=reports,
        retriever=GoldenRetriever(settings.golden_path),
        models=models,
        persona=PersonaStore(settings.persona_path),
        metrics=_load_yaml(settings.metrics_path),
        known_brands=known_brands,
        masker=masker,
        logger=logger,
        cache=QueryCache(),
    )
    if clock is not None:
        deps.clock = clock
    Path(settings.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
    graph = build_graph(deps, SqliteSaver(conn))
    return Runtime(
        settings=settings,
        deps=deps,
        graph=graph,
        user_id=user,
        display_name=str(record.get("display_name") or user),
        role=str(record.get("role") or "executive"),
        brands=brands,
        session_id=session_id or uuid.uuid4().hex,
        verbose=verbose,
        callbacks=langfuse_callbacks(settings),
        _checkpoint_conn=conn,
    )


def _load_users(path: str) -> dict:
    file = Path(path)
    if not file.exists():
        return {}
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return data.get("users") or {}


def _load_yaml(path: str) -> dict:
    file = Path(path)
    if not file.exists():
        return {}
    return yaml.safe_load(file.read_text(encoding="utf-8")) or {}
