"""Rendering the selection page.

Self-contained by necessity: these pages are opened from the filesystem, where
a strict browser blocks ``fetch()`` of local files as cross-origin. Anything
loaded at runtime — a CDN stylesheet, a markdown renderer, the JSON itself —
would silently fail. So the CSS is inline and the content is baked in.
"""

from __future__ import annotations

from html import escape

from .models import ANALYSABLE_ASSET_CLASSES
from .ranking import rank_reason
from .thesis import NO_INFORMATION

_CSS = """
:root { color-scheme: light dark; --fg:#1a1a1a; --muted:#666; --bg:#fff;
  --card:#f7f7f8; --line:#e3e3e6; --accent:#1a5fb4; --warn:#8a5a00; }
@media (prefers-color-scheme: dark) { :root { --fg:#e8e8ea; --muted:#a0a0a8;
  --bg:#16161a; --card:#1e1e24; --line:#2e2e36; --accent:#7cb0ff; --warn:#e0b050; } }
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
  font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size:1.6rem; margin:0 0 .25rem; }
.sub { color:var(--muted); margin:0 0 1.5rem; font-size:.92rem; }
.stats { display:flex; flex-wrap:wrap; gap:.5rem 1.5rem; padding:.85rem 1rem;
  background:var(--card); border:1px solid var(--line); border-radius:8px; margin-bottom:1.5rem; }
.stats div { font-size:.88rem; }
.stats b { display:block; font-size:1.25rem; font-weight:600; }
.note { border-left:3px solid var(--warn); background:var(--card); padding:.7rem .9rem;
  border-radius:0 6px 6px 0; margin-bottom:1.5rem; font-size:.9rem; }
.card { border:1px solid var(--line); border-radius:8px; padding:1rem 1.1rem;
  margin-bottom:.85rem; background:var(--card); }
.card.peer { background:transparent; }
.hd { display:flex; align-items:baseline; gap:.6rem; flex-wrap:wrap; }
.rank { color:var(--muted); font-variant-numeric:tabular-nums; font-size:.85rem; }
.tic { font-size:1.15rem; font-weight:650; }
.name { color:var(--muted); font-size:.9rem; }
.tag { font-size:.72rem; text-transform:uppercase; letter-spacing:.04em;
  padding:.12rem .45rem; border:1px solid var(--line); border-radius:99px; color:var(--muted); }
.tag.seed { color:var(--accent); border-color:var(--accent); }
.why { margin:.6rem 0 .5rem; }
.why.none { color:var(--muted); font-style:italic; }
.reason { font-size:.88rem; color:var(--muted); margin:.35rem 0 .5rem; }
table { width:100%; border-collapse:collapse; font-size:.85rem; margin-top:.5rem; }
th,td { text-align:left; padding:.35rem .5rem; border-top:1px solid var(--line); }
th { color:var(--muted); font-weight:500; }
td.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
a { color:var(--accent); }
.wrap { overflow-x:auto; }
footer { color:var(--muted); font-size:.82rem; margin-top:2.5rem;
  border-top:1px solid var(--line); padding-top:1rem; }
"""


def _thesis_block(candidate) -> str:
    if candidate.thesis is None:
        return '<p class="why none">Thesis not generated (outside the thesis limit).</p>'
    if candidate.thesis == NO_INFORMATION:
        return f'<p class="why none">{escape(NO_INFORMATION)}</p>'
    window = ""
    if candidate.thesis_news_window:
        start, end = candidate.thesis_news_window
        window = f' <span class="rank">(news {escape(start)} → {escape(end)})</span>'
    return f'<p class="why">{escape(candidate.thesis)}{window}</p>'


def _transactions_table(candidate) -> str:
    if not candidate.transactions:
        return ""
    rows = []
    for txn in sorted(candidate.transactions, key=lambda t: t.notification_date, reverse=True):
        owner = f" ({txn.owner})" if txn.owner else ""
        managed = escape(txn.managed_by) if txn.managed_by else "—"
        rows.append(
            "<tr>"
            f"<td>{escape(txn.member)}{owner}</td>"
            f"<td>{escape(txn.state_district)}</td>"
            f'<td class="num">{txn.txn_date.isoformat()}</td>'
            f'<td class="num">{txn.notification_date.isoformat()}</td>'
            f'<td class="num">{escape(txn.amount_text)}</td>'
            f"<td>{managed}</td>"
            f'<td><a href="{escape(txn.source_url)}" rel="noreferrer">#{escape(txn.doc_id)}</a></td>'
            "</tr>"
        )
    return (
        '<div class="wrap"><table><thead><tr>'
        "<th>Member</th><th>District</th><th>Traded</th><th>Disclosed</th>"
        "<th>Amount</th><th>Held via</th><th>Filing</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def _card(candidate) -> str:
    is_seed = candidate.origin == "seed"
    name = ""
    if is_seed and candidate.transactions:
        name = candidate.transactions[0].asset_name
    elif candidate.peer_name:
        name = candidate.peer_name

    tag = '<span class="tag seed">disclosed</span>' if is_seed else '<span class="tag">peer</span>'
    if candidate.asset_class and candidate.asset_class not in ANALYSABLE_ASSET_CLASSES:
        tag += f' <span class="tag">{escape(candidate.asset_class)}</span>'

    return (
        f'<article class="card {"seed" if is_seed else "peer"}">'
        f'<div class="hd"><span class="rank">#{candidate.rank}</span>'
        f'<span class="tic">{escape(candidate.ticker)}</span>'
        f'<span class="name">{escape(name)}</span>{tag}</div>'
        f'<p class="reason">{escape(rank_reason(candidate))}</p>'
        f"{_thesis_block(candidate)}"
        f"{_transactions_table(candidate)}"
        "</article>"
    )


def render_selection_html(result) -> str:
    """Render the candidate list produced by a pull."""
    window_start = (
        result.options.as_of.toordinal() - result.options.days
    )
    from datetime import date as _date

    start = _date.fromordinal(window_start).isoformat()
    end = result.options.as_of.isoformat()

    note = ""
    if result.unreadable:
        items = "".join(
            f'<li><a href="{escape(u.source_url)}" rel="noreferrer">#{escape(u.doc_id)}</a> '
            f"— {escape(u.member)}, {u.filing_date.isoformat()} ({escape(u.reason)})</li>"
            for u in result.unreadable[:25]
        )
        more = (
            f"<li>… and {len(result.unreadable) - 25} more</li>"
            if len(result.unreadable) > 25
            else ""
        )
        note = (
            f'<div class="note"><b>{len(result.unreadable)} filing(s) could not be read.</b> '
            "A minority of House PTRs are scanned paper rather than electronic "
            "submissions, and no text can be extracted from them. They are listed "
            f"here so this page does not overstate its own coverage.<ul>{items}{more}</ul></div>"
        )

    cards = "".join(_card(c) for c in result.candidates) or (
        '<div class="note">No candidates matched the filters for this window.</div>'
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock selection — {escape(start)} to {escape(end)}</title>
<style>{_CSS}</style></head><body><main>
<h1>Stock selection</h1>
<p class="sub">From {escape(result.source_name)} disclosures notified between
{escape(start)} and {escape(end)} · generated
{escape(result.generated_at.strftime("%Y-%m-%d %H:%M"))}</p>
<div class="stats">
  <div><b>{len(result.seeds)}</b> disclosed tickers</div>
  <div><b>{len(result.peer_candidates)}</b> peers added</div>
  <div><b>{result.filings_seen}</b> filings read</div>
  <div><b>{result.transactions_selected}</b> purchases kept</div>
  <div><b>${result.options.min_amount:,}</b> minimum bracket</div>
</div>
{note}
{cards}
<footer>Ranking is lexicographic: disclosed tickers first, ordered by how many
distinct members bought them, then by the largest disclosed bracket, then by how
recently they were disclosed. Peers follow their parent, ordered by market
weight. A thesis is an inference from public news — the disclosure forms record
no reasoning, and nothing here should be read as a member's rationale.</footer>
</main></body></html>"""
