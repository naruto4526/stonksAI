"""Time helpers. Exchange timestamps are Indian Standard Time without a zone."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

IST = timezone(timedelta(hours=5, minutes=30), "IST")
UTC = timezone.utc

_MONTHS = {
    name: number
    for number, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}

# "14-Nov-2026 19:58:00" (NSE RSS pubDate, NSE JSON an_dt, BSE RSS pubDate)
_EXCHANGE_RE = re.compile(
    r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$"
)
# "2026-11-14T22:32:23.52" (BSE API; fractions of 1-7 digits)
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?$"
)
# NSE attachment names: PREFIX_DDMMYYYYHHMMSS_rest.pdf (the upload time)
_NSE_FILENAME_TS = re.compile(r"_(\d{2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})_")


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_exchange_dt(value: str | None) -> datetime | None:
    """Parse '14-Nov-2026 19:58:00' as IST. Month names are matched without locale."""
    if not value:
        return None
    match = _EXCHANGE_RE.match(value.strip())
    if not match:
        return None
    day, mon, year, hour, minute, second = match.groups()
    month = _MONTHS.get(mon.lower())
    if month is None:
        return None
    try:
        return datetime(int(year), month, int(day), int(hour), int(minute), int(second or 0), tzinfo=IST)
    except ValueError:
        return None


def parse_iso_ist(value: str | None) -> datetime | None:
    """Parse a naive ISO timestamp such as '2026-11-14T22:32:23.52' as IST."""
    if not value:
        return None
    match = _ISO_RE.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction = match.groups()
    micro = int((fraction or "0")[:6].ljust(6, "0"))
    try:
        return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=IST)
    except ValueError:
        return None


def parse_rfc822(value: str | None, assume_ist: bool = False) -> datetime | None:
    """Parse an RSS date. ``assume_ist`` keeps the clock time and replaces the zone with IST."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if assume_ist or parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def parse_nse_filename_ts(url: str | None) -> datetime | None:
    """Upload time embedded in an NSE attachment name, e.g. GOYAL_14112026140142_x.pdf."""
    if not url:
        return None
    name = url.rsplit("/", 1)[-1]
    match = _NSE_FILENAME_TS.search(name)
    if not match:
        return None
    day, month, year, hour, minute, second = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=IST)
    except ValueError:
        return None


def to_iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def fmt_ist(value: datetime | None, with_date: bool = False) -> str:
    if value is None:
        return "-"
    pattern = "%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S"
    return value.astimezone(IST).strftime(pattern)
