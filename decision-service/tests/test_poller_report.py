import unittest
from datetime import datetime, timedelta

from decision_service.models import Filing
from decision_service.poller import Poller
from decision_service.report import build_report, format_report, percentile
from decision_service.sources.base import PollResult, Snapshot, Source
from decision_service.store import Store
from decision_service.timeutil import IST

T0 = datetime(2026, 11, 16, 10, 0, 0, tzinfo=IST)


def filing(source, exchange, key, published, company="Acme Limited"):
    return Filing(
        source=source,
        exchange=exchange,
        source_id=key,
        company=company,
        published_at=published,
        attachment_url=f"https://example.test/{key}.pdf",
    )


class ScriptedSource(Source):
    """Returns pre-built poll results instead of fetching."""

    def __init__(self, name, exchange, results):
        self.name = name
        self.exchange = exchange
        super().__init__(interval=0.01)
        self._results = list(results)

    def parse(self, body):
        return Snapshot([])

    def poll(self):
        result = self._results.pop(0)
        result.source = self.name
        return result


def ok(at, filings, build=None):
    return PollResult(source="", started_at=at, finished_at=at + timedelta(milliseconds=200),
                      status=200, elapsed_ms=200.0, wire_bytes=1000, body_bytes=5000,
                      filings=filings, build_time=build)


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_baseline_then_new_filings(self):
        old = filing("a", "NSE", "old", T0 - timedelta(hours=1))
        new = filing("a", "NSE", "new", T0 + timedelta(seconds=58))
        source = ScriptedSource("a", "NSE", [
            PollResult(source="", started_at=T0, finished_at=T0, status=None, error="URLError: offline"),
            ok(T0 + timedelta(seconds=10), [old]),
            PollResult(source="", started_at=T0 + timedelta(seconds=20), finished_at=T0 + timedelta(seconds=20), status=304, not_modified=True),
            ok(T0 + timedelta(seconds=60), [new, old]),
        ])
        seen_callbacks = []
        run_id = self.store.start_run(["a"])
        poller = Poller(self.store, [source], run_id, on_new=lambda s, r, f: seen_callbacks.append([x.source_id for x in f]))
        for _ in range(4):
            poller.poll_once(source)

        rows = {row["source_id"]: row for row in self.store.sightings(run_id)}
        self.assertEqual(rows["old"]["backlog"], 1)  # first successful poll is the baseline
        self.assertEqual(rows["new"]["backlog"], 0)
        self.assertEqual(seen_callbacks, [["new"]])
        stats = poller.stats["a"]
        self.assertEqual((stats.polls, stats.errors, stats.not_modified, stats.new_filings), (4, 1, 1, 1))

    def test_repeated_items_in_one_poll_count_once(self):
        dup = filing("a", "BSE", "axis", T0)
        source = ScriptedSource("a", "BSE", [ok(T0, []), ok(T0 + timedelta(seconds=20), [dup, dup, dup])])
        printed = []
        run_id = self.store.start_run(["a"])
        poller = Poller(self.store, [source], run_id, on_new=lambda s, r, f: printed.extend(f))
        poller.poll_once(source)
        poller.poll_once(source)
        self.assertEqual(len(printed), 1)
        self.assertEqual(poller.stats["a"].new_filings, 1)

    def test_backoff(self):
        source = ScriptedSource("a", "NSE", [])
        source.interval = 5
        poller = Poller(self.store, [source], 1, jitter=0.0, max_backoff=60)
        self.assertEqual(poller.next_delay(source, PollResult("a", T0, status=200), 0), 5)
        self.assertEqual(poller.next_delay(source, PollResult("a", T0, error="x"), 2), 20)
        self.assertEqual(poller.next_delay(source, PollResult("a", T0, error="x"), 6), 60)
        self.assertEqual(poller.next_delay(source, PollResult("a", T0, status=429, error="HTTP 429"), 1), 60)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.run_id = self.store.start_run(["fast", "slow", "bse"])
        # The run "started" before T0 so the cross-exchange window covers the filings.
        self.store._conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", ((T0 - timedelta(minutes=5)).isoformat(), self.run_id))

        x_pub = T0 + timedelta(seconds=100)
        y_pub = T0 + timedelta(seconds=200)
        fast = ScriptedSource("fast", "NSE", [
            ok(T0, []),
            ok(T0 + timedelta(seconds=102), [filing("fast", "NSE", "x", x_pub)]),
            ok(T0 + timedelta(seconds=203), [filing("fast", "NSE", "y", y_pub), filing("fast", "NSE", "x", x_pub)]),
            ok(T0 + timedelta(seconds=1500), []),
        ])
        slow = ScriptedSource("slow", "NSE", [
            ok(T0, [], build=T0),
            ok(T0 + timedelta(seconds=160), [filing("slow", "NSE", "x", x_pub)], build=T0 + timedelta(seconds=150)),
            ok(T0 + timedelta(seconds=1500), [], build=T0 + timedelta(seconds=450)),
        ])
        bse = ScriptedSource("bse", "BSE", [
            ok(T0, []),
            ok(T0 + timedelta(seconds=140), [filing("bse", "BSE", "bx", x_pub + timedelta(seconds=30), company="ACME Ltd")]),
        ])
        self.sources = [fast, slow, bse]
        poller = Poller(self.store, self.sources, self.run_id)
        for source in self.sources:
            while source._results:
                poller.poll_once(source)
        self.report = build_report(self.store, self.run_id, grace_s=600)

    def tearDown(self):
        self.store.close()

    def test_lag(self):
        fast = self.report["sources"]["fast"]
        self.assertEqual(fast["new_filings"], 2)
        self.assertAlmostEqual(fast["lag_p50_s"], 2.7, places=1)  # 2.2 s and 3.2 s
        self.assertAlmostEqual(self.report["sources"]["slow"]["lag_max_s"], 60.2, places=1)

    def test_pair_comparison(self):
        pair = next(p for p in self.report["pairs"] if p["a"] == "fast" and p["b"] == "slow")
        self.assertEqual(pair["both"], 1)
        self.assertEqual(pair["a_first"], 1)
        self.assertAlmostEqual(pair["median_b_minus_a_s"], 58.0, places=1)
        self.assertEqual(pair["missed_by_b"], 1)  # y never reached "slow"
        self.assertEqual(pair["missed_by_a"], 0)
        self.assertFalse(any(p["a"] == "bse" or p["b"] == "bse" for p in self.report["pairs"]))

    def test_behind_peers_and_rebuilds(self):
        slow = self.report["sources"]["slow"]
        self.assertAlmostEqual(slow["max_behind_peers_s"], 100.0, places=1)  # y (200 s) vs x (100 s)
        self.assertEqual(slow["rebuilds_seen"], 3)
        self.assertAlmostEqual(slow["median_rebuild_gap_s"], 225.0, places=1)

    def test_cross_exchange(self):
        cross = self.report["cross_exchange"]
        self.assertEqual(cross["matched"], 1)
        self.assertEqual(cross["nse_first"], 1)
        self.assertAlmostEqual(cross["median_bse_minus_nse_s"], 30.0, places=1)

    def test_text_report(self):
        text = format_report(self.report)
        for expected in ("PER SOURCE", "fast vs slow", "NSE VS BSE", "rebuilt every"):
            self.assertIn(expected, text)

    def test_percentile(self):
        self.assertIsNone(percentile([], 50))
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(percentile([5], 90), 5)


if __name__ == "__main__":
    unittest.main()
