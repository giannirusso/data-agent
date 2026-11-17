"""Local Golden Bucket stub.

Trios are question / SQL / report examples written by an analyst. BM25 keeps the
prototype off the embedding API. `GoldenRetriever.search` is the swap point for
the production hybrid retriever.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class GoldenRetriever:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.trios = self._load()
        corpus = [_tokenize(self._document(trio)) for trio in self.trios]
        self._bm25 = BM25Okapi(corpus or [[""]])

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
        return rows

    @staticmethod
    def _document(trio: dict) -> str:
        metrics = " ".join(trio.get("metrics") or [])
        return " ".join(
            [
                trio.get("question") or "",
                trio.get("report_md") or "",
                metrics,
                " ".join(trio.get("tables") or []),
            ]
        )

    def search(self, query: str, k: int = 3) -> list[dict]:
        if not self.trios or not query.strip():
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
        hits = []
        for index, score in ranked[:k]:
            if score <= 0:
                continue
            trio = dict(self.trios[index])
            trio["score"] = float(score)
            hits.append(trio)
        return hits
