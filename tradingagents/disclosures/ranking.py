"""Ordering candidates so a capped run analyses the most interesting ones.

Deliberately lexicographic rather than a weighted score: every position in the
list can be explained in one sentence ("three members bought it"), and there
are no invented coefficients to defend or tune.
"""

from __future__ import annotations

from datetime import date

from .models import Candidate

_EPOCH = date.min


def _seed_key(candidate: Candidate) -> tuple:
    """Sort key for a disclosed purchase, most interesting first.

    1. How many distinct members bought it. Independent people reaching the
       same conclusion is the only real conviction signal in this data.
    2. The largest disclosed bracket floor. Amounts are ranges, so the floor is
       the only comparable figure.
    3. How recently it was disclosed.
    """
    return (
        -candidate.cluster,
        -candidate.amount_lower,
        -(candidate.latest_notification or _EPOCH).toordinal(),
        candidate.ticker,
    )


def _peer_key(candidate: Candidate, seed_order: dict[str, int]) -> tuple:
    """Sort key for an expanded peer: follow its parent, then market weight."""
    return (
        seed_order.get(candidate.parent or "", len(seed_order)),
        -(candidate.market_weight or 0.0),
        candidate.ticker,
    )


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Return candidates ordered for analysis, with ``rank`` assigned.

    Peers rank below every seed: a ticker somebody actually bought outranks one
    we merely inferred is similar.
    """
    seeds = sorted([c for c in candidates if c.origin == "seed"], key=_seed_key)
    seed_order = {c.ticker: i for i, c in enumerate(seeds)}
    peers = sorted(
        [c for c in candidates if c.origin != "seed"],
        key=lambda c: _peer_key(c, seed_order),
    )

    ordered = seeds + peers
    for position, candidate in enumerate(ordered, start=1):
        candidate.rank = position
    return ordered


def rank_reason(candidate: Candidate) -> str:
    """One human-readable sentence explaining a candidate's position."""
    if candidate.origin == "seed":
        members = candidate.cluster
        who = "1 member" if members == 1 else f"{members} members"
        amount = f"${candidate.amount_lower:,}+" if candidate.amount_lower else "an undisclosed amount"
        return f"Disclosed purchase by {who}, largest bracket from {amount}."
    basis = candidate.peer_basis or "industry"
    weight = (
        f", {candidate.market_weight:.1%} of the group by market weight"
        if candidate.market_weight
        else ""
    )
    return f"Not disclosed — {basis} peer of {candidate.parent}{weight}."
