"""Small in-process LRU for successful query frames.

The key includes the rewritten SQL, the brand parameters, and the salt, so two
users never share a result. Production would use Redis; this keeps the same idea
without another service.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any


class QueryCache:
    def __init__(self, maxsize: int = 128) -> None:
        self.maxsize = maxsize
        self._data: OrderedDict[str, Any] = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    @staticmethod
    def make_key(sql: str, params: dict[str, Any] | None, user_id: str) -> str:
        payload = json.dumps(
            {"sql": sql, "params": params or {}, "user_id": user_id},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, key: str):
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)
