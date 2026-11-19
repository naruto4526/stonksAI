"""SQLite store for poll results and filing sightings (Postgres comes later)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .models import Filing
from .sources.base import PollResult
from .timeutil import to_iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    sources TEXT NOT NULL,
    settings TEXT
);
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status INTEGER,
    elapsed_ms REAL,
    wire_bytes INTEGER,
    body_bytes INTEGER,
    not_modified INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    newest_published_at TEXT,
    build_time TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS polls_run_source ON polls(run_id, source, started_at);
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    source TEXT NOT NULL,
    exchange TEXT NOT NULL,
    source_id TEXT NOT NULL,
    match_key TEXT NOT NULL,
    company TEXT,
    company_key TEXT,
    symbol TEXT,
    isin TEXT,
    subject TEXT,
    headline TEXT,
    attachment_url TEXT,
    published_at TEXT,
    filed_at TEXT,
    detected_at TEXT NOT NULL,
    backlog INTEGER NOT NULL DEFAULT 0,
    company_id TEXT,
    event_type TEXT,
    event_id INTEGER,
    UNIQUE (run_id, source, source_id)
);
CREATE INDEX IF NOT EXISTS sightings_run_key ON sightings(run_id, match_key);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT NOT NULL,
    isin TEXT,
    nse_symbol TEXT,
    bse_code TEXT,
    company TEXT,
    event_type TEXT NOT NULL,
    session_date TEXT NOT NULL,
    in_market_hours INTEGER NOT NULL DEFAULT 0,
    news_time TEXT,
    first_seen_at TEXT,
    first_source TEXT,
    first_run_id INTEGER,
    copies INTEGER NOT NULL DEFAULT 1,
    sources TEXT,
    exchanges TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    backlog INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_company_session ON events(company_id, session_date);
"""

# Columns added after the first release; older databases get them on open.
SIGHTING_MIGRATIONS = {"company_id": "TEXT", "event_type": "TEXT", "event_id": "INTEGER"}
EVENT_COLUMNS = (
    "company_id", "isin", "nse_symbol", "bse_code", "company", "event_type", "session_date",
    "in_market_hours", "news_time", "first_seen_at", "first_source", "first_run_id", "copies",
    "sources", "exchanges", "status", "backlog",
)


class Store:
    def __init__(self, path: str | Path) -> None:
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            # Older databases have a sightings table without the event columns: add them,
            # then create the index that uses them.
            self._conn.executescript(SCHEMA)
            existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(sightings)")}
            for column, kind in SIGHTING_MIGRATIONS.items():
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE sightings ADD COLUMN {column} {kind}")
            self._conn.execute("CREATE INDEX IF NOT EXISTS sightings_event ON sightings(event_id)")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # Writes ------------------------------------------------------------------------
    def start_run(self, sources: Iterable[str], settings: dict | None = None) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO runs (started_at, sources, settings) VALUES (?, ?, ?)",
                (to_iso(utcnow()), ",".join(sources), json.dumps(settings or {})),
            )
            return int(cursor.lastrowid)

    def record_poll(self, run_id: int, result: PollResult, new_count: int) -> None:
        newest = max((f.published_at for f in result.filings if f.published_at), default=None)
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO polls (run_id, source, started_at, finished_at, status, elapsed_ms,
                       wire_bytes, body_bytes, not_modified, item_count, new_count,
                       newest_published_at, build_time, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    result.source,
                    to_iso(result.started_at),
                    to_iso(result.finished_at),
                    result.status,
                    result.elapsed_ms,
                    result.wire_bytes,
                    result.body_bytes,
                    int(result.not_modified),
                    len(result.filings),
                    new_count,
                    to_iso(newest),
                    to_iso(result.build_time),
                    result.error,
                ),
            )

    def record_sightings(self, run_id: int, filings: Iterable[Filing], detected_at, backlog: bool) -> int:
        rows = [
            (
                run_id,
                f.source,
                f.exchange,
                f.source_id,
                f.match_key,
                f.company,
                f.company_key,
                f.symbol,
                f.isin,
                f.subject,
                f.headline,
                f.attachment_url,
                to_iso(f.published_at),
                to_iso(f.filed_at),
                to_iso(detected_at),
                int(backlog),
            )
            for f in filings
        ]
        if not rows:
            return 0
        with self._lock, self._conn:
            before = self._conn.total_changes
            self._conn.executemany(
                """INSERT OR IGNORE INTO sightings (run_id, source, exchange, source_id, match_key,
                       company, company_key, symbol, isin, subject, headline, attachment_url,
                       published_at, filed_at, detected_at, backlog)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return self._conn.total_changes - before

    # Events ------------------------------------------------------------------------
    def find_events(self, company_id: str, session_date: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM events WHERE company_id = ? AND session_date = ? ORDER BY id", (company_id, session_date)
        )

    def create_event(self, **fields) -> int:
        unknown = set(fields) - set(EVENT_COLUMNS)
        if unknown:
            raise ValueError(f"unknown event fields: {sorted(unknown)}")
        now = to_iso(utcnow())
        columns = [*fields, "created_at", "updated_at"]
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"INSERT INTO events ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                (*fields.values(), now, now),
            )
            return int(cursor.lastrowid)

    def update_event(self, event_id: int, **fields) -> None:
        unknown = set(fields) - set(EVENT_COLUMNS)
        if unknown:
            raise ValueError(f"unknown event fields: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE events SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), to_iso(utcnow()), event_id),
            )

    def set_sighting_events(self, run_id: int, links: list[tuple[str, str, int, str, str]]) -> None:
        """links: (source, source_id, event_id, company_id, event_type)."""
        if not links:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                """UPDATE sightings SET event_id = ?, company_id = ?, event_type = ?
                   WHERE run_id = ? AND source = ? AND source_id = ?""",
                [(event_id, company_id, event_type, run_id, source, source_id)
                 for source, source_id, event_id, company_id, event_type in links],
            )

    def clear_events(self, run_id: int | None = None) -> None:
        with self._lock, self._conn:
            if run_id is None:
                self._conn.execute("DELETE FROM events")
                self._conn.execute("UPDATE sightings SET event_id = NULL, company_id = NULL, event_type = NULL")
            else:
                self._conn.execute("DELETE FROM events WHERE first_run_id = ?", (run_id,))
                self._conn.execute(
                    "UPDATE sightings SET event_id = NULL, company_id = NULL, event_type = NULL WHERE run_id = ?", (run_id,)
                )

    def events(self, run_id: int | None = None) -> list[sqlite3.Row]:
        if run_id is None:
            return self._query("SELECT * FROM events ORDER BY news_time, id")
        return self._query("SELECT * FROM events WHERE first_run_id = ? ORDER BY news_time, id", (run_id,))

    # Reads -------------------------------------------------------------------------
    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def runs(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM runs ORDER BY id")

    def run(self, run_id: int) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM runs WHERE id = ?", (run_id,))
        return rows[0] if rows else None

    def latest_run_id(self) -> int | None:
        rows = self._query("SELECT MAX(id) AS id FROM runs")
        return rows[0]["id"] if rows and rows[0]["id"] is not None else None

    def polls(self, run_id: int) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM polls WHERE run_id = ? ORDER BY started_at, id", (run_id,))

    def sightings(self, run_id: int) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM sightings WHERE run_id = ? ORDER BY detected_at, id", (run_id,))
