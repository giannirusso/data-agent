"""Input guard. Rules always run; the Flash classifier is quota-aware."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent.deps import Deps, status
from agent.llm.parsing import extract_json
from agent.llm.prompts import GUARD_SYSTEM

_INJECTION = re.compile(
    r"(ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above)\s+instructions"
    r"|disregard\s+(your\s+)?(rules|instructions)"
    r"|system\s+prompt"
    r"|you\s+are\s+now"
    r"|\bjailbreak\b"
    r"|do\s+anything\s+now"
    r"|</?(?:system|assistant)>"
    r"|\bdrop\s+(?:table|database)\b"
    r"|\btruncate\s+table\b"
    r"|;\s*delete\s+from\b"
    r"|\bunion\s+select\b)",
    re.IGNORECASE,
)
_PII_NOUN = re.compile(
    r"\b(e-?mails?|phone\s+numbers?|street\s+address(?:es)?|home\s+address(?:es)?"
    r"|mailing\s+address(?:es)?|full\s+names?|first\s+names?|last\s+names?"
    r"|social\s+security|\bssns?\b|credit\s+cards?|passwords?"
    r"|postal\s+codes?|zip\s+codes?)\b",
    re.IGNORECASE,
)
_PII_TARGET = re.compile(r"\b(customers?|users?|clients?|people|buyers?|shoppers?)\b", re.IGNORECASE)
_PII_VERB = re.compile(r"\b(show|list|give|dump|export|share|reveal|send)\b", re.IGNORECASE)
_DML = re.compile(
    r"(\b(drop|truncate|alter|grant|revoke)\b.*\b(table|database|schema|dataset)\b"
    r"|\b(delete\s+from|update|insert\s+into)\b.*\b(orders|users|products|order_items)\b)",
    re.IGNORECASE,
)
_OFF_TOPIC = re.compile(
    r"\b(weather|forecast|poem|joke|recipe|diagnose|stock\s+price|write\s+me\s+(some\s+)?code|python\s+script|election)\b",
    re.IGNORECASE,
)
_ANALYTICS = re.compile(
    r"\b(revenue|order|sale|customer|product|brand|churn|margin|aov|report|schema|table|data|quarter|month|state|category|spend)\b",
    re.IGNORECASE,
)
_SUSPICIOUS = re.compile(r"\b(ignore|jailbreak|password|secret|pretend|act as)\b", re.IGNORECASE)

_MESSAGES = {
    "pii": (
        "I can't share personal details such as emails, phone numbers, or street addresses. "
        "I can show the same analysis by pseudonymous customer key with spend and order count."
    ),
    "injection": (
        "I can only help with retail analytics on your brands. "
        "I won't follow instructions that try to change those rules."
    ),
    "dml": (
        "The analytics database is read-only. I can query it, and I can delete your own saved reports after you confirm."
    ),
    "off_topic": (
        "I answer questions about customers, products, and sales in this dataset. "
        "Try revenue, a comparison, or ask what data is available."
    ),
}


@dataclass
class GuardDecision:
    allowed: bool
    category: str
    reason: str


def rule_guard(text: str) -> GuardDecision:
    raw = text or ""
    if _INJECTION.search(raw):
        return GuardDecision(False, "injection", "injection or jailbreak pattern")
    if _PII_NOUN.search(raw) and (_PII_TARGET.search(raw) or _PII_VERB.search(raw)):
        return GuardDecision(False, "pii", "request for personal data")
    if _DML.search(raw):
        return GuardDecision(False, "dml", "request to modify analytical tables")
    if _OFF_TOPIC.search(raw) and not _ANALYTICS.search(raw):
        return GuardDecision(False, "off_topic", "outside retail analytics")
    return GuardDecision(True, "analytics", "rules passed")


def refusal_text(category: str) -> str:
    return _MESSAGES.get(category, _MESSAGES["off_topic"])


def guard_node(state: dict, deps: Deps) -> dict:
    status(deps, "checking the question")
    text = state.get("user_text") or ""
    decision = rule_guard(text)
    if decision.allowed and _should_classify(deps, text):
        decision = _classify(deps, text, decision)
    if not decision.allowed:
        return {
            "refusal": True,
            "intent": "refused",
            "response": refusal_text(decision.category),
            "status": "refused",
            "save_report": False,
        }
    return {"refusal": False, "status": "ok"}


def _should_classify(deps: Deps, text: str) -> bool:
    mode = (deps.settings.llm_guard or "suspicious").lower()
    if mode == "off" or not deps.models.available():
        return False
    if mode == "always":
        return True
    return _SUSPICIOUS.search(text) is not None


def _classify(deps: Deps, text: str, fallback: GuardDecision) -> GuardDecision:
    try:
        raw = deps.models.generate(tier="flash", system=GUARD_SYSTEM, user=text)
    except Exception:
        return fallback
    data = extract_json(raw)
    if not data or "allowed" not in data:
        return fallback
    if data.get("allowed") is False:
        category = str(data.get("category") or "off_topic")
        if category not in _MESSAGES:
            category = "off_topic"
        return GuardDecision(False, category, str(data.get("reason") or "classifier"))
    return fallback
