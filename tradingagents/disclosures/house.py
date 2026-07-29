"""US House of Representatives disclosure source.

The Clerk publishes one ZIP per year containing a tab-delimited index of every
financial-disclosure filing. Periodic Transaction Reports (``FilingType == "P"``)
are the ones that name individual trades; each row's ``DocID`` addresses a PDF.

Most PTRs are filed electronically and extract as text. A minority are scanned
paper, which extract as nothing — those are reported as ``UnreadableFiling``
rather than dropped, so a pull never silently understates what was disclosed.
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import date
from pathlib import Path

import requests

from .models import UnreadableFiling
from .parsing import (
    PTR_FILING_TYPE,
    index_member_name,
    parse_filing_date,
    parse_index,
    parse_ptr_text,
)
from .source import DisclosureBatch

logger = logging.getLogger(__name__)

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

# The Clerk's site rejects requests without a UA. Identify the tool honestly.
USER_AGENT = "TradingAgents disclosure reader (+https://github.com/TauricResearch/TradingAgents)"

REQUEST_TIMEOUT = 30


class HouseDisclosureSource:
    """Reads House PTRs for a date window."""

    name = "us-house"

    def __init__(self, cache_dir: str | Path | None = None, session=None):
        # expanduser so a configured "~/.tradingagents/cache" caches where the
        # user meant, rather than in a literal "~" directory beside the cwd.
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)

    # --- fetching ---------------------------------------------------------

    def _get(self, url: str) -> bytes:
        response = self.session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.content

    def _cached(self, key: str, fetch) -> bytes:
        """Fetch through an on-disk cache when one is configured.

        Filings are immutable once published, so a cache hit is always valid
        and keeps repeat pulls off the Clerk's servers.
        """
        if not self.cache_dir:
            return fetch()
        path = self.cache_dir / "house_disclosures" / key
        if path.exists():
            return path.read_bytes()
        data = fetch()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def fetch_index(self, year: int) -> list[dict]:
        """Return the year's index rows, PTRs only."""
        raw = self._cached(f"{year}FD.zip", lambda: self._get(INDEX_URL.format(year=year)))
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".txt")]
            if not names:
                raise ValueError(f"No index text file in the {year} House disclosure archive")
            text = archive.read(names[0]).decode("utf-8", errors="replace")
        return [r for r in parse_index(text) if r.get("filing_type") == PTR_FILING_TYPE]

    def fetch_ptr_text(self, year: int, doc_id: str) -> str:
        """Extract the text of one PTR PDF. Empty string when it is a scan."""
        import pdfplumber

        raw = self._cached(
            f"{year}/{doc_id}.pdf",
            lambda: self._get(PTR_PDF_URL.format(year=year, doc_id=doc_id)),
        )
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)

    # --- the seam ---------------------------------------------------------

    def fetch(self, start: date, end: date) -> DisclosureBatch:
        batch = DisclosureBatch(source_name=self.name)

        for year in range(start.year, end.year + 1):
            try:
                rows = self.fetch_index(year)
            except Exception as exc:
                # A missing year (e.g. the window reaches into a year the Clerk
                # has not published) must not abort a pull that has data.
                logger.warning("House index for %s unavailable: %s", year, exc)
                continue

            for row in rows:
                filing_date = parse_filing_date(row.get("filing_date", ""))
                if filing_date is None or not (start <= filing_date <= end):
                    continue

                doc_id = row.get("doc_id", "").strip()
                if not doc_id:
                    continue

                batch.filings_seen += 1
                member = index_member_name(row)
                url = PTR_PDF_URL.format(year=year, doc_id=doc_id)

                try:
                    text = self.fetch_ptr_text(year, doc_id)
                except Exception as exc:
                    batch.unreadable.append(
                        UnreadableFiling(
                            doc_id=doc_id, member=member, filing_date=filing_date,
                            source_url=url, reason=f"fetch failed: {exc}",
                        )
                    )
                    continue

                if not text.strip():
                    # Scanned paper rather than an electronic submission.
                    batch.unreadable.append(
                        UnreadableFiling(
                            doc_id=doc_id, member=member, filing_date=filing_date,
                            source_url=url, reason="no extractable text (scanned filing)",
                        )
                    )
                    continue

                batch.transactions.extend(
                    parse_ptr_text(
                        text,
                        member=member,
                        state_district=row.get("state_district", ""),
                        doc_id=doc_id,
                        filing_date=filing_date,
                        source_url=url,
                    )
                )

        return batch
