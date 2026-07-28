"""``pull-stocks``: disclosures in, a ranked candidate list out.

The pull writes two artifacts into ``./picks/{timestamp}/``:

``seeds.json``
    Every candidate, ranked, with the provenance that justifies it. Immutable —
    ``stocks-review`` records its progress in a separate file so a re-run never
    rewrites the record of what was disclosed.
``selection.html``
    The same list, readable, with a link to each source filing.

The candidate list is complete rather than capped. Only the top slice gets an
LLM thesis, and the review cap is applied later, so widening either does not
require re-fetching and re-parsing every filing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from tradingagents.dataflows.symbol_utils import normalize_symbol

from .filters import DEFAULT_MIN_AMOUNT, DEFAULT_WINDOW_DAYS, select_transactions
from .models import Candidate, Transaction, UnreadableFiling
from .peers import DEFAULT_PEERS_PER_SEED, find_peers
from .ranking import rank_candidates
from .source import DisclosureSource
from .thesis import build_thesis

logger = logging.getLogger(__name__)

DEFAULT_THESIS_LIMIT = 25
PICKS_DIRNAME = "picks"


@dataclass
class PullOptions:
    days: int = DEFAULT_WINDOW_DAYS
    min_amount: int = DEFAULT_MIN_AMOUNT
    peers: int = DEFAULT_PEERS_PER_SEED
    thesis_limit: int = DEFAULT_THESIS_LIMIT
    as_of: date = field(default_factory=date.today)


@dataclass
class PullResult:
    candidates: list[Candidate]
    unreadable: list[UnreadableFiling]
    filings_seen: int
    transactions_selected: int
    source_name: str
    options: PullOptions
    generated_at: datetime

    @property
    def seeds(self) -> list[Candidate]:
        return [c for c in self.candidates if c.origin == "seed"]

    @property
    def peer_candidates(self) -> list[Candidate]:
        return [c for c in self.candidates if c.origin == "peer"]


def _group_into_seeds(transactions: list[Transaction]) -> list[Candidate]:
    """Collapse transactions into one candidate per ticker.

    Several members buying the same ticker is the signal we rank on, so the
    grouping is by symbol and every contributing transaction is retained.
    """
    by_ticker: dict[str, Candidate] = {}
    for txn in transactions:
        symbol = normalize_symbol(txn.ticker)
        candidate = by_ticker.get(symbol)
        if candidate is None:
            candidate = Candidate(ticker=symbol, asset_class=txn.asset_class, origin="seed")
            by_ticker[symbol] = candidate
        candidate.transactions.append(txn)
    return list(by_ticker.values())


def _expand_peers(seeds: list[Candidate], limit: int) -> list[Candidate]:
    """Add industry/sector peers for each seed, deduped against everything seen."""
    if limit <= 0:
        return []

    seen = {c.ticker.upper() for c in seeds}
    peers: list[Candidate] = []
    for seed in seeds:
        found, basis, group = find_peers(seed.ticker, limit=limit, exclude=seen)
        for ticker, name, weight in found:
            symbol = normalize_symbol(ticker)
            if symbol.upper() in seen:
                continue
            seen.add(symbol.upper())
            peers.append(
                Candidate(
                    ticker=symbol,
                    asset_class=seed.asset_class,
                    origin="peer",
                    parent=seed.ticker,
                    peer_name=name,
                    market_weight=weight,
                    peer_basis=basis,
                    peer_group=group,
                )
            )
    return peers


def pull_stocks(
    source: DisclosureSource,
    llm=None,
    options: PullOptions | None = None,
) -> PullResult:
    """Run the pull: fetch, filter, expand, rank, then write theses."""
    options = options or PullOptions()
    end = options.as_of
    start = end - timedelta(days=options.days)

    batch = source.fetch(start, end)
    selected = select_transactions(
        batch.transactions, start=start, end=end, min_amount=options.min_amount
    )

    seeds = _group_into_seeds(selected)
    candidates = rank_candidates(seeds + _expand_peers(seeds, options.peers))

    # Theses cost a news fetch and an LLM call each, so only the slice that
    # could plausibly be analysed gets one. The rest keep thesis=None, which
    # the page renders as "not generated" — distinct from "no news found".
    if llm is not None and options.thesis_limit > 0:
        for candidate in candidates[: options.thesis_limit]:
            candidate.thesis, candidate.thesis_news_window = build_thesis(
                candidate, llm, end
            )

    return PullResult(
        candidates=candidates,
        unreadable=batch.unreadable,
        filings_seen=batch.filings_seen,
        transactions_selected=len(selected),
        source_name=batch.source_name,
        options=options,
        generated_at=datetime.now(),
    )


def result_to_dict(result: PullResult) -> dict:
    return {
        "generated_at": result.generated_at.isoformat(timespec="seconds"),
        "source": result.source_name,
        "window": {
            "start": (result.options.as_of - timedelta(days=result.options.days)).isoformat(),
            "end": result.options.as_of.isoformat(),
            "days": result.options.days,
        },
        "filters": {
            "purchases_only": True,
            "min_amount": result.options.min_amount,
            "peers_per_seed": result.options.peers,
            "thesis_limit": result.options.thesis_limit,
        },
        "counts": {
            "filings_seen": result.filings_seen,
            "transactions_selected": result.transactions_selected,
            "seeds": len(result.seeds),
            "peers": len(result.peer_candidates),
            "total": len(result.candidates),
            "unreadable_filings": len(result.unreadable),
        },
        # Filings we could not read are reported, not dropped: a pull that
        # silently skipped scanned paper would overstate its own completeness.
        "unreadable_filings": [u.to_dict() for u in result.unreadable],
        "candidates": [c.to_dict() for c in result.candidates],
    }


def write_pull(result: PullResult, root: str | Path = ".") -> Path:
    """Write ``seeds.json`` and ``selection.html``; return the pick directory."""
    from .render import render_selection_html

    stamp = result.generated_at.strftime("%Y%m%d_%H%M%S")
    pick_dir = Path(root) / PICKS_DIRNAME / stamp
    pick_dir.mkdir(parents=True, exist_ok=True)

    payload = result_to_dict(result)
    (pick_dir / "seeds.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    (pick_dir / "selection.html").write_text(
        render_selection_html(result), encoding="utf-8"
    )
    return pick_dir


def latest_pick_dir(root: str | Path = ".") -> Path | None:
    """Most recent pick directory, or None when nothing has been pulled."""
    picks = Path(root) / PICKS_DIRNAME
    if not picks.is_dir():
        return None
    dirs = sorted(d for d in picks.iterdir() if (d / "seeds.json").exists())
    return dirs[-1] if dirs else None
