"""Public financial-disclosure ingestion.

Turns congressional trade disclosures into a ranked list of tickers for the
trading graph to analyse. ``HouseDisclosureSource`` is the only implementation
today; ``DisclosureSource`` is the seam a second jurisdiction plugs into.
"""

from .house import HouseDisclosureSource
from .models import Candidate, Transaction, UnreadableFiling
from .pipeline import (
    PullOptions,
    PullResult,
    latest_pick_dir,
    pull_stocks,
    result_to_dict,
    write_pull,
)
from .source import DisclosureBatch, DisclosureSource

__all__ = [
    "Candidate",
    "DisclosureBatch",
    "DisclosureSource",
    "HouseDisclosureSource",
    "PullOptions",
    "PullResult",
    "Transaction",
    "UnreadableFiling",
    "latest_pick_dir",
    "pull_stocks",
    "result_to_dict",
    "write_pull",
]
