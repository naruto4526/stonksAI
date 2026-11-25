"""Command line: list sources, poll them, and report on a run.

    python -m decision_service sources
    python -m decision_service poll --duration 6h30m
    python -m decision_service report
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from . import __version__
from . import documents as documents_mod
from . import securities as securities_mod
from .events import EventTracker, rebuild_events
from .http import DEFAULT_USER_AGENT, HttpClient
from .poller import Poller
from .report import build_report, format_report
from .sources import DEFAULT_SOURCES, SOURCES, Source
from .store import TABLES, Store, copy_database, describe_target, is_postgres_url
from .timeutil import fmt_ist, from_iso, utcnow

DEFAULT_DB = Path("data") / "sources.sqlite"
DB_HELP = "SQLite file or postgresql:// URL (default: STONKS_DB from the environment or .env, else data/sources.sqlite)"


def load_env_file(path: str | Path = ".env") -> None:
    """Read KEY=VALUE lines from a .env file into the environment, without overriding what is set."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def default_db() -> str:
    return os.environ.get("STONKS_DB") or str(DEFAULT_DB)


def require_db(target: str, hint: str = " Run the 'poll' command first.") -> None:
    if not is_postgres_url(target) and not Path(target).exists():
        raise SystemExit(f"No database at {target}.{hint}")

UNOFFICIAL_WARNING = """\
WARNING: you enabled unofficial sources ({names}).
These are the undocumented JSON endpoints behind the NSE/BSE websites. NSE's terms
of use prohibit systematic or automated data collection from its website, and
either exchange can block your IP address without notice. Use them only for a
short, low-rate comparison, and never on the server that places your orders.
"""

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)([hms])")


def parse_duration(text: str) -> float:
    """'90' -> 90 s, '45s', '30m', '6h30m'."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise argparse.ArgumentTypeError(f"bad duration {text!r}; use e.g. 45s, 30m, 6h30m")
    scale = {"h": 3600, "m": 60, "s": 1}
    return sum(float(number) * scale[unit] for number, unit in parts)


def parse_assignments(values: list[str] | None, what: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or []:
        for item in value.split(","):
            if not item.strip():
                continue
            name, sep, setting = item.partition("=")
            if not sep or not name.strip() or not setting.strip():
                raise SystemExit(f"--{what} expects NAME=VALUE, got {item!r}")
            result[name.strip()] = setting.strip()
    return result


def build_sources(
    names: list[str],
    intervals: dict[str, str],
    urls: dict[str, str],
    user_agent: str,
    allow_unofficial: bool,
) -> list[Source]:
    unknown = [n for n in [*names, *intervals, *urls] if n not in SOURCES]
    if unknown:
        raise SystemExit(f"Unknown source(s): {', '.join(sorted(set(unknown)))}. Run 'sources' to list them.")
    blocked = [n for n in names if not SOURCES[n].official and not allow_unofficial]
    if blocked:
        raise SystemExit(
            f"{', '.join(blocked)} {'is' if len(blocked) == 1 else 'are'} unofficial. "
            "Add --allow-unofficial if you accept the terms-of-use risk (see README)."
        )
    sources = []
    for name in names:
        interval = float(intervals[name]) if name in intervals else None
        source = SOURCES[name](url=urls.get(name), interval=interval)
        source.client.user_agent = user_agent
        sources.append(source)
    return sources


# Commands ------------------------------------------------------------------------

def cmd_sources(_args) -> int:
    for name, cls in SOURCES.items():
        default = " (default)" if name in DEFAULT_SOURCES else ""
        kind = "official" if cls.official else "UNOFFICIAL"
        print(f"{name}{default}  [{cls.exchange}, {kind}, every {cls.default_interval:g}s]")
        print(f"    {cls.default_url}")
        print(f"    {cls.description}")
    return 0


def cmd_poll(args) -> int:
    names = [n.strip() for n in (args.sources or ",".join(DEFAULT_SOURCES)).split(",") if n.strip()]
    sources = build_sources(
        names,
        parse_assignments(args.interval, "interval"),
        parse_assignments(args.url, "url"),
        args.user_agent,
        args.allow_unofficial,
    )
    unofficial = [s.name for s in sources if not s.official]
    if unofficial:
        print(UNOFFICIAL_WARNING.format(names=", ".join(unofficial)), file=sys.stderr)

    store = Store(args.db)
    run_id = store.start_run(
        names,
        settings={
            "intervals": {s.name: s.interval for s in sources},
            "urls": {s.name: s.url for s in sources},
            "duration_s": args.duration,
            "user_agent": args.user_agent,
        },
    )

    def on_new(source, result, filings) -> None:
        if args.quiet:
            return
        for filing in filings:
            lag = (
                f"{(result.finished_at - filing.published_at).total_seconds():6.1f}s"
                if filing.published_at and result.finished_at
                else "     ?"
            )
            topic = filing.subject or filing.headline or ""
            if filing.event_id is None:
                tag = ""
            elif filing.event_is_new:
                tag = f"  [new {filing.event_type} event #{filing.event_id}]"
            elif filing.event_repeat:
                tag = f"  [same file as event #{filing.event_id}]"
            else:
                tag = f"  [copy of event #{filing.event_id}]"
            print(f"{fmt_ist(result.finished_at)}  {source.name:<14} lag {lag}  {filing.company[:38]:<38}  {topic[:50]}{tag}")

    tracker = None
    if not args.no_events:
        master = securities_mod.SecurityMaster.load_dir(args.securities_dir)
        tracker = EventTracker(store, master, run_id)
        if len(master):
            print(f"Grouping filings into events, {len(master):,} securities loaded from {args.securities_dir}.")
        else:
            print(
                f"Grouping filings into events by company name: no security list in {args.securities_dir}. "
                "Run 'securities --download' to match by ISIN."
            )
    poller = Poller(store, sources, run_id, on_new=on_new, tracker=tracker)
    print(f"Run {run_id}: polling {', '.join(f'{s.name} every {s.interval:g}s' for s in sources)}")
    print(f"Saving to {describe_target(args.db)}. The first poll of each source is a baseline. Press Ctrl+C to stop.")
    poller.start()

    deadline = time.monotonic() + args.duration if args.duration else None
    next_beat = time.monotonic() + args.heartbeat if args.heartbeat else None
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.2)
            if next_beat is not None and time.monotonic() >= next_beat:
                next_beat += args.heartbeat
                parts = []
                for name, st in poller.stats.items():
                    status = st.last_error or st.last_status
                    parts.append(f"{name}: {st.polls} polls, {st.not_modified} unchanged, {st.errors} errors, {st.new_filings} new, last {status}")
                print(f"{fmt_ist(utcnow())}  heartbeat | " + " | ".join(parts))
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        poller.stop()

    if not args.no_report:
        print()
        print(format_report(build_report(store, run_id)))
    store.close()
    return 0


def cmd_report(args) -> int:
    require_db(args.db)
    store = Store(args.db)
    try:
        report = build_report(
            store,
            args.run,
            session_hours=args.session_hours,
            grace_s=args.grace,
            cross_exchange_window_s=args.window,
        )
    except ValueError as exc:
        store.close()
        raise SystemExit(str(exc))
    print(json.dumps(report, indent=2, default=str) if args.json else format_report(report))
    store.close()
    return 0


def cmd_securities(args) -> int:
    if args.download:
        client = HttpClient(user_agent=args.user_agent)
        for filename, rows, detail in securities_mod.download(args.dir, client):
            if rows:
                print(f"{filename}: {rows:,} securities from {detail}")
            else:
                print(f"{filename}: download failed ({detail})")
    for path in args.import_files or []:
        source = Path(path)
        target = Path(args.dir) / source.name
        rows = securities_mod.SecurityMaster().load_file(source)
        if rows == 0:
            raise SystemExit(f"{source}: no rows with an ISIN column; not imported")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        print(f"{source.name}: {rows:,} securities imported")
    master = securities_mod.SecurityMaster.load_dir(args.dir)
    if not master.files:
        print(f"No security lists in {args.dir}. Use --download, or --import a CSV saved from NSE or BSE.")
        return 0
    for name, rows in master.files.items():
        print(f"  {name}: {rows:,}")
    print(
        f"{len(master):,} securities: {len(master.by_symbol):,} NSE symbols, "
        f"{len(master.by_bse_code):,} BSE codes, {len(master.by_name):,} names"
    )
    return 0


def cmd_events(args) -> int:
    require_db(args.db)
    store = Store(args.db)
    if args.rebuild:
        master = securities_mod.SecurityMaster.load_dir(args.securities_dir)
        count = rebuild_events(store, master, args.run)
        scope = f"run {args.run} onward" if args.run else "all runs"
        print(f"Rebuilt events for {scope} from {count:,} filings ({len(master):,} securities loaded).")
    rows = store.events(args.run)
    if args.type:
        rows = [r for r in rows if r["event_type"] == args.type]
    if args.multi:
        rows = [r for r in rows if "," in (r["exchanges"] or "")]
    if not args.include_routine:
        rows = [r for r in rows if r["event_type"] != "routine"]
    if args.live:
        rows = [r for r in rows if not r["backlog"]]
    print(f"{len(rows):,} events" + (f" (showing the last {args.limit})" if len(rows) > args.limit else ""))
    for r in (rows[-args.limit:] if args.limit > 0 else []):
        lag = "-"
        if r["first_seen_at"] and r["news_time"]:
            lag = f"{(from_iso(r['first_seen_at']) - from_iso(r['news_time'])).total_seconds():.0f}s"
        ident = r["nse_symbol"] or r["bse_code"] or r["company_id"]
        print(
            f"#{r['id']:<6} {fmt_ist(from_iso(r['news_time']), with_date=True)}  {r['event_type']:<18} "
            f"{ident[:14]:<14} {(r['company'] or '')[:34]:<34} copies {r['copies']:<3} {r['exchanges']:<8} "
            f"first {r['first_source']:<15} lag {lag:<7} {r['status']}"
        )
    store.close()
    return 0


def cmd_documents(args) -> int:
    import importlib.util

    require_db(args.db)
    universe = None
    if args.universe != "all":
        if not Path(args.universe).exists():
            raise SystemExit(f"No universe file at {args.universe}. Pass --universe all to test every company.")
        universe = documents_mod.load_universe(args.universe)
    store = Store(args.db)
    attachments = documents_mod.select_attachments(store, universe, args.limit, args.seed)
    store.close()
    if not attachments:
        print("No attachments to test: no non-routine events with attachments in that universe.")
        return 1
    scope = "every company" if universe is None else f"{len(universe):,} companies in {args.universe}"
    print(f"Testing {len(attachments)} attachments from {len({a.event_id for a in attachments})} non-routine events "
          f"({scope}), one request every {args.delay:g} s. Files are saved in {args.out}.")
    if importlib.util.find_spec("pypdf") is None:
        print(documents_mod.PDF_LIB_MISSING)
    client = HttpClient(user_agent=args.user_agent, timeout=args.timeout)
    records: list[dict] = []
    try:
        documents_mod.run_test(client, attachments, args.out, delay=args.delay, refetch=args.refetch, records=records)
    except KeyboardInterrupt:
        print("\nStopped early: summarising what was tested.")
    if not records:
        return 1
    summary = documents_mod.summarize(records)
    print()
    print(documents_mod.format_summary(summary))
    path = documents_mod.save_report(records, summary, args.out)
    print(f"\nFull results: {path}")
    return 0


def cmd_migrate(args) -> int:
    require_db(args.source, "")
    target = args.target or default_db()
    if target == args.source:
        raise SystemExit("Nothing to do: the source and the target are the same database. Set STONKS_DB or pass --to.")
    source_store, target_store = Store(args.source), Store(target)
    print(f"Copying {describe_target(args.source)}\n     to {describe_target(target)}")
    try:
        counts = copy_database(source_store, target_store, replace=args.replace)
    except ValueError as exc:
        raise SystemExit(f"Not copied: {exc}. Pass --replace to empty the target first.")
    for table, n in counts.items():
        expected = source_store.count(table)
        print(f"  {table:<10} {n:>7,} rows{'' if n == expected else f' (source has {expected:,})'}")
    source_store.close()
    target_store.close()
    return 0


def _timings(fn, n: int) -> list[float]:
    out = []
    for _ in range(n):
        started = time.perf_counter()
        fn()
        out.append((time.perf_counter() - started) * 1000)
    return out


def _ms(values: list[float]) -> str:
    ordered = sorted(values)
    return f"median {ordered[len(ordered) // 2]:,.1f} ms, max {ordered[-1]:,.1f} ms (n={len(values)})"


def cmd_dbcheck(args) -> int:
    """Is the database reachable, what is in it, and how long does a round trip take?"""
    require_db(args.db, "")
    started = time.perf_counter()
    store = Store(args.db)
    opened_ms = (time.perf_counter() - started) * 1000
    print(f"Database: {describe_target(args.db)} (opened in {opened_ms:,.0f} ms)")
    print("  rows: " + ", ".join(f"{table} {store.count(table):,}" for table in TABLES))
    print(f"  query through the store: {_ms(_timings(lambda: store._query('SELECT 1 AS x'), args.n))}")
    store.close()
    if not is_postgres_url(args.db):
        return 0

    import socket
    from urllib.parse import urlsplit

    import psycopg

    parts = urlsplit(args.db)
    variants = [("as configured", args.db)]
    if parts.hostname == "localhost":
        found = sorted({info[4][0] for info in socket.getaddrinfo("localhost", parts.port or 5432, type=socket.SOCK_STREAM)})
        print(f"  localhost resolves to: {', '.join(found)}")
        variants.append(("via 127.0.0.1", args.db.replace("@localhost:", "@127.0.0.1:", 1)))
    connect_ms = {}
    for label, url in variants:
        connects = _timings(lambda: psycopg.connect(url, autocommit=True, connect_timeout=10).close(), 3)
        connect_ms[label] = sorted(connects)[1]
        conn = psycopg.connect(url, autocommit=True, connect_timeout=10)
        row = conn.execute("SELECT current_setting('server_version')").fetchone()
        version = row[0] if isinstance(row, tuple) else next(iter(row.values()))
        selects = _timings(lambda: conn.execute("SELECT 1").fetchall(), args.n)

        def write():  # what every store write costs in round trips: BEGIN, the statement, COMMIT
            with conn.transaction():
                conn.execute("SELECT 1").fetchall()

        writes = _timings(write, args.n)
        conn.close()
        print(f"  {label}: PostgreSQL {version}")
        print(f"    connect: {', '.join(f'{x:,.0f} ms' for x in connects)}")
        print(f"    query round trip: {_ms(selects)}")
        print(f"    write in a transaction: {_ms(writes)}")
    if connect_ms.get("as configured", 0) > 1000 and connect_ms.get("via 127.0.0.1", 1e9) < 200:
        print("  Connecting through localhost is slow here (it tries IPv6 ::1 first). "
              "Use 127.0.0.1 instead of localhost in STONKS_DB.")
    return 0


def cmd_runs(args) -> int:
    require_db(args.db, "")
    store = Store(args.db)
    for run in store.runs():
        print(f"{run['id']:>4}  {run['started_at']}  {run['sources']}")
    store.close()
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m decision_service", description="stonksAI announcement-source comparison.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true", help="log every warning and retry")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sources", help="list the available sources")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("poll", help="poll sources and record what each saw")
    p.add_argument("--sources", help=f"comma-separated names (default: {','.join(DEFAULT_SOURCES)})")
    p.add_argument("--duration", type=parse_duration, help="stop after e.g. 30m or 6h30m (default: until Ctrl+C)")
    p.add_argument("--interval", action="append", metavar="NAME=SECONDS", help="override a poll interval, e.g. nse_rss=3")
    p.add_argument("--url", action="append", metavar="NAME=URL", help="override a source URL (for testing)")
    p.add_argument("--db", default=default_db(), help=DB_HELP)
    p.add_argument("--allow-unofficial", action="store_true", help="permit the unofficial JSON endpoints")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    p.add_argument("--heartbeat", type=parse_duration, default=60.0, help="status line interval (0 to disable)")
    p.add_argument("--quiet", action="store_true", help="don't print each new filing")
    p.add_argument("--no-report", action="store_true", help="skip the report at the end")
    p.add_argument("--no-events", action="store_true", help="don't group filings into events")
    p.add_argument("--securities-dir", default=str(securities_mod.DEFAULT_DIR), help="folder with security list CSVs")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("report", help="compare sources for a recorded run")
    p.add_argument("--db", default=default_db(), help=DB_HELP)
    p.add_argument("--run", type=int, help="run id (default: latest)")
    p.add_argument("--json", action="store_true", help="print the raw numbers as JSON")
    p.add_argument("--session-hours", type=float, default=6.5, help="session length for bandwidth projection")
    p.add_argument("--grace", type=float, default=600.0, help="seconds before a filing counts as missed")
    p.add_argument("--window", type=float, default=900.0, help="seconds allowed between NSE and BSE copies")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("securities", help="download or import the lists that map companies to ISINs")
    p.add_argument("--download", action="store_true", help="fetch NSE's equity and SME lists")
    p.add_argument("--import", dest="import_files", nargs="+", metavar="CSV", help="add a CSV saved from NSE or BSE")
    p.add_argument("--dir", default=str(securities_mod.DEFAULT_DIR))
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    p.set_defaults(func=cmd_securities)

    p = sub.add_parser("events", help="list events, or rebuild them from recorded filings")
    p.add_argument("--db", default=default_db(), help=DB_HELP)
    p.add_argument("--rebuild", action="store_true", help="regroup recorded filings into events")
    p.add_argument("--run", type=int, help="limit to one run")
    p.add_argument("--type", help="only this event type (results, order_win, ...)")
    p.add_argument("--multi", action="store_true", help="only events seen on both exchanges")
    p.add_argument("--live", action="store_true", help="only events first seen after polling started")
    p.add_argument("--include-routine", action="store_true", help="include routine filings")
    p.add_argument("--limit", type=int, default=50, help="how many events to print (default 50)")
    p.add_argument("--securities-dir", default=str(securities_mod.DEFAULT_DIR))
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("documents", help="test downloading and reading the attachments of recorded events")
    p.add_argument("--db", default=default_db(), help=DB_HELP)
    p.add_argument("--limit", type=int, default=100, help="attachments to test (default 100)")
    p.add_argument("--universe", default=str(documents_mod.DEFAULT_UNIVERSE),
                   help="file of ISINs to test, or 'all' (default: the Nifty 500 list)")
    p.add_argument("--out", default=str(documents_mod.DEFAULT_OUT), help="folder for the downloaded files and the report")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1)")
    p.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for one download")
    p.add_argument("--seed", type=int, default=7, help="which random sample to take")
    p.add_argument("--refetch", action="store_true", help="download again even if the file is already saved")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    p.set_defaults(func=cmd_documents)

    p = sub.add_parser("migrate", help="copy a SQLite database into PostgreSQL (or any other store)")
    p.add_argument("--from", dest="source", default=str(DEFAULT_DB), help=f"database to copy (default: {DEFAULT_DB})")
    p.add_argument("--to", dest="target", help="database to copy into (default: STONKS_DB)")
    p.add_argument("--replace", action="store_true", help="empty the target's tables first")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("dbcheck", help="check the database: reachable, row counts, round-trip time")
    p.add_argument("--db", default=default_db(), help=DB_HELP)
    p.add_argument("-n", type=int, default=20, help="queries to time (default 20)")
    p.set_defaults(func=cmd_dbcheck)

    p = sub.add_parser("runs", help="list recorded runs")
    p.add_argument("--db", default=default_db(), help=DB_HELP)
    p.set_defaults(func=cmd_runs)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # Windows consoles can't print every company name
        except (AttributeError, ValueError):
            pass
    load_env_file()  # STONKS_DB and friends, before the defaults are read
    args = make_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    return args.func(args)
