"""The HTML companion written beside each run's markdown report."""

from __future__ import annotations

import re

import pytest

from tradingagents.report_html import render_report_html
from tradingagents.reporting import write_report_tree

MARKDOWN = """# Trading Analysis Report: TEST

## I. Analyst Team Reports

### Market Analyst

| Indicator | Value |
|---|---|
| RSI | 62 |

- momentum **up**
- support at `142.10`
"""


@pytest.mark.unit
class TestRendering:
    def test_page_is_self_contained(self):
        html = render_report_html(MARKDOWN, "TEST")
        # Opened over file://, where fetch() of local files is cross-origin and
        # blocked, so nothing may be loaded at runtime.
        assert "<style>" in html
        assert "<script" not in html
        assert not re.search(r'(?:src|href)="https?://', html)

    def test_markdown_structure_survives(self):
        html = render_report_html(MARKDOWN, "TEST")
        assert "<h2>" in html and "<h3>" in html
        assert "<strong>up</strong>" in html
        assert "<code>142.10</code>" in html

    def test_tables_get_their_own_scroll_container(self):
        # A wide table must scroll inside itself rather than making the page
        # scroll horizontally.
        html = render_report_html(MARKDOWN, "TEST")
        assert '<div class="wrap"><table>' in html
        assert html.count("<table>") == html.count('<div class="wrap"><table>')

    def test_title_is_escaped(self):
        html = render_report_html("# x", '<script>alert(1)</script>')
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


@pytest.mark.unit
class TestWrittenAlongsideMarkdown:
    STATE = {
        "market_report": "## Signals\n\n- momentum up\n",
        "risk_debate_state": {"judge_decision": "**Rating**: Buy"},
    }

    def test_html_is_written_next_to_the_markdown(self, tmp_path):
        md = write_report_tree(self.STATE, "TEST", tmp_path / "TEST")
        assert md.exists()
        assert md.with_suffix(".html").exists()

    def test_render_failure_does_not_lose_the_report(self, tmp_path, monkeypatch):
        # The markdown is the authoritative artifact; a rendering problem must
        # never cost a completed analysis.
        import tradingagents.report_html as rh

        def _boom(*a, **k):
            raise RuntimeError("renderer exploded")

        monkeypatch.setattr(rh, "render_report_html", _boom)
        md = write_report_tree(self.STATE, "TEST", tmp_path / "TEST")
        assert md.exists()
        assert "Rating" in md.read_text()
