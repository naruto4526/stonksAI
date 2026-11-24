"""Document test: download a sample of event attachments and measure what reading them costs.

    python -m decision_service documents --limit 100

For each attachment it records whether the download worked, how long it took, what the
file really is (PDF, XBRL, ZIP, or an HTML error page), and for PDFs the pages, the text
that comes out and whether it looks scanned. The numbers feed FR-5/FR-6, the latency
budget and the LLM budget in the requirements doc.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .http import HttpClient
from .models import attachment_key
from .report import percentile
from .timeutil import utcnow

DEFAULT_UNIVERSE = Path("data") / "securities" / "nifty500_isins.txt"
DEFAULT_OUT = Path("data") / "documents"

# Rough: English filings run about 4 characters a token, tables of figures fewer.
CHARS_PER_TOKEN = 3.5
PROMPT_TOKENS = 1_500  # instructions, schema and event details around the document
OUTPUT_TOKENS = 1_000
EVENTS_PER_MONTH = 150 * 22  # 150 events a trading day
# USD per million tokens, list prices on 24 Nov 2026 (platform.claude.com pricing page)
PRICES = {
    "Claude Haiku 5.5": [(100_000, 0.10, 0.50), (None, 0.50, 2.50)],
    "Claude Sonnet 5.5": [(None, 2.00, 10.00)],
}
SCANNED_CHARS_PER_PAGE = 100  # below this a PDF is probably page images
FINANCIAL_FACTS = {
    "RevenueFromOperations", "Income", "ProfitLossForPeriod", "ProfitBeforeTax",
    "ProfitLossFromContinuingOperations", "ProfitLossForThePeriod",
    "BasicEarningsLossPerShareFromContinuingOperations", "ComprehensiveIncomeForThePeriod",
}
PDF_LIB_MISSING = "pypdf is not installed: only downloads are measured. Install it with: py -m pip install pypdf"


def load_universe(path: str | Path) -> set[str]:
    """ISINs from a text file: whitespace-separated, lines starting with # ignored."""
    isins = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        isins.update(t for t in line.split() if re.fullmatch(r"IN[A-Z0-9]{10}", t))
    return isins


@dataclass
class Attachment:
    event_id: int
    event_type: str
    company: str
    company_id: str
    exchange: str
    url: str
    key: str
    news_time: str | None
    sources: list[str] = field(default_factory=list)


def select_attachments(store, universe: set[str] | None, limit: int, seed: int = 7) -> list[Attachment]:
    """Distinct attachments of non-routine events: results first (rare and the largest), then a
    random sample. An event's copies are kept together, so NSE and BSE copies can be compared."""
    events = [e for e in store.events() if e["status"] != "routine"
              and (universe is None or e["company_id"] in universe)]
    rng = random.Random(seed)
    rng.shuffle(events)
    events.sort(key=lambda e: e["event_type"] != "results")  # stable: results first, rest stay shuffled
    chosen: list[Attachment] = []
    for event in events:
        if len(chosen) >= limit:
            break
        copies: dict[tuple[str, str], Attachment] = {}
        for row in store.event_copies(event["id"]):
            key = attachment_key(row["attachment_url"])
            if not key:
                continue
            found = copies.get((row["exchange"], key))
            if found is None:
                copies[(row["exchange"], key)] = Attachment(
                    event["id"], event["event_type"], event["company"] or "", event["company_id"],
                    row["exchange"], row["attachment_url"], key, event["news_time"], [row["source"]])
            elif row["source"] not in found.sources:
                found.sources.append(row["source"])
        chosen.extend(copies.values())
    return chosen


def alternate_urls(url: str) -> list[str]:
    """NSE moved to nseindia.in; try the other address if one fails."""
    urls = [url]
    if "nsearchives.nseindia.com" in url:
        urls.append(url.replace("nsearchives.nseindia.com", "nsearchives.nseindia.in"))
    elif "nsearchives.nseindia.in" in url:
        urls.append(url.replace("nsearchives.nseindia.in", "nsearchives.nseindia.com"))
    return urls


def _referer(url: str) -> str | None:
    if "bseindia.com" in url:
        return "https://www.bseindia.com/"
    if "nseindia." in url:
        return "https://www.nseindia.com/"
    return None


def sniff(body: bytes, content_type: str = "") -> str:
    head = body[:2048].lstrip()
    lower = head.lower()
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if lower.startswith(b"<!doctype html") or lower.startswith(b"<html") or b"<html" in lower[:300]:
        return "html"
    if head.startswith(b"<"):
        return "xbrl" if b"xbrl" in lower else "xml"
    if not body:
        return "empty"
    return "other"


def inspect_pdf(body: bytes) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"pdf_lib": None}
    started = time.perf_counter()
    info: dict = {"pdf_lib": "pypdf"}
    try:
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted:
            info["encrypted"] = True
            reader.decrypt("")
        chars = []
        for page in reader.pages[:300]:
            try:
                chars.append(len((page.extract_text() or "").strip()))
            except Exception:  # one bad page shouldn't hide the rest
                chars.append(0)
        info["pages"] = len(reader.pages)
        info["chars"] = sum(chars)
        info["pages_with_text"] = sum(1 for c in chars if c >= 50)
        info["scanned"] = bool(chars) and sum(chars) / len(chars) < SCANNED_CHARS_PER_PAGE
        info["tokens_est"] = round(sum(chars) / CHARS_PER_TOKEN)
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"[:200]
    info["extract_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return info


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def inspect_xml(body: bytes) -> dict:
    facts: Counter = Counter()
    contexts = 0
    try:
        for _, element in ET.iterparse(io.BytesIO(body), events=("end",)):
            name = _local(element.tag)
            if name == "context":
                contexts += 1
            elif "contextRef" in element.attrib:
                facts[name] += 1
            element.clear()
    except ET.ParseError as exc:
        return {"error": f"ParseError: {exc}"[:200]}
    return {
        "facts": sum(facts.values()),
        "contexts": contexts,
        "financial_facts": sorted(set(facts) & FINANCIAL_FACTS),
        "top_facts": [name for name, _ in facts.most_common(6)],
    }


def inspect_zip(body: bytes) -> dict:
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as exc:
        return {"error": f"BadZipFile: {exc}"[:200]}
    members = [m for m in archive.infolist() if not m.is_dir()]
    info = {
        "members": len(members),
        "member_kinds": dict(Counter(Path(m.filename).suffix.lower().lstrip(".") or "none" for m in members)),
        "names": [m.filename for m in members[:5]],
    }
    pdfs = sorted((m for m in members if m.filename.lower().endswith(".pdf")), key=lambda m: -m.file_size)
    if pdfs:
        info["largest_pdf"] = inspect_pdf(archive.read(pdfs[0]))
    return info


def _fetch(client: HttpClient, url: str) -> dict:
    """Try the URL, then NSE's other address. Network errors are recorded, not raised."""
    tried = []
    for candidate in alternate_urls(url):
        headers = {"Accept": "application/pdf,application/xml,application/zip,*/*;q=0.8"}
        referer = _referer(candidate)
        if referer:
            headers["Referer"] = referer
        try:
            result = client.get(candidate, headers=headers)
        except Exception as exc:
            tried.append({"url": candidate, "status": None, "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        attempt = {"url": candidate, "status": result.status, "elapsed_ms": round(result.elapsed_ms, 1),
                   "content_type": result.headers.get("content-type", ""), "body": result.body}
        tried.append(attempt)
        if result.ok:
            break
    final = tried[-1]
    return {**final, "attempts": [{k: v for k, v in t.items() if k != "body"} for t in tried]}


def _safe_name(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:150]


def run_test(client: HttpClient, attachments: list[Attachment], out_dir: str | Path, delay: float = 1.0,
             refetch: bool = False, log: Callable[[str], None] = print, records: list | None = None) -> list[dict]:
    """Download and inspect each attachment. Pass `records` to keep partial results if interrupted.
    Files already downloaded are read from out_dir instead (unless refetch)."""
    out_dir = Path(out_dir)
    records = [] if records is None else records
    last_request = 0.0
    for number, attachment in enumerate(attachments, 1):
        path = out_dir / attachment.exchange.lower() / _safe_name(attachment.key)
        record = {**asdict(attachment), "cached": False}
        if path.exists() and not refetch:
            body = path.read_bytes()
            record.update(cached=True, status=200, ok=True)
        else:
            wait = delay - (time.monotonic() - last_request)
            if wait > 0:
                time.sleep(wait)
            fetched = _fetch(client, attachment.url)
            last_request = time.monotonic()
            body = fetched.pop("body", b"") or b""
            record.update(status=fetched.get("status"), elapsed_ms=fetched.get("elapsed_ms"),
                          content_type=fetched.get("content_type"), final_url=fetched.get("url"),
                          error=fetched.get("error"), attempts=fetched.get("attempts"))
            record["ok"] = record["status"] is not None and 200 <= record["status"] < 300
        record["bytes"] = len(body)
        record["kind"] = sniff(body, record.get("content_type") or "") if record["ok"] else None
        if record["ok"] and record["kind"] in ("html", "empty", "other"):
            record["ok"] = False
            record["error"] = f"not a document ({record['kind']})"
        if record["ok"]:
            record["sha256"] = hashlib.sha256(body).hexdigest()
            if not record["cached"]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
            if record["kind"] == "pdf":
                record["pdf"] = inspect_pdf(body)
            elif record["kind"] in ("xbrl", "xml"):
                record["xml"] = inspect_xml(body)
            elif record["kind"] == "zip":
                record["zip"] = inspect_zip(body)
        records.append(record)
        log(_progress_line(number, len(attachments), record))
    return records


def _mb(n: int | None) -> str:
    return f"{(n or 0) / 1_048_576:.2f} MB" if (n or 0) >= 100_000 else f"{(n or 0) / 1024:.0f} KB"


def _progress_line(number: int, total: int, r: dict) -> str:
    head = f"[{number:>3}/{total}] {r['exchange']} {r['event_type'][:14]:<14} {r['company'][:28]:<28}"
    if not r["ok"]:
        return f"{head} FAILED {r.get('status')} {r.get('error') or ''}".rstrip()
    timing = "cached" if r["cached"] else f"{r.get('elapsed_ms', 0):,.0f} ms"
    detail = ""
    pdf = r.get("pdf") or (r.get("zip") or {}).get("largest_pdf")
    if pdf and "pages" in pdf:
        detail = f"{pdf['pages']} pages, {pdf['chars']:,} chars{' SCANNED' if pdf['scanned'] else ''}, read in {pdf['extract_ms']:,.0f} ms"
    elif r.get("xml"):
        x = r["xml"]
        detail = f"{x.get('facts', 0)} facts" + (", financials" if x.get("financial_facts") else "")
    return f"{head} {r['kind']:<4} {_mb(r['bytes']):>9} {timing:>9}  {detail}"


def _stats(values: list[float]) -> dict | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"n": len(values), "p50": percentile(values, 50), "p90": percentile(values, 90), "max": max(values)}


def _cost(input_tokens: float, model: str) -> float:
    for ceiling, price_in, price_out in PRICES[model]:
        if ceiling is None or input_tokens <= ceiling:
            return (input_tokens * price_in + OUTPUT_TOKENS * price_out) / 1_000_000
    return 0.0


def _words(text_sample: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text_sample.lower()))


def summarize(records: list[dict]) -> dict:
    by_exchange: dict[str, Counter] = {}
    for r in records:
        c = by_exchange.setdefault(r["exchange"], Counter())
        c["tried"] += 1
        c["ok" if r["ok"] else "failed"] += 1
    failures = Counter(f"{r['exchange']} {r.get('status')} {r.get('error') or ''}".strip() for r in records if not r["ok"])
    ok = [r for r in records if r["ok"]]
    pdfs = [r for r in ok if r.get("pdf") and "pages" in r["pdf"]]

    # The document the decision would read for each event: NSE's PDF, else BSE's, else a PDF in a ZIP
    per_event: dict[int, dict] = {}
    for r in ok:
        pdf = r.get("pdf") if r.get("pdf") and "pages" in r["pdf"] else (r.get("zip") or {}).get("largest_pdf")
        if not pdf or "pages" not in pdf:
            continue
        rank = 0 if r["exchange"] == "NSE" and r["kind"] == "pdf" else 1 if r["kind"] == "pdf" else 2
        best = per_event.get(r["event_id"])
        if best is None or rank < best["rank"]:
            per_event[r["event_id"]] = {"rank": rank, "type": r["event_type"], "pdf": pdf}
    event_tokens = [e["pdf"]["tokens_est"] + PROMPT_TOKENS for e in per_event.values() if not e["pdf"]["scanned"]]
    by_type: dict[str, list[int]] = {}
    for e in per_event.values():
        by_type.setdefault(e["type"], []).append(e["pdf"]["tokens_est"])

    costs = {}
    if event_tokens:
        for model in PRICES:
            per = sum(_cost(t, model) for t in event_tokens) / len(event_tokens)
            costs[model] = {"per_event_usd": round(per, 5), "per_month_usd": round(per * EVENTS_PER_MONTH, 2)}

    # NSE and BSE copies of the same event: the same file, or at least the same words?
    pairs = []
    by_event: dict[int, dict[str, dict]] = {}
    for r in pdfs:
        by_event.setdefault(r["event_id"], {}).setdefault(r["exchange"], r)
    for copies in by_event.values():
        if "NSE" in copies and "BSE" in copies:
            pairs.append(copies["NSE"]["sha256"] == copies["BSE"]["sha256"])

    xmls = [r for r in ok if r.get("xml")]
    zips = [r for r in ok if r.get("zip")]
    return {
        "attachments": len(records),
        "events": len({r["event_id"] for r in records}),
        "by_exchange": {k: dict(v) for k, v in by_exchange.items()},
        "kinds": dict(Counter(r["kind"] for r in ok)),
        "failures": dict(failures.most_common(10)),
        "download_ms": {ex: _stats([r.get("elapsed_ms") for r in ok if r["exchange"] == ex and not r["cached"]])
                        for ex in sorted(by_exchange)},
        "bytes": _stats([r["bytes"] for r in ok]),
        "pdf_pages": _stats([r["pdf"]["pages"] for r in pdfs]),
        "pdf_tokens": _stats([r["pdf"]["tokens_est"] for r in pdfs]),
        "pdf_extract_ms": _stats([r["pdf"]["extract_ms"] for r in pdfs]),
        "pdf_scanned": sum(1 for r in pdfs if r["pdf"]["scanned"]),
        "pdf_errors": sum(1 for r in ok if r.get("pdf") and "error" in r["pdf"]),
        "pdf_lib_missing": any(r.get("pdf") == {"pdf_lib": None} for r in ok),
        "pdf_count": len(pdfs),
        "event_input_tokens": _stats(event_tokens),
        "tokens_by_type": {t: _stats(v) for t, v in sorted(by_type.items())},
        "cost_estimate": costs,
        "xbrl": {"count": len(xmls), "with_financials": sum(1 for r in xmls if r["xml"].get("financial_facts")),
                 "types": dict(Counter(r["event_type"] for r in xmls))},
        "zip": {"count": len(zips), "with_pdf": sum(1 for r in zips if r["zip"].get("largest_pdf"))},
        "nse_bse_pairs": {"pairs": len(pairs), "identical_files": sum(pairs)},
    }


def _fmt(stats: dict | None, unit: str = "", scale: float = 1.0) -> str:
    if not stats:
        return "-"
    return f"median {stats['p50'] / scale:,.0f}{unit}, 90% under {stats['p90'] / scale:,.0f}{unit}, max {stats['max'] / scale:,.0f}{unit} (n={stats['n']})"


def format_summary(s: dict) -> str:
    lines = [f"DOCUMENT TEST: {s['attachments']} attachments from {s['events']} events"]
    for exchange, c in sorted(s["by_exchange"].items()):
        lines.append(f"  {exchange}: {c.get('ok', 0)} of {c['tried']} downloaded | time {_fmt(s['download_ms'].get(exchange), ' ms')}")
    if s["failures"]:
        lines.append("  failures: " + "; ".join(f"{k} x{v}" for k, v in s["failures"].items()))
    lines.append(f"  what they were: {', '.join(f'{k} {v}' for k, v in sorted(s['kinds'].items(), key=lambda kv: -kv[1]))}")
    lines.append(f"  size: {_fmt(s['bytes'], ' KB', 1024)}")
    if s["pdf_lib_missing"]:
        lines.append(f"  {PDF_LIB_MISSING}")
    else:
        lines.append(f"  PDFs: {s['pdf_count']} read; pages {_fmt(s['pdf_pages'])}")
        lines.append(f"    text, as tokens: {_fmt(s['pdf_tokens'])}")
        lines.append(f"    reading time: {_fmt(s['pdf_extract_ms'], ' ms')}")
        lines.append(f"    looked scanned (under {SCANNED_CHARS_PER_PAGE} characters a page): {s['pdf_scanned']}; unreadable: {s['pdf_errors']}")
        lines.append(f"  input per decision (document + {PROMPT_TOKENS:,} prompt tokens): {_fmt(s['event_input_tokens'])}")
        for t, st in s["tokens_by_type"].items():
            lines.append(f"    {t:<18} {_fmt(st)}")
        for model, c in s["cost_estimate"].items():
            lines.append(f"  {model}: ${c['per_event_usd']:.4f} an event, about ${c['per_month_usd']:,.2f} a month at 150 events a trading day")
    x = s["xbrl"]
    lines.append(f"  XBRL files: {x['count']} ({x['with_financials']} with financial figures); event types {x['types']}")
    lines.append(f"  ZIP files: {s['zip']['count']} ({s['zip']['with_pdf']} with a PDF inside)")
    p = s["nse_bse_pairs"]
    lines.append(f"  events with both an NSE and a BSE PDF: {p['pairs']}; byte-identical files: {p['identical_files']}")
    return "\n".join(lines)


def save_report(records: list[dict], summary: dict, out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"document-test-{utcnow().strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"summary": summary, "records": records}, indent=1, default=str), encoding="utf-8")
    return path
