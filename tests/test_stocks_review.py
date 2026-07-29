"""Logic tests for the batch review runner.

The graph is stubbed throughout — these cover selection, durability, failure
isolation and the summary page, not the analysis itself.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from tradingagents.disclosures.render import render_summary_html
from tradingagents.disclosures.review import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    ReviewOptions,
    existing_reports,
    relative_link,
    review_candidates,
    select_for_review,
)

CONFIG = {"llm_provider": "ollama", "deep_think_llm": "Qwen3", "quick_think_llm": "Qwen3"}


def _candidate(ticker, rank, asset_class="ST", origin="seed", **extra):
    base = {
        "ticker": ticker, "rank": rank, "asset_class": asset_class, "origin": origin,
        "members": ["Rep. A"], "transactions": [
            {"source_url": "https://example.invalid/1.pdf", "doc_id": "1"}
        ],
    }
    base.update(extra)
    return base


def _write_pick(tmp_path, candidates):
    pick = tmp_path / "picks" / "20260728_120000"
    pick.mkdir(parents=True)
    (pick / "seeds.json").write_text(json.dumps({"candidates": candidates}))
    (pick / "selection.html").write_text("<html></html>")
    return pick


class _StubGraph:
    """Stands in for TradingAgentsGraph."""

    def __init__(self, decision="Buy", fail_on=()):
        self.decision = decision
        self.fail_on = set(fail_on)

    def propagate(self, ticker, trade_date, asset_type="stock"):
        if ticker in self.fail_on:
            raise RuntimeError(f"boom for {ticker}")
        return {"ticker": ticker}, self.decision

    def save_reports(self, final_state, ticker, save_path):
        save_path.mkdir(parents=True, exist_ok=True)
        md = save_path / "complete_report.md"
        md.write_text(f"# {ticker}")
        (save_path / "complete_report.html").write_text(f"<h1>{ticker}</h1>")
        return md


# --- selection -------------------------------------------------------------

@pytest.mark.unit
class TestSelection:
    def test_respects_rank_order_and_cap(self, tmp_path):
        candidates = [_candidate("C", 3), _candidate("A", 1), _candidate("B", 2)]
        runnable, _ = select_for_review(
            candidates, ReviewOptions(max_symbols=2), reports_root=tmp_path
        )
        assert [c["ticker"] for c in runnable] == ["A", "B"]

    def test_filters_unanalysable_asset_classes(self, tmp_path):
        candidates = [
            _candidate("AAA", 1, "ST"), _candidate("OPT", 2, "OP"),
            _candidate("FUND", 3, "MF"), _candidate("ETF", 4, "ET"),
        ]
        runnable, skipped = select_for_review(
            candidates, ReviewOptions(), reports_root=tmp_path
        )
        assert [c["ticker"] for c in runnable] == ["AAA", "ETF"]
        assert {c["ticker"] for c, _ in skipped} == {"OPT", "FUND"}

    def test_asset_classes_are_configurable(self, tmp_path):
        candidates = [_candidate("OPT", 1, "OP")]
        runnable, _ = select_for_review(
            candidates, ReviewOptions(asset_classes=("ST", "OP")), reports_root=tmp_path
        )
        assert [c["ticker"] for c in runnable] == ["OPT"]

    def test_skips_recently_analysed_tickers(self, tmp_path):
        recent = datetime.now() - timedelta(days=2)
        (tmp_path / f"AAA_{recent.strftime('%Y%m%d_%H%M%S')}").mkdir(parents=True)
        runnable, skipped = select_for_review(
            [_candidate("AAA", 1)], ReviewOptions(skip_days=7), reports_root=tmp_path
        )
        assert runnable == []
        assert "2d ago" in skipped[0][1]

    def test_stale_reports_do_not_block(self, tmp_path):
        old = datetime.now() - timedelta(days=30)
        (tmp_path / f"AAA_{old.strftime('%Y%m%d_%H%M%S')}").mkdir(parents=True)
        runnable, _ = select_for_review(
            [_candidate("AAA", 1)], ReviewOptions(skip_days=7), reports_root=tmp_path
        )
        assert [c["ticker"] for c in runnable] == ["AAA"]

    def test_force_overrides_recency(self, tmp_path):
        recent = datetime.now() - timedelta(days=1)
        (tmp_path / f"AAA_{recent.strftime('%Y%m%d_%H%M%S')}").mkdir(parents=True)
        runnable, _ = select_for_review(
            [_candidate("AAA", 1)], ReviewOptions(force=True), reports_root=tmp_path
        )
        assert [c["ticker"] for c in runnable] == ["AAA"]

    def test_retry_failed_targets_only_failures(self, tmp_path):
        previous = {
            "AAA": {"status": STATUS_OK}, "BBB": {"status": STATUS_FAILED},
            "CCC": {"status": STATUS_SKIPPED},
        }
        runnable, _ = select_for_review(
            [_candidate("AAA", 1), _candidate("BBB", 2), _candidate("CCC", 3)],
            ReviewOptions(retry_failed=True), reports_root=tmp_path, previous=previous,
        )
        assert [c["ticker"] for c in runnable] == ["BBB"]

    def test_existing_reports_picks_the_newest(self, tmp_path):
        (tmp_path / "AAA_20260101_000000").mkdir()
        (tmp_path / "AAA_20260601_000000").mkdir()
        (tmp_path / "not-a-report").mkdir()
        found = existing_reports(tmp_path)
        assert found["AAA"] == datetime(2026, 6, 1)
        assert "not-a-report" not in found

    def test_missing_reports_dir_is_fine(self, tmp_path):
        assert existing_reports(tmp_path / "nope") == {}


# --- running ---------------------------------------------------------------

@pytest.mark.unit
class TestRunning:
    def test_records_decision_and_report_paths(self, tmp_path):
        pick = _write_pick(tmp_path, [_candidate("AAA", 1)])
        state = review_candidates(
            pick, CONFIG, ReviewOptions(), reports_root=tmp_path / "reports",
            graph_factory=lambda: _StubGraph("Overweight"),
        )
        entry = state.results["AAA"]
        assert entry["status"] == STATUS_OK
        assert entry["decision"] == "Overweight"
        assert entry["report_html"].endswith("complete_report.html")

    def test_one_failure_does_not_stop_the_batch(self, tmp_path):
        pick = _write_pick(tmp_path, [
            _candidate("AAA", 1), _candidate("BAD", 2), _candidate("CCC", 3),
        ])
        state = review_candidates(
            pick, CONFIG, ReviewOptions(), reports_root=tmp_path / "reports",
            graph_factory=lambda: _StubGraph(fail_on=["BAD"]),
        )
        assert state.results["AAA"]["status"] == STATUS_OK
        assert state.results["CCC"]["status"] == STATUS_OK
        assert state.results["BAD"]["status"] == STATUS_FAILED
        assert "boom for BAD" in state.results["BAD"]["error"]

    def test_state_is_flushed_after_every_ticker(self, tmp_path):
        # A long batch must survive an interruption without losing finished work.
        seen = []
        pick = _write_pick(tmp_path, [_candidate("AAA", 1), _candidate("BBB", 2)])
        state_path = pick / "review_state.json"

        def progress(event, ticker, entry):
            if event == "done" and state_path.exists():
                seen.append(set(json.loads(state_path.read_text())["results"]))

        review_candidates(
            pick, CONFIG, ReviewOptions(), reports_root=tmp_path / "reports",
            graph_factory=lambda: _StubGraph(), progress=progress,
        )
        assert seen[0] == {"AAA"}
        assert seen[1] == {"AAA", "BBB"}

    def test_records_the_resolved_model(self, tmp_path):
        pick = _write_pick(tmp_path, [_candidate("AAA", 1)])
        state = review_candidates(
            pick, CONFIG, ReviewOptions(), reports_root=tmp_path / "reports",
            graph_factory=lambda: _StubGraph(),
        )
        assert state.data["model"]["provider"] == "ollama"
        assert state.data["model"]["deep_think_llm"] == "Qwen3"

    def test_seeds_json_is_never_modified(self, tmp_path):
        pick = _write_pick(tmp_path, [_candidate("AAA", 1)])
        before = (pick / "seeds.json").read_text()
        review_candidates(
            pick, CONFIG, ReviewOptions(), reports_root=tmp_path / "reports",
            graph_factory=lambda: _StubGraph(),
        )
        assert (pick / "seeds.json").read_text() == before

    def test_crypto_uses_the_crypto_pipeline(self, tmp_path):
        seen = {}

        class _Recorder(_StubGraph):
            def propagate(self, ticker, trade_date, asset_type="stock"):
                seen[ticker] = asset_type
                return {}, "Hold"

        pick = _write_pick(tmp_path, [
            _candidate("BTC-USD", 1, "CT"), _candidate("AAA", 2, "ST"),
        ])
        review_candidates(
            pick, CONFIG, ReviewOptions(), reports_root=tmp_path / "reports",
            graph_factory=lambda: _Recorder(),
        )
        assert seen == {"BTC-USD": "crypto", "AAA": "stock"}

    def test_concurrency_runs_every_ticker(self, tmp_path):
        pick = _write_pick(tmp_path, [_candidate(f"T{i}", i) for i in range(1, 6)])
        state = review_candidates(
            pick, CONFIG, ReviewOptions(concurrency=3), reports_root=tmp_path / "reports",
            graph_factory=lambda: _StubGraph(),
        )
        assert sum(1 for e in state.results.values() if e["status"] == STATUS_OK) == 5

    def test_resumes_from_previous_state(self, tmp_path):
        pick = _write_pick(tmp_path, [_candidate("AAA", 1)])
        review_candidates(
            pick, CONFIG, ReviewOptions(), reports_root=tmp_path / "reports",
            graph_factory=lambda: _StubGraph(),
        )
        # A second pass keeps the earlier result rather than starting empty.
        state = review_candidates(
            pick, CONFIG, ReviewOptions(), reports_root=tmp_path / "reports",
            graph_factory=lambda: _StubGraph(),
        )
        assert "AAA" in state.results


# --- summary ---------------------------------------------------------------

@pytest.mark.unit
class TestSummary:
    def _state_and_candidates(self, tmp_path):
        candidates = [
            _candidate("AAA", 1, thesis="Solid pipeline."),
            _candidate("BAD", 2),
            _candidate("OPT", 3, "OP"),
        ]
        pick = _write_pick(tmp_path, candidates)
        state = review_candidates(
            pick, CONFIG, ReviewOptions(), reports_root=tmp_path / "reports",
            graph_factory=lambda: _StubGraph(fail_on=["BAD"]),
        )
        return state, candidates, pick

    def test_summary_shows_every_outcome(self, tmp_path):
        state, candidates, pick = self._state_and_candidates(tmp_path)
        html = render_summary_html(state, candidates, pick)
        assert "AAA" in html and "Buy" in html
        assert "failed" in html and "skipped" in html

    def test_summary_is_self_contained(self, tmp_path):
        state, candidates, pick = self._state_and_candidates(tmp_path)
        html = render_summary_html(state, candidates, pick)
        assert "<style>" in html
        assert "<script" not in html and "fetch(" not in html

    def test_report_link_is_relative(self, tmp_path):
        state, candidates, pick = self._state_and_candidates(tmp_path)
        html = render_summary_html(state, candidates, pick)
        # Must resolve from the pick directory, not an absolute machine path.
        assert 'href="../../reports/AAA_' in html

    def test_links_back_to_the_selection_page(self, tmp_path):
        state, candidates, pick = self._state_and_candidates(tmp_path)
        assert 'href="selection.html"' in render_summary_html(state, candidates, pick)

    def test_escapes_injected_content(self, tmp_path):
        candidates = [_candidate("AAA", 1, thesis="<script>alert(1)</script>")]
        pick = _write_pick(tmp_path, candidates)
        state = review_candidates(
            pick, CONFIG, ReviewOptions(), reports_root=tmp_path / "reports",
            graph_factory=lambda: _StubGraph(),
        )
        html = render_summary_html(state, candidates, pick)
        assert "<script>alert(1)</script>" not in html


@pytest.mark.unit
def test_relative_link(tmp_path):
    target = tmp_path / "reports" / "AAA_1" / "complete_report.html"
    start = tmp_path / "picks" / "20260728_120000"
    assert relative_link(target, start) == "../../reports/AAA_1/complete_report.html"


@pytest.mark.unit
def test_review_options_default_to_analysable_classes():
    assert ReviewOptions().asset_classes == ("ST", "ET", "CT")
    assert ReviewOptions().trade_date == date.today()
