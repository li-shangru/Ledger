"""
Date parsing utilities.

Handles various date formats commonly found in bank exports.
"""

import re
from datetime import UTC, date, datetime, timedelta

# Pre-compiled patterns
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MDY_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
YMD_SLASH = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")


def parse_date(value: str | None) -> date | None:
    """
    Parse a date string to date object.

    Supports:
    - ISO format: 2025-01-15
    - M/D/YYYY: 1/15/2025
    - MM/DD/YYYY: 01/15/2025
    - YYYY/MM/DD: 2025/01/15

    Returns None if parsing fails.
    """
    if value is None:
        return None

    s = str(value).strip()
    if not s:
        return None

    # ISO format: 2025-01-15
    if ISO_DATE.match(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None

    # M/D/YYYY or MM/DD/YYYY
    if m := MDY_SLASH.match(s):
        try:
            month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return date(year, month, day)
        except ValueError:
            return None

    # YYYY/MM/DD
    if m := YMD_SLASH.match(s):
        try:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return date(year, month, day)
        except ValueError:
            return None

    return None


def format_iso(d: date) -> str:
    """Format date as ISO string."""
    return d.strftime("%Y-%m-%d")


def quarter_of(d: date) -> str:
    """Get quarter string for a date."""
    q = (d.month - 1) // 3 + 1
    return f"Q{q}"


def year_quarter(d: date) -> str:
    """Get YYYY-QN string."""
    return f"{d.year}-{quarter_of(d)}"


def year_month(d: date) -> str:
    """Get YYYY-MM string."""
    return d.strftime("%Y-%m")


def next_month_start(d: date) -> date:
    """Return the first day of the month following *d*."""
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def month_end(d: date) -> date:
    """Return the last day of the month containing *d*."""
    return next_month_start(d.replace(day=1)) - timedelta(days=1)


def naive_utc_now() -> datetime:
    """Return current UTC time as a naive datetime (tzinfo stripped for SQLite storage)."""
    return datetime.now(UTC).replace(tzinfo=None)
