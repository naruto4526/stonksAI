import importlib.util
import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

from decision_service import documents
from decision_service.cli import main
from decision_service.events import EventTracker
from decision_service.http import HttpClient
from decision_service.models import Filing
from decision_service.securities import SecurityMaster
from decision_service.store import Store
from decision_service.timeutil import IST

from .helpers import FIXTURES, FeedServer

HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
THU = datetime(2026, 11, 19, 10, 0, tzinfo=IST)
AXIS = "INE238A01034"
RESULTS_TEXT = ("Unaudited financial results for the quarter ended 30 September 2026. Revenue from operations "
                "rose 12 percent and profit after tax rose 18 percent against the same quarter last year.")


def make_pdf(pages: list[str]) -> bytes:
    """A minimal PDF with one line of Helvetica text per page ('' = a page with no text, like a scan)."""
    n = len(pages)
    page_ids = [4 + 2 * i for i in range(n)]
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % p for p in page_ids) + b"] /Count %d >>" % n,
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for pid, text in zip(page_ids, pages):
        stream = f"BT /F1 9 Tf 20 720 Td ({text}) Tj ET".encode() if text else b""
        objs[pid] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (pid + 1))
        objs[pid + 1] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = out.tell()
        out.write(b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n")
    xref, count = out.tell(), max(objs) + 1
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % count)
    for num in range(1, count):
        out.write(b"%010d 00000 n \n" % offsets[num])
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (count, xref))
    return out.getvalue()


XBRL = b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/in-capmkt">
<xbrli:context id="OneD"><xbrli:entity><xbrli:identifier scheme="http://www.nseindia.com">AXISBANK</xbrli:identifier></xbrli:entity>
<xbrli:period><xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
<in-capmkt:RevenueFromOperations contextRef="OneD" unitRef="INR" decimals="-5">1000</in-capmkt:RevenueFromOperations>
<in-capmkt:ProfitLossForPeriod contextRef="OneD" unitRef="INR" decimals="-5">200</in-capmkt:ProfitLossForPeriod>
</xbrli:xbrl>"""


def zipped(name: str, body: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, body)
    return buf.getvalue()


def master() -> SecurityMaster:
    m = SecurityMaster()
    for name in ("nse_equity_l.csv", "bse_scrips.csv"):
        m.load_file(FIXTURES / name)
    return m


class SniffTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(documents.sniff(b"%PDF-1.7\n..."), "pdf")
        self.assertEqual(documents.sniff(b"PK\x03\x04rest"), "zip")
        self.assertEqual(documents.sniff(XBRL), "xbrl")
        self.assertEqual(documents.sniff(b"<?xml version='1.0'?><rss/>"), "xml")
        self.assertEqual(documents.sniff(b"\n<!DOCTYPE html><html><body>Access Denied</body></html>"), "html")
        self.assertEqual(documents.sniff(b""), "empty")

    def test_alternate_urls(self):
        self.assertEqual(documents.alternate_urls("https://nsearchives.nseindia.com/corporate/A.pdf"),
                         ["https://nsearchives.nseindia.com/corporate/A.pdf", "https://nsearchives.nseindia.in/corporate/A.pdf"])
        self.assertEqual(documents.alternate_urls("https://www.bseindia.com/x.pdf"), ["https://www.bseindia.com/x.pdf"])

    def test_universe_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "u.txt"
            path.write_text("# Nifty test\nINE238A01034 INE596F01018\nnot-an-isin\n", encoding="utf-8")
            self.assertEqual(documents.load_universe(path), {"INE238A01034", "INE596F01018"})

    def test_xbrl_inspection(self):
        info = documents.inspect_xml(XBRL)
        self.assertEqual((info["facts"], info["contexts"]), (2, 1))
        self.assertEqual(info["financial_facts"], ["ProfitLossForPeriod", "RevenueFromOperations"])


class DocumentTestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = FeedServer().__enter__()
        results_pdf = make_pdf([RESULTS_TEXT, RESULTS_TEXT])
        routes = {
            "/corporate/AXIS_RESULTS.pdf": ("application/pdf", results_pdf),
            "/AttachLive/results-guid.pdf": ("application/pdf", results_pdf),  # BSE serves the same file
            "/corporate/xbrl/AXIS_RESULTS.xml": ("application/xml", XBRL),
            "/corporate/AXIS_ORDER.zip": ("application/zip", zipped("order.pdf", make_pdf([RESULTS_TEXT]))),
            "/corporate/AXIS_SCAN.pdf": ("application/pdf", make_pdf(["", ""])),
            "/AttachLive/blocked.pdf": ("text/html", b"<!DOCTYPE html><html><body>Access Denied</body></html>"),
            "/corporate/PTC_RESULTS.pdf": ("application/pdf", results_pdf),
        }
        for path, (ctype, body) in routes.items():
            self.server.routes[path] = lambda handler, c=ctype, b=body: (200, {"Content-Type": c}, b)
        self.db = Path(self.tmp.name) / "db.sqlite"
        self.store = Store(self.db)
        run = self.store.start_run(["nse_rss", "bse_rss"])
        tracker = EventTracker(self.store, master(), run)
        u = self.server.url

        def see(source, exchange, company, subject, headline, minutes, path, code=None):
            f = Filing(source=source, exchange=exchange, source_id=f"{path}@{minutes}", company=company,
                       published_at=THU + timedelta(minutes=minutes), subject=subject, headline=headline,
                       symbol=code, attachment_url=u(path))
            self.store.record_sightings(run, [f], THU, backlog=False)
            o = tracker.process(f, THU)
            self.store.set_sighting_events(run, [(f.source, f.source_id, o.event_id, o.company_id, o.event_type)])

        see("nse_rss", "NSE", "Axis Bank Limited", "Outcome of Board Meeting", "financial results", 0, "/corporate/AXIS_RESULTS.pdf")
        see("bse_rss", "BSE", "AXIS Bank Ltd", None, "Unaudited Financial Results", 1, "/AttachLive/results-guid.pdf", "532215")
        see("nse_rss", "NSE", "Axis Bank Limited", "Financial Result Updates", "financial results", 2, "/corporate/xbrl/AXIS_RESULTS.xml")
        see("nse_rss", "NSE", "Axis Bank Limited", "Bagging/Receiving of orders/contracts", "", 120, "/corporate/AXIS_ORDER.zip")
        see("nse_rss", "NSE", "Axis Bank Limited", "Press Release", "", 240, "/corporate/AXIS_SCAN.pdf")
        see("bse_rss", "BSE", "AXIS Bank Ltd", None, "Press Release", 241, "/AttachLive/blocked.pdf", "532215")
        see("nse_rss", "NSE", "PTC Industries Limited", "Outcome of Board Meeting", "financial results", 5, "/corporate/PTC_RESULTS.pdf")
        see("nse_rss", "NSE", "Axis Bank Limited", "Copy of Newspaper Publication", "", 300, "/corporate/AXIS_NEWS.pdf")

    def tearDown(self):
        self.store.close()
        self.server.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_selection_filters_and_puts_results_first(self):
        chosen = documents.select_attachments(self.store, {AXIS}, limit=50)
        keys = [a.key for a in chosen]
        self.assertEqual(chosen[0].event_type, "results")
        self.assertEqual(set(keys), {"axis_results.pdf", "results-guid.pdf", "axis_results.xml",
                                     "axis_order.zip", "axis_scan.pdf", "blocked.pdf"})  # no PTC, no newspaper copy
        everything = documents.select_attachments(self.store, None, limit=50)
        self.assertIn("ptc_results.pdf", [a.key for a in everything])

    def test_run_inspect_and_summarise(self):
        out = Path(self.tmp.name) / "docs"
        chosen = documents.select_attachments(self.store, {AXIS}, limit=50)
        lines = []
        records = documents.run_test(HttpClient(timeout=5), chosen, out, delay=0, log=lines.append)
        by_key = {r["key"]: r for r in records}
        self.assertEqual(len(lines), 6)
        self.assertEqual(by_key["axis_results.xml"]["kind"], "xbrl")
        self.assertIn("RevenueFromOperations", by_key["axis_results.xml"]["xml"]["financial_facts"])
        self.assertEqual(by_key["axis_order.zip"]["zip"]["members"], 1)
        self.assertFalse(by_key["blocked.pdf"]["ok"])
        self.assertEqual(by_key["blocked.pdf"]["error"], "not a document (html)")
        self.assertTrue((out / "nse" / "axis_results.pdf").exists())

        summary = documents.summarize(records)
        self.assertEqual(summary["by_exchange"]["BSE"], {"tried": 2, "ok": 1, "failed": 1})
        self.assertEqual(summary["nse_bse_pairs"], {"pairs": 1, "identical_files": 1})
        self.assertEqual(summary["xbrl"]["with_financials"], 1)
        text = documents.format_summary(summary)
        self.assertIn("DOCUMENT TEST: 6 attachments from 3 events", text)

        if HAS_PYPDF:
            pdf = by_key["axis_results.pdf"]["pdf"]
            self.assertEqual(pdf["pages"], 2)
            self.assertGreater(pdf["chars"], 300)
            self.assertFalse(pdf["scanned"])
            self.assertTrue(by_key["axis_scan.pdf"]["pdf"]["scanned"])
            self.assertEqual(by_key["axis_order.zip"]["zip"]["largest_pdf"]["pages"], 1)
            self.assertEqual(summary["pdf_scanned"], 1)
            self.assertIn("Claude Haiku 5.5", summary["cost_estimate"])

        # A second run reads saved files and only retries the one that failed
        before = len(self.server.requests)
        again = documents.run_test(HttpClient(timeout=5), chosen, out, delay=0, log=lambda _: None)
        self.assertEqual(sum(r["cached"] for r in again), 5)
        self.assertEqual(len(self.server.requests) - before, 1)

    def test_cli(self):
        out = Path(self.tmp.name) / "cli"
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["documents", "--db", str(self.db), "--universe", "all", "--out", str(out),
                         "--delay", "0", "--limit", "20", "--timeout", "5"])
        text = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Testing 7 attachments from 4 non-routine events (every company)", text)
        self.assertIn("DOCUMENT TEST", text)
        self.assertEqual(len(list(out.glob("document-test-*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
