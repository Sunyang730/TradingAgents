"""The "why is this on the list" narrative.

A Periodic Transaction Report discloses *what* was traded and *when*. It does
not disclose why, and nothing in the form carries the filer's reasoning. So the
thesis here is explicitly an inference drawn from public news over the period
between the trade and today — never a claim about what the member was thinking.

The window runs from the transaction date to the present rather than sitting
around the trade date, because Yahoo's per-ticker feed is depth-limited: for a
heavily covered mega-cap, fifty articles can span a single day, so a window
centred on a trade 30-90 days ago comes back empty. Widening to "trade date
until now" degrades gracefully — thinly covered names still surface news from
the period of the trade, and busy names surface current context.
"""

from __future__ import annotations

import logging
from datetime import date

from tradingagents.dataflows.interface import route_to_vendor

from .models import Candidate

logger = logging.getLogger(__name__)

# What the selection page shows when the news window came back empty. Stated
# plainly rather than filled with speculation.
NO_INFORMATION = "No information available on the why — no news found for this window."

_EMPTY_MARKERS = ("no news found", "error fetching news")

_SEED_PROMPT = """\
{member} disclosed a {amount} purchase of {ticker} ({name}) on {txn_date}.

The disclosure form gives no reason for the trade — it records only what was \
bought and when. Below is public news about {ticker} from the trade date to today.

{news}

In 2-3 sentences, say what in this news could make {ticker} attractive right now. \
Write about the company and its news only. Do not claim to know why {member} \
bought it, do not speculate about their motives or access to information, and do \
not mention the disclosure. If the news does not support any clear thesis, say so.\
"""

_PEER_PROMPT = """\
{ticker} ({name}) is a {basis} peer of {parent}, which was recently purchased and \
disclosed by a member of Congress. {ticker} itself was not disclosed by anyone.

Below is public news about {ticker} from {start} to today.

{news}

In 2-3 sentences, say what in this news could make {ticker} attractive right now. \
Write about the company and its news only. If the news does not support any clear \
thesis, say so.\
"""


def news_window(candidate: Candidate, as_of: date) -> tuple[str, str]:
    """The ``(start, end)`` news window for a candidate, as ISO strings."""
    dates = [t.txn_date for t in candidate.transactions]
    start = min(dates) if dates else as_of
    if start > as_of:
        start = as_of
    return start.isoformat(), as_of.isoformat()


def _fetch_news(ticker: str, start: str, end: str) -> str | None:
    """Return news text, or None when the window yielded nothing usable."""
    try:
        news = route_to_vendor("get_news", ticker, start, end)
    except Exception as exc:
        logger.warning("News fetch failed for %s: %s", ticker, exc)
        return None
    if not news or not news.strip():
        return None
    # The vendors report emptiness and failure as prose rather than raising.
    lowered = news.lower()
    if any(marker in lowered for marker in _EMPTY_MARKERS):
        return None
    return news


def build_thesis(candidate: Candidate, llm, as_of: date) -> tuple[str, tuple[str, str]]:
    """Write the candidate's thesis. Returns ``(thesis, window)``."""
    start, end = news_window(candidate, as_of)
    news = _fetch_news(candidate.ticker, start, end)
    if news is None:
        return NO_INFORMATION, (start, end)

    if candidate.origin == "seed" and candidate.transactions:
        txn = max(candidate.transactions, key=lambda t: t.amount_lower)
        prompt = _SEED_PROMPT.format(
            member=txn.member, amount=txn.amount_text, ticker=candidate.ticker,
            name=txn.asset_name, txn_date=txn.txn_date.isoformat(), news=news,
        )
    else:
        prompt = _PEER_PROMPT.format(
            ticker=candidate.ticker, name=candidate.peer_name or candidate.ticker,
            basis=candidate.peer_basis or "industry", parent=candidate.parent,
            start=start, news=news,
        )

    try:
        response = llm.invoke(prompt)
    except Exception as exc:
        logger.warning("Thesis generation failed for %s: %s", candidate.ticker, exc)
        return NO_INFORMATION, (start, end)

    text = getattr(response, "content", response)
    if isinstance(text, list):      # some providers return content blocks
        text = " ".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in text
        )
    text = str(text).strip()
    return (text or NO_INFORMATION), (start, end)
