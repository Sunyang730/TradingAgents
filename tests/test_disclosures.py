"""Logic tests for the disclosure pipeline.

Offline by construction: parsing runs against extracted-text samples, and peer
lookup / news / LLM calls are stubbed. The samples reproduce the quirks of real
House PTR extraction — NUL-filled headers, tickers that wrap away from their
asset-type tag, amount brackets split across lines, and repeated page furniture.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from tradingagents.disclosures.filters import select_transactions
from tradingagents.disclosures.models import Candidate, Transaction, UnreadableFiling
from tradingagents.disclosures.parsing import (
    amount_lower_bound,
    index_member_name,
    parse_filing_date,
    parse_index,
    parse_ptr_text,
)
from tradingagents.disclosures.pipeline import (
    PullOptions,
    PullResult,
    _group_into_seeds,
    result_to_dict,
)
from tradingagents.disclosures.ranking import rank_candidates, rank_reason
from tradingagents.disclosures.render import render_selection_html
from tradingagents.disclosures.thesis import NO_INFORMATION, build_thesis, news_window

# Real extraction quirks, reproduced. "\x00" is what the letter-spaced
# small-caps headers actually extract as — they are not spaces.
PTR_TEXT = (
    "Filing ID #20034105\n"
    "P        T           R     \n"
    "Clerk of the House of Representatives • Legislative Resource Center\n"
    "Name: Hon. Lisa McClain\n"
    "Status: Member\n"
    "State/District: MI09\n"
    "ID Owner Asset Transaction Date Notification Amount Cap.\n"
    "Type Date Gains >\n"
    "$200?\n"
    "SP BigBear.ai, Inc. Common Stock P 02/04/2026 03/01/2026 $1,001 - $15,000\n"
    "(BBAI) [ST]\n"
    "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New\n"
    "S\x00\x00\x00\x00\x00\x00\x00\x00\x00 O\x00: Charles Schwab 401K > Schwab 824\n"
    "Apple Inc. - Common Stock (AAPL) S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000\n"
    "[ST]\n"
    "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New\n"
    "S\x00\x00\x00\x00\x00\x00\x00\x00\x00 O\x00: Putnam Investments\n"
    "D\x00\x00\x00\x00\x00\x00\x00\x00\x00: The full transaction included sales: "
    "NVDA – 5 shares sold @ $180.00/share\n"
    "JT Honeywell Aerospace Inc. - Common E 06/29/2026 06/30/2026 $15,001 -\n"
    "Stock (HONAV) [ST] $50,000\n"
    "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New\n"
    "METROPOLITAN GOVT NA S 03/16/2026 04/14/2026 $1,001 - $15,000\n"
    "DAVIDSON CNTY TENN HEALTH &\n"
    "ED L FACS BRD 3.750% 03/15/28 B\n"
    "[GS]\n"
    "* For the complete list of asset type abbreviations, please visit ...\n"
    "Digitally Signed: Hon. Lisa McClain , 03/04/2026\n"
)


def _parse(text=PTR_TEXT):
    return parse_ptr_text(
        text,
        member="Lisa McClain",
        state_district="MI09",
        doc_id="20034105",
        filing_date=date(2026, 3, 4),
        source_url="https://example.invalid/20034105.pdf",
    )


# --- index -----------------------------------------------------------------

@pytest.mark.unit
class TestIndex:
    # The Clerk's index is CRLF-terminated; unstripped \r corrupts the DocID,
    # which is what addresses the PDF.
    INDEX = (
        "Prefix\tLast\tFirst\tSuffix\tFilingType\tStateDst\tYear\tFilingDate\tDocID\r\n"
        "Hon.\tMcClain\tLisa\t\tP\tMI09\t2026\t3/4/2026\t20034105\r\n"
        "Hon.\tAlford\tMark\t\tC\tMO04\t2026\t4/15/2026\t10078673\r\n"
        "\r\n"
    )

    def test_parses_rows_and_strips_crlf(self):
        rows = parse_index(self.INDEX)
        assert len(rows) == 2
        assert rows[0]["doc_id"] == "20034105"
        assert rows[0]["filing_type"] == "P"

    def test_skips_blank_lines(self):
        assert all(r["doc_id"] for r in parse_index(self.INDEX))

    def test_member_name(self):
        assert index_member_name(parse_index(self.INDEX)[0]) == "Lisa McClain"

    def test_filing_date(self):
        assert parse_filing_date("3/4/2026") == date(2026, 3, 4)
        assert parse_filing_date("") is None
        assert parse_filing_date("garbage") is None


# --- amounts ---------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "text,expected",
    [
        ("$1,001 - $15,000", 1001),
        ("$15,001 - $50,000", 15001),
        ("$1,000,001 - $5,000,000", 1000001),
        ("$50,000,000 +", 50000000),
        ("", 0),
        ("None", 0),
    ],
)
def test_amount_lower_bound(text, expected):
    assert amount_lower_bound(text) == expected


# --- PTR body --------------------------------------------------------------

@pytest.mark.unit
class TestPtrParsing:
    def test_extracts_expected_tickers(self):
        # The municipal bond has no ticker and must be skipped; everything with
        # a resolvable symbol must survive.
        assert [t.ticker for t in _parse()] == ["BBAI", "AAPL", "HONAV"]

    def test_ticker_split_from_its_asset_class_tag(self):
        # "(AAPL)" ends one line, "[ST]" opens the next.
        aapl = next(t for t in _parse() if t.ticker == "AAPL")
        assert aapl.asset_class == "ST"

    def test_nul_filled_header_yields_manager(self):
        # SUBHOLDING OF extracts with NUL bytes, not spaces.
        assert next(t for t in _parse() if t.ticker == "BBAI").managed_by == (
            "Charles Schwab 401K > Schwab 824"
        )

    def test_description_tickers_do_not_leak_into_other_rows(self):
        # AAPL's DESCRIPTION names NVDA; NVDA was not transacted.
        assert "NVDA" not in [t.ticker for t in _parse()]

    def test_wrapped_amount_bracket_is_rejoined(self):
        honav = next(t for t in _parse() if t.ticker == "HONAV")
        assert honav.amount_lower == 15001
        assert honav.amount_text == "$15,001 - $50,000"

    def test_owner_codes(self):
        by_ticker = {t.ticker: t for t in _parse()}
        assert by_ticker["BBAI"].owner == "SP"
        assert by_ticker["HONAV"].owner == "JT"
        assert by_ticker["AAPL"].owner == ""

    def test_transaction_types_and_partial(self):
        by_ticker = {t.ticker: t for t in _parse()}
        assert by_ticker["BBAI"].txn_type == "P" and by_ticker["BBAI"].is_purchase
        assert by_ticker["AAPL"].txn_type == "S" and by_ticker["AAPL"].partial
        assert by_ticker["HONAV"].txn_type == "E"

    def test_asset_name_excludes_tags_and_amounts(self):
        by_ticker = {t.ticker: t for t in _parse()}
        assert by_ticker["HONAV"].asset_name == "Honeywell Aerospace Inc. - Common Stock"
        assert "$" not in by_ticker["HONAV"].asset_name

    def test_page_furniture_never_becomes_an_asset_name(self):
        assert all("ID Owner" not in t.asset_name for t in _parse())

    def test_empty_text_is_not_an_error(self):
        assert _parse("") == []


# --- filters ---------------------------------------------------------------

def _txn(ticker="AAA", txn_type="P", amount=15001, notified=date(2026, 6, 1), member="A"):
    return Transaction(
        ticker=ticker, asset_name=f"{ticker} Inc", asset_class="ST", txn_type=txn_type,
        partial=False, txn_date=date(2026, 5, 1), notification_date=notified,
        amount_text=f"${amount:,} - x", amount_lower=amount, owner="", managed_by="",
        member=member, state_district="XX01", doc_id="1", filing_date=date(2026, 6, 1),
        source_url="u",
    )


@pytest.mark.unit
class TestFilters:
    WINDOW = {"start": date(2026, 4, 1), "end": date(2026, 7, 1)}

    def test_keeps_purchases_only(self):
        txns = [_txn(txn_type="P"), _txn(txn_type="S"), _txn(txn_type="E")]
        assert [t.txn_type for t in select_transactions(txns, **self.WINDOW)] == ["P"]

    def test_amount_floor_is_inclusive(self):
        txns = [_txn(amount=15001), _txn(amount=15000)]
        kept = select_transactions(txns, **self.WINDOW, min_amount=15001)
        assert [t.amount_lower for t in kept] == [15001]

    def test_window_uses_notification_not_transaction_date(self):
        # Filed long after the trade: the trade date is outside the window but
        # the disclosure is inside it, and disclosure is what makes it news.
        late = _txn(notified=date(2026, 6, 20))
        assert select_transactions([late], **self.WINDOW) == [late]

    def test_excludes_disclosures_outside_the_window(self):
        assert select_transactions([_txn(notified=date(2026, 1, 5))], **self.WINDOW) == []

    def test_managed_holdings_are_kept(self):
        # A member's own 401k and a third-party trust are filed identically;
        # the form does not distinguish them, so neither is dropped.
        txn = _txn()
        object.__setattr__(txn, "managed_by", "Putnam Investments")
        assert select_transactions([txn], **self.WINDOW) == [txn]


# --- grouping and ranking --------------------------------------------------

@pytest.mark.unit
class TestGroupingAndRanking:
    def test_groups_by_ticker_across_members(self):
        seeds = _group_into_seeds([_txn(member="A"), _txn(member="B"), _txn(member="A")])
        assert len(seeds) == 1
        assert seeds[0].cluster == 2          # distinct members, not transactions

    def test_cluster_outranks_amount(self):
        two = _group_into_seeds([_txn("BBB", member="A"), _txn("BBB", member="B")])
        one = _group_into_seeds([_txn("AAA", amount=1_000_001, member="C")])
        assert [c.ticker for c in rank_candidates(two + one)] == ["BBB", "AAA"]

    def test_amount_breaks_cluster_ties(self):
        seeds = _group_into_seeds([_txn("AAA", amount=15001), _txn("BBB", amount=100001)])
        assert [c.ticker for c in rank_candidates(seeds)] == ["BBB", "AAA"]

    def test_recency_breaks_amount_ties(self):
        seeds = _group_into_seeds([
            _txn("AAA", notified=date(2026, 5, 1)),
            _txn("BBB", notified=date(2026, 6, 1)),
        ])
        assert [c.ticker for c in rank_candidates(seeds)] == ["BBB", "AAA"]

    def test_peers_rank_below_every_seed(self):
        seeds = _group_into_seeds([_txn("AAA")])
        peer = Candidate(ticker="ZZZ", asset_class="ST", origin="peer",
                         parent="AAA", market_weight=0.9)
        ranked = rank_candidates(seeds + [peer])
        assert [c.ticker for c in ranked] == ["AAA", "ZZZ"]
        assert [c.rank for c in ranked] == [1, 2]

    def test_peers_follow_parent_then_market_weight(self):
        seeds = _group_into_seeds([_txn("AAA", member="A"), _txn("AAA", member="B"),
                                   _txn("BBB", member="C")])
        peers = [
            Candidate(ticker="B1", asset_class="ST", origin="peer", parent="BBB", market_weight=0.9),
            Candidate(ticker="A1", asset_class="ST", origin="peer", parent="AAA", market_weight=0.1),
            Candidate(ticker="A2", asset_class="ST", origin="peer", parent="AAA", market_weight=0.5),
        ]
        order = [c.ticker for c in rank_candidates(seeds + peers)]
        assert order == ["AAA", "BBB", "A2", "A1", "B1"]

    def test_rank_reason_distinguishes_origin(self):
        seed = _group_into_seeds([_txn("AAA")])[0]
        peer = Candidate(ticker="ZZZ", asset_class="ST", origin="peer",
                         parent="AAA", peer_basis="industry", market_weight=0.25)
        assert "Disclosed purchase" in rank_reason(seed)
        assert "Not disclosed" in rank_reason(peer)
        assert "AAA" in rank_reason(peer)


# --- thesis ----------------------------------------------------------------

class _StubLLM:
    def __init__(self, text="A plausible thesis."):
        self.text = text
        self.prompts: list[str] = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return type("R", (), {"content": self.text})()


@pytest.mark.unit
class TestThesis:
    def test_window_runs_from_trade_date_to_today(self):
        seed = _group_into_seeds([_txn()])[0]
        assert news_window(seed, date(2026, 7, 1)) == ("2026-05-01", "2026-07-01")

    def test_empty_news_yields_the_no_information_sentinel(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.disclosures.thesis.route_to_vendor",
            lambda *a, **k: "No news found for AAA between x and y",
        )
        llm = _StubLLM()
        seed = _group_into_seeds([_txn()])[0]
        thesis, _ = build_thesis(seed, llm, date(2026, 7, 1))
        assert thesis == NO_INFORMATION
        assert llm.prompts == []          # no LLM call when there is nothing to read

    def test_vendor_error_is_not_presented_as_a_thesis(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.disclosures.thesis.route_to_vendor",
            lambda *a, **k: "Error fetching news for AAA: boom",
        )
        thesis, _ = build_thesis(_group_into_seeds([_txn()])[0], _StubLLM(), date(2026, 7, 1))
        assert thesis == NO_INFORMATION

    def test_llm_failure_degrades_to_the_sentinel(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.disclosures.thesis.route_to_vendor", lambda *a, **k: "## news"
        )

        class Boom:
            def invoke(self, prompt):
                raise RuntimeError("model down")

        thesis, _ = build_thesis(_group_into_seeds([_txn()])[0], Boom(), date(2026, 7, 1))
        assert thesis == NO_INFORMATION

    def test_prompt_forbids_claiming_the_members_reasoning(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.disclosures.thesis.route_to_vendor", lambda *a, **k: "## news"
        )
        llm = _StubLLM()
        build_thesis(_group_into_seeds([_txn()])[0], llm, date(2026, 7, 1))
        prompt = llm.prompts[0]
        assert "gives no reason" in prompt
        assert "Do not claim to know why" in prompt


# --- serialisation and rendering -------------------------------------------

def _result(candidates, unreadable=()):
    return PullResult(
        candidates=rank_candidates(candidates),
        unreadable=list(unreadable),
        filings_seen=3,
        transactions_selected=len(candidates),
        source_name="us-house",
        options=PullOptions(as_of=date(2026, 7, 1)),
        generated_at=datetime(2026, 7, 1, 9, 30),
    )


@pytest.mark.unit
class TestSerialisation:
    def test_seeds_json_is_json_serialisable_with_iso_dates(self):
        payload = result_to_dict(_result(_group_into_seeds([_txn()])))
        text = json.dumps(payload)          # must not raise on date objects
        assert '"2026-05-01"' in text
        assert payload["counts"]["seeds"] == 1

    def test_unreadable_filings_are_reported_not_dropped(self):
        unreadable = [UnreadableFiling(
            doc_id="9116218", member="X", filing_date=date(2026, 2, 1),
            source_url="u", reason="no extractable text (scanned filing)",
        )]
        payload = result_to_dict(_result(_group_into_seeds([_txn()]), unreadable))
        assert payload["counts"]["unreadable_filings"] == 1
        assert payload["unreadable_filings"][0]["doc_id"] == "9116218"


@pytest.mark.unit
class TestRendering:
    def test_page_is_self_contained(self):
        html = render_selection_html(_result(_group_into_seeds([_txn()])))
        # Opened over file://, where fetch() of local files is blocked and any
        # external request would silently fail.
        assert "<style>" in html
        for forbidden in ("http://cdn", "https://cdn", "<script", "fetch("):
            assert forbidden not in html

    def test_filing_link_is_present(self):
        html = render_selection_html(_result(_group_into_seeds([_txn()])))
        assert "https://example.invalid" in html or 'href="u"' in html

    def test_ticker_and_thesis_are_rendered(self):
        seeds = _group_into_seeds([_txn("AAA")])
        seeds[0].thesis = "Because reasons."
        seeds[0].thesis_news_window = ("2026-05-01", "2026-07-01")
        html = render_selection_html(_result(seeds))
        assert "AAA" in html and "Because reasons." in html

    def test_missing_thesis_reads_differently_from_no_news(self):
        seeds = _group_into_seeds([_txn("AAA")])
        not_generated = render_selection_html(_result(seeds))
        assert "Thesis not generated" in not_generated

        seeds2 = _group_into_seeds([_txn("BBB")])
        seeds2[0].thesis = NO_INFORMATION
        assert "No information available" in render_selection_html(_result(seeds2))

    def test_html_escapes_injected_content(self):
        seeds = _group_into_seeds([_txn("AAA", member="<script>alert(1)</script>")])
        html = render_selection_html(_result(seeds))
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_empty_result_renders_a_page(self):
        html = render_selection_html(_result([]))
        assert "No candidates matched" in html
