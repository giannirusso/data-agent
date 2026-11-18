"""Fill a known analyst pattern without calling the model.

A retrieved trio is used as SQL only when the question is the same kind of
template and the wording overlaps. N, the period, and product names are filled
here. Novel questions stay on the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from agent.reports.period import (
    _bounds,
    _iso,
    _latest_complete,
    _latest_year_for_quarter,
    _quarter,
    _year,
)

_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "by",
        "in",
        "on",
        "for",
        "to",
        "and",
        "or",
        "our",
        "were",
        "was",
        "what",
        "which",
        "who",
        "how",
        "with",
        "using",
        "one",
        "another",
        "last",
        "this",
        "their",
        "is",
        "are",
        "did",
        "do",
        "does",
        "from",
        "that",
        "those",
        "these",
        "into",
        "over",
        "than",
        "not",
        "no",
        "please",
        "show",
        "me",
        "us",
    }
)

# Shared tokens of the shorter side. 0.45 keeps "top products by revenue"
# aligned with the analyst wording and rejects a vague overlap.
_MIN_SIMILARITY = 0.45

_OPEN = "status NOT IN ('Cancelled', 'Returned')"
_ITEM_OPEN = "oi.status NOT IN ('Cancelled', 'Returned')"


@dataclass(frozen=True)
class GoldenSql:
    trio_id: str
    kind: str
    sql: str
    params: dict
    period_label: str
    similarity: float


def match_template(question: str, trios: list[dict], *, today: date) -> GoldenSql | None:
    kind = classify(question)
    if not kind:
        return None
    best: tuple[float, dict] | None = None
    for trio in trios or []:
        if classify(str(trio.get("question") or "")) != kind:
            continue
        score = similarity(question, str(trio.get("question") or ""))
        if best is None or score > best[0]:
            best = (score, trio)
    if best is None or best[0] < _MIN_SIMILARITY:
        return None
    score, trio = best
    window = extract_window(question, today)
    limit = _limit(question)
    names = _product_names(question) if kind == "compare_products" else None
    if kind == "compare_products" and not names:
        return None
    sql, params = _sql(kind, window=window, limit=limit, names=names)
    label = window[2] if window else "all available history"
    return GoldenSql(
        trio_id=str(trio.get("id") or ""),
        kind=kind,
        sql=sql,
        params=params,
        period_label=label,
        similarity=score,
    )


def classify(text: str) -> str | None:
    """Return a template kind, or None when the question should use the model."""
    raw = text or ""
    q = raw.lower()
    if re.search(r"\bwhy\b", q):
        return None
    if re.search(r"\b(email|e-mail|phone|address|first name|last name)\b", q):
        return None
    if _product_names(raw):
        return "compare_products"
    if "return rate" in q:
        return "return_rate"
    if (
        re.search(r"\bcustomers?\b", q)
        and re.search(r"\b(spend|spent|spending)\b", q)
        and re.search(r"\b(top|most|highest)\b", q)
    ):
        return "top_customers"
    if "category" in q and "revenue" in q and "state" not in q and not re.search(r"\bproducts?\b", q):
        return "revenue_by_category"
    if re.search(r"\b(monthly|by month|each month|per month)\b", q) and "revenue" in q:
        return "monthly_revenue"
    if re.search(r"\b(top|best|most|highest)\b", q) and re.search(r"\bproducts?\b", q) and "revenue" in q:
        return "top_products"
    return None


def similarity(left: str, right: str) -> float:
    a = set(_tokens(left))
    b = set(_tokens(right))
    if not a or not b or len(a & b) < 2:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def needs_data_window(question: str) -> bool:
    """Relative periods need the latest order date. A named year does not."""
    q = (question or "").lower()
    if _year(question) and not re.search(r"\blast\s+(quarter|month|year)\b", q):
        return False
    return extract_window(question, date.today()) is not None or bool(
        re.search(r"\blast\s+(quarter|month|year)\b|\bprevious\s+(quarter|month|year)\b", q)
    )


def extract_window(text: str, today: date) -> tuple[datetime, datetime, str] | None:
    raw = text or ""
    lowered = raw.lower()
    if re.search(r"\b(last|previous|past)\s+quarter\b", lowered):
        quarter, year = _latest_complete(today)
        start, end = _bounds(year, quarter)
        return start, end, f"Q{quarter} {year}"
    quarter = _quarter(raw)
    year = _year(raw)
    if quarter is not None:
        if year is None:
            year = _latest_year_for_quarter(quarter, today)
        start, end = _bounds(year, quarter)
        return start, end, f"Q{quarter} {year}"
    if re.search(r"\b(last|previous)\s+month\b", lowered):
        first = today.replace(day=1)
        end = datetime(first.year, first.month, 1, tzinfo=timezone.utc)
        previous = first - timedelta(days=1)
        start = datetime(previous.year, previous.month, 1, tzinfo=timezone.utc)
        return start, end, f"{start:%B %Y}"
    if re.search(r"\b(last|previous)\s+year\b", lowered):
        year = today.year - 1
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        return start, end, str(year)
    if year is not None:
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        return start, end, str(year)
    return None


def _sql(
    kind: str,
    *,
    window: tuple[datetime, datetime, str] | None,
    limit: int,
    names: tuple[str, str] | None,
) -> tuple[str, dict]:
    params: dict = {}
    date_sql = ""
    item_date = ""
    if window is not None:
        start, end, _label = window
        params["period_start"] = _iso(start)
        params["period_end"] = _iso(end)
        date_sql = "AND created_at >= @period_start AND created_at < @period_end"
        item_date = "AND oi.created_at >= @period_start AND oi.created_at < @period_end"
    if kind == "top_products":
        sql = f"""
        SELECT p.name, p.category, ROUND(SUM(oi.sale_price), 2) AS revenue,
               COUNT(DISTINCT oi.order_id) AS orders
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE {_ITEM_OPEN}
          {item_date}
        GROUP BY p.name, p.category
        ORDER BY revenue DESC
        LIMIT {limit}
        """
    elif kind == "monthly_revenue":
        sql = f"""
        SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) AS month,
               ROUND(SUM(sale_price), 2) AS revenue,
               COUNT(DISTINCT order_id) AS orders
        FROM order_items
        WHERE {_OPEN}
          {date_sql}
        GROUP BY month
        ORDER BY month
        """
    elif kind == "revenue_by_category":
        sql = f"""
        SELECT p.category, ROUND(SUM(oi.sale_price), 2) AS revenue,
               COUNT(*) AS items
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE {_ITEM_OPEN}
          {item_date}
        GROUP BY p.category
        ORDER BY revenue DESC
        """
    elif kind == "top_customers":
        sql = f"""
        SELECT customer_key, ROUND(SUM(sale_price), 2) AS spend,
               COUNT(DISTINCT order_id) AS orders
        FROM order_items
        WHERE {_OPEN}
          {date_sql}
        GROUP BY customer_key
        ORDER BY spend DESC
        LIMIT {limit}
        """
    elif kind == "return_rate":
        sql = f"""
        SELECT p.category,
               ROUND(COUNTIF(oi.status = 'Returned') / NULLIF(COUNT(*), 0), 4) AS return_rate,
               COUNT(*) AS items
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE 1 = 1
          {item_date}
        GROUP BY p.category
        ORDER BY return_rate DESC
        """
    else:
        params["product_names"] = [names[0], names[1]] if names else []
        sql = f"""
        SELECT p.name, ROUND(SUM(oi.sale_price), 2) AS revenue,
               COUNT(DISTINCT oi.order_id) AS orders
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE {_ITEM_OPEN}
          AND p.name IN UNNEST(@product_names)
          {item_date}
        GROUP BY p.name
        ORDER BY revenue DESC
        """
    return _compact(sql), params


def _limit(text: str) -> int:
    match = re.search(r"\btop\s+(\d+)\b", text or "", re.IGNORECASE)
    if not match:
        match = re.search(r"\b(\d+)\s+(?:products|customers)\b", text or "", re.IGNORECASE)
    if not match:
        return 10
    return max(1, min(50, int(match.group(1))))


def _product_names(text: str) -> tuple[str, str] | None:
    cleaned = re.sub(
        r"\b(last|this|previous|past)\s+(quarter|month|year)\b",
        " ",
        text or "",
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\bq\s*[1-4]\b(?:\s+20\d{2})?", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b20\d{2}\b", " ", cleaned)
    cleaned = re.sub(r"\bby revenue\b", " ", cleaned, flags=re.IGNORECASE)
    match = re.search(
        r"\bcompare\s+(?:the\s+)?(?:product\s+)?(.+?)\s+(?:vs\.?|versus)\s+(?:the\s+)?(?:product\s+)?(.+?)(?:\?|$)",
        cleaned,
        re.IGNORECASE,
    )
    if not match:
        return None
    left = _clean_name(match.group(1))
    right = _clean_name(match.group(2))
    if len(left) < 2 or len(right) < 2:
        return None
    return left, right


def _clean_name(value: str) -> str:
    text = " ".join((value or "").split()).strip(" \"'")
    text = re.sub(r"^(the|product)\s+", "", text, flags=re.IGNORECASE)
    return text.strip(" .,:;")


def _tokens(text: str) -> list[str]:
    words = []
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if word in _STOP or word.isdigit():
            continue
        words.append("versus" if word in {"vs", "versus"} else word)
    return words


def _compact(sql: str) -> str:
    return "\n".join(line.rstrip() for line in sql.strip().splitlines() if line.strip())
