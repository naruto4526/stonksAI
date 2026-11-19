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

Useful options:

```powershell
py -m decision_service poll --interval nse_rss=3 --quiet       # change an interval, hide per-filing lines
py -m decision_service report                                   # report on the latest run again
py -m decision_service report --run 2 --json                    # a specific run, raw numbers
py -m decision_service runs                                     # list recorded runs
```

To include the unofficial JSON endpoints for a short comparison (your call, see above):

```powershell
py -m decision_service poll --sources nse_rss,nse_json,bse_rss,bse_api --allow-unofficial --duration 1h
```

## Events: one decision per filing

A company's filing usually shows up several times: NSE's PDF, NSE's XBRL copy, BSE's
copy, sometimes a re-upload. The poller groups these copies into one **event**, keyed by
company, event type and trading session. The first copy creates the event, and that is
what the decision step will act on. Later copies join it as copies, not new news, even if
they arrive hours later. Each event keeps the earliest exchange timestamp of any copy as
its news time.

- **Company:** the ISIN, looked up in NSE's lists of listed equities (and BSE's list of
  scrips, if you import it). Without the lists it falls back to the normalised company
  name, which works for most companies but misses ones named differently on each exchange.
- **Event type:** from keywords in the subject and headline: `results`,
  `merger_acquisition`, `shareholding`, `order_win`, `agreement`, `fund_raise`,
  `capital_return`, `credit_rating`, `legal_regulatory`, `management`, `update`,
  `routine`, or `other` when nothing matches. Routine filings (newspaper notices, trading
  window, NAV, meeting schedules, debenture-trustee letters and so on) get status
  `routine`, so the decision step can skip them.
- **Trading session:** a filing between 09:15 and 15:30 on a weekday belongs to that day;
  before the open, to the same day; after the close or at a weekend, to the next weekday.
  Exchange holidays are not modelled yet.
- **Matching:** results merge across the whole session (a company files results once a
  day). Other types merge when the copies' exchange timestamps are within 60 minutes, so
  two separate updates on one day stay separate. A copy whose type can't be read ("As per
  attachment" is common on BSE) joins the company's nearest event within 30 minutes and
  takes its type.

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

Backfilled with name matching only (the company lists weren't loaded):

- 1,822 live filings became 801 events; 1,021 copies joined an existing event.
- 534 events were routine, 56 unreadable (`other`), 267 non-routine.
- 162 events appeared on both exchanges: BSE first for 82, NSE first for 80, median 159 s
  between the copies.
- Non-routine events were first seen a median 129 s after the earliest exchange timestamp.
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
