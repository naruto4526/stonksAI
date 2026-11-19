"""The JSON endpoint behind BSE's announcements page. Unofficial: see README."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from ..models import Filing
from ..timeutil import IST, parse_iso_ist, utcnow
from .base import Snapshot, Source

ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"


class BseApiSource(Source):
    name = "bse_api"
    exchange = "BSE"
    default_url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    default_interval = 10.0
    official = False
    supports_conditional = False
    description = (
        "JSON behind BSE's announcements page (page 1, newest 50). Unofficial and "
        "undocumented; responses carry max-age=60."
    )

    def request_url(self) -> str:
        today = utcnow().astimezone(IST).strftime("%Y%m%d")
        query = urlencode(
            {
                "pageno": 1,
                "strCat": -1,
                "strPrevDate": today,
                "strScrip": "",
                "strSearch": "P",
                "strToDate": today,
                "strType": "C",
                "subcategory": -1,
            }
        )
        return f"{self.url}?{query}"

    def request_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com",
        }

    def parse(self, body: bytes) -> Snapshot:
        data = json.loads(body.decode("utf-8-sig"))
        rows = data.get("Table", []) if isinstance(data, dict) else []
        filings = []
        for row in rows:
            attachment = (row.get("ATTACHMENTNAME") or "").strip()
            subject = " / ".join(part for part in (row.get("CATEGORYNAME"), row.get("SUBCATNAME")) if part)
            filings.append(
                Filing(
                    source=self.name,
                    exchange=self.exchange,
                    source_id=str(row.get("NEWSID") or f"{row.get('SCRIP_CD')}@{row.get('NEWS_DT')}"),
                    company=(row.get("SLONGNAME") or "").strip(),
                    published_at=parse_iso_ist(row.get("DissemDT")) or parse_iso_ist(row.get("NEWS_DT")),
                    filed_at=parse_iso_ist(row.get("News_submission_dt")),
                    symbol=str(row["SCRIP_CD"]) if row.get("SCRIP_CD") else None,
                    subject=subject or None,
                    headline=row.get("HEADLINE") or row.get("NEWSSUB") or None,
                    attachment_url=ATTACHMENT_BASE + attachment if attachment else None,
                )
            )
        return Snapshot(filings)
