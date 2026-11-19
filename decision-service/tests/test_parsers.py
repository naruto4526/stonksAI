import json
import unittest
from datetime import datetime

from decision_service.models import attachment_key, company_key
from decision_service.sources import SOURCES
from decision_service.timeutil import IST, parse_exchange_dt, parse_iso_ist, parse_rfc822

from .helpers import fixture


class NseRssTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = SOURCES["nse_rss"]().parse(fixture("nse_rss.xml"))

    def test_reads_every_item(self):
        self.assertEqual(len(self.snapshot.filings), 5)

    def test_first_item_fields(self):
        filing = self.snapshot.filings[0]
        self.assertEqual(filing.exchange, "NSE")
        self.assertEqual(filing.company, "Bright Outdoor Media Limited")
        self.assertEqual(filing.subject, "Outcome of Board Meeting")
        self.assertTrue(filing.headline.startswith("Bright Outdoor Media Limited has submitted"))
        self.assertEqual(filing.published_at, datetime(2026, 11, 14, 19, 58, 0, tzinfo=IST))
        self.assertEqual(filing.filed_at, datetime(2026, 11, 14, 19, 57, 37, tzinfo=IST))
        self.assertIsNone(filing.symbol)  # the file-name prefix is not a reliable symbol
        self.assertEqual(filing.match_key, "brightoutdoor_14112026195737_bm_outcome_bright_for_14-11-2026_signed.pdf")

    def test_build_time_is_ist(self):
        self.assertEqual(self.snapshot.build_time, datetime(2026, 11, 14, 19, 58, 42, tzinfo=IST))

    def test_xbrl_item(self):
        filing = self.snapshot.filings[3]
        self.assertIsNone(filing.filed_at)
        self.assertEqual(filing.subject, "Agreements,Contracts,Arrangements,MOU-XBRL")

    def test_legacy_source_parses_the_same_feed(self):
        legacy = SOURCES["nse_rss_legacy"]().parse(fixture("nse_rss.xml"))
        self.assertEqual([f.match_key for f in legacy.filings], [f.match_key for f in self.snapshot.filings])
        self.assertTrue(all(f.source == "nse_rss_legacy" for f in legacy.filings))

    def test_malformed_xml_falls_back_to_regex(self):
        broken = fixture("nse_rss.xml").replace(b"Bright Outdoor Media Limited has", b"Bright & Outdoor has", 1)
        snapshot = SOURCES["nse_rss"]().parse(broken)
        self.assertEqual(len(snapshot.filings), 5)
        self.assertEqual(snapshot.filings[1].company, "Kontor Space Limited")


class BseRssTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = SOURCES["bse_rss"]().parse(fixture("bse_rss.xml"))

    def test_items(self):
        self.assertEqual(len(self.snapshot.filings), 4)
        filing = self.snapshot.filings[0]
        self.assertEqual(filing.company, "Tandhan Industries Ltd")
        self.assertEqual(filing.symbol, "512062")
        self.assertEqual(filing.published_at, datetime(2026, 11, 14, 22, 32, 23, tzinfo=IST))
        self.assertIsNone(filing.subject)
        self.assertEqual(filing.match_key, "7d0fa5f5-5e52-45cd-82e7-398f7b0a3d6d.pdf")
        self.assertEqual(self.snapshot.filings[3].headline, "Investor Presentation for Q2 &H1FY27")

    def test_build_time_labelled_gmt_is_read_as_ist(self):
        self.assertEqual(self.snapshot.build_time, datetime(2026, 11, 14, 22, 33, 46, tzinfo=IST))


class NseJsonTests(unittest.TestCase):
    def test_list_payload(self):
        filings = SOURCES["nse_json"]().parse(fixture("nse_json.json")).filings
        self.assertEqual(len(filings), 2)
        filing = filings[0]
        self.assertEqual(filing.symbol, "WILLAMAGOR")
        self.assertEqual(filing.isin, "INE210A01017")
        self.assertEqual(filing.subject, "Copy of Newspaper Publication")
        self.assertEqual(filing.published_at, datetime(2026, 11, 14, 19, 34, 56, tzinfo=IST))
        self.assertEqual(filing.filed_at, datetime(2026, 11, 14, 19, 34, 55, tzinfo=IST))
        self.assertEqual(filing.source_id, "106861159")
        self.assertEqual(filings[1].symbol, "PTCIL")

    def test_wrapped_payload(self):
        wrapped = json.dumps({"data": json.loads(fixture("nse_json.json"))}).encode()
        self.assertEqual(len(SOURCES["nse_json"]().parse(wrapped).filings), 2)


class BseApiTests(unittest.TestCase):
    def setUp(self):
        self.filings = SOURCES["bse_api"]().parse(fixture("bse_api.json")).filings

    def test_fields(self):
        filing = self.filings[0]
        self.assertEqual(filing.company, "Rajasthan Tube Manufacturing Company Ltd")
        self.assertEqual(filing.symbol, "530253")
        self.assertEqual(filing.subject, "Result / Financial Results")
        self.assertEqual(filing.published_at, datetime(2026, 11, 14, 22, 33, 1, 907000, tzinfo=IST))
        self.assertTrue(filing.attachment_url.endswith("/AttachLive/d6eaad00-d730-4aeb-88cf-b1a8f985dfea.pdf"))

    def test_two_digit_fraction(self):
        self.assertEqual(self.filings[1].published_at, datetime(2026, 11, 14, 22, 32, 23, 520000, tzinfo=IST))

    def test_matches_the_rss_copy_of_the_same_filing(self):
        rss = SOURCES["bse_rss"]().parse(fixture("bse_rss.xml")).filings
        self.assertEqual(self.filings[1].match_key, rss[0].match_key)


class TimeAndKeyTests(unittest.TestCase):
    def test_exchange_dates(self):
        self.assertEqual(parse_exchange_dt("14-Nov-2026 19:58:00"), datetime(2026, 11, 14, 19, 58, tzinfo=IST))
        self.assertEqual(parse_exchange_dt("4-nov-2026 09:05"), datetime(2026, 11, 4, 9, 5, tzinfo=IST))
        self.assertIsNone(parse_exchange_dt("2026-11-14 19:58:00"))
        self.assertIsNone(parse_exchange_dt("31-Feb-2026 10:00:00"))
        self.assertIsNone(parse_exchange_dt(None))

    def test_iso_dates(self):
        self.assertEqual(parse_iso_ist("2026-11-14T22:33:01"), datetime(2026, 11, 14, 22, 33, 1, tzinfo=IST))
        self.assertIsNone(parse_iso_ist("not a date"))

    def test_rfc822(self):
        self.assertEqual(
            parse_rfc822("Sat, 14 Nov 2026 14:28:44 GMT").astimezone(IST),
            datetime(2026, 11, 14, 19, 58, 44, tzinfo=IST),
        )
        self.assertEqual(
            parse_rfc822("Sat, 14 Nov 2026 22:33:46 GMT", assume_ist=True),
            datetime(2026, 11, 14, 22, 33, 46, tzinfo=IST),
        )

    def test_company_key(self):
        self.assertEqual(company_key("Shanti Overseas (India) Limited"), company_key("Shanti Overseas (India) Ltd"))
        self.assertEqual(company_key("Tandhan Industries Ltd (512062)"), "tandhanindustries")
        self.assertEqual(company_key("Williamson Magor & Company Limited"), "williamsonmagorandcompany")

    def test_attachment_key_ignores_host(self):
        self.assertEqual(
            attachment_key("https://nsearchives.nseindia.com/corporate/A_1.pdf"),
            attachment_key("https://nsearchives.nseindia.in/corporate/A_1.pdf?x=1"),
        )


if __name__ == "__main__":
    unittest.main()
