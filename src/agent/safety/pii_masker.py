"""Final-text PII scrubber.

Emails, phones, card numbers, and street addresses are removed with regex.
Person names are removed only when they match a value that was actually present
(a first_name/last_name cell, or an explicit known-name list) or when the text
labels them ("customer Jane Doe"). Capitalized product titles and categories
are left alone. The same function is applied to logs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[\s\-.]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]\d{4}(?!\w)"
)
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CARD_RE = re.compile(r"\b(?:\d{4}[\s\-]){3}\d{4}\b")
STREET_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z0-9.'\-]+\s+){0,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl)\b\.?",
    re.IGNORECASE,
)
ZIP_RE = re.compile(r"\b(?:zip|postal(?:\s*code)?)\s*[:#]?\s*\d{5}(?:-\d{4})?\b", re.IGNORECASE)
COORD_RE = re.compile(r"\b-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b")
# Only a name that follows an explicit label. "Sport Coats" and product titles do not.
LABELED_NAME_RE = re.compile(
    r"(?i:\b(?:customer(?:\s+name)?|contact|full\s+name|name|mr|mrs|ms)\.?\s+(?:is\s+|:\s*)?)"
    r"([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20})?)\b"
)

_NOT_A_NAME = frozenset(
    """
    count key keys revenue sales sale total average order orders product products
    category categories brand brands report reports insight insights data table
    tables item items customer customers number numbers id ids status state
    department departments cost price margin profit aov amount
    """.split()
)

_PII_COLUMNS = frozenset(
    {
        "first_name",
        "firstname",
        "last_name",
        "lastname",
        "email",
        "street_address",
        "streetaddress",
    }
)


@dataclass
class MaskResult:
    text: str
    hits: int


class PiiMasker:
    def __init__(self, known_names: set[str] | None = None, protected_bigrams: set[str] | None = None) -> None:
        # protected_bigrams is accepted so older call sites keep working. Names are
        # no longer guessed from capitalization, so brands do not need a stoplist.
        del protected_bigrams
        self.known_names = _clean_names(known_names or set())

    def mask(self, text: str, extra_names: set[str] | None = None) -> MaskResult:
        if not text:
            return MaskResult(text or "", 0)
        hits = 0
        updated, n = _sub(EMAIL_RE, text, "[EMAIL]")
        hits += n
        updated, n = _sub(PHONE_RE, updated, "[PHONE]")
        hits += n
        updated, n = _sub(SSN_RE, updated, "[SSN]")
        hits += n
        updated, n = _sub(CARD_RE, updated, "[CARD]")
        hits += n
        updated, n = _sub(STREET_RE, updated, "[ADDRESS]")
        hits += n
        updated, n = _sub(ZIP_RE, updated, "[POSTAL]")
        hits += n
        updated, n = _sub(COORD_RE, updated, "[COORD]")
        hits += n
        updated, n = _sub(LABELED_NAME_RE, updated, _replace_labeled)
        hits += n
        names = self.known_names | _clean_names(extra_names or set())
        updated, n = _mask_known_names(updated, names)
        hits += n
        return MaskResult(updated, hits)

    def mask_obj(self, value, extra_names: set[str] | None = None):
        if isinstance(value, str):
            return self.mask(value, extra_names=extra_names).text
        if isinstance(value, dict):
            return {key: self.mask_obj(item, extra_names=extra_names) for key, item in value.items()}
        if isinstance(value, list):
            return [self.mask_obj(item, extra_names=extra_names) for item in value]
        if isinstance(value, tuple):
            return [self.mask_obj(item, extra_names=extra_names) for item in value]
        return value


def collect_pii_values(frames: list | None) -> set[str]:
    """Exact strings from PII columns, if a frame ever contains them."""
    found: set[str] = set()
    for frame in frames or []:
        rows = frame.get("rows") if isinstance(frame, dict) else None
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            first = last = None
            for key, value in row.items():
                if value is None or value == "":
                    continue
                lowered = str(key).lower()
                text = str(value).strip()
                if lowered in _PII_COLUMNS and text:
                    found.add(text)
                if lowered in {"first_name", "firstname"}:
                    first = text
                if lowered in {"last_name", "lastname"}:
                    last = text
            if first and last:
                found.add(f"{first} {last}")
    return _clean_names(found)


def _clean_names(names: set[str]) -> set[str]:
    cleaned = set()
    for name in names:
        text = str(name).strip()
        if len(text) < 2:
            continue
        if text.lower() in _NOT_A_NAME:
            continue
        cleaned.add(text)
    return cleaned


def _replace_labeled(match: re.Match[str]) -> str:
    name = match.group(1)
    if any(part.lower() in _NOT_A_NAME for part in name.split()):
        return match.group(0)
    return match.group(0).replace(name, "[NAME]")


def _mask_known_names(text: str, names: set[str]) -> tuple[str, int]:
    hits = 0
    for name in sorted(names, key=len, reverse=True):
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)
        text, count = _sub(pattern, text, "[NAME]")
        hits += count
    return text, hits


def _sub(pattern: re.Pattern[str], text: str, repl) -> tuple[str, int]:
    hits = 0

    def wrap(match: re.Match[str]) -> str:
        nonlocal hits
        hits += 1
        if callable(repl):
            return repl(match)
        return repl

    return pattern.sub(wrap, text), hits
