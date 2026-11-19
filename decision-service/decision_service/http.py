"""A small HTTP client on the standard library: gzip, conditional GETs, cookies, timing."""

from __future__ import annotations

import gzip
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from http.cookiejar import CookieJar

DEFAULT_USER_AGENT = "stonksAI-source-poller/0.1 (personal research; Python urllib)"


@dataclass
class FetchResult:
    url: str
    status: int
    body: bytes = b""
    wire_bytes: int = 0
    elapsed_ms: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304


def _decode(raw: bytes, encoding: str) -> bytes:
    encoding = encoding.lower()
    if "gzip" in encoding:
        return gzip.decompress(raw)
    if "deflate" in encoding:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


class HttpClient:
    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 15.0,
        use_cookies: bool = False,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.cookies = CookieJar() if use_cookies else None
        handlers = [urllib.request.HTTPCookieProcessor(self.cookies)] if self.cookies is not None else []
        self._opener = urllib.request.build_opener(*handlers)

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
        }
        if headers:
            request_headers.update(headers)
        if etag:
            request_headers["If-None-Match"] = etag
        if last_modified:
            request_headers["If-Modified-Since"] = last_modified
        request = urllib.request.Request(url, headers=request_headers, method="GET")

        start = time.perf_counter()
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as error:  # 304 and 4xx/5xx arrive here
            raw = error.read() or b""
            status = error.code
            response_headers = {key.lower(): value for key, value in (error.headers or {}).items()}
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        body = _decode(raw, response_headers.get("content-encoding", "")) if raw else b""
        return FetchResult(url, status, body, len(raw), elapsed_ms, response_headers)
