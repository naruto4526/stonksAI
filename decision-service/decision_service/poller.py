"""Polls every source on its own thread and records what each one saw, and when."""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .events import EventTracker
from .models import Filing
from .sources.base import PollResult, Source
from .store import Store

log = logging.getLogger(__name__)

NewFilingsCallback = Callable[[Source, PollResult, "list[Filing]"], None]


@dataclass
class SourceStats:
    polls: int = 0
    errors: int = 0
    not_modified: int = 0
    new_filings: int = 0
    last_status: int | None = None
    last_error: str | None = None


class Poller:
    def __init__(
        self,
        store: Store,
        sources: list[Source],
        run_id: int,
        *,
        jitter: float = 0.1,
        max_backoff: float = 300.0,
        on_new: NewFilingsCallback | None = None,
        tracker: EventTracker | None = None,
    ) -> None:
        self.store = store
        self.tracker = tracker
        self.sources = sources
        self.run_id = run_id
        self.jitter = jitter
        self.max_backoff = max_backoff
        self.on_new = on_new
        self.stats = {source.name: SourceStats() for source in sources}
        self.stop_event = threading.Event()
        self._seen: dict[str, set[str]] = {source.name: set() for source in sources}
        self._baseline_taken: set[str] = set()
        self._threads: list[threading.Thread] = []

    def poll_once(self, source: Source) -> tuple[PollResult, list[Filing]]:
        """One poll: fetch, keep only filings this source hasn't shown before, store."""
        result = source.poll()
        stats = self.stats[source.name]
        stats.polls += 1
        stats.last_status = result.status
        stats.last_error = result.error
        if result.error:
            stats.errors += 1
        if result.not_modified:
            stats.not_modified += 1

        # Feeds repeat items (BSE's RSS listed one Axis Bank filing 11 times on
        # 18 Nov 2026), so de-duplicate within the poll as well as across polls.
        seen = self._seen[source.name]
        new = []
        for filing in result.filings:
            if filing.source_id not in seen:
                seen.add(filing.source_id)
                new.append(filing)

        # The first successful poll is a baseline: those filings existed before we
        # started, so they are stored as backlog and left out of lag figures.
        is_baseline = result.ok and not result.not_modified and source.name not in self._baseline_taken
        if is_baseline:
            self._baseline_taken.add(source.name)

        self.store.record_poll(self.run_id, result, len(new))
        if new:
            self.store.record_sightings(self.run_id, new, result.finished_at, backlog=is_baseline)
            if self.tracker is not None:
                self._track(new, result.finished_at, is_baseline)
        if not is_baseline:
            stats.new_filings += len(new)
            if new and self.on_new:
                self.on_new(source, result, new)
        return result, new

    def _track(self, filings: list[Filing], detected_at, backlog: bool) -> None:
        """Attach each new filing to an event (oldest first), then record the links."""
        links = []
        for filing in sorted(filings, key=lambda f: (f.published_at is None, f.published_at or detected_at)):
            try:
                outcome = self.tracker.process(filing, detected_at, backlog=backlog)
            except Exception:  # grouping must never stop polling
                log.exception("event tracking failed for %s %s", filing.source, filing.source_id)
                continue
            links.append((filing.source, filing.source_id, outcome.event_id, outcome.company_id, outcome.event_type))
        self.store.set_sighting_events(self.run_id, links)

    def next_delay(self, source: Source, result: PollResult, failures: int) -> float:
        delay = source.interval * (1.0 + random.uniform(-self.jitter, self.jitter))
        if failures:
            delay = min(self.max_backoff, source.interval * (2 ** failures))
        if result.status in (403, 429):
            delay = max(delay, 60.0)
        return delay

    def _loop(self, source: Source) -> None:
        try:
            source.prepare()
        except Exception as exc:  # the first poll will report the real failure
            log.warning("%s: preparation failed: %s", source.name, exc)
        failures = 0
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                result, _ = self.poll_once(source)
            except Exception:  # never let one source kill its thread
                log.exception("%s: unexpected error", source.name)
                failures += 1
                self.stop_event.wait(min(self.max_backoff, source.interval * 2 ** failures))
                continue
            if result.error:
                failures += 1
                log.warning("%s: %s", source.name, result.error)
            else:
                failures = 0
            delay = self.next_delay(source, result, failures)
            self.stop_event.wait(max(0.0, delay - (time.monotonic() - started)))

    def start(self) -> None:
        for source in self.sources:
            thread = threading.Thread(target=self._loop, args=(source,), name=f"poll-{source.name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 30.0) -> None:
        self.stop_event.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
