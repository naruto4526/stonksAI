"""Groups copies of one real-world filing into a single event.

An event is keyed by company (ISIN when known) + event type + the trading session the
news affects. The first copy from any feed creates the event; later copies, from the
same or another feed, attach to it, so one event means one decision.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .models import Filing
from .securities import SecurityMaster
from .timeutil import IST, from_iso, to_iso, utcnow

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
# Copies are matched on exchange timestamps, which feed stalls don't change. Results
# merge across the whole session (a company files results once a day); other types only
# within an hour, so two separate updates on one day stay separate. A copy whose type
# couldn't be read ("other") joins a same-company event within 30 minutes.
SESSION_WIDE_TYPES = {"results"}
SAME_TYPE_WINDOW = timedelta(minutes=60)
OTHER_MERGE_WINDOW = timedelta(minutes=30)

# Checked in order; the first match wins. Matched against NSE's subject plus the headline
# (BSE's RSS has only a headline, which is often "As per file attached").
EVENT_RULES: list[tuple[str, re.Pattern]] = [
    (name, re.compile(pattern, re.I))
    for name, pattern in [
        ("results", r"financial results?|unaudited|audited (standalone|consolidated|results)|results? for the (quarter|half|year)"
                    r"|quarterly results|integrated filing- ?financial"),
        ("shareholding", r"regulation 29\b|regulation 29\s*\(|substantial acquisition of shares|regulation 7\s*\(2\)|\bsast\b"),
        ("merger_acquisition", r"acquisition|acquire|merger|amalgamation|demerger|scheme of arrangement|takeover|open offer|stake sale"),
        ("order_win", r"bagging|receiving of orders|work order|letter of (award|intent)|\bloa\b|order (win|received|inflow)"
                      r"|orders?/contracts?|contract (award|win)"),
        ("agreement", r"\bagreements?\b|memorandum of understanding|\bmou\b|joint venture|strategic partnership"),
        ("fund_raise", r"\bqip\b|qualified institutions? placement|preferential (issue|allotment)|rights issue|fund ?rais"
                       r"|issue of (equity|warrants|non-convertible|debentures)|\bwarrants\b"),
        ("capital_return", r"dividend|buy ?back|bonus issue|stock split|sub-?division"),
        ("credit_rating", r"credit rating"),
        ("legal_regulatory", r"penalt|show cause|litigation|insolvency|\bnclt\b|sebi order|tax demand|search and seizure"
                             r"|\braid\b|fraud|default in|action\(s\) taken|orders? passed|key licen[cs]es|regulatory approval"),
        ("management", r"resignation|appointment|cessation|change in (directors|management|kmp)|managing director"
                       r"|chief executive|\bceo\b|\bcfo\b"),
        ("routine", r"newspaper|trading window|analyst|investor meet|con\.? ?call|conference call|schedule of"
                    r"|declaration of nav|\bnav\b|loss of share|duplicate share|share certificate|74 ?\(5\)"
                    r"|compliance|shareholders'? meeting|postal ballot|e-voting|annual general meeting|\bagm\b"
                    r"|record date|redemption|payment of interest|\besop|\besos|\besps|spurt in volume"
                    r"|intimation of board meeting|board meeting intimation|monitoring agency|statement of deviation|xbrl"
                    r"|no dues|\bnoc\b|trusteeship|debenture trustee|portfolio|expense ratio|asset liability"
                    r"|regulation (5[0-9]|60)\b|fv reduction|interest and principal"),
        ("update", r"press release|investor presentation|general updates?|\bupdates?\b|clarification"),
    ]
]
ROUTINE = "routine"
OTHER = "other"


def classify(subject: str | None, headline: str | None) -> str:
    text = f"{subject or ''} | {headline or ''}"
    for name, pattern in EVENT_RULES:
        if pattern.search(text):
            return name
    return OTHER


def trading_session(moment: datetime) -> tuple[date, bool]:
    """The session the news can first be traded in, and whether it arrived during market hours.

    Weekends roll forward to Monday. Exchange holidays aren't modelled yet.
    """
    local = moment.astimezone(IST)
    day, clock = local.date(), local.time()
    if day.weekday() < 5 and MARKET_OPEN <= clock <= MARKET_CLOSE:
        return day, True
    if day.weekday() < 5 and clock < MARKET_OPEN:
        return day, False
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day, False


@dataclass
class EventOutcome:
    event_id: int
    company_id: str
    event_type: str
    is_new: bool
    status: str
    first_source: str
    first_seen_at: datetime | None


class EventTracker:
    def __init__(self, store, securities: SecurityMaster | None = None, run_id: int | None = None) -> None:
        self.store = store
        self.securities = securities or SecurityMaster()
        self.run_id = run_id
        self._lock = threading.Lock()  # one find-or-create at a time across poller threads

    def process(self, filing: Filing, detected_at: datetime | None = None, backlog: bool = False) -> EventOutcome:
        detected_at = detected_at or utcnow()
        company_id, security = self.securities.resolve(filing)
        event_type = classify(filing.subject, filing.headline)
        news_time = filing.published_at or detected_at
        session, in_hours = trading_session(news_time)

        with self._lock:
            candidates = self.store.find_events(company_id, session.isoformat())
            match = self._pick(candidates, event_type, news_time)
            if match is None:
                status = ROUTINE if event_type == ROUTINE else "new"
                event_id = self.store.create_event(
                    company_id=company_id,
                    isin=security.isin if security else (filing.isin or None),
                    nse_symbol=(security.nse_symbol if security else None) or (filing.symbol if filing.exchange == "NSE" else None),
                    bse_code=(security.bse_code if security else None) or (filing.symbol if filing.exchange == "BSE" else None),
                    company=filing.company,
                    event_type=event_type,
                    session_date=session.isoformat(),
                    in_market_hours=int(in_hours),
                    news_time=to_iso(news_time),
                    first_seen_at=to_iso(detected_at),
                    first_source=filing.source,
                    first_run_id=self.run_id,
                    sources=filing.source,
                    exchanges=filing.exchange,
                    status=status,
                    backlog=int(backlog),
                )
                outcome = EventOutcome(event_id, company_id, event_type, True, status, filing.source, detected_at)
            else:
                outcome = self._attach(match, filing, event_type, news_time, detected_at)

        filing.company_id = company_id
        filing.event_id = outcome.event_id
        filing.event_type = outcome.event_type
        filing.event_is_new = outcome.is_new
        return outcome

    @staticmethod
    def _pick(candidates, event_type: str, news_time: datetime):
        def gap(row):
            return abs((from_iso(row["news_time"]) - news_time).total_seconds()) if row["news_time"] else float("inf")

        window = None if event_type in SESSION_WIDE_TYPES else SAME_TYPE_WINDOW.total_seconds()
        same = [
            row for row in candidates
            if row["event_type"] == event_type and (window is None or gap(row) <= window)
        ]
        if same:
            return min(same, key=gap)
        loose = [
            row for row in candidates
            if (row["event_type"] == OTHER or event_type == OTHER) and gap(row) <= OTHER_MERGE_WINDOW.total_seconds()
        ]
        return min(loose, key=gap) if loose else None

    def _attach(self, row, filing: Filing, event_type: str, news_time: datetime, detected_at: datetime) -> EventOutcome:
        changes: dict[str, object] = {"copies": row["copies"] + 1}
        sources = set(filter(None, (row["sources"] or "").split(",")))
        exchanges = set(filter(None, (row["exchanges"] or "").split(",")))
        changes["sources"] = ",".join(sorted(sources | {filing.source}))
        changes["exchanges"] = ",".join(sorted(exchanges | {filing.exchange}))
        current_type, status = row["event_type"], row["status"]
        if current_type == OTHER and event_type != OTHER:  # a copy with a readable subject names the event
            changes["event_type"] = current_type = event_type
            if status == "new" and event_type == ROUTINE:
                changes["status"] = status = ROUTINE
        if row["news_time"] is None or news_time < from_iso(row["news_time"]):
            changes["news_time"] = to_iso(news_time)
        if filing.exchange == "NSE" and not row["nse_symbol"] and filing.symbol:
            changes["nse_symbol"] = filing.symbol
        if filing.exchange == "BSE" and not row["bse_code"] and filing.symbol:
            changes["bse_code"] = filing.symbol
        self.store.update_event(row["id"], **changes)
        return EventOutcome(
            row["id"], row["company_id"], current_type, False, status, row["first_source"], from_iso(row["first_seen_at"])
        )


def filing_from_row(row) -> Filing:
    """Rebuild a Filing from a stored sightings row."""
    return Filing(
        source=row["source"],
        exchange=row["exchange"],
        source_id=row["source_id"],
        company=row["company"] or "",
        published_at=from_iso(row["published_at"]),
        filed_at=from_iso(row["filed_at"]),
        symbol=row["symbol"],
        isin=row["isin"],
        subject=row["subject"],
        headline=row["headline"],
        attachment_url=row["attachment_url"],
    )


def rebuild_events(store, securities: SecurityMaster | None = None, run_id: int | None = None) -> int:
    """Recreate events from stored sightings, in the order they were detected."""
    store.clear_events(run_id)
    tracker = EventTracker(store, securities)
    run_ids = [run_id] if run_id is not None else [row["id"] for row in store.runs()]
    processed = 0
    for current in run_ids:
        tracker.run_id = current
        rows = sorted(store.sightings(current), key=lambda r: (r["detected_at"], r["published_at"] or "", r["id"]))
        links = []
        for row in rows:
            filing = filing_from_row(row)
            outcome = tracker.process(filing, from_iso(row["detected_at"]), backlog=bool(row["backlog"]))
            links.append((row["source"], row["source_id"], outcome.event_id, outcome.company_id, outcome.event_type))
            processed += 1
        store.set_sighting_events(current, links)
    return processed
