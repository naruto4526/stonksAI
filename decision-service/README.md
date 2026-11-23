# Decision service: announcement source comparison

This is the first piece of the decision service. It polls NSE and BSE announcement
sources side by side and records when each one shows each filing, so the primary
source can be chosen from measurements. The `Filing` model and the source adapters
are what the LLM decision step will build on.

It uses only the Python standard library: nothing to install.

## What it measures

- **Lag:** when this tool first saw a filing, minus the exchange's own timestamp on it.
- **Missed filings:** one source showed a filing and another still hadn't 10 minutes later.
- **How far behind its peers a source is:** catches a feed that silently stops updating,
  as the old nseindia.com feed did on 14 Nov 2026.
- **NSE vs BSE:** for the same company's filing, which exchange published it first.
- **Cost:** response time, how often the feed answered "not modified", and the projected
  download for a 6.5-hour session.

## Sources

| Name | Exchange | Status | Default interval | Notes |
| --- | --- | --- | --- | --- |
| `nse_rss` | NSE | Official | 5 s | Announcements feed on `nsearchives.nseindia.in`. Whole day in one file; answers 304 when unchanged. |
| `nse_rss_legacy` | NSE | Official | 60 s | The same feed on the old `.com` address. Froze at 14:06 IST on 14 Nov 2026, later recovered; kept as a backup copy. |
| `bse_rss` | BSE | Official | 20 s | Latest Corporate Announcements feed. About 3.1 MB per fetch (270 KB compressed), no 304, may be cached for 60 s. |
| `nse_json` | NSE | Unofficial, opt-in | 10 s | The JSON behind NSE's announcements page. Has symbol and ISIN. |
| `bse_api` | BSE | Unofficial, opt-in | 10 s | The JSON behind BSE's announcements page. Has category and subcategory. |

**Terms of use.** NSE's terms prohibit systematic or automated data collection from its
website. The RSS feeds are published by the exchanges for feed readers, but they sit on
the same websites, so keep the intervals polite and consider asking NSE
(marketdata@nse.co.in) what polling rate it accepts. The two JSON sources are what the
websites call internally. They stay off unless you pass `--allow-unofficial`, and they
should never run from the IP address registered with your broker.

## Setup (Windows)

1. Install Python 3.10 or newer, for example `winget install Python.Python.3.12`.
2. Check it: `py --version`
3. From this folder:

```powershell
cd D:\repos\stonksAI\decision-service
py -m decision_service sources
```

## Run a comparison

Run it during market hours on a trading day (09:00 to 15:30 IST), ideally in results season.
Start a little before 09:00:

```powershell
py -m decision_service poll --duration 6h45m
```

It prints each new filing with its lag, a status line every minute, and the report at
the end. Ctrl+C stops it early and still prints the report. Results go to
`data\sources.sqlite`. Keep the PC from sleeping while it runs.

Both feeds hold only today's filings, from midnight IST. A filing published after the
poller stops is still picked up (as backlog) if you restart before midnight, but it is
gone once the day rolls over. Evenings matter, because many companies file results after
the close. To catch everything, keep it running past midnight: started at 09:00,
`--duration 15h30m` runs until 00:30.

### Commands

| Command | What it's for |
| --- | --- |
| `py -m decision_service sources` | The sources, their default intervals and whether they're official |
| `py -m decision_service securities` | How many companies are loaded for ISIN matching (`--download` refreshes NSE's lists) |
| `py -m decision_service poll --duration 6h45m` | Poll and record. Prints each new filing, a heartbeat every minute and the report at the end |
| `py -m decision_service runs` | Recorded runs: id, start time (UTC) and sources |
| `py -m decision_service report` | Health and lag per source, feed comparisons and the events summary for the latest run (`--run 2` for another run, `--json` for raw numbers) |
| `py -m decision_service events` | The latest non-routine events (`--run 2`, `--live`, `--multi`, `--type results`, `--include-routine`, `--limit 0` for all) |
| `py -m decision_service events --rebuild` | Regroup recorded filings into events (`--run 3` for run 3 onward) |
| `py -m unittest discover -s tests -t .` | The tests |

Options for `poll`:

```powershell
py -m decision_service poll --quiet --heartbeat 5m     # no per-filing lines, a status line every 5 minutes
py -m decision_service poll --interval nse_rss=3       # change a poll interval
py -m decision_service poll --no-events                # record filings without grouping them into events
py -m decision_service -v poll --duration 30m          # also log every warning and retry
py -u -m decision_service poll --duration 15h30m 2>&1 | Tee-Object -FilePath data\poll.log   # keep a copy of the output
```

In Git Bash, end that last command with `| tee data/poll.log` instead. `-u` stops Python
buffering its output, so lines reach the log as they are printed.

To include the unofficial JSON endpoints for a short comparison (your call, see above):

```powershell
py -m decision_service poll --sources nse_rss,nse_json,bse_rss,bse_api --allow-unofficial --duration 1h
```

### Checking that the poller works

**Before a run**

1. `py -m unittest discover -s tests -t .` ends with `OK`.
2. `py -m decision_service securities` shows about 3,200 securities. If it shows none, run
   `py -m decision_service securities --download`.
3. Stop the PC sleeping while plugged in: `powercfg /change standby-timeout-ac 0`.
   Afterwards, set it back with a number of minutes, e.g. `powercfg /change standby-timeout-ac 30`.

**A 10-minute test**, best during market hours, when filings arrive every minute or so:

```powershell
py -m decision_service poll --duration 10m
```

- The start says `Grouping filings into events, 3,215 securities loaded`. If it says
  "by company name" instead, the company lists aren't loaded.
- Each source's first poll is the day so far. It is stored as backlog and nothing is
  printed for it.
- After that, every new filing prints one line: time, source, lag, company, subject and a tag.
  - `[new results event #12]`: a new event.
  - `[copy of event #12]`: another copy of a filing already in that event, usually from the other exchange.
  - `[same file as event #12]`: the same item on NSE's second feed. Every NSE filing shows up twice.
- Lags of 1 to 3 minutes are normal in market hours. On 19 Nov, while the PC was awake,
  the median was 84 s for BSE and 131 s for NSE.
- The heartbeat line, once a minute: polls go up for every source, errors stay at or near
  0, and `last` is 200 or 304. `nse_rss` answers 304 ("unchanged") most of the time;
  `bse_rss` never does.

**Warning signs**

- Heartbeat lines missing for some minutes: the PC slept. Lags measured after that are inflated.
- `getaddrinfo failed` or another `URLError`: no network, often just after waking. The
  poller retries on its own, waiting longer each time.
- HTTP 403 or 429: the exchange is throttling. The poller waits at least 60 s. If it keeps
  happening, raise the intervals.
- An occasional HTTP 406 from BSE (twice on 19 Nov) is harmless.
- One source's `new` count stops rising during market hours while the others keep going:
  that feed has stalled. NSE's did, from 09:18 to 09:59 on 19 Nov. The report's
  `max behind` shows how far it fell behind.

**After a run**, run `py -m decision_service report`:

- PER SOURCE: few errors. A `lag max` measured in hours means the PC slept or a feed stalled.
- SAME EXCHANGE: `missed by A` and `missed by B` should both be 0.
- EVENTS: "more sightings were the same items on a second feed" should be close to the
  number of NSE filings, since each NSE filing arrives on both NSE feeds.
- `py -m decision_service events --live --limit 20`: skim the types. Companies listed on
  both exchanges should mostly show `copies 2` and `BSE,NSE`.

**After a restart**, the new run re-reads the whole day, and those filings should go back
to their existing events. About a minute after restarting, run
`py -m decision_service events --run <new run id> --include-routine`. It lists the events the
new run created; their times should all be after the previous run stopped.

## Events: one decision per filing

A company's filing usually shows up several times: NSE's PDF, NSE's XBRL copy, BSE's
copy, sometimes a re-upload. The poller groups these copies into one **event**, keyed by
company, event type and trading session. The first copy creates the event, and that is
what the decision step will act on. Later copies join it as copies, not new news, even if
they arrive hours later. Each event keeps the earliest exchange timestamp of any copy as
its news time.

- **Company:** the ISIN, looked up in NSE's lists of listed equities (and BSE's list of
  scrips, if you import it). With only NSE's lists, every company is found by name:
  lowercase, "&" as "and", without "Limited"/"Ltd", a leading "The" or punctuation, and
  "(I)" read as "(India)". No match: the company ID is `name:<that name>`; a filing with
  no name at all uses its exchange code (`bse:9012103`). `bse_code` prefers the company's
  equity code (5xxxxx) over its bond codes.
- **Event type:** from keywords in the subject and headline: `results`,
  `merger_acquisition`, `shareholding`, `order_win`, `agreement`, `fund_raise`,
  `capital_return`, `credit_rating`, `legal_regulatory`, `management`, `update`,
  `routine`, or `other` when nothing matches. Rules are checked in order and the first
  match wins. Filings that mention results without being them (call transcripts and
  recordings, call schedules, newspaper copies, board-meeting notices) are checked first
  and count as routine, except board-meeting notices about a buyback, bonus, split,
  dividend, fund raise or merger. Routine filings get status `routine`, so the decision
  step can skip them.
- **Trading session:** a filing between 09:15 and 15:30 on a weekday belongs to that day;
  before the open, to the same day; after the close or at a weekend, to the next weekday.
  Exchange holidays are not modelled yet.
- **Seen before:** a filing already linked to an event is recognised exactly and goes
  straight back to that event. NSE's `.in` and `.com` feeds carry the same items, and
  every restart re-reads the day. The identity is the exchange plus the item's
  `source_id`: attachment file name and exchange timestamp. The file name alone isn't
  unique, because BSE posts one PDF as a separate item per mutual-fund scheme. The
  company must match too, unless the repeat has no company name. A repeat isn't a new
  copy: `copies` counts distinct items.
- **Matching:** a new copy joins one of the company's events in the same session if
  - it has the same type: results anywhere in the session (a company files results once
    a day), other types within 60 minutes, so two separate updates on one day stay apart;
  - or either type is `other` ("As per attachment" is common on BSE), within 30 minutes;
  - or it comes from the other exchange, within 30 minutes, whatever the types: the NSE
    and BSE copies of one filing often classify differently. The event must not already
    have a copy from that exchange.

  The closest eligible event wins, with a type mismatch counted as 10 extra minutes.
- **When copies disagree on the type:** the more specific one wins (unreadable, then
  routine, then a vague update, then anything specific), so a copy that looks routine
  never hides one that doesn't. Between two specific types, NSE's wins, because it is a
  category the company picked from NSE's list rather than BSE's free text.
  `type_exchange` records which exchange's copy set the type. Once an event's status is
  past `new`/`routine`, a late copy doesn't change it.

Set up the company lists once, and again now and then for new listings:

```powershell
py -m decision_service securities --download      # NSE mainboard and SME lists into data\securities
py -m decision_service securities                 # show what is loaded
```

BSE's list of scrips is optional: download it from bseindia.com and run
`py -m decision_service securities --import path\to\the.csv`. Columns are found from the
header (Security Code, ISIN No, ...); I haven't seen BSE's file itself, so check the count
it prints.

Then:

```powershell
py -m decision_service poll --duration 6h45m        # groups while polling: [new results event #12] / [copy of event #12]
py -m decision_service events                       # latest events, routine ones hidden
py -m decision_service events --type results --multi   # results seen on both exchanges
py -m decision_service events --rebuild             # regroup every recorded run (after loading lists or changing rules)
py -m decision_service events --rebuild --run 3     # regroup run 3 and every later run; earlier runs stay as they are
```

`poll --no-events` turns grouping off. Runs recorded before this existed have no events
until you run `events --rebuild` once.

## Reading the report

- For each exchange, prefer the official source with no missed filings and an acceptable
  lag p90. The US research found the tradable drift starts about 15 minutes after a
  release, so a lag of seconds is fine; minutes is a warning; hours means a dead feed.
- A large **max behind** means that source went stale while a peer kept updating.
- If BSE often publishes a company's filing before NSE, poll both exchanges.
- **MB/session** is the bandwidth price of the chosen interval.

Caveats: lag includes the poll interval (on average half of it); exchange timestamps have
one-second resolution; NSE-to-BSE matching uses ISINs when the company lists are loaded
and company names otherwise, which is approximate.

## Findings while building (14 Nov 2026)

- NSE now serves from `nseindia.in`. The RSS feed on the old `.com` address stopped at 14:06 IST.
- The current NSE feed held the whole day (1,208 filings at 19:58 IST) and supports ETag/304.
- The NSE attachment file-name prefix is not always the stock symbol: PTC Industries files
  as `PTCINDUSTRIES_...` but trades as `PTCIL`. The RSS feed has no symbol or ISIN, so a
  company-name-to-symbol lookup (from NSE's list of listed equities) is needed later.
- BSE's RSS held about 8,150 items, carries the scrip code but no category, and labels its
  build time "GMT" when it is IST (handled in the parser).
- BSE's JSON showed a 22:33:01 filing that its RSS, built at 22:33:46, didn't include yet.
  One observation; the poller measures this properly.

## Grouping on the 19 Nov run

Regrouped on 21 Nov with NSE's company lists loaded and the matching rules above:

- Run 2's 1,822 live sightings were 1,203 distinct filings; the other 619 were the same
  items on NSE's second feed. They became 790 events: 413 copies joined an existing event.
- 556 events were routine, 48 unreadable (`other`), 186 typed and non-routine.
  311 events were matched to an ISIN; most of the rest are mutual funds, bonds and
  companies listed only on BSE.
- 173 events appeared on both exchanges: BSE first for 90, NSE first for 83, median 168 s
  between the copies.
- Across all three runs, NSE-only and BSE-only events of the same company within 30
  minutes of each other (likely missed copies) fell from 57 to 1, and that one is two
  different companies with similar names.
- Run 3 re-read 2,321 filings that run 2 had already seen; all but 3 went back to their
  earlier event. The 3 are mutual-fund NAV items whose scheme name BSE's feed changed
  between the runs.
- In a sample of 60 company events, 56 had the right type, 1 was wrong and 3 were debatable.
- Non-routine events were first seen a median 140 s after the earliest exchange timestamp.
  The p90 (about 48 minutes) includes the stretch when the PC slept, so it overstates the lag.
- Where one company had several non-routine events in a session, spot checks showed
  genuinely separate filings (for example a CFO resignation in the morning and a director's
  resignation two hours later).

## Layout

```
decision_service/
  models.py      Filing: the record every later stage uses
  securities.py  company lists: ISIN, NSE symbol, BSE code, name
  events.py      event types, trading sessions, grouping copies into events
  sources/       one adapter per source; subclass Source and register it in sources/__init__.py
  http.py        gzip, conditional GET, timing
  poller.py      one thread per source, baseline + de-duplication, back-off, event grouping
  store.py       SQLite for now (Postgres later)
  report.py      the comparison
  cli.py         python -m decision_service ...
tests/           py -m unittest discover -s tests -t .
```

## Next steps toward the decision service

Model exchange holidays in the trading session, download and extract the PDFs for events
with status `new`, then add the LLM decision step. It takes an event rather than a single
filing and moves its status from `new` to `deciding` to `decided`.
