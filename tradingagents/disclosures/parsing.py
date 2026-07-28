"""Pure parsing of House disclosure artifacts: the yearly index and PTR text.

Everything here is a plain function over strings so it can be exercised without
a network. The fetching side lives in ``house.py``.

Two properties of the source documents drive the shape of this code:

* The index is a tab-delimited file with CRLF line endings, so fields carry a
  trailing ``\\r`` unless stripped.
* PTR PDFs render their section headers in letter-spaced small-caps. Only the
  leading capital of each word survives extraction, and the intervening glyphs
  come through as **NUL bytes** rather than spaces: ``FILING STATUS`` extracts
  as ``F\\x00\\x00\\x00\\x00\\x00 S\\x00\\x00\\x00\\x00\\x00:``. Text is
  normalised before matching, and headers are matched by shape rather than by
  their literal text.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .models import Transaction

# --- index ----------------------------------------------------------------

INDEX_COLUMNS = (
    "prefix", "last", "first", "suffix",
    "filing_type", "state_district", "year", "filing_date", "doc_id",
)

# Periodic Transaction Report. Other codes in the same index are annual
# reports, amendments, extensions and termination filings.
PTR_FILING_TYPE = "P"


def parse_index(text: str) -> list[dict]:
    """Parse the yearly ``{YEAR}FD.txt`` index into row dicts."""
    rows = []
    lines = text.splitlines()
    for line in lines[1:]:            # first line is the header
        line = line.rstrip("\r")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < len(INDEX_COLUMNS):
            continue
        # Not strict: the index carries trailing columns in some years, and
        # extra fields past DocID are of no interest here.
        row = dict(zip(INDEX_COLUMNS, (p.strip() for p in parts), strict=False))
        rows.append(row)
    return rows


def index_member_name(row: dict) -> str:
    """Human-readable member name from an index row."""
    parts = [row.get("first", ""), row.get("last", ""), row.get("suffix", "")]
    return " ".join(p for p in parts if p).strip()


def parse_filing_date(raw: str) -> date | None:
    """Parse the index's ``M/D/YYYY`` filing date."""
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%-m/%-d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# --- PTR body -------------------------------------------------------------

# A transaction row: an optional owner code, the asset description, the
# transaction type, two dates, and the amount bracket. The asset name is
# non-greedy so the type letter binds to the first standalone P/S/E that is
# followed by a date rather than to a letter inside the company name.
_TXN_ROW = re.compile(
    r"""^
    (?:(?P<owner>SP|DC|JT)\s+)?
    (?P<asset>.+?)\s+
    (?P<type>[PSE])
    (?P<partial>\s*\(partial\))?\s+
    (?P<txn_date>\d{2}/\d{2}/\d{4})\s+
    (?P<notification>\d{2}/\d{2}/\d{4})\s+
    (?P<amount>\$[\d,]+(?:\s*-\s*(?:\$[\d,]+)?)?|\$[\d,]+\s*\+)
    """,
    re.VERBOSE,
)

# Ticker and asset-type tag, e.g. "(BBAI) [ST]", when they are adjacent.
_TICKER_TAG = re.compile(r"\((?P<ticker>[A-Z][A-Z0-9.\-/]{0,9})\)\s*\[(?P<asset_class>[A-Z]{2})\]")
# A parenthesised ticker on its own. Filings wrap the asset-type tag onto the
# next line often enough that requiring the pair would drop real rows. The
# uppercase-only shape keeps this from matching "(partial)" or "(Owner: SP)".
_TICKER_ONLY = re.compile(r"\((?P<ticker>[A-Z][A-Z0-9.\-/]{0,9})\)")
# An asset-type tag with no ticker at all, e.g. bonds filed as free text.
_BARE_TAG = re.compile(r"\[(?P<asset_class>[A-Z]{2})\]")

_AMOUNT_CONTINUATION = re.compile(r"^\$?[\d,]+$")
_AMOUNT_TAIL = re.compile(r"\$[\d,]+\s*$")

# Letter-spaced small-caps headers, identified by shape: a leading capital, a
# run of filler, and a colon. ``SUBHOLDING OF`` is the only one whose value we
# read; the rest matter because they mark where a row's wrapped detail ends.
_SUBHOLDING = re.compile(r"^S\s{2,}O\s*:\s*(?P<value>.+)$")
_MANGLED_HEADER = re.compile(r"^[A-Z](?:\s{2,}[A-Z\s]*)?\s*:")

# Page furniture. Multi-page filings repeat the table header and footer on
# every page, so these are skipped rather than treated as the end of the table
# — stopping at the first footer would silently truncate a filing to page one.
# They are dropped before continuation lines are collected so they cannot be
# appended to the preceding row's asset name.
_PAGE_NOISE = re.compile(
    r"^(?:"
    r"Filing ID\s*#|Clerk of the House|Name:|Status:|State/District:"
    r"|ID Owner Asset|Type Date|\$200\?|\*\s|Digitally Signed"
    r"|I CERTIFY|my knowledge|Yes No"
    r")"
)


def normalize_pdf_text(text: str) -> str:
    """Make extracted PTR text safe to match against.

    Letter-spaced headers extract with NUL bytes where the missing glyphs were;
    turning them into spaces is what lets the header shapes above match at all.
    """
    return (text or "").replace("\x00", " ")


def _to_date(raw: str) -> date:
    return datetime.strptime(raw, "%m/%d/%Y").date()


def amount_lower_bound(amount_text: str) -> int:
    """Lower bound of a disclosed amount bracket, in whole dollars.

    House PTRs disclose ranges rather than values, so the floor is the only
    figure available for ordering. ``"$15,001 - $50,000"`` yields ``15001``.
    """
    match = re.search(r"\$\s*([\d,]+)", amount_text or "")
    if not match:
        return 0
    return int(match.group(1).replace(",", ""))


def _clean_asset_name(raw: str) -> str:
    """Strip the ticker/asset-class tag and tidy whitespace."""
    name = _TICKER_TAG.sub("", raw)
    name = _TICKER_ONLY.sub("", name)
    name = _BARE_TAG.sub("", name)
    name = _AMOUNT_TAIL.sub("", name)      # a wrapped bracket tail, not a name
    return re.sub(r"\s+", " ", name).strip(" -–")


def parse_ptr_text(
    text: str,
    *,
    member: str,
    state_district: str,
    doc_id: str,
    filing_date: date,
    source_url: str,
) -> list[Transaction]:
    """Extract transaction lines from the text of one PTR PDF.

    Rows whose ticker cannot be resolved are skipped: without a symbol there is
    nothing for the trading graph to analyse.
    """
    transactions: list[Transaction] = []
    lines = [ln.rstrip() for ln in normalize_pdf_text(text).splitlines()]

    # Collect (row_match, continuation_lines) pairs first, so a row can read
    # the lines that belong to it — the ticker and the tail of a wrapped
    # amount both land on following lines.
    pending: list[tuple[re.Match, list[str]]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or _PAGE_NOISE.match(stripped):
            continue
        match = _TXN_ROW.match(stripped)
        if match:
            pending.append((match, []))
        elif pending:
            pending[-1][1].append(stripped)

    for match, continuation in pending:
        asset_raw = match.group("asset")
        amount_text = match.group("amount").strip()

        # A row's asset name, ticker and amount may wrap, but only onto the
        # lines before its first detail header. Anything past that is
        # FILING STATUS / SUBHOLDING OF / DESCRIPTION — and DESCRIPTION often
        # names *other* tickers ("AAPL - 20.313 shares sold @ ..."), which
        # would otherwise bleed into the wrong row.
        wrapped: list[str] = []
        for cont in continuation:
            if _MANGLED_HEADER.match(cont):
                break
            wrapped.append(cont)

        for cont in wrapped:
            # A bracket split across lines: "$15,001 -" then "$50,000".
            if amount_text.rstrip().endswith("-") and _AMOUNT_CONTINUATION.match(cont):
                amount_text = f"{amount_text} {cont}".strip()
            else:
                asset_raw = f"{asset_raw} {cont}"

        # The upper bound sometimes wraps onto a line that also carries the
        # asset's tail, e.g. "Stock (HONAV) [ST] $50,000". The lower bound is
        # what we order on, so this only affects how the bracket reads.
        if amount_text.rstrip().endswith("-"):
            tail = _AMOUNT_TAIL.search(asset_raw)
            if tail:
                amount_text = f"{amount_text} {tail.group(0).strip()}"

        ticker = ""
        asset_class = ""
        tag = _TICKER_TAG.search(asset_raw)
        if tag:
            ticker, asset_class = tag.group("ticker"), tag.group("asset_class")
        else:
            # The pair is commonly split across the wrap, e.g. "(AAPL)" ending
            # one line and "[ST]" opening the next.
            only = _TICKER_ONLY.search(asset_raw)
            bare = _BARE_TAG.search(asset_raw)
            if only:
                ticker = only.group("ticker")
            if bare:
                asset_class = bare.group("asset_class")

        if not ticker:
            continue

        managed_by = ""
        for cont in continuation:
            sub = _SUBHOLDING.match(cont)
            if sub:
                managed_by = sub.group("value").strip()
                break

        transactions.append(
            Transaction(
                ticker=ticker,
                asset_name=_clean_asset_name(asset_raw),
                asset_class=asset_class,
                txn_type=match.group("type"),
                partial=bool(match.group("partial")),
                txn_date=_to_date(match.group("txn_date")),
                notification_date=_to_date(match.group("notification")),
                amount_text=re.sub(r"\s+", " ", amount_text),
                amount_lower=amount_lower_bound(amount_text),
                owner=match.group("owner") or "",
                managed_by=managed_by,
                member=member,
                state_district=state_district,
                doc_id=doc_id,
                filing_date=filing_date,
                source_url=source_url,
            )
        )

    return transactions
