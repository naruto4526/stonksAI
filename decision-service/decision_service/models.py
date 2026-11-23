"""The normalized filing record every source produces."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_BSE_SCRIP_SUFFIX = re.compile(r"\(\d+\)\s*$")
_LEGAL_SUFFIX = re.compile(r"\b(limited|ltd)\b\.?")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_LEADING_THE = re.compile(r"^\s*the\s+")  # NSE: "The Federal Bank Limited", BSE: "Federal Bank Ltd"
_INDIA_SHORT = re.compile(r"\(\s*i\s*\)")  # "W S Industries (I) Limited" = "W. S. Industries (India) Ltd"


def company_key(name: str | None) -> str:
    """Loose company key for matching the same company across NSE and BSE."""
    if not name:
        return ""
    text = name.lower().replace("&", "and")
    text = _BSE_SCRIP_SUFFIX.sub("", text)
    text = _INDIA_SHORT.sub(" india ", text)
    text = _LEADING_THE.sub("", text)
    text = _LEGAL_SUFFIX.sub(" ", text)
    return _NON_ALNUM.sub("", text)


def attachment_key(url: str | None) -> str | None:
    """File name of an attachment, ignoring the host (nseindia.com and nseindia.in match)."""
    if not url:
        return None
    name = url.strip().split("?", 1)[0].rsplit("/", 1)[-1].lower()
    return name or None


@dataclass
class Filing:
    source: str
    exchange: str
    source_id: str
    company: str
    published_at: datetime | None = None  # exchange broadcast time
    filed_at: datetime | None = None  # company upload / receipt time, when the source gives it
    symbol: str | None = None  # NSE symbol or BSE scrip code
    isin: str | None = None
    subject: str | None = None
    headline: str | None = None
    attachment_url: str | None = None
    # Filled in by the event tracker
    company_id: str | None = None  # ISIN, or "name:<key>" when the ISIN is unknown
    event_id: int | None = None
    event_type: str | None = None
    event_is_new: bool | None = None
    event_repeat: bool | None = None  # this exact filing was already linked to the event

    @property
    def match_key(self) -> str:
        """Key for matching one filing across sources of the same exchange."""
        return attachment_key(self.attachment_url) or f"{self.exchange}:{self.source_id}"

    @property
    def company_key(self) -> str:
        return company_key(self.company)
