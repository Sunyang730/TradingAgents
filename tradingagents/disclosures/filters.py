"""Which disclosed transactions become candidates.

The House publishes far more than is worth analysing: sales, exchanges, trivial
amounts, and instruments with no analysable symbol. These rules cut the feed to
what a multi-agent run can actually say something about.
"""

from __future__ import annotations

from datetime import date

from .models import Transaction

# The smallest bracket the House uses is $1,001-$15,000. A floor just above it
# drops positions too small to signal much without discarding a whole tier.
DEFAULT_MIN_AMOUNT = 15001
DEFAULT_WINDOW_DAYS = 90


def is_within_window(txn: Transaction, start: date, end: date) -> bool:
    """Whether the trade was *disclosed* inside the window.

    Notification date, not transaction date: a PTR may be filed up to 45 days
    after the trade, so filtering on the trade date would make a pull's
    contents depend on filing lag rather than on what is newly public.
    """
    return start <= txn.notification_date <= end


def select_transactions(
    transactions: list[Transaction],
    *,
    start: date,
    end: date,
    min_amount: int = DEFAULT_MIN_AMOUNT,
    purchases_only: bool = True,
) -> list[Transaction]:
    """Apply the seed inclusion rules.

    Managed and blind-trust holdings are deliberately kept — the member's own
    401k and a third-party-run trust are both filed as ``SUBHOLDING OF``, and
    the distinction between them is not in the document. The manager is
    recorded on the transaction so the provenance can say so plainly instead.
    """
    selected = []
    for txn in transactions:
        if purchases_only and not txn.is_purchase:
            continue
        if not is_within_window(txn, start, end):
            continue
        if txn.amount_lower < min_amount:
            continue
        selected.append(txn)
    return selected
