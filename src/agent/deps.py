"""Dependencies closed over by graph nodes. Tests replace the model and BigQuery."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from agent.data.cache import QueryCache
from agent.golden.retriever import GoldenRetriever
from agent.llm.models import ModelProvider
from agent.obs.logging import AgentLogger
from agent.persona import PersonaStore
from agent.safety.pii_masker import PiiMasker
from agent.store.reports_repo import ReportsRepo
from config.settings import Settings


@dataclass
class Deps:
    settings: Settings
    bq: object
    reports: ReportsRepo
    retriever: GoldenRetriever
    models: ModelProvider
    persona: PersonaStore
    metrics: dict
    known_brands: list[str]
    masker: PiiMasker
    logger: AgentLogger
    cache: QueryCache
    clock: Callable[[], float] = time.time
    on_status: Callable[[str], None] | None = None


def status(deps: Deps, message: str) -> None:
    if deps.on_status:
        deps.on_status(message)
