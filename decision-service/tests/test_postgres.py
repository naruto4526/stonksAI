"""The store on PostgreSQL.

Runs when STONKS_TEST_DB, or else STONKS_DB (both also read from .env), is a postgresql:// URL
and psycopg is installed; skipped otherwise. Each test creates its own schema and drops it
afterwards, so the real tables are never touched.
"""

import io
import os
import secrets
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import quote

from decision_service.cli import load_env_file, main
from decision_service.events import EventTracker, rebuild_events
from decision_service.report import build_report
from decision_service.sources import SOURCES
from decision_service.store import Store, copy_database, is_postgres_url
from decision_service.timeutil import utcnow

from .helpers import fixture
from .test_events import master

load_env_file(Path(__file__).resolve().parents[1] / ".env")
URL = os.environ.get("STONKS_TEST_DB") or os.environ.get("STONKS_DB") or ""

try:
    import psycopg
except ImportError:
    psycopg = None


def in_schema(url: str, schema: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}options={quote('-csearch_path=' + schema)}"


def record_fixture_run(store: Store) -> int:
    """A run with the NSE and BSE feed fixtures recorded and grouped into events, as the poller does."""
    run = store.start_run(["nse_rss", "bse_rss"])
    tracker = EventTracker(store, master(), run)
    filings = SOURCES["nse_rss"]().parse(fixture("nse_rss.xml")).filings + SOURCES["bse_rss"]().parse(fixture("bse_rss.xml")).filings
    now = utcnow()
    store.record_sightings(run, filings, now, backlog=False)
    links = []
    for f in sorted(filings, key=lambda f: f.published_at or now):
        o = tracker.process(f, now)
        links.append((f.source, f.source_id, o.event_id, o.company_id, o.event_type))
    store.set_sighting_events(run, links)
    return run


@unittest.skipUnless(is_postgres_url(URL) and psycopg is not None,
                     "set STONKS_TEST_DB to a postgresql:// URL and install psycopg to test PostgreSQL")
class PostgresStoreTests(unittest.TestCase):
    def setUp(self):
        self.schema = "test_" + secrets.token_hex(4)
        self.admin = psycopg.connect(URL, autocommit=True)
        self.admin.execute(f"CREATE SCHEMA {self.schema}")
        self.url = in_schema(URL, self.schema)
        self.store = Store(self.url)

    def tearDown(self):
        self.store.close()
        self.admin.execute(f"DROP SCHEMA {self.schema} CASCADE")
        self.admin.close()

    def test_schema_is_created_once_and_reopens(self):
        self.assertEqual(self.store.dialect, "postgres")
        self.assertIn("type_exchange", self.store.column_names("events"))
        self.assertIn("event_id", self.store.column_names("sightings"))
        Store(self.url).close()  # a second open finds everything in place

    def test_runs_and_duplicate_sightings(self):
        self.assertEqual(self.store.start_run(["nse_rss"]), 1)
        self.assertEqual(self.store.start_run(["nse_rss"]), 2)
        filings = SOURCES["nse_rss"]().parse(fixture("nse_rss.xml")).filings
        first = self.store.record_sightings(2, filings, utcnow(), backlog=True)
        self.assertEqual(first, len({f.source_id for f in filings}))
        self.assertEqual(self.store.record_sightings(2, filings, utcnow(), backlog=True), 0)  # ON CONFLICT DO NOTHING
        rows = self.store.sightings(2)
        self.assertEqual(len(rows), first)
        self.assertEqual(rows[0]["backlog"], 1)
        self.assertEqual(self.store.latest_run_id(), 2)

    def test_events_rebuild_and_report(self):
        run = record_fixture_run(self.store)
        events = self.store.events()
        self.assertTrue(events)
        linked = [s for s in self.store.sightings(run) if s["event_id"] is not None]
        self.assertEqual(len(linked), len(self.store.sightings(run)))
        some = linked[0]
        self.assertEqual(self.store.linked_event(some["exchange"], some["source_id"])["id"], some["event_id"])
        self.assertEqual(rebuild_events(self.store, master()), len(linked))
        self.assertEqual(len(self.store.events()), len(events))
        self.assertIn("events", build_report(self.store, run))

    def test_copy_from_sqlite_keeps_ids_and_numbering(self):
        with tempfile.TemporaryDirectory() as tmp:
            sqlite = Store(Path(tmp) / "old.sqlite")
            sqlite.start_run(["nse_rss"])
            record_fixture_run(sqlite)
            counts = copy_database(sqlite, self.store)
            for table, n in counts.items():
                self.assertEqual(n, sqlite.count(table), table)
            self.assertEqual(self.store.start_run(["nse_rss"]), 3)  # numbering carries on after the copied ids
            with self.assertRaises(ValueError):
                copy_database(sqlite, self.store)  # not into a filled database...
            self.assertEqual(copy_database(sqlite, self.store, replace=True)["runs"], 2)  # ...unless asked
            sqlite.close()

    def test_cli_on_postgres(self):
        run = record_fixture_run(self.store)
        out = io.StringIO()
        with redirect_stdout(out):
            main(["events", "--db", self.url, "--rebuild", "--include-routine"])
            main(["report", "--db", self.url, "--run", str(run)])
        text = out.getvalue()
        self.assertIn("Rebuilt events for all runs", text)
        self.assertIn("PER SOURCE", text)


if __name__ == "__main__":
    unittest.main()
