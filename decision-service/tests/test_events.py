import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from decision_service.events import EventTracker, classify, rebuild_events, trading_session
from decision_service.models import Filing
from decision_service.poller import Poller
from decision_service.report import build_report
from decision_service.securities import SecurityMaster
from decision_service.sources.base import PollResult, Snapshot, Source
from decision_service.store import Store
from decision_service.timeutil import IST

from .helpers import FIXTURES

THU = datetime(2026, 11, 19, 10, 0, tzinfo=IST)


def master() -> SecurityMaster:
    m = SecurityMaster()
    for name in ("nse_equity_l.csv", "nse_sme_equity_l.csv", "bse_scrips.csv"):
        m.load_file(FIXTURES / name)
    return m


def nse(company, subject, headline, published, key, symbol=None):
    return Filing(source="nse_rss", exchange="NSE", source_id=key, company=company, published_at=published,
                  subject=subject, headline=headline, symbol=symbol,
                  attachment_url=f"https://nsearchives.nseindia.com/corporate/{key}")


def bse(company, headline, published, key, code=None):
    return Filing(source="bse_rss", exchange="BSE", source_id=key, company=company, published_at=published,
                  headline=headline, symbol=code,
                  attachment_url=f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{key}")


class ClassifyTests(unittest.TestCase):
    def test_rules(self):
        cases = [
            ("Outcome of Board Meeting", "Axis has submitted to the Exchange, the financial results for the period", "results"),
            (None, "Unaudited Financial Results quarter 30.09.2026", "results"),
            ("Bagging/Receiving of orders/contracts", "", "order_win"),
            ("Declaration of NAV", "", "routine"),
            ("Analyst/Investor Meet Para A-XBRL", "", "routine"),
            ("Analysts/Institutional Investor Meet/Con. Call Updates", "Schedule of meet", "routine"),
            (None, "As per file attached", "other"),
            ("Press Release", "Company has informed the Exchange regarding a press release", "update"),
            (None, "Record date for interim dividend", "capital_return"),
            ("Acquisition", "Acquisition of 51% stake", "merger_acquisition"),
            ("Change in Directors/ Key Managerial Personnel", "Resignation of CFO", "management"),
            (None, "Credit rating reaffirmed", "credit_rating"),
            ("Agreements (Sub-para 5-Para B)", "", "agreement"),
            ("NOC/No dues certificate/consent/permission", "IDBI Trusteeship Services Limited has informed", "routine"),
            ("Action(s) taken or orders passed", "", "legal_regulatory"),
            ("Disclosure Under Regulation 51", "", "routine"),
            (None, "Current Expense Ratio as on 18-11-2026", "routine"),
            (None, "The Exchange has received the disclosure under Regulation 29(2) of SEBI (Substantial Acquisition of Shares and Takeovers)", "shareholding"),
        ]
        for subject, headline, expected in cases:
            with self.subTest(subject=subject, headline=headline):
                self.assertEqual(classify(subject, headline), expected)


class SessionTests(unittest.TestCase):
    def test_sessions(self):
        self.assertEqual(trading_session(THU), (date(2026, 11, 19), True))
        self.assertEqual(trading_session(THU.replace(hour=8)), (date(2026, 11, 19), False))
        self.assertEqual(trading_session(THU.replace(hour=18)), (date(2026, 11, 20), False))
        friday_evening = datetime(2026, 11, 20, 16, 0, tzinfo=IST)
        self.assertEqual(trading_session(friday_evening), (date(2026, 11, 23), False))
        saturday = datetime(2026, 11, 21, 11, 0, tzinfo=IST)
        self.assertEqual(trading_session(saturday), (date(2026, 11, 23), False))
        self.assertEqual(trading_session(THU.replace(hour=15, minute=30)), (date(2026, 11, 19), True))


class SecurityMasterTests(unittest.TestCase):
    def setUp(self):
        self.m = master()

    def test_counts(self):
        self.assertEqual(len(self.m), 9)  # 6 NSE + 2 SME + 1 BSE-only; Axis appears in both lists
        self.assertEqual(self.m.by_isin["INE238A01034"].bse_code, "532215")
        self.assertEqual(self.m.by_isin["INE238A01034"].nse_symbol, "AXISBANK")

    def test_resolution(self):
        cases = [
            (nse("Axis Bank Limited", None, None, THU, "a.pdf"), "INE238A01034"),  # RSS: name only
            (nse("Something Else", None, None, THU, "b.pdf", symbol="PTCIL"), "INE596F01018"),  # JSON: symbol
            (bse("AXIS Bank Ltd", "", THU, "c.pdf", code="532215"), "INE238A01034"),  # BSE: scrip code
            (bse("Shanti Overseas (India) Ltd", "", THU, "d.pdf", code="526853"), "INE933X01016"),  # BSE: via NSE name
            (bse("Example BSE Only Ltd", "", THU, "e.pdf", code="599999"), "INE000X01010"),
            (bse("Unknown Traders Ltd", "", THU, "f.pdf", code="511111"), "name:unknowntraders"),
        ]
        for filing, expected in cases:
            with self.subTest(company=filing.company):
                self.assertEqual(self.m.resolve(filing)[0], expected)

    def test_ambiguous_names_are_not_guessed(self):
        m = SecurityMaster()
        m.load_text("SYMBOL,NAME OF COMPANY,ISIN NUMBER\nAAA,Twin Limited,INE111A01011\nBBB,Twin Limited,INE222B01011\n")
        self.assertEqual(m.resolve(nse("Twin Limited", None, None, THU, "t.pdf"))[0], "name:twin")


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.tracker = EventTracker(self.store, master(), run_id=1)

    def tearDown(self):
        self.store.close()

    def test_pdf_xbrl_and_bse_copies_make_one_event(self):
        t = THU + timedelta(hours=3)
        first = self.tracker.process(nse("Axis Bank Limited", "Analysts/Institutional Investor Meet/Con. Call Updates", "", t, "AXIS_1.pdf"), t)
        second = self.tracker.process(nse("Axis Bank Limited", "Analyst/Investor Meet Para A-XBRL", "", t + timedelta(minutes=2), "SAIIM_1.xml"), t)
        third = self.tracker.process(bse("AXIS Bank Ltd", "ANALYSTS/INSTITUTIONAL INVESTORS MEET", t + timedelta(seconds=88), "u1.pdf", "532215"), t)
        self.assertTrue(first.is_new)
        self.assertFalse(second.is_new)
        self.assertFalse(third.is_new)
        events = self.store.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["copies"], 3)
        self.assertEqual(events[0]["exchanges"], "BSE,NSE")
        self.assertEqual(events[0]["status"], "routine")

    def test_late_copy_hours_later_joins_and_earlier_stamp_wins(self):
        bse_time = THU + timedelta(minutes=5)
        nse_time = THU  # NSE stamped earlier, but its feed stalled
        a = self.tracker.process(bse("AXIS Bank Ltd", "Unaudited Financial Results", bse_time, "r.pdf", "532215"), bse_time + timedelta(minutes=1))
        b = self.tracker.process(nse("Axis Bank Limited", "Outcome of Board Meeting", "submitted the financial results", nse_time, "R.pdf"), THU + timedelta(hours=2))
        self.assertEqual(a.event_id, b.event_id)
        event = self.store.events()[0]
        self.assertEqual(event["first_source"], "bse_rss")
        self.assertEqual(datetime.fromisoformat(event["news_time"]), nse_time)
        self.assertEqual(event["status"], "new")

    def test_different_types_are_different_events(self):
        a = self.tracker.process(nse("Axis Bank Limited", "Outcome of Board Meeting", "financial results", THU, "1.pdf"), THU)
        b = self.tracker.process(nse("Axis Bank Limited", "Bagging/Receiving of orders/contracts", "", THU, "2.pdf"), THU)
        self.assertNotEqual(a.event_id, b.event_id)

    def test_same_type_merges_within_an_hour_only(self):
        a = self.tracker.process(nse("Axis Bank Limited", "Press Release", "", THU, "p1.pdf"), THU)
        b = self.tracker.process(bse("AXIS Bank Ltd", "Press Release", THU + timedelta(minutes=40), "p2.pdf", "532215"), THU)
        c = self.tracker.process(nse("Axis Bank Limited", "Press Release", "", THU + timedelta(hours=3), "p3.pdf"), THU)
        self.assertEqual(a.event_id, b.event_id)
        self.assertNotEqual(a.event_id, c.event_id)

    def test_unreadable_copy_merges_within_window_and_takes_the_type(self):
        a = self.tracker.process(bse("AXIS Bank Ltd", "As per file attached", THU, "x.pdf", "532215"), THU)
        self.assertEqual(a.event_type, "other")
        b = self.tracker.process(nse("Axis Bank Limited", "Outcome of Board Meeting", "financial results", THU + timedelta(minutes=10), "X.pdf"), THU)
        self.assertEqual(a.event_id, b.event_id)
        self.assertEqual(self.store.events()[0]["event_type"], "results")

    def test_unreadable_copy_outside_window_stays_separate(self):
        a = self.tracker.process(bse("AXIS Bank Ltd", "As per file attached", THU, "x.pdf", "532215"), THU)
        b = self.tracker.process(nse("Axis Bank Limited", "Outcome of Board Meeting", "financial results", THU + timedelta(hours=2), "X.pdf"), THU)
        self.assertNotEqual(a.event_id, b.event_id)

    def test_evening_filing_and_next_morning_copy_share_a_session(self):
        evening = THU.replace(hour=18)
        morning = THU.replace(hour=9, minute=20) + timedelta(days=1)
        a = self.tracker.process(nse("PTC Industries Limited", "Outcome of Board Meeting", "financial results", evening, "P.pdf"), evening)
        b = self.tracker.process(bse("PTC Industries Ltd", "Financial Results", morning, "p.pdf"), morning)
        self.assertEqual(a.event_id, b.event_id)
        event = self.store.events()[0]
        self.assertEqual(event["session_date"], "2026-11-20")
        self.assertEqual(event["in_market_hours"], 0)

    def test_backlog_flag(self):
        self.tracker.process(nse("BSE Limited", "Press Release", "", THU, "B.pdf"), THU, backlog=True)
        self.assertEqual(self.store.events()[0]["backlog"], 1)


class ScriptedSource(Source):
    def __init__(self, name, exchange, results):
        self.name, self.exchange = name, exchange
        super().__init__(interval=0.01)
        self._results = list(results)

    def parse(self, body):
        return Snapshot([])

    def poll(self):
        result = self._results.pop(0)
        result.source = self.name
        return result


def ok(at, filings):
    return PollResult(source="", started_at=at, finished_at=at, status=200, filings=filings)


class PollerAndRebuildTests(unittest.TestCase):
    def test_poller_links_sightings_and_rebuild_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            run_id = store.start_run(["nse_rss", "bse_rss"])
            n = ScriptedSource("nse_rss", "NSE", [
                ok(THU, []),
                ok(THU + timedelta(minutes=4), [nse("Axis Bank Limited", "Bagging/Receiving of orders/contracts", "", THU, "O.pdf")]),
            ])
            b = ScriptedSource("bse_rss", "BSE", [
                ok(THU, []),
                ok(THU + timedelta(minutes=1), [bse("AXIS Bank Ltd", "Receipt of order worth Rs 500 crore, bagging", THU, "o.pdf", "532215")]),
            ])
            printed = []
            poller = Poller(store, [n, b], run_id, tracker=EventTracker(store, master(), run_id),
                            on_new=lambda s, r, f: printed.extend((x.source, x.event_id, x.event_is_new) for x in f))
            for source in (n, b):
                poller.poll_once(source)
            poller.poll_once(b)
            poller.poll_once(n)
            self.assertEqual(printed, [("bse_rss", 1, True), ("nse_rss", 1, False)])
            linked = [(r["source"], r["event_id"], r["company_id"]) for r in store.sightings(run_id)]
            self.assertEqual(sorted(linked), [("bse_rss", 1, "INE238A01034"), ("nse_rss", 1, "INE238A01034")])

            summary = build_report(store, run_id)["events"]
            self.assertEqual(summary["both_exchanges"], 1)
            self.assertEqual(summary["first_on"], {"BSE": 1})
            self.assertEqual(summary["copies_absorbed"], 1)

            processed = rebuild_events(store, master())
            self.assertEqual(processed, 2)
            events = store.events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["first_source"], "bse_rss")
            store.close()

    def test_old_database_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite"
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, sources TEXT NOT NULL, settings TEXT);
                CREATE TABLE sightings (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, source TEXT NOT NULL,
                    exchange TEXT NOT NULL, source_id TEXT NOT NULL, match_key TEXT NOT NULL, company TEXT, company_key TEXT,
                    symbol TEXT, isin TEXT, subject TEXT, headline TEXT, attachment_url TEXT, published_at TEXT, filed_at TEXT,
                    detected_at TEXT NOT NULL, backlog INTEGER NOT NULL DEFAULT 0, UNIQUE (run_id, source, source_id));
                INSERT INTO runs (started_at, sources) VALUES ('2026-11-19T04:00:00+00:00', 'nse_rss');
                INSERT INTO sightings (run_id, source, exchange, source_id, match_key, company, company_key, subject, headline,
                    published_at, detected_at) VALUES (1, 'nse_rss', 'NSE', 'a', 'a.pdf', 'Axis Bank Limited', 'axisbank',
                    'Press Release', '', '2026-11-19T04:30:00+00:00', '2026-11-19T04:32:00+00:00');
            """)
            con.commit()
            con.close()
            store = Store(path)
            columns = {r["name"] for r in store._conn.execute("PRAGMA table_info(sightings)")}
            self.assertTrue({"company_id", "event_type", "event_id"} <= columns)
            self.assertEqual(rebuild_events(store, master()), 1)
            self.assertEqual(store.events()[0]["company_id"], "INE238A01034")
            store.close()


if __name__ == "__main__":
    unittest.main()
