"""Registry of announcement sources."""

from __future__ import annotations

from .base import PollResult, Snapshot, Source
from .bse_api import BseApiSource
from .bse_rss import BseRssSource
from .nse_json import NseJsonSource
from .nse_rss import NseRssLegacySource, NseRssSource

SOURCES: dict[str, type[Source]] = {
    cls.name: cls
    for cls in (NseRssSource, NseRssLegacySource, BseRssSource, NseJsonSource, BseApiSource)
}

# Official feeds only. The unofficial JSON endpoints need --allow-unofficial.
DEFAULT_SOURCES = ("nse_rss", "nse_rss_legacy", "bse_rss")

__all__ = ["SOURCES", "DEFAULT_SOURCES", "PollResult", "Snapshot", "Source"]
