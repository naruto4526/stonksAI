"""BSE's official 'Latest Corporate Announcements' RSS feed."""

from __future__ import annotations

import re

from ..models import Filing, attachment_key
from ..timeutil import parse_exchange_dt, parse_rfc822
from .base import Snapshot, Source
from .rss import parse_rss

_TITLE_RE = re.compile(r"^(.*?)\s*\((\d+)\)\s*$")


class BseRssSource(Source):
    name = "bse_rss"
    exchange = "BSE"
    default_url = "https://www.bseindia.com/data/xml/announcements.xml"
    default_interval = 20.0
    official = True
    supports_conditional = True  # harmless: BSE sent no ETag or Last-Modified when checked
    description = (
        "BSE Latest Corporate Announcements RSS. Official; about 270 KB compressed on "
        "every poll (no 'not modified'); responses may be cached for up to 60 s."
    )

    def parse(self, body: bytes) -> Snapshot:
        meta, items = parse_rss(body)
        filings = []
        for item in items:
            title = item.get("title", "")
            match = _TITLE_RE.match(title)
            company = match.group(1) if match else title
            scrip_code = item.get("scripcode") or (match.group(2) if match else None)
            link = item.get("link") or None
            pub_date = item.get("pubDate", "")
            key = attachment_key(link) or title
            filings.append(
                Filing(
                    source=self.name,
                    exchange=self.exchange,
                    source_id=f"{key}@{pub_date}",
                    company=company,
                    published_at=parse_exchange_dt(pub_date),
                    symbol=scrip_code,
                    headline=item.get("description") or None,
                    attachment_url=link,
                )
            )
        # BSE labels lastBuildDate "GMT", but the clock time is IST: on 14 Nov 2026 the
        # feed said "22:33:46 GMT" while the server's own Date header was 17:05 GMT.
        return Snapshot(filings, parse_rfc822(meta.get("lastBuildDate"), assume_ist=True))
