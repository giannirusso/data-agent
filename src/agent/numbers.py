"""Display rounding for money. Counts and ids stay exact."""

from __future__ import annotations

_MONEY = ("revenue", "margin", "profit", "sale", "price", "cost", "aov", "amount", "spend", "gmv")


def is_money(column: str) -> bool:
    name = str(column).lower()
    return any(token in name for token in _MONEY)


def round_value(column: str, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if not is_money(column):
        return value
    return round(float(value), 2)


def format_cell(column: str, value) -> str:
    if value is None:
        return ""
    if is_money(column) and isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.2f}"
    return str(value).replace("|", "\\|").replace("\n", " ")
