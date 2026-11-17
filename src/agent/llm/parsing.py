"""Extract JSON and SQL from model text without trusting the prose around them."""

from __future__ import annotations

import json
import re


def extract_json(text: str) -> dict | None:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def extract_sql(text: str) -> str:
    if not text:
        return ""
    fenced = re.search(r"```sql\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    fenced = re.search(r"```\s*(.*?)```", text, re.DOTALL)
    if fenced and "select" in fenced.group(1).lower():
        return fenced.group(1).strip()
    data = extract_json(text)
    if data and data.get("sql"):
        return str(data["sql"]).strip()
    return text.strip()
