"""``stocks-review``: run the trading graph over a pull's candidate list.

Progress lives in ``review_state.json`` beside — never inside — ``seeds.json``.
Keeping them apart means a re-run, a crash, or a retry can never rewrite the
record of what was actually disclosed.

A batch is long: at roughly a quarter-hour per ticker locally, a full cap is an
overnight job. So nothing here prompts, a single ticker's failure never aborts
the run, and every completed ticker is durable the moment it finishes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from threading import Lock

from .models import ANALYSABLE_ASSET_CLASSES

logger = logging.getLogger(__name__)

DEFAULT_MAX_SYMBOLS = 25
DEFAULT_SKIP_DAYS = 7
DEFAULT_REPORTS_DIRNAME = "reports"

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

_REPORT_DIR_RE = re.compile(r"^(?P<ticker>.+)_(?P<stamp>\d{8}_\d{6})$")


@dataclass
class ReviewOptions:
    max_symbols: int = DEFAULT_MAX_SYMBOLS
    concurrency: int = 1
    skip_days: int = DEFAULT_SKIP_DAYS
    force: bool = False
    retry_failed: bool = False
    asset_classes: tuple[str, ...] = ANALYSABLE_ASSET_CLASSES
    trade_date: date = field(default_factory=date.today)


def load_candidates(pick_dir: Path) -> tuple[dict, list[dict]]:
    """Read ``seeds.json``; return ``(payload, candidates)``."""
    payload = json.loads((Path(pick_dir) / "seeds.json").read_text(encoding="utf-8"))
    return payload, payload.get("candidates", [])


def existing_reports(reports_root: Path) -> dict[str, datetime]:
    """Newest report timestamp per ticker under ``reports_root``."""
    newest: dict[str, datetime] = {}
    if not reports_root.is_dir():
        return newest
    for entry in reports_root.iterdir():
        if not entry.is_dir():
            continue
        match = _REPORT_DIR_RE.match(entry.name)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match.group("stamp"), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        ticker = match.group("ticker")
        if ticker not in newest or stamp > newest[ticker]:
            newest[ticker] = stamp
    return newest


def select_for_review(
    candidates: list[dict],
    options: ReviewOptions,
    *,
    reports_root: Path,
    previous: dict | None = None,
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Split candidates into those to run and those to skip, with reasons.

    Skipping a recently analysed ticker is deliberately conservative. Re-running
    one is not wasted work: the graph stores each decision for reflection on the
    next same-ticker run, which is how the memory log learns. The default only
    avoids re-deciding the same names within a week.
    """
    previous = previous or {}
    recent = existing_reports(reports_root)
    now = datetime.now()

    runnable: list[dict] = []
    skipped: list[tuple[dict, str]] = []

    for candidate in sorted(candidates, key=lambda c: c.get("rank") or 10**6):
        ticker = candidate["ticker"]
        asset_class = candidate.get("asset_class") or ""

        # A retry pass is about the failures only; anything that already
        # succeeded stays as it is.
        if options.retry_failed and previous.get(ticker, {}).get("status") != STATUS_FAILED:
            continue

        if asset_class and asset_class not in options.asset_classes:
            skipped.append((candidate, f"asset class {asset_class} not selected"))
            continue

        if not options.force and not options.retry_failed:
            last = recent.get(ticker)
            if last and (now - last).days < options.skip_days:
                skipped.append(
                    (candidate, f"analysed {(now - last).days}d ago (< {options.skip_days}d)")
                )
                continue

        runnable.append(candidate)
        if len(runnable) >= options.max_symbols:
            break

    return runnable, skipped


class ReviewState:
    """``review_state.json``, flushed after every ticker."""

    def __init__(self, path: Path, pick_dir: Path, options: ReviewOptions, model: dict):
        self.path = Path(path)
        self.lock = Lock()
        self.data = {
            "pick_dir": str(pick_dir),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": None,
            # Recorded so a batch's decisions stay comparable and reproducible
            # even after the config changes underneath it.
            "model": model,
            "options": {
                "max_symbols": options.max_symbols,
                "concurrency": options.concurrency,
                "skip_days": options.skip_days,
                "force": options.force,
                "retry_failed": options.retry_failed,
                "asset_classes": list(options.asset_classes),
                "trade_date": options.trade_date.isoformat(),
            },
            "results": {},
        }
        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
                self.data["results"] = existing.get("results", {})
                self.data["started_at"] = existing.get("started_at", self.data["started_at"])
            except (OSError, ValueError):
                logger.warning("Could not read %s; starting fresh", self.path)

    @property
    def results(self) -> dict:
        return self.data["results"]

    def record(self, ticker: str, entry: dict) -> None:
        with self.lock:
            self.data["results"][ticker] = entry
            self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


def default_graph_factory(config: dict):
    """Build one graph per ticker.

    Per-ticker construction rather than one shared instance: the graph carries
    per-run state, and rebuilding it is negligible beside the minutes an actual
    analysis takes. It also makes ``--concurrency`` safe without sharing.
    """
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    def factory():
        return TradingAgentsGraph(config=config)

    return factory


def review_candidates(
    pick_dir: Path,
    config: dict,
    options: ReviewOptions | None = None,
    *,
    reports_root: Path | None = None,
    graph_factory=None,
    progress=None,
) -> ReviewState:
    """Run the graph over a pull's candidates. Returns the populated state."""
    options = options or ReviewOptions()
    pick_dir = Path(pick_dir)
    reports_root = Path(reports_root or Path.cwd() / DEFAULT_REPORTS_DIRNAME)
    graph_factory = graph_factory or default_graph_factory(config)

    _, candidates = load_candidates(pick_dir)
    state_path = pick_dir / "review_state.json"
    previous = {}
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8")).get("results", {})
        except (OSError, ValueError):
            previous = {}

    runnable, skipped = select_for_review(
        candidates, options, reports_root=reports_root, previous=previous
    )

    model = {
        "provider": config.get("llm_provider"),
        "deep_think_llm": config.get("deep_think_llm"),
        "quick_think_llm": config.get("quick_think_llm"),
    }
    state = ReviewState(state_path, pick_dir, options, model)

    for candidate, reason in skipped:
        state.record(candidate["ticker"], {
            "status": STATUS_SKIPPED,
            "rank": candidate.get("rank"),
            "reason": reason,
        })

    def run_one(candidate: dict) -> None:
        ticker = candidate["ticker"]
        started = datetime.now()
        if progress:
            progress("start", ticker, None)
        try:
            graph = graph_factory()
            asset_type = "crypto" if candidate.get("asset_class") == "CT" else "stock"
            final_state, decision = graph.propagate(
                ticker, options.trade_date.isoformat(), asset_type=asset_type
            )
            stamp = started.strftime("%Y%m%d_%H%M%S")
            save_path = reports_root / f"{ticker}_{stamp}"
            report_md = graph.save_reports(final_state, ticker, save_path)
            entry = {
                "status": STATUS_OK,
                "rank": candidate.get("rank"),
                "decision": decision,
                "report_dir": str(save_path),
                "report_md": str(report_md),
                "report_html": str(report_md.with_suffix(".html")),
                "started_at": started.isoformat(timespec="seconds"),
                "duration_s": round((datetime.now() - started).total_seconds(), 1),
            }
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not end the batch
            logger.exception("Review failed for %s", ticker)
            entry = {
                "status": STATUS_FAILED,
                "rank": candidate.get("rank"),
                "error": f"{type(exc).__name__}: {exc}",
                "started_at": started.isoformat(timespec="seconds"),
                "duration_s": round((datetime.now() - started).total_seconds(), 1),
            }
        state.record(ticker, entry)
        if progress:
            progress("done", ticker, entry)

    if options.concurrency > 1:
        with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
            list(pool.map(run_one, runnable))
    else:
        for candidate in runnable:
            run_one(candidate)

    return state


def relative_link(target: str | Path, start: Path) -> str:
    """A relative href from ``start`` to ``target``, POSIX-style for the browser."""
    try:
        return Path(os.path.relpath(Path(target), start)).as_posix()
    except ValueError:
        # Different drives on Windows: fall back to an absolute file path.
        return Path(target).as_posix()
