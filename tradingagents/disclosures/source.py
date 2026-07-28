"""The disclosure-source seam.

One implementation ships today (US House). The protocol exists so a second
jurisdiction — the Senate is the obvious next one — becomes a new module rather
than a refactor of the pipeline.

A source's whole job is to turn a date window into ``Transaction`` records plus
an honest account of what it could not read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from .models import Transaction, UnreadableFiling


@dataclass
class DisclosureBatch:
    """Everything one source produced for a window."""

    transactions: list[Transaction] = field(default_factory=list)
    unreadable: list[UnreadableFiling] = field(default_factory=list)
    filings_seen: int = 0
    source_name: str = ""


@runtime_checkable
class DisclosureSource(Protocol):
    """A jurisdiction that publishes machine-readable trade disclosures."""

    name: str

    def fetch(self, start: date, end: date) -> DisclosureBatch:
        """Return disclosures notified within ``[start, end]`` inclusive."""
        ...
