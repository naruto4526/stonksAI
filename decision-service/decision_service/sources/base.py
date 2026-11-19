"""Base class for an announcement source: fetch, parse, remember caching headers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from ..http import HttpClient
from ..models import Filing
from ..timeutil import utcnow


@dataclass
class Snapshot:
    filings: list[Filing]
    build_time: datetime | None = None  # when the source says it was last rebuilt


@dataclass
class PollResult:
    source: str
    started_at: datetime
    finished_at: datetime | None = None
    status: int | None = None
    elapsed_ms: float | None = None
    wire_bytes: int = 0
    body_bytes: int = 0
    not_modified: bool = False
    filings: list[Filing] = field(default_factory=list)
    build_time: datetime | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Source(ABC):
    name: str = ""
    exchange: str = ""
    default_url: str = ""
    default_interval: float = 10.0
    official: bool = True
    supports_conditional: bool = True
    description: str = ""

    def __init__(
        self,
        client: HttpClient | None = None,
        url: str | None = None,
        interval: float | None = None,
    ) -> None:
        self.client = client or self.make_client()
        self.url = url or self.default_url
        self.interval = float(interval) if interval is not None else self.default_interval
        self._etag: str | None = None
        self._last_modified: str | None = None

    # Hooks a source can override -------------------------------------------------
    def make_client(self) -> HttpClient:
        return HttpClient()

    def request_url(self) -> str:
        return self.url

    def request_headers(self) -> dict[str, str]:
        return {}

    def prepare(self) -> None:
        """Run once before polling starts (for example, to collect cookies)."""

    def on_auth_error(self) -> bool:
        """Called on HTTP 401/403. Return True to retry the request once."""
        return False

    @abstractmethod
    def parse(self, body: bytes) -> Snapshot:
        ...

    # Polling ---------------------------------------------------------------------
    def _fetch(self):
        return self.client.get(
            self.request_url(),
            headers=self.request_headers(),
            etag=self._etag if self.supports_conditional else None,
            last_modified=self._last_modified if self.supports_conditional else None,
        )

    def poll(self) -> PollResult:
        result = PollResult(source=self.name, started_at=utcnow())
        try:
            fetch = self._fetch()
            if fetch.status in (401, 403) and self.on_auth_error():
                fetch = self._fetch()
        except Exception as exc:  # network errors, timeouts, bad gzip
            result.finished_at = utcnow()
            result.error = f"{type(exc).__name__}: {exc}"
            return result

        result.finished_at = utcnow()
        result.status = fetch.status
        result.elapsed_ms = fetch.elapsed_ms
        result.wire_bytes = fetch.wire_bytes
        result.body_bytes = len(fetch.body)

        if fetch.not_modified:
            result.not_modified = True
            return result
        if not fetch.ok:
            result.error = f"HTTP {fetch.status}"
            return result

        if self.supports_conditional:
            self._etag = fetch.headers.get("etag")
            self._last_modified = fetch.headers.get("last-modified")

        try:
            snapshot = self.parse(fetch.body)
        except Exception as exc:
            result.error = f"parse error: {type(exc).__name__}: {exc}"
            return result
        result.filings = snapshot.filings
        result.build_time = snapshot.build_time
        return result
