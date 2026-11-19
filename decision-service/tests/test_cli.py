"""End to end: poll two local feeds through the CLI, then read the report."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path

from decision_service.cli import main, parse_duration
from decision_service.timeutil import IST, utcnow

from decision_service.sources import SOURCES
from decision_service.store import Store

from .helpers import FIXTURES, FeedServer, cached_route, fixture


class CliTests(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(parse_duration("90"), 90)
        self.assertEqual(parse_duration("6h30m"), 23400)
        self.assertEqual(parse_duration("45s"), 45)
        with self.assertRaises(Exception):
            parse_duration("soon")

    def test_unofficial_sources_need_opt_in(self):
        with self.assertRaises(SystemExit) as raised, tempfile.TemporaryDirectory() as tmp:
            main(["poll", "--sources", "nse_json", "--db", str(Path(tmp) / "x.sqlite"), "--duration", "1"])
        self.assertIn("--allow-unofficial", str(raised.exception))

    def test_poll_and_report(self):
        calls = {"nse": 0}
        base_nse = fixture("nse_rss.xml")

        def nse_body():
            calls["nse"] += 1
            if calls["nse"] == 1:
                return base_nse
            # From the second request on, a new filing published two seconds ago.
            published = (utcnow() - timedelta(seconds=2)).astimezone(IST).strftime("%d-%b-%Y %H:%M:%S")
            item = (
                "<item><title>New Filing Limited</title>"
                "<link>https://nsearchives.nseindia.com/corporate/NEWFILING_16112026100000_x.pdf</link>"
                "<description>New Filing Limited has submitted results |SUBJECT: Outcome of Board Meeting</description>"
                f"<pubDate>{published}</pubDate></item>"
            ).encode()
            if not hasattr(nse_body, "frozen"):
                nse_body.frozen = base_nse.replace(b"<item>", item + b"<item>", 1)
            return nse_body.frozen

        with FeedServer() as server, tempfile.TemporaryDirectory() as tmp:
            server.routes["/nse.xml"] = cached_route(nse_body)
            server.routes["/bse.xml"] = cached_route(lambda: fixture("bse_rss.xml"))
            db = str(Path(tmp) / "runs.sqlite")

            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = main([
                    "poll", "--sources", "nse_rss,bse_rss",
                    "--url", f"nse_rss={server.url('/nse.xml')}",
                    "--url", f"bse_rss={server.url('/bse.xml')}",
                    "--interval", "nse_rss=0.2,bse_rss=0.3",
                    "--duration", "2", "--heartbeat", "0", "--db", db,
                ])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("New Filing Limited", text)
            self.assertIn("PER SOURCE", text)

            out = io.StringIO()
            with redirect_stdout(out):
                main(["report", "--db", db, "--json"])
            report = json.loads(out.getvalue())
            nse = report["sources"]["nse_rss"]
            self.assertEqual(nse["new_filings"], 1)
            self.assertGreater(nse["not_modified_share"], 0.5)  # unchanged polls came back as 304
            self.assertTrue(1.5 <= nse["lag_p50_s"] <= 10, nse["lag_p50_s"])
            self.assertEqual(report["sources"]["bse_rss"]["new_filings"], 0)


    def test_securities_import_and_events_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            sec_dir = Path(tmp) / "securities"
            out = io.StringIO()
            with redirect_stdout(out):
                main(["securities", "--dir", str(sec_dir), "--import",
                      str(FIXTURES / "nse_equity_l.csv"), str(FIXTURES / "bse_scrips.csv")])
            self.assertIn("7 securities", out.getvalue())  # 6 NSE + 1 BSE-only; Axis is in both

            db = str(Path(tmp) / "runs.sqlite")
            store = Store(db)
            run_id = store.start_run(["nse_rss", "bse_rss"])
            nse = SOURCES["nse_rss"]().parse(fixture("nse_rss.xml")).filings
            bse = SOURCES["bse_rss"]().parse(fixture("bse_rss.xml")).filings
            store.record_sightings(run_id, nse + bse, utcnow(), backlog=False)
            store.close()

            out = io.StringIO()
            with redirect_stdout(out):
                main(["events", "--db", db, "--rebuild", "--securities-dir", str(sec_dir), "--include-routine"])
            text = out.getvalue()
            self.assertIn("Rebuilt events for all runs from 9 filings", text)
            self.assertIn("results", text)


if __name__ == "__main__":
    unittest.main()
