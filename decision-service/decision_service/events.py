"""Groups copies of one real-world filing into a single event.

An event is keyed by company (ISIN when known) + event type + the trading session the
news affects. The first copy from any feed creates the event; later copies, from the
same or another feed, attach to it, so one event means one decision.

A filing seen again (NSE's .in and .com feeds carry the same items, and every restart
re-reads the day) is recognised exactly and goes to the event it already belongs to.
Its identity is the exchange plus the item's source_id: attachment file name and the
exchange's timestamp. The file name alone isn't enough, because BSE attaches one PDF to
dozens of mutual-fund schemes, one item per scheme. The matching rules below are only
for new copies.
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
# Copies are matched on exchange timestamps, which feed stalls don't change.
# - Same type: results merge across the whole session (a company files results once a
#   day); other types within an hour, so two separate updates on one day stay separate.
# - A copy whose type couldn't be read ("other") joins a same-company event within 30 min.
# - Other exchange: an NSE copy and a BSE copy of the same company within 30 minutes pair
#   up whatever their types, because the two copies' text often classifies differently.
#   The event must not already have a copy from that exchange.
# Among eligible events the closest wins; a type mismatch counts as 10 extra minutes, so
# type only breaks ties when a company files several things close together.
SESSION_WIDE_TYPES = {"results"}
SAME_TYPE_WINDOW = timedelta(minutes=60)
OTHER_MERGE_WINDOW = timedelta(minutes=30)
CROSS_EXCHANGE_WINDOW = timedelta(minutes=30)
TYPE_MISMATCH_PENALTY = timedelta(minutes=10)

# Checked in order; the first match wins. Matched against NSE's subject plus the headline
# (BSE's RSS has only a headline, which is often "As per file attached").
EVENT_RULES: list[tuple[str, re.Pattern]] = [
    (name, re.compile(pattern, re.I))
    for name, pattern in [
        # Filings that mention results but aren't them: call transcripts, call schedules,
        # newspaper copies, board-meeting notices, debt record dates.
        ("routine", r"transcript|earnings (conference )?call|conference call|con\.? ?call|concall"
                    r"|audio recording|video recording|recording of|link of recording|investors? (meet|conference)"
                    r"|analysts?/(institutional )?investors?|schedule of (analyst|investor)|newspaper|advertisement"
                    r"|regulation 60\b|regulation 60\s*\(|interest payment|payment of interest"
                    # A board-meeting notice is routine only when it isn't about a corporate action:
                    # "meeting on 27 Nov to consider a buyback / fund raise" can move the price.
                    r"|(intimation of (the )?board meeting|board meeting intimation|notice of (the )?board meeting"
                    r"|meeting of the board of directors)(?![\s\S]*(buy ?back|bonus|split|sub-?division|dividend|fund ?rais"
                    r"|rais(e|ing) (of )?funds?|rights (issue|basis)|issu(e|ance) of (equity|securities|shares|warrants)"
                    r"|private placement|\bqip\b|qualified institution|preferential|warrants|debentures|\bncds?\b|merger"
                    r"|amalgamation|demerger|acquisition|scheme of|delisting|capital reduction))"),
        ("results", r"financial results?|unaudited|audited (standalone|consolidated|results)|results? for the (quarter|half|year)"
                    r"|quarterly results|integrated filing- ?financial"),
        ("shareholding", r"regulation 29\b|regulation 29\s*\(|substantial acquisition of shares|regulation 7\s*\(2\)|\bsast\b"),
        ("merger_acquisition", r"acquisition|acquire|merger|amalgamation|demerger|scheme of arrangement|takeover|open offer|stake sale"),
        ("order_win", r"bagging|receiving of orders|work orders?|work of order|purchase orders?|supply orders?"
                      r"|letter of (award|intent)|\bloa\b|order (win|received|inflow)|orders?/contracts?|contract (award|win)"
                      r"|orders? (worth|valued at|aggregating|amounting)"
                      r"|(receipt|received|receives|bagged|bags|secured|secures) (of )?(an? |new |large |major |prestigious )*"
                      r"(work |purchase |supply |export |repeat )?orders? (from|worth|for|of)"
                      r"(?! (the )?(hon|court|tribunal|itat|nclt|nclat|sebi|income|gst|commissioner|dy\.|deputy|assistant|authority|registrar))"),
        ("agreement", r"\bagreements?\b|memorandum of understanding|\bmou\b|joint venture|strategic partnership"),
        ("fund_raise", r"\bqip\b|qualified institutions? placement|preferential (issue|allotment)|rights issue|fund ?rais"
                       r"|issue of (equity|warrants|non-convertible|debentures)|\bwarrants\b"),
        ("capital_return", r"dividend|buy ?back|bonus issue|stock split|sub-?division"),
        ("credit_rating", r"credit rating"),
        ("legal_regulatory", r"penalt|show cause|litigation|insolvency|\bnclt\b|sebi order|tax demand|search and seizure"
                             r"|\braid\b|fraud|default in|action\(s\) taken|orders? passed|key licen[cs]es|regulatory approval"
                             r"|tribunal|\bitat\b|hon'*ble (high |supreme )?court|demand notice|demand order"),
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
UPDATE = "update"
OPEN_STATUSES = {"new", ROUTINE}  # statuses a later copy may still change


def type_rank(event_type: str) -> int:
    """How much a type says: unreadable < routine < vague update < a specific type."""
    return {OTHER: 0, ROUTINE: 1, UPDATE: 2}.get(event_type, 3)


def merge_types(current: str, current_exchange: str | None, new: str, new_exchange: str) -> tuple[str, str | None]:
    """The event type when copies disagree, and which exchange's copy it came from.

    A more specific type wins, so a copy that looks routine never hides one that doesn't
    (NSE "Disclosure Under Regulation 51" vs BSE "Reaffirmation of Credit Rating"). Between
    two specific types NSE's wins: it is a category the company picked from NSE's list,
    while BSE's is classified from free text.
    """
    if new == current:
        return current, "NSE" if "NSE" in (current_exchange, new_exchange) else (current_exchange or new_exchange)
    if type_rank(new) > type_rank(current):
        return new, new_exchange
    if type_rank(new) == type_rank(current) and new_exchange == "NSE" and current_exchange != "NSE":
        return new, new_exchange
    return current, current_exchange


def is_equity_bse_code(code: str | None) -> bool:
    """BSE equity codes are six digits starting with 5; bonds and funds use 7xxxxx, 9xxxxx and longer codes."""
    return bool(code) and code.isdigit() and len(code) == 6 and code.startswith("5")


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
    repeat: bool = False  # the same filing as one already in the event


class EventTracker:
    def __init__(self, store, securities: SecurityMaster | None = None, run_id: int | None = None) -> None:
        self.store = store
        self.securities = securities or SecurityMaster()
        self.run_id = run_id
        self._lock = threading.Lock()  # one find-or-create at a time across poller threads
        # (exchange, source_id) -> event id, for filings handled by this tracker whose links
        # may not be written to sightings yet (same poll, or another feed's thread)
        self._linked: dict[tuple[str, str], int] = {}

    def process(self, filing: Filing, detected_at: datetime | None = None, backlog: bool = False) -> EventOutcome:
        detected_at = detected_at or utcnow()
        company_id, security = self.securities.resolve(filing)
        event_type = classify(filing.subject, filing.headline)
        news_time = filing.published_at or detected_at
        session, in_hours = trading_session(news_time)

        identity = (filing.exchange, filing.source_id)
        with self._lock:
            known = self._known_event(identity, company_id)
            match = None
            if known is None:
                candidates = self.store.find_events(company_id, session.isoformat())
                match = self._pick(candidates, event_type, news_time, filing.exchange)
            if known is not None:
                outcome = self._attach(known, filing, event_type, news_time, detected_at, repeat=True)
            elif match is None:
                status = ROUTINE if event_type == ROUTINE else "new"
                event_id = self.store.create_event(
                    company_id=company_id,
                    isin=security.isin if security else (filing.isin or None),
                    nse_symbol=(security.nse_symbol if security else None) or (filing.symbol if filing.exchange == "NSE" else None),
                    bse_code=(security.bse_code if security else None) or (filing.symbol if filing.exchange == "BSE" else None),
                    company=filing.company,
                    event_type=event_type,
                    type_exchange=filing.exchange,
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
            self._linked[identity] = outcome.event_id

        filing.company_id = company_id
        filing.event_id = outcome.event_id
        filing.event_type = outcome.event_type
        filing.event_is_new = outcome.is_new
        filing.event_repeat = outcome.repeat
        return outcome

    def _known_event(self, identity: tuple[str, str], company_id: str):
        """The event this exact filing already belongs to, if any (this run or an earlier one)."""
        event_id = self._linked.get(identity)
        row = self.store.get_event(event_id) if event_id is not None else self.store.linked_event(*identity)
        if row is None:
            return None
        # A different company on the same item would be a coincidence: leave it to the rules.
        # An item that lost its company name (BSE sometimes lists it without one) still matches.
        nameless = company_id.startswith(("bse:", "nse:", "unknown:"))
        return row if row["company_id"] == company_id or nameless else None

    @staticmethod
    def _pick(candidates, event_type: str, news_time: datetime, exchange: str):
        best, best_score = None, None
        for row in candidates:
            if not row["news_time"]:
                continue
            gap = abs(from_iso(row["news_time"]) - news_time)
            row_type = row["event_type"]
            other_exchange = exchange not in set(filter(None, (row["exchanges"] or "").split(",")))
            if row_type == event_type and (event_type in SESSION_WIDE_TYPES or gap <= SAME_TYPE_WINDOW):
                score = gap
            elif OTHER in (row_type, event_type) and gap <= OTHER_MERGE_WINDOW:
                score = gap
            elif other_exchange and gap <= CROSS_EXCHANGE_WINDOW:
                score = gap + TYPE_MISMATCH_PENALTY
            else:
                continue
            if best_score is None or score < best_score:
                best, best_score = row, score
        return best

    def _attach(self, row, filing: Filing, event_type: str, news_time: datetime, detected_at: datetime,
                repeat: bool = False) -> EventOutcome:
        """Add a copy to an event. A repeat (the same filing again) is recorded as another
        source but isn't a new copy, and its text was already taken into account."""
        changes: dict[str, object] = {} if repeat else {"copies": row["copies"] + 1}
        sources = set(filter(None, (row["sources"] or "").split(",")))
        exchanges = set(filter(None, (row["exchanges"] or "").split(",")))
        if filing.source not in sources:
            changes["sources"] = ",".join(sorted(sources | {filing.source}))
        if filing.exchange not in exchanges:
            changes["exchanges"] = ",".join(sorted(exchanges | {filing.exchange}))
        current_type, status = row["event_type"], row["status"]
        if not repeat:
            merged, type_exchange = merge_types(current_type, row["type_exchange"], event_type, filing.exchange)
            if merged != current_type or type_exchange != row["type_exchange"]:
                changes["event_type"], changes["type_exchange"] = merged, type_exchange
                current_type = merged
            if status in OPEN_STATUSES:  # once a decision has started, a late copy doesn't reopen it
                wanted = ROUTINE if current_type == ROUTINE else "new"
                if wanted != status:
                    changes["status"] = status = wanted
        if row["news_time"] is None or news_time < from_iso(row["news_time"]):
            changes["news_time"] = to_iso(news_time)
        if filing.exchange == "NSE" and not row["nse_symbol"] and filing.symbol:
            changes["nse_symbol"] = filing.symbol
        if filing.exchange == "BSE" and filing.symbol and not is_equity_bse_code(row["bse_code"]):
            # Keep the company's equity code for price lookups; a bond or fund code only
            # stands in until one turns up.
            if not row["bse_code"] or is_equity_bse_code(filing.symbol):
                changes["bse_code"] = filing.symbol
        if changes:
            self.store.update_event(row["id"], **changes)
        return EventOutcome(
            row["id"], row["company_id"], current_type, False, status, row["first_source"], from_iso(row["first_seen_at"]),
            repeat,
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
    """Recreate events from stored sightings, in the order they were detected.

    With run_id, that run and every later one are rebuilt (later runs' sightings can belong
    to events that run created); earlier runs are left as they are.
    """
    store.clear_events(run_id)
    tracker = EventTracker(store, securities)
    run_ids = [row["id"] for row in store.runs() if run_id is None or row["id"] >= run_id]
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
