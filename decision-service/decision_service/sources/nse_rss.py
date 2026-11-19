"""NSE's official Announcements RSS feed."""

from __future__ import annotations

from ..models import Filing, attachment_key
from ..timeutil import parse_exchange_dt, parse_nse_filename_ts, parse_rfc822
from .base import Snapshot, Source
from .rss import parse_rss


def split_subject(description: str) -> tuple[str | None, str | None]:
    """NSE puts the category after '|SUBJECT:' at the end of the description."""
    if "|SUBJECT:" in description:
        headline, _, subject = description.partition("|SUBJECT:")
        return headline.strip() or None, subject.strip() or None
    return description.strip() or None, None


class NseRssSource(Source):
    name = "nse_rss"
    exchange = "NSE"
    default_url = "https://nsearchives.nseindia.in/content/RSS/Online_announcements.xml"
    default_interval = 5.0
    official = True
    supports_conditional = True
    description = (
        "NSE Announcements RSS on the current nseindia.in address. Official; the whole "
        "day's filings in one file; answers 'not modified' (304) when unchanged."
    )

    def parse(self, body: bytes) -> Snapshot:
        meta, items = parse_rss(body)
        filings = []
        for item in items:
            link = item.get("link") or None
            pub_date = item.get("pubDate", "")
            headline, subject = split_subject(item.get("description", ""))
            key = attachment_key(link) or item.get("title", "")
            filings.append(
                Filing(
                    source=self.name,
                    exchange=self.exchange,
                    source_id=f"{key}@{pub_date}",
                    company=item.get("title", ""),
                    published_at=parse_exchange_dt(pub_date),
                    filed_at=parse_nse_filename_ts(link),
                    subject=subject,
                    headline=headline,
                    attachment_url=link,
                    # No symbol: the file-name prefix is not always the NSE symbol
                    # (PTC Industries files as PTCINDUSTRIES_..., its symbol is PTCIL).
                )
            )
        return Snapshot(filings, parse_rfc822(meta.get("lastBuildDate")))


class NseRssLegacySource(NseRssSource):
    name = "nse_rss_legacy"
    default_url = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
    default_interval = 60.0
    description = (
        "The same feed on the old nseindia.com address. On 14 Nov 2026 it had stopped "
        "updating at 14:06 IST; polled slowly to see whether it recovers."
    )
