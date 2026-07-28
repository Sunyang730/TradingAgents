"""Data types shared by the disclosure pipeline.

A ``Transaction`` is one line of one filing, kept as close to what was actually
disclosed as possible. A ``Candidate`` is a ticker the pipeline proposes for
analysis, carrying the provenance that justifies it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

# Owner codes used in the House form: the filer, their spouse, a dependent
# child, or a jointly held account.
OWNER_CODES = {"SP": "spouse", "DC": "dependent child", "JT": "joint"}

# Asset-type codes. Only a few matter to us; the full list is published at
# https://fd.house.gov/reference/asset-type-codes.aspx.
ASSET_STOCK = "ST"
ASSET_ETF = "ET"
ASSET_CRYPTO = "CT"
ASSET_OPTION = "OP"

# Asset classes the trading graph can meaningfully analyse. Options name a
# contract rather than an instrument, and mutual funds / government securities
# are frequently free-text with no resolvable ticker.
ANALYSABLE_ASSET_CLASSES = (ASSET_STOCK, ASSET_ETF, ASSET_CRYPTO)

PURCHASE_TYPES = ("P",)


@dataclass(frozen=True)
class Transaction:
    """One disclosed transaction line."""

    ticker: str
    asset_name: str
    asset_class: str          # ST, ET, OP, MF, GS, CT, ...
    txn_type: str             # P, S, E
    partial: bool
    txn_date: date
    notification_date: date
    amount_text: str          # as filed, e.g. "$1,001 - $15,000"
    amount_lower: int         # lower bound of the bracket, for ordering
    owner: str                # "", SP, DC, JT
    managed_by: str           # SUBHOLDING OF value; "" when self-directed
    member: str
    state_district: str
    doc_id: str
    filing_date: date
    source_url: str

    @property
    def is_purchase(self) -> bool:
        return self.txn_type in PURCHASE_TYPES

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("txn_date", "notification_date", "filing_date"):
            d[key] = d[key].isoformat()
        return d


@dataclass
class Candidate:
    """A ticker proposed for analysis, with the provenance that justifies it."""

    ticker: str
    asset_class: str
    origin: str                       # "seed" or "peer"
    transactions: list[Transaction] = field(default_factory=list)
    # Peer-only provenance.
    parent: str | None = None
    peer_name: str | None = None      # company name, since peers have no filing
    market_weight: float | None = None
    peer_basis: str | None = None     # "industry" or "sector"
    peer_group: str | None = None     # the industry/sector key used
    # Filled in later by the thesis step.
    thesis: str | None = None
    thesis_news_window: tuple[str, str] | None = None
    rank: int | None = None

    @property
    def members(self) -> list[str]:
        """Distinct members who disclosed a transaction in this ticker."""
        return sorted({t.member for t in self.transactions})

    @property
    def cluster(self) -> int:
        """How many distinct members bought it — the conviction signal."""
        return len(self.members)

    @property
    def amount_lower(self) -> int:
        """Largest disclosed bracket floor across this ticker's transactions."""
        return max((t.amount_lower for t in self.transactions), default=0)

    @property
    def latest_notification(self) -> date | None:
        return max((t.notification_date for t in self.transactions), default=None)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "asset_class": self.asset_class,
            "origin": self.origin,
            "rank": self.rank,
            "cluster": self.cluster,
            "members": self.members,
            "amount_lower": self.amount_lower,
            "latest_notification": (
                self.latest_notification.isoformat() if self.latest_notification else None
            ),
            "parent": self.parent,
            "peer_name": self.peer_name,
            "market_weight": self.market_weight,
            "peer_basis": self.peer_basis,
            "peer_group": self.peer_group,
            "thesis": self.thesis,
            "thesis_news_window": list(self.thesis_news_window)
            if self.thesis_news_window
            else None,
            "transactions": [t.to_dict() for t in self.transactions],
        }


@dataclass(frozen=True)
class UnreadableFiling:
    """A filing we could not extract text from.

    Recorded rather than dropped: a small share of House PTRs are scanned paper
    rather than electronic submissions, and silently discarding them would
    misrepresent the pull as complete.
    """

    doc_id: str
    member: str
    filing_date: date
    source_url: str
    reason: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["filing_date"] = d["filing_date"].isoformat()
        return d
