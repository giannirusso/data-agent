"""Optional LangFuse callback. Missing keys or a down service never block a turn."""

from __future__ import annotations

import os

from config.settings import Settings


def langfuse_callbacks(settings: Settings) -> list:
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return []
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler(public_key=settings.langfuse_public_key)]
    except Exception:
        return []
