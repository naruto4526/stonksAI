"""The JSON endpoint behind NSE's announcements page. Unofficial: see README."""

from __future__ import annotations

import json

from ..http import HttpClient
from ..models import Filing
from ..timeutil import parse_exchange_dt
from .base import Snapshot, Source


class NseJsonSource(Source):
    name = "nse_json"
    exchange = "NSE"
    default_url = "https://www.nseindia.in/api/corporate-announcements?index=equities"
    home_url = "https://www.nseindia.in/companies-listing/corporate-filings-announcements"
    default_interval = 10.0
    official = False
    supports_conditional = False
    description = (
        "JSON behind NSE's announcements page (newest 20). Unofficial and undocumented; "
        "NSE's terms of use prohibit automated data collection from the website."
    )

    def make_client(self) -> HttpClient:
        return HttpClient(use_cookies=True)

    def request_headers(self) -> dict[str, str]:
        return {"Accept": "application/json, text/plain, */*", "Referer": self.home_url}

    def prepare(self) -> None:
        # The API expects the cookies a normal visit to the page sets.
        self.client.get(self.home_url, headers={"Accept": "text/html,application/xhtml+xml"})

    def on_auth_error(self) -> bool:
        self.prepare()
        return True

    def parse(self, body: bytes) -> Snapshot:
        data = json.loads(body.decode("utf-8-sig"))
        rows = data if isinstance(data, list) else data.get("data", [])
        filings = []
        for row in rows:
            received = parse_exchange_dt(row.get("an_dt"))
            disseminated = parse_exchange_dt(row.get("exchdisstime"))
            filings.append(
                Filing(
                    source=self.name,
                    exchange=self.exchange,
                    source_id=str(row.get("seq_id") or f"{row.get('symbol')}@{row.get('an_dt')}"),
                    company=(row.get("sm_name") or "").strip(),
                    published_at=disseminated or received,
                    filed_at=received,
                    symbol=row.get("symbol") or None,
                    isin=row.get("sm_isin") or None,
                    subject=row.get("desc") or None,
                    headline=row.get("attchmntText") or None,
                    attachment_url=row.get("attchmntFile") or None,
                )
            )
        return Snapshot(filings)
