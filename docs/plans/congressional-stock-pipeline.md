# Plan: Congressional-disclosure stock pipeline (`pull-stocks` / `stocks-review`)

**Status:** commit 1 landed; commits 2–4 outstanding
**Resume phrase:** *"Resume the congressional stock pipeline plan in `docs/plans/congressional-stock-pipeline.md`."*

| Commit | State |
| --- | --- |
| 1 — per-tier reasoning effort | **done** |
| 2 — `pull-stocks` | not started |
| 3 — `stocks-review` | not started |
| 4 — Markdown → HTML rendering | not started |

Two new CLI commands. `pull-stocks` builds a ranked, provenance-carrying candidate
list from US House financial disclosures and industry peers. `stocks-review` runs the
existing multi-agent graph over that list one ticker at a time and summarises the
results. Every decision below was settled in a grilling session; the *Rejected
alternatives* section exists so they are not relitigated.

## Verified environment facts

These were measured on 2026-07-28, not assumed. Re-check before trusting.

| Fact | Evidence |
| --- | --- |
| House index is fetchable, unauthenticated | `GET disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.zip` → 200, 52 700 bytes |
| Index is tab-delimited | Columns: `Prefix, Last, First, Suffix, FilingType, StateDst, Year, FilingDate, DocID` |
| PTRs are `FilingType == "P"` | 320 of 1436 rows for 2026 YTD (~45/month) |
| PTR PDFs are text, not scans | 15–17 embedded font objects, one DCTDecode letterhead image → **no OCR needed** |
| Ticker is in the PDF text | `Amazon.com, Inc. - Common Stock (AMZN) [ST]` → regex, not an LLM job |
| PTRs contain **no rationale** | Only free-text field is `DESCRIPTION`, a mechanical share/price dump |
| Blind trusts are common | `SUBHOLDING OF: Putnam Investments` — member never chose the trade |
| Peer lookup works, keyless | `yf.Industry("internet-retail").top_companies` → ranked DataFrame w/ market weight |
| yfinance ships the APIs | 1.5.2 has `Sector`, `Industry`, `EquityQuery`, `screen`; `pyproject` already pins `>=1.4.1` |
| Yahoo news depth is coverage-inverse | AMZN: 50 articles span **one day**; AWK: 50 span ~3 months |
| No Alpha Vantage key present | `.env` has 17 vars — all LLM providers plus `OLLAMA_BASE_URL` |
| Local models available | `Qwen3:latest` 8.2B Q4_K_M (tools+thinking), `ornith:latest` 9.0B; **nothing smaller installed** |
| Thinking is the latency cost | Qwen3 `think=True` 10.4s/347tok vs `think=False` 1.3s/45tok — identical 40 tok/s |
| `reasoning_effort:"none"` works over `/v1` | 6.6s → 1.1s. `reasoning_effort:"low"` and `/no_think` do **not** disable it |
| Ollama runs through the OpenAI-compat client | `openai_client.py:227`, base_url `http://localhost:11434/v1` |
| Per-ticker checkpoint DBs | `checkpointer.py:42-45`, `check_same_thread=False` → parallel-safe |
| Observed run cost | ~15 min/ticker locally (`AWK_...090449` → `AIG_...092046`) |

## New dependencies

`pdfplumber` (PTR parsing), `markdown` (report → HTML). Both pure-Python.

---

## Commit 1 — per-tier reasoning effort (lands first)

Three blockers, all confirmed in code:

1. `trading_graph.py:163` injects `reasoning_effort` only when `provider == "openai"`.
2. `openai_client.py:178` `_supports_reasoning_effort()` regex-rejects anything but
   `^(gpt-5|o[1-9])`, so it is silently dropped at line 328.
3. `_get_provider_kwargs()` (`trading_graph.py:155`) builds **one** kwargs dict passed
   identically to `deep_client` (line 101) and `quick_client` (line 107). There is no
   per-tier parameter path at all.

Changes:
- Split `_get_provider_kwargs()` into deep/quick variants.
- Allow `reasoning_effort` passthrough for `ollama` and `openai_compatible`.
- Add `TRADINGAGENTS_QUICK_REASONING_EFFORT` / `TRADINGAGENTS_DEEP_REASONING_EFFORT`.
- Ollama defaults: quick `none`, deep unset.
- Fold in the existing uncommitted `default_config.py` switch to Ollama/Qwen3.

The quick tier drives the four analysts and their tool loops, so this is the largest
lever on batch runtime. Benefits plain `analyze` too.

**Landed.** Measured end-to-end through `create_llm_client` against the local Ollama
server on identical input: deep tier 10.76 s / 292 output tokens, quick tier
**1.33 s / 45 output tokens**. Full suite 593 passed, ruff clean.

## Commit 2 — `pull-stocks`

`DisclosureSource` protocol with a single `HouseDisclosureSource` implementation, so
Senate drops in later without a refactor. (Senate deliberately deferred: it needs an
agreement-cookie + CSRF session flow and a share of its PTRs are scanned paper
requiring OCR.)

Pipeline: fetch `{YEAR}FD.zip` → parse index → keep `FilingType == "P"` → fetch
`ptr-pdfs/{YEAR}/{DocID}.pdf` → pdfplumber extract.

Per transaction line capture: `ticker`, `asset_class` (`ST`/`ET`/`OP`/`MF`/`GS`/`CT`),
`txn_type`, `txn_date`, `notification_date`, `amount_bracket`, `owner`, `managed_by`
(from `SUBHOLDING OF`), `description`, `member`, `doc_id`, `pdf_url`.
Symbols normalised through the existing `normalize_symbol()`
(`dataflows/symbol_utils.py:104`) — handles `BRK/B` → `BRK.B`.

Parser caution: letter-spaced small-caps headers extract mangled — `FILING STATUS`
renders as `F      S     :` and `SUBHOLDING OF` as `S          O :`. Match defensively.

**Filters**
- Purchases only: `P` and `P (partial)`. Sales and exchanges dropped.
- Notification date within `--days`.
- Amount bracket lower bound at or above `--min-amount`.
- Blind-trust / managed lines are **kept**, but `managed_by` is recorded and surfaced
  in the HTML so a Putnam-executed trade is never narrated as the member's conviction.
- **All** asset classes recorded; gating happens at review time, not pull time.

**Expansion** — `Ticker.info["industryKey"]` → `yf.Industry(...).top_companies`,
falling back to `sectorKey` → `yf.Sector(...)` when the industry is missing or too
thin. `--peers` per seed, deduped against existing candidates, recording `parent` and
`market_weight`.

**Thesis** — news window is **transaction date → today** (not a window centred on the
trade: Yahoo cannot reach back that far for large caps). Routed through the existing
vendor interface (`get_news_yfinance`, keyless). The LLM writes the thesis from that
news; if the window is empty the field is the literal *"No information available."*
Records the window actually covered. Only the top `--thesis-limit` candidates get one;
the rest carry `thesis: null`. A peer's thesis is framed as *peer of {parent}*.

**Ranking — lexicographic tiers, no weights** (so the HTML can state exactly why a
ticker ranked where it did):
1. Seeds by distinct-member cluster count ↓
2. then amount bracket lower bound ↓
3. then notification recency ↓
4. Peers rank below all seeds, ordered by parent rank then market weight ↓

**Writes** — `./picks/{YYYYMMDD_HHMMSS}/`
- `seeds.json` — immutable, **all** ranked candidates with full provenance
- `selection.html` — why each was selected, which filing, live link to the source PDF

## Commit 3 — `stocks-review`

Non-interactive (an overnight batch must not block on a prompt). Reads the newest
`./picks/*` or an explicit `--pick`.

- Model from `DEFAULT_CONFIG` (so `.env` / `TRADINGAGENTS_*` still rules) with
  `--provider` / `--deep-model` / `--quick-model` overrides. The resolved trio is
  recorded in `review_state.json` so runs stay reproducible and comparable.
- Default asset classes `ST,ET,CT`; `--asset-classes` widens. `OP`/`MF`/`GS` are
  excluded by default — they name contracts or untradeable instruments and would burn
  a full multi-agent run producing nonsense.
- `--max-symbols` (default 25), `--concurrency` (default 1), `--date` (default today).
- Skips tickers whose newest report is under `--skip-days` old unless `--force`.
  Deliberately not aggressive: `trading_graph.py:468` stores each decision for
  deferred reflection on the next same-ticker run, so periodic re-analysis is how the
  memory log learns.
- `--retry-failed` re-runs only failures. A ticker failure is recorded and skipped and
  **never** aborts the batch.
- Per ticker: `propagate()` → `save_reports()` → render `complete_report.html`.
- Writes `review_state.json` (per-ticker status, report path, decision) beside — never
  inside — `seeds.json`, keeping provenance immutable.
- Then `summary.html`: decision, score, filing reference, link to the rendered report.

## Commit 4 — Markdown → HTML rendering

Write `complete_report.html` beside every `complete_report.md`, including for plain
`analyze` runs. Self-contained with inline CSS and **no CDN or runtime fetch** —
these pages are opened over `file://`, where Chrome blocks `fetch()` of local files as
cross-origin, so a client-side markdown renderer cannot work.

## Defaults

`--days 90` · `--min-amount 15001` · `--peers 5` · `--thesis-limit 25` ·
`--max-symbols 25` · `--skip-days 7` · `--concurrency 1`

## Testing

**Logic only**, by explicit decision: filtering, ranking, capping, `seeds.json` schema,
dedup. No PDF/parser fixtures, no HTML snapshots. Marked `unit`, offline, matching the
repo's monkeypatch conventions (CI runs `pytest -q` on py3.10–3.13 with no network).

Known accepted risk: the PDF parser is unpinned, so a House form-layout change or a
pdfplumber upgrade will fail silently rather than loudly.

## Scale expectation

90 days ≈ 135 filings → 40–120 unique seeds → with peers ≈ 240–720 candidates in
`seeds.json`, of which 25 are analysed. At ~15 min/ticker that is ~6 hours, which the
commit-1 thinking split should cut materially.

## Rejected alternatives

- **SEC Form 4 / 13F / 13D-G** — considered and dropped when scope narrowed to
  congressional disclosures. All three are EDGAR-based and would share one client.
- **Senate PTRs** — deferred, not abandoned; the `DisclosureSource` protocol exists
  for it. Blocked on the agreement/CSRF session flow and OCR for scanned filings.
- **LLM-based symbol extraction** — rejected; the ticker is literally parenthesised in
  the PDF, so an LLM would be slower, non-deterministic and able to hallucinate.
- **LLM-named peers** — rejected in favour of `yf.Industry.top_companies`, which is
  free, deterministic and needs no new dependency.
- **Alpha Vantage NEWS_SENTIMENT** for historical news — genuinely supports
  `time_from`/`time_to`, but needs a new API key and its free tier (~25 req/day)
  conflicts with a 25-symbol batch.
- **Weighted composite score** — rejected; the weights would be invented and
  unexplainable in the HTML.
- **Mutable single `seeds.json`** — rejected; entangles provenance with run state.
- **Capping at pull time** — rejected; widening the cap would require a full re-pull.
- **Linking the HTML at raw `.md`** — rejected; renders as unstyled plain text.
- **Parallel-by-default review** — rejected; local Ollama serialises on GPU unless
  `OLLAMA_NUM_PARALLEL` is raised and VRAM allows, so it mostly buys messier logs.
