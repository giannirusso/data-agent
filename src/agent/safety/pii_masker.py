"""Final-text PII scrubber.

Regex covers emails, phone numbers, card numbers, street addresses, and labelled
person-name patterns. A conservative Capitalized Name pass skips brands, places,
and business words. Presidio is used when it is installed; otherwise this regex
layer is the whole masker. The same function is applied to logs.
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
LABELED_NAME_RE = re.compile(
    r"\b(?:customer(?:\s+name)?|contact|name|mr|mrs|ms)\.?\s*(?:is|:)?\s*"
    r"([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20})?)\b"
)
NAME_RE = re.compile(r"\b[A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20})+\b")

# Tokens that are not person names in this domain. Compared in lowercase.
_STOP = frozenset(
    """
    action items order orders revenue average brand brands total customer customers
    key keys traffic source search organic email campaign product products category
    department quarter month week year sales sale report reports insight insights
    united states new york jersey mexico north south east west carolina dakota
    virginia rhode island hong kong los angeles costa rica sri lanka saudi arabia
    czech republic puerto rico south korea united kingdom district columbia
    big query average order customer key traffic source action item calvin klein
    allegra tommy hilfiger texas california florida ohio georgia illinois
    """.split()
)

_PROTECTED_BIGRAMS = frozenset(
    {
        "calvin klein",
        "allegra k",
        "tommy hilfiger",
        "new york",
        "new jersey",
        "new mexico",
        "north carolina",
        "south carolina",
        "north dakota",
        "south dakota",
        "west virginia",
        "rhode island",
        "hong kong",
        "los angeles",
        "costa rica",
        "sri lanka",
        "saudi arabia",
        "czech republic",
        "puerto rico",
        "south korea",
        "united kingdom",
        "united states",
        "action items",
        "order frequency",
        "average order",
        "customer key",
        "traffic source",
        "district columbia",
    }
)


@dataclass
class MaskResult:
    text: str
    hits: int


class PiiMasker:
    def __init__(self, protected_bigrams: set[str] | None = None) -> None:
        extra = {b.lower() for b in (protected_bigrams or set())}
        self.protected = _PROTECTED_BIGRAMS | extra
        self._presidio = None
        self._presidio_tried = False

    def mask(self, text: str) -> MaskResult:
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

        def labeled(match: re.Match[str]) -> str:
            return match.group(0).replace(match.group(1), "[NAME]")

        updated, n = _sub(LABELED_NAME_RE, updated, labeled)
        hits += n
        updated, n = self._mask_names(updated)
        hits += n
        updated, n = self._mask_presidio(updated)
        hits += n
        return MaskResult(updated, hits)

    def mask_obj(self, value):
        if isinstance(value, str):
            return self.mask(value).text
        if isinstance(value, dict):
            return {k: self.mask_obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.mask_obj(v) for v in value]
        if isinstance(value, tuple):
            return [self.mask_obj(v) for v in value]
        return value

    def _mask_names(self, text: str) -> tuple[str, int]:
        hits = 0

        def repl(match: re.Match[str]) -> str:
            nonlocal hits
            words = match.group(0).split()
            index = 0
            while index < len(words) and words[index].lower() in _STOP:
                index += 1
            tail = words[index:]
            if len(tail) < 2:
                return match.group(0)
            for first, second in zip(tail, tail[1:]):
                bigram = f"{first} {second}".lower()
                if bigram in self.protected or second.lower() in _STOP:
                    return match.group(0)
            hits += 1
            prefix = " ".join(words[:index])
            masked = "[NAME]"
            return f"{prefix} {masked}".strip()

        return NAME_RE.sub(repl, text), hits

    def _mask_presidio(self, text: str) -> tuple[str, int]:
        engine = self._engine()
        if engine is None:
            return text, 0
        try:
            results = engine.analyze(
                text=text,
                entities=["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "LOCATION"],
                language="en",
            )
        except Exception:
            return text, 0
        if not results:
            return text, 0
        hits = 0
        chars = list(text)
        for result in sorted(results, key=lambda item: item.start, reverse=True):
            label = {
                "PERSON": "[NAME]",
                "EMAIL_ADDRESS": "[EMAIL]",
                "PHONE_NUMBER": "[PHONE]",
                "LOCATION": "[ADDRESS]",
            }.get(result.entity_type, "[REDACTED]")
            chars[result.start : result.end] = list(label)
            hits += 1
        return "".join(chars), hits

    def _engine(self):
        if self._presidio_tried:
            return self._presidio
        self._presidio_tried = True
        try:
            from presidio_analyzer import AnalyzerEngine

            self._presidio = AnalyzerEngine()
        except Exception:
            self._presidio = None
        return self._presidio


def _sub(pattern: re.Pattern[str], text: str, repl) -> tuple[str, int]:
    hits = 0

    def wrap(match: re.Match[str]) -> str:
        nonlocal hits
        hits += 1
        if callable(repl):
            return repl(match)
        return repl

    return pattern.sub(wrap, text), hits
