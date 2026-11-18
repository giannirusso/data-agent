"""Resolve which quarter a report is about.

The data window is the latest `orders.created_at` in the user's brand scope,
read once per session. If that lookup fails, the window is today's date.
A quarter named without a year is the latest complete one on or before that date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

_QUARTER_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
}


@dataclass(frozen=True)
class ReportPeriod:
    quarter: int
    year: int
    start: datetime
    end: datetime
    previous_start: datetime
    label: str
    note: str

    @property
    def params(self) -> dict[str, str]:
        return {
            "period_start": _iso(self.start),
            "period_end": _iso(self.end),
            "previous_start": _iso(self.previous_start),
        }


def parse_period(
    text: str,
    *,
    data_through: date | None = None,
    source: str = "today",
) -> ReportPeriod:
    raw = text or ""
    quarter = _quarter(raw)
    year = _year(raw)
    limit = data_through or date.today()
    assumed = False
    if quarter is None and year is None:
        quarter, year = _latest_complete(limit)
        assumed = True
    elif quarter is None:
        quarter = _latest_complete_in_year(year, limit) or 4
        assumed = True
    elif year is None:
        year = _latest_year_for_quarter(quarter, limit)
        assumed = True
    start, end = _bounds(year, quarter)
    previous_quarter, previous_year = _previous(year, quarter)
    previous_start, _ = _bounds(previous_year, previous_quarter)
    label = f"Q{quarter} {year}"
    last_day = end.date().fromordinal(end.date().toordinal() - 1)
    if assumed and _year(raw) is None:
        note = (
            f"No year was given. This report uses {label} "
            f"({start.date()} through {last_day}), the latest complete Q{quarter} "
            f"on or before {_window_phrase(limit, source)}. "
            f"Name a year, for example Q{quarter} 2023, to override."
        )
    else:
        note = (
            f"Period is {label} ({start.date()} through {last_day}). "
            f"Revenue excludes Cancelled and Returned."
        )
    return ReportPeriod(
        quarter=quarter,
        year=year,
        start=start,
        end=end,
        previous_start=previous_start,
        label=label,
        note=note,
    )


def _quarter(text: str) -> int | None:
    match = re.search(r"\bq\s*([1-4])\b", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(first|second|third|fourth)\s+quarter\b", text, re.IGNORECASE)
    if match:
        return _QUARTER_WORDS[match.group(1).lower()]
    return None


def _year(text: str) -> int | None:
    match = re.search(r"\b(20\d{2})\b", text)
    if match:
        return int(match.group(1))
    return None


def _window_phrase(limit: date, source: str) -> str:
    if source == "orders":
        return f"the latest order date in your scope ({limit.isoformat()})"
    if source == "injected":
        return f"the order date supplied for this session ({limit.isoformat()})"
    return f"today ({limit.isoformat()}), because the latest-order lookup failed"


def _complete(year: int, quarter: int, limit: date) -> bool:
    _, end = _bounds(year, quarter)
    last_day = end.date().fromordinal(end.date().toordinal() - 1)
    return last_day <= limit


def _latest_complete(limit: date) -> tuple[int, int]:
    quarter = (limit.month - 1) // 3 + 1
    year = limit.year
    if not _complete(year, quarter, limit):
        quarter, year = _previous(year, quarter)
    return quarter, year


def _latest_year_for_quarter(quarter: int, limit: date) -> int:
    year = limit.year
    while year >= 2019:
        if _complete(year, quarter, limit):
            return year
        year -= 1
    return limit.year


def _latest_complete_in_year(year: int, limit: date) -> int | None:
    for quarter in (4, 3, 2, 1):
        if _complete(year, quarter, limit):
            return quarter
    return None


LATEST_ORDER_SQL = "SELECT MAX(created_at) AS latest_order FROM orders"


def resolve_data_through(deps, *, brands: list[str], salt: str) -> tuple[date, str]:
    """Latest order date, cached on the session. Today's date if BigQuery fails."""
    cached = getattr(deps, "data_through", None)
    if isinstance(cached, date):
        return cached, getattr(deps, "data_through_source", "injected")
    try:
        found = _query_latest_order(deps, brands=brands, salt=salt)
    except Exception:
        found = None
    if found is None:
        fallback = date.today()
        deps.data_through = fallback
        deps.data_through_source = "today"
        return fallback, "today"
    deps.data_through = found
    deps.data_through_source = "orders"
    return found, "orders"


def _query_latest_order(deps, *, brands: list[str], salt: str) -> date | None:
    from agent.safety.sql_validator import validate_and_rewrite

    checked = validate_and_rewrite(LATEST_ORDER_SQL)
    if not checked.ok or not checked.sql:
        raise RuntimeError(checked.error or "latest-order SQL was rejected")
    params = {"entitled_brands": list(brands), "pii_salt": salt}
    dry = deps.bq.dry_run(checked.sql, params=params)
    scanned = int(getattr(dry, "total_bytes_processed", 0) or 0)
    if scanned > deps.settings.bq_max_bytes:
        raise RuntimeError("latest-order dry run is over the byte budget")
    frame = deps.bq.execute(
        checked.sql,
        params=params,
        maximum_bytes_billed=deps.settings.bq_max_bytes,
        timeout=min(30, int(deps.settings.bq_timeout_seconds)),
    )
    return _coerce_date(_first_value(frame))


def _first_value(frame):
    if frame is None:
        return None
    if hasattr(frame, "empty") and frame.empty:
        return None
    if hasattr(frame, "iloc"):
        row = frame.iloc[0]
        for key in ("latest_order", "latest"):
            if key in frame.columns:
                return row[key]
        return row.iloc[0]
    if isinstance(frame, list) and frame:
        row = frame[0]
        if isinstance(row, dict):
            return row.get("latest_order", row.get("latest"))
    return None


def _coerce_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nat", "none", "nan", "null"}:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()


def _bounds(year: int, quarter: int) -> tuple[datetime, datetime]:
    month = (quarter - 1) * 3 + 1
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 10:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 3, 1, tzinfo=timezone.utc)
    return start, end


def _previous(year: int, quarter: int) -> tuple[int, int]:
    if quarter == 1:
        return 4, year - 1
    return quarter - 1, year


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
