"""Resolve which quarter a report is about.

thelook_ecommerce in this prototype is treated as ending on 31 Dec 2024.
If the user names a year, that year is used. If they name only a quarter,
the year is the latest one for which that quarter is complete inside the
assumed data window. If they name neither, the report uses the latest
complete quarter in that window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

DATA_END = date(2024, 12, 31)

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


def parse_period(text: str, *, today: date | None = None) -> ReportPeriod:
    raw = text or ""
    quarter = _quarter(raw)
    year = _year(raw)
    today = today or date.today()
    assumed = False
    if quarter is None and year is None:
        quarter, year = _latest_complete(today)
        assumed = True
    elif quarter is None:
        quarter = _latest_complete_in_year(year, today) or 4
        assumed = True
    elif year is None:
        year = _latest_year_for_quarter(quarter, today)
        assumed = True
    start, end = _bounds(year, quarter)
    previous_quarter, previous_year = _previous(year, quarter)
    previous_start, _ = _bounds(previous_year, previous_quarter)
    label = f"Q{quarter} {year}"
    if assumed and _year(raw) is None:
        note = (
            f"No year was given. This report uses {label} "
            f"({start.date()} through the day before {end.date()}), "
            f"the latest complete Q{quarter} inside the assumed thelook_ecommerce "
            f"window ending {DATA_END.isoformat()}. Name a year, for example Q{quarter} 2023, to override."
        )
    else:
        note = (
            f"Period is {label} ({start.date()} through the day before {end.date()}). "
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


def _horizon(today: date) -> date:
    return min(today, DATA_END)


def _complete(year: int, quarter: int, limit: date) -> bool:
    _, end = _bounds(year, quarter)
    last_day = end.date().fromordinal(end.date().toordinal() - 1)
    return last_day <= limit


def _latest_complete(today: date) -> tuple[int, int]:
    limit = _horizon(today)
    quarter = (limit.month - 1) // 3 + 1
    year = limit.year
    if not _complete(year, quarter, limit):
        quarter, year = _previous(year, quarter)
    return quarter, year


def _latest_year_for_quarter(quarter: int, today: date) -> int:
    limit = _horizon(today)
    year = limit.year
    while year >= 2019:
        if _complete(year, quarter, limit):
            return year
        year -= 1
    return DATA_END.year


def _latest_complete_in_year(year: int, today: date) -> int | None:
    limit = _horizon(today)
    for quarter in (4, 3, 2, 1):
        if _complete(year, quarter, limit):
            return quarter
    return None


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
