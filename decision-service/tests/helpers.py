"""Shared test helpers: fixture loading and a tiny local feed server."""

from __future__ import annotations

import gzip
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


Route = Callable[[BaseHTTPRequestHandler], "tuple[int, dict[str, str], bytes]"]


def cached_route(body_fn: Callable[[], bytes], content_type: str = "application/xml") -> Route:
    """Serve body_fn() with an ETag, honouring If-None-Match and gzip like the NSE feed."""

    def route(handler: BaseHTTPRequestHandler):
        body = body_fn()
        etag = '"' + hashlib.sha1(body).hexdigest() + '"'
        if handler.headers.get("If-None-Match") == etag:
            return 304, {"ETag": etag}, b""
        headers = {"ETag": etag, "Content-Type": content_type}
        if "gzip" in (handler.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        return 200, headers, body

    return route


class FeedServer:
    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server naming
                server.requests.append((self.path, dict(self.headers)))
                route = server.routes.get(self.path.split("?", 1)[0])
                if route is None:
                    status, headers, body = 404, {}, b"not found"
                else:
                    status, headers, body = route(self)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def __enter__(self) -> "FeedServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
