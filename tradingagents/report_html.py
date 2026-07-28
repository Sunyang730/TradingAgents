"""Render a run's consolidated markdown report as a standalone HTML page.

These pages are opened from the filesystem, where a browser blocks ``fetch()``
of local files as cross-origin. Anything fetched at runtime — a CDN stylesheet,
a client-side markdown renderer, the report itself — would silently fail. So
the conversion happens here, at write time, and the CSS is inline.
"""

from __future__ import annotations

from html import escape

_CSS = """
:root { color-scheme: light dark; --fg:#1a1a1a; --muted:#666; --bg:#fff;
  --card:#f7f7f8; --line:#e3e3e6; --accent:#1a5fb4; }
@media (prefers-color-scheme: dark) { :root { --fg:#e8e8ea; --muted:#a0a0a8;
  --bg:#16161a; --card:#1e1e24; --line:#2e2e36; --accent:#7cb0ff; } }
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.25rem 5rem; background:var(--bg); color:var(--fg);
  font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
main { max-width: 52rem; margin: 0 auto; }
h1 { font-size:1.7rem; margin:0 0 1.5rem; padding-bottom:.6rem;
  border-bottom:2px solid var(--line); }
h2 { font-size:1.3rem; margin:2.5rem 0 .8rem; padding-top:1rem;
  border-top:1px solid var(--line); }
h3 { font-size:1.08rem; margin:1.8rem 0 .5rem; color:var(--accent); }
h4 { font-size:.98rem; margin:1.2rem 0 .4rem; }
p { margin:.7rem 0; }
ul,ol { padding-left:1.4rem; }
li { margin:.3rem 0; }
strong { font-weight:640; }
blockquote { margin:1rem 0; padding:.6rem 1rem; border-left:3px solid var(--accent);
  background:var(--card); border-radius:0 6px 6px 0; }
code { background:var(--card); padding:.12rem .35rem; border-radius:4px;
  font-size:.9em; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
pre { background:var(--card); padding:.9rem 1rem; border-radius:8px; overflow-x:auto; }
pre code { background:none; padding:0; }
.wrap { overflow-x:auto; margin:1rem 0; }
table { border-collapse:collapse; width:100%; font-size:.92rem; }
th,td { text-align:left; padding:.45rem .7rem; border:1px solid var(--line);
  vertical-align:top; }
th { background:var(--card); font-weight:600; }
a { color:var(--accent); }
hr { border:none; border-top:1px solid var(--line); margin:2rem 0; }
"""

# Markdown extensions worth having for these reports: agents emit pipe tables
# and occasional fenced blocks, and "sane_lists" stops a numbered list from
# being restarted by an intervening paragraph.
_EXTENSIONS = ("tables", "fenced_code", "sane_lists", "nl2br")


def markdown_to_html(text: str) -> str:
    """Convert report markdown to an HTML fragment."""
    import markdown as md

    return md.markdown(text, extensions=list(_EXTENSIONS))


def render_report_html(markdown_text: str, title: str) -> str:
    """Wrap a converted report in a self-contained page."""
    body = markdown_to_html(markdown_text)
    # Tables are the one element that reliably overflows on a narrow window;
    # give each its own scroll container so the page itself never does.
    body = body.replace("<table>", '<div class="wrap"><table>').replace(
        "</table>", "</table></div>"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>{_CSS}</style></head><body><main>
{body}
</main></body></html>"""
