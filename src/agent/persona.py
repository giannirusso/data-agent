"""Hot-reloaded persona block. Safety rules do not live in this file."""

from __future__ import annotations

from pathlib import Path

import yaml


class PersonaStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._mtime: float | None = None
        self._data: dict | None = None

    def load(self) -> dict:
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if self._data is None or mtime != self._mtime:
            if self.path.exists():
                self._data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            else:
                self._data = {"version": "0", "tone": "Clear, calm, and concise."}
            self._mtime = mtime
        return self._data

    @property
    def version(self) -> str:
        return str(self.load().get("version", "0"))

    def prompt_block(self) -> str:
        data = self.load()
        lines = [f"{key}: {value}" for key, value in data.items()]
        return "\n".join(lines)
