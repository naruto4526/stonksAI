import unittest

from decision_service.http import HttpClient

from .helpers import FeedServer, cached_route

BODY = b"<rss>" + b"<item>same</item>" * 200 + b"</rss>"


class HttpClientTests(unittest.TestCase):
    def test_gzip_and_not_modified(self):
        with FeedServer() as server:
            server.routes["/feed.xml"] = cached_route(lambda: BODY)
            client = HttpClient()
            first = client.get(server.url("/feed.xml"))
            self.assertEqual(first.status, 200)
            self.assertEqual(first.body, BODY)
            self.assertLess(first.wire_bytes, len(BODY))  # arrived compressed

            second = client.get(server.url("/feed.xml"), etag=first.headers["etag"])
            self.assertTrue(second.not_modified)
            self.assertEqual(second.body, b"")
            self.assertEqual(server.requests[-1][1].get("If-None-Match"), first.headers["etag"])

    def test_error_status_is_returned_not_raised(self):
        with FeedServer() as server:
            result = HttpClient().get(server.url("/missing"))
            self.assertEqual(result.status, 404)
            self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
