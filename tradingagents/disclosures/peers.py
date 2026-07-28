"""Expanding a disclosed ticker into comparable companies.

Yahoo groups every listed company into a sector and a narrower industry, and
publishes each group's constituents ranked by market weight. That is a
deterministic, keyless answer to "what else is like this", which is why the
expansion does not ask an LLM to name peers: the model would be slower,
non-reproducible across runs, and capable of inventing tickers that do not
trade.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_PEERS_PER_SEED = 5

# Below this many names, an industry is too thin to be a useful comparison set
# and the broader sector is a better answer than a near-empty list.
MIN_INDUSTRY_SIZE = 3


def _group_members(key: str, kind: str) -> list[tuple[str, str, float]]:
    """Return ``(ticker, name, market_weight)`` for one Yahoo sector/industry."""
    import yfinance as yf

    factory = yf.Industry if kind == "industry" else yf.Sector
    frame = factory(key).top_companies
    if frame is None or frame.empty:
        return []

    members = []
    for ticker, row in frame.iterrows():
        try:
            weight = float(row.get("market weight") or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        members.append((str(ticker), str(row.get("name") or ""), weight))
    return members


def find_peers(
    ticker: str,
    *,
    limit: int = DEFAULT_PEERS_PER_SEED,
    exclude: set[str] | None = None,
) -> tuple[list[tuple[str, str, float]], str | None, str | None]:
    """Find comparable companies for ``ticker``.

    Returns ``(peers, basis, group_key)`` where ``basis`` is "industry" or
    "sector". Falls back from industry to sector when the ticker has no
    industry classification or its industry is too thin to compare against.
    Returns an empty list rather than raising: a seed whose peers cannot be
    resolved is still a perfectly good seed.
    """
    import yfinance as yf

    exclude = {t.upper() for t in (exclude or set())}
    exclude.add(ticker.upper())

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:
        logger.warning("Peer lookup failed for %s: %s", ticker, exc)
        return [], None, None

    for kind in ("industry", "sector"):
        key = info.get(f"{kind}Key")
        if not key:
            continue
        try:
            members = _group_members(key, kind)
        except Exception as exc:
            logger.warning("%s lookup failed for %s (%s): %s", kind, ticker, key, exc)
            continue

        candidates = [m for m in members if m[0].upper() not in exclude]
        # A thin industry says more about Yahoo's taxonomy than about the
        # company, so widen rather than return a token peer or two.
        if kind == "industry" and len(members) < MIN_INDUSTRY_SIZE:
            continue
        if candidates:
            return candidates[:limit], kind, key

    return [], None, None
