"""Security master: maps NSE symbols, BSE scrip codes and company names to ISINs.

Loads CSV files from data/securities/:
  - NSE's list of listed equities (EQUITY_L.csv) and SME equities (SME_EQUITY_L.csv),
    downloadable with `python -m decision_service securities --download`;
  - optionally BSE's "List of Scrips" CSV, saved by hand from bseindia.com.
Columns are found by name, so either exchange's file layout works.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

from .http import HttpClient
from .models import Filing, company_key

DEFAULT_DIR = Path("data") / "securities"

NSE_DOWNLOADS = {
    "nse_equity_l.csv": (
        "https://nsearchives.nseindia.in/content/equities/EQUITY_L.csv",
        "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    ),
    "nse_sme_equity_l.csv": (
        "https://nsearchives.nseindia.in/emerge/corporates/content/SME_EQUITY_L.csv",
        "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv",
    ),
}

_ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")
_COLUMNS = {
    "isin": ("isinnumber", "isinno", "isin", "isincode"),
    "nse_symbol": ("symbol",),
    "bse_code": ("securitycode", "scripcode", "fininstrmid"),
    "name": ("nameofcompany", "issuername", "companyname", "securityname", "fininstrmnm"),
}
_AMBIGUOUS = ""


def _norm(header: str) -> str:
    return re.sub(r"[^a-z]", "", (header or "").lower())


def _pick(row: dict[str, str], field: str) -> str | None:
    for key in _COLUMNS[field]:
        value = row.get(key)
        if value and value.strip() and value.strip() != "-":
            return value.strip()
    return None


@dataclass
class Security:
    isin: str
    name: str
    nse_symbol: str | None = None
    bse_code: str | None = None


class SecurityMaster:
    def __init__(self) -> None:
        self.by_isin: dict[str, Security] = {}
        self.by_symbol: dict[str, str] = {}
        self.by_bse_code: dict[str, str] = {}
        self.by_name: dict[str, str] = {}
        self.files: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self.by_isin)

    @classmethod
    def load_dir(cls, directory: str | Path = DEFAULT_DIR) -> "SecurityMaster":
        master = cls()
        directory = Path(directory)
        if directory.is_dir():
            for path in sorted(directory.glob("*.csv")):
                master.load_file(path)
        return master

    def load_file(self, path: str | Path) -> int:
        path = Path(path)
        return self.load_text(path.read_text(encoding="utf-8-sig", errors="replace"), path.name)

    def load_text(self, text: str, label: str = "csv") -> int:
        reader = csv.DictReader(io.StringIO(text))
        count = 0
        for raw in reader:
            row = {_norm(k): (v or "") for k, v in raw.items() if k}
            isin = (_pick(row, "isin") or "").upper()
            if not _ISIN_RE.match(isin):
                continue
            name = _pick(row, "name") or ""
            # A BSE file has a numeric code column; NSE files have a text symbol.
            code = _pick(row, "bse_code")
            symbol = None if code else _pick(row, "nse_symbol")
            security = self.by_isin.get(isin)
            if security is None:
                security = self.by_isin[isin] = Security(isin=isin, name=name)
            if symbol:
                security.nse_symbol = security.nse_symbol or symbol
                self.by_symbol[symbol.upper()] = isin
            if code:
                security.bse_code = security.bse_code or code
                self.by_bse_code[code] = isin
            key = company_key(name)
            if key:
                existing = self.by_name.get(key)
                if existing is None:
                    self.by_name[key] = isin
                elif existing != isin:
                    self.by_name[key] = _AMBIGUOUS  # two securities share a name: don't guess
            count += 1
        self.files[label] = count
        return count

    def resolve(self, filing: Filing) -> tuple[str, Security | None]:
        """Return (company_id, security). company_id is the ISIN when known, else 'name:<key>'."""
        isin = None
        if filing.isin and filing.isin.upper() in self.by_isin:
            isin = filing.isin.upper()
        elif filing.exchange == "NSE" and filing.symbol and filing.symbol.upper() in self.by_symbol:
            isin = self.by_symbol[filing.symbol.upper()]
        elif filing.exchange == "BSE" and filing.symbol and filing.symbol in self.by_bse_code:
            isin = self.by_bse_code[filing.symbol]
        key = company_key(filing.company)
        if isin is None and key:
            isin = self.by_name.get(key) or None
        if isin:
            return isin, self.by_isin[isin]
        if filing.isin:
            return filing.isin.upper(), None
        return f"name:{key}", None


def download(directory: str | Path = DEFAULT_DIR, client: HttpClient | None = None) -> list[tuple[str, int, str]]:
    """Fetch NSE's equity lists. Returns (file name, rows loaded, URL used) per file."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    client = client or HttpClient()
    results = []
    for filename, urls in NSE_DOWNLOADS.items():
        last_error = "no URL tried"
        for url in urls:
            try:
                fetch = client.get(url, headers={"Accept": "text/csv,*/*"})
            except Exception as exc:  # network failure: try the next address
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if not fetch.ok:
                last_error = f"HTTP {fetch.status}"
                continue
            text = fetch.body.decode("utf-8-sig", errors="replace")
            rows = SecurityMaster().load_text(text, filename)
            if rows == 0:
                last_error = "no rows with an ISIN"
                continue
            (directory / filename).write_text(text, encoding="utf-8")
            results.append((filename, rows, url))
            break
        else:
            results.append((filename, 0, last_error))
    return results
