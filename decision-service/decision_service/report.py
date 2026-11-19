"""Turns a recorded run into per-source and cross-source comparisons."""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta
from itertools import combinations
from statistics import mean

from .sources import SOURCES
from .store import Store
from .timeutil import fmt_ist, from_iso

TIE_SECONDS = 0.5


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in 0-100."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * q / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _seconds(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


def _exchange(name: str, sightings) -> str:
    if name in SOURCES:
        return SOURCES[name].exchange
    for row in sightings:
        if row["source"] == name:
            return row["exchange"]
    return "?"


def _behind_peers(names: list[str], exchanges: dict[str, str], polls) -> dict[str, list[float]]:
    """At each poll: how far this source's newest filing trails the newest any same-exchange peer has shown."""
    newest: dict[str, datetime] = {}
    behind: dict[str, list[float]] = {name: [] for name in names}
    for poll in polls:  # already ordered by start time
        name = poll["source"]
        if name not in behind or poll["error"] is not None:
            continue
        if poll["newest_published_at"]:
            value = from_iso(poll["newest_published_at"])
            if name not in newest or value > newest[name]:
                newest[name] = value
        if name not in newest:
            continue
        peers = [newest[other] for other in names if other != name and exchanges[other] == exchanges[name] and other in newest]
        if peers:
            behind[name].append(max(0.0, _seconds(max(peers), newest[name])))
    return behind


def _source_stats(name, polls, sightings, behind, session_hours) -> dict:
    mine = [p for p in polls if p["source"] == name]
    ok = [p for p in mine if p["error"] is None]
    starts = sorted(from_iso(p["started_at"]) for p in mine)
    gaps = [_seconds(b, a) for a, b in zip(starts, starts[1:])]
    interval = percentile(gaps, 50)
    wire = [p["wire_bytes"] or 0 for p in ok]
    kb_per_poll = mean(wire) / 1000.0 if wire else None
    mb_per_session = (
        kb_per_poll * (session_hours * 3600.0 / interval) / 1000.0 if kb_per_poll is not None and interval else None
    )
    live = [s for s in sightings if s["source"] == name and not s["backlog"]]
    lags = [_seconds(from_iso(s["detected_at"]), from_iso(s["published_at"])) for s in live if s["published_at"]]
    upload_lags = [_seconds(from_iso(s["detected_at"]), from_iso(s["filed_at"])) for s in live if s["filed_at"]]
    builds = sorted({from_iso(p["build_time"]) for p in ok if p["build_time"]})
    build_gaps = [_seconds(b, a) for a, b in zip(builds, builds[1:])]
    errors = Counter(p["error"] for p in mine if p["error"])
    return {
        "exchange": SOURCES[name].exchange if name in SOURCES else "?",
        "official": SOURCES[name].official if name in SOURCES else None,
        "polls": len(mine),
        "errors": len(mine) - len(ok),
        "not_modified_share": (sum(p["not_modified"] for p in ok) / len(ok)) if ok else None,
        "median_interval_s": interval,
        "median_response_ms": percentile([p["elapsed_ms"] for p in ok if p["elapsed_ms"] is not None], 50),
        "kb_per_poll": kb_per_poll,
        "mb_per_session": mb_per_session,
        "new_filings": len(live),
        "lag_p50_s": percentile(lags, 50),
        "lag_p90_s": percentile(lags, 90),
        "lag_max_s": max(lags) if lags else None,
        "lag_from_upload_p50_s": percentile(upload_lags, 50),
        "max_behind_peers_s": max(behind[name]) if behind[name] else None,
        "p90_behind_peers_s": percentile(behind[name], 90),
        "rebuilds_seen": len(builds),
        "median_rebuild_gap_s": percentile(build_gaps, 50),
        "top_errors": errors.most_common(3),
    }


def _pair_stats(a: str, b: str, sightings, run_end: datetime, grace_s: float) -> dict:
    first_seen: dict[str, dict[str, object]] = {a: {}, b: {}}
    for row in sightings:
        if row["source"] in first_seen and row["match_key"] not in first_seen[row["source"]]:
            first_seen[row["source"]][row["match_key"]] = row
    seen_a, seen_b = first_seen[a], first_seen[b]
    stats = {"a": a, "b": b, "both": 0, "a_first": 0, "b_first": 0, "ties": 0, "missed_by_a": 0, "missed_by_b": 0}
    diffs: list[float] = []
    for key in set(seen_a) | set(seen_b):
        row_a, row_b = seen_a.get(key), seen_b.get(key)
        if row_a and row_b:
            if row_a["backlog"] and row_b["backlog"]:
                continue
            stats["both"] += 1
            if row_a["backlog"] or row_b["backlog"]:
                stats["a_first" if row_a["backlog"] else "b_first"] += 1
                continue
            diff = _seconds(from_iso(row_b["detected_at"]), from_iso(row_a["detected_at"]))
            diffs.append(diff)
            if abs(diff) < TIE_SECONDS:
                stats["ties"] += 1
            elif diff > 0:
                stats["a_first"] += 1
            else:
                stats["b_first"] += 1
        else:
            row = row_a or row_b
            if row["backlog"] or _seconds(run_end, from_iso(row["detected_at"])) < grace_s:
                continue
            stats["missed_by_b" if row_a else "missed_by_a"] += 1
    stats["median_b_minus_a_s"] = percentile(diffs, 50)
    stats["max_abs_diff_s"] = max((abs(d) for d in diffs), default=None)
    return stats


def _cross_exchange(sightings, window_s: float) -> dict | None:
    """Pair NSE and BSE copies of a filing by company and exchange timestamp.

    Uses exchange timestamps, so filings already in the feeds when the run started
    count too. Only the span both exchanges' feeds cover is compared.
    """
    earliest: dict[str, dict[str, object]] = {"NSE": {}, "BSE": {}}
    for row in sightings:
        if row["exchange"] not in earliest or not row["published_at"] or not row["company_key"]:
            continue
        bucket = earliest[row["exchange"]]
        current = bucket.get(row["match_key"])
        if current is None or row["published_at"] < current["published_at"]:
            bucket[row["match_key"]] = row
    if not earliest["NSE"] or not earliest["BSE"]:
        return None
    # Start where both feeds have data, less one matching window so a copy just
    # before the other feed's earliest item can still be paired.
    latest_first = max(from_iso(min(r["published_at"] for r in bucket.values())) for bucket in earliest.values())
    span_start = latest_first - timedelta(seconds=window_s)
    for exchange in earliest:
        earliest[exchange] = {
            k: r for k, r in earliest[exchange].items() if from_iso(r["published_at"]) >= span_start
        }

    bse_by_company: dict[str, list] = {}
    for row in earliest["BSE"].values():
        bse_by_company.setdefault(row["company_key"], []).append(row)
    used: set[int] = set()
    diffs: list[float] = []
    for nse in sorted(earliest["NSE"].values(), key=lambda r: r["published_at"]):
        nse_time = from_iso(nse["published_at"])
        best, best_gap = None, None
        for bse in bse_by_company.get(nse["company_key"], []):
            if bse["id"] in used:
                continue
            gap = _seconds(from_iso(bse["published_at"]), nse_time)
            if abs(gap) <= window_s and (best_gap is None or abs(gap) < abs(best_gap)):
                best, best_gap = bse, gap
        if best is not None:
            used.add(best["id"])
            diffs.append(best_gap)
    return {
        "nse_filings": len(earliest["NSE"]),
        "bse_filings": len(earliest["BSE"]),
        "matched": len(diffs),
        "nse_first": sum(1 for d in diffs if d >= 1),
        "bse_first": sum(1 for d in diffs if d <= -1),
        "within_1s": sum(1 for d in diffs if abs(d) < 1),
        "median_bse_minus_nse_s": percentile(diffs, 50),
        "p90_abs_gap_s": percentile([abs(d) for d in diffs], 90),
        "window_s": window_s,
        "span_start": span_start.isoformat(),
    }


def _events_summary(store: Store, run_id: int, sightings) -> dict | None:
    events = store.events(run_id)
    if not events:
        return None
    live = [e for e in events if not e["backlog"]]
    first_by_exchange: dict[int, dict[str, str]] = {}
    for row in sightings:
        if row["event_id"] is None:
            continue
        seen = first_by_exchange.setdefault(row["event_id"], {})
        if row["exchange"] not in seen or row["detected_at"] < seen[row["exchange"]]:
            seen[row["exchange"]] = row["detected_at"]
    both = [e for e in live if len(first_by_exchange.get(e["id"], {})) == 2]
    gaps = []
    first_on = Counter()
    for event in both:
        seen = first_by_exchange[event["id"]]
        gap = _seconds(from_iso(seen["BSE"]), from_iso(seen["NSE"]))
        gaps.append(abs(gap))
        first_on["NSE" if gap > 0 else "BSE"] += 1
    material = [e for e in live if e["event_type"] not in ("routine",)]
    lags = [
        _seconds(from_iso(e["first_seen_at"]), from_iso(e["news_time"]))
        for e in material
        if e["first_seen_at"] and e["news_time"]
    ]
    run_sightings = [s for s in sightings if s["event_id"] is not None and not s["backlog"]]
    return {
        "live_events": len(live),
        "by_type": dict(Counter(e["event_type"] for e in live).most_common()),
        "live_filings": len(run_sightings),
        "copies_absorbed": max(0, len(run_sightings) - len(live)),
        "with_isin": sum(1 for e in live if not e["company_id"].startswith("name:")),
        "both_exchanges": len(both),
        "first_on": dict(first_on),
        "median_exchange_gap_s": percentile(gaps, 50),
        "material_events": len(material),
        "material_lag_p50_s": percentile(lags, 50),
        "material_lag_p90_s": percentile(lags, 90),
        "material_lag_max_s": max(lags) if lags else None,
        "in_market_hours": sum(1 for e in live if e["in_market_hours"]),
    }


def build_report(
    store: Store,
    run_id: int | None = None,
    *,
    session_hours: float = 6.5,
    grace_s: float = 600.0,
    cross_exchange_window_s: float = 900.0,
) -> dict:
    run_id = run_id or store.latest_run_id()
    if run_id is None:
        raise ValueError("No runs recorded yet. Run the 'poll' command first.")
    run = store.run(run_id)
    if run is None:
        raise ValueError(f"Run {run_id} not found.")
    polls = store.polls(run_id)
    sightings = store.sightings(run_id)
    names = [name for name in run["sources"].split(",") if name]
    exchanges = {name: _exchange(name, sightings) for name in names}
    run_start = from_iso(run["started_at"])
    finishes = [from_iso(p["finished_at"] or p["started_at"]) for p in polls]
    run_end = max(finishes, default=run_start)

    behind = _behind_peers(names, exchanges, polls)
    report = {
        "run_id": run_id,
        "started_at": run_start.isoformat(),
        "ended_at": run_end.isoformat(),
        "duration_s": _seconds(run_end, run_start),
        "session_hours": session_hours,
        "sources": {name: _source_stats(name, polls, sightings, behind, session_hours) for name in names},
        "pairs": [
            _pair_stats(a, b, sightings, run_end, grace_s)
            for a, b in combinations(names, 2)
            if exchanges[a] == exchanges[b]
        ],
        "cross_exchange": _cross_exchange(sightings, cross_exchange_window_s),
        "events": _events_summary(store, run_id, sightings),
        "grace_s": grace_s,
    }
    return report


# Text output ---------------------------------------------------------------------

def _num(value, digits: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return f"{value:,}"


def _pct(value) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]
    def line(cells):
        return "  ".join(str(cell).ljust(width) if i == 0 else str(cell).rjust(width) for i, (cell, width) in enumerate(zip(cells, widths)))
    return [line(headers), line(["-" * w for w in widths]), *(line(row) for row in rows)]


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def format_report(report: dict) -> str:
    start, end = datetime.fromisoformat(report["started_at"]), datetime.fromisoformat(report["ended_at"])
    out = [
        f"Run {report['run_id']}: {fmt_ist(start, with_date=True)} to {fmt_ist(end)} IST ({_duration(report['duration_s'])})",
        "",
        "PER SOURCE",
    ]
    rows = []
    for name, s in report["sources"].items():
        rows.append([
            name + ("" if s["official"] in (None, True) else " (unofficial)"),
            _num(s["polls"]), _num(s["errors"]), _pct(s["not_modified_share"]),
            _num(s["median_response_ms"], 0), _num(s["kb_per_poll"]), _num(s["mb_per_session"], 0),
            _num(s["new_filings"]), _num(s["lag_p50_s"]), _num(s["lag_p90_s"]), _num(s["lag_max_s"]),
            _num(s["max_behind_peers_s"], 0),
        ])
    out += _table(
        ["source", "polls", "errors", "304s", "resp ms", "KB/poll", "MB/session*", "new filings",
         "lag p50 s", "lag p90 s", "lag max s", "max behind s"],
        rows,
    )
    out += [
        "",
        "  lag = when this tool first saw a filing minus the exchange's timestamp on it.",
        "  max behind = the most this source's newest filing trailed the newest a same-exchange peer had shown.",
        f"  * projected download for a {report['session_hours']:g}-hour session at the observed poll interval.",
    ]
    for name, s in report["sources"].items():
        notes = []
        if s["median_rebuild_gap_s"] is not None:
            notes.append(f"rebuilt every {_num(s['median_rebuild_gap_s'], 0)} s (median of {s['rebuilds_seen']} rebuilds)")
        if s["lag_from_upload_p50_s"] is not None:
            notes.append(f"median {_num(s['lag_from_upload_p50_s'])} s after the company's upload")
        if s["top_errors"]:
            notes.append("errors: " + "; ".join(f"{err} x{count}" for err, count in s["top_errors"]))
        if notes:
            out.append(f"  {name}: " + " | ".join(notes))

    if report["pairs"]:
        out += ["", "SAME EXCHANGE, SAME FILING (by attachment file name)"]
        rows = [
            [f"{p['a']} vs {p['b']}", _num(p["both"]), _num(p["a_first"]), _num(p["b_first"]), _num(p["ties"]),
             _num(p["median_b_minus_a_s"]), _num(p["missed_by_a"]), _num(p["missed_by_b"])]
            for p in report["pairs"]
        ]
        out += _table(["A vs B", "both saw", "A first", "B first", "ties", "median B-A s", "missed by A", "missed by B"], rows)
        out.append(f"  missed = one source showed it and the other still hadn't {report['grace_s']:.0f} s later.")

    cross = report["cross_exchange"]
    if cross:
        out += [
            "",
            "NSE VS BSE (same company, exchange timestamps within "
            f"{cross['window_s'] / 60:.0f} min, filings since {fmt_ist(from_iso(cross['span_start']), with_date=True)} IST; "
            "name matching is approximate)",
            f"  NSE filings {cross['nse_filings']:,}, BSE filings {cross['bse_filings']:,}, matched {cross['matched']:,}",
            f"  NSE first {cross['nse_first']:,}, BSE first {cross['bse_first']:,}, within 1 s {cross['within_1s']:,}",
            f"  median BSE minus NSE {_num(cross['median_bse_minus_nse_s'])} s, 90% of gaps under {_num(cross['p90_abs_gap_s'])} s",
        ]
    ev = report.get("events")
    if ev:
        types = ", ".join(f"{name} {count:,}" for name, count in ev["by_type"].items())
        out += [
            "",
            "EVENTS (copies of one filing grouped by company, event type and trading session)",
            f"  {ev['live_events']:,} new events from {ev['live_filings']:,} filings: {ev['copies_absorbed']:,} copies joined an existing event",
            f"  by type: {types}",
            f"  company identified by ISIN for {ev['with_isin']:,} of {ev['live_events']:,}; {ev['in_market_hours']:,} arrived in market hours",
        ]
        if ev["both_exchanges"]:
            out.append(
                f"  on both exchanges: {ev['both_exchanges']:,} (first on BSE {ev['first_on'].get('BSE', 0):,}, "
                f"first on NSE {ev['first_on'].get('NSE', 0):,}; median {_num(ev['median_exchange_gap_s'], 0)} s between copies)"
            )
        out.append(
            f"  {ev['material_events']:,} non-routine events: first seen {_num(ev['material_lag_p50_s'], 0)} s after the earliest "
            f"exchange timestamp (median), 90% within {_num(ev['material_lag_p90_s'], 0)} s, worst {_num(ev['material_lag_max_s'], 0)} s"
        )
    return "\n".join(out)
