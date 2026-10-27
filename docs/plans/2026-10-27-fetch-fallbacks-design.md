# Fetch fallbacks: block detection, local browser, local PDFs, gentle Wayback

Status: approved design (2026-10-27). Branch `fix/fetch-fallbacks` off `master` (after PR #13).

## Problem

The 2026-10-26 Peregrine run lost a large share of sources at the fetch stage:

- Bot walls (Cloudflare and similar) on citizenportal.ai, MuckRock, eScribe, durhamnc.gov, placer.ca.gov. Jina's servers are blocked; the Playwright fallback exists in code but Playwright was never installed (tier report: `playwright: missing`), and it runs last, after Wayback.
- Plain 403s for scripted clients on government PDFs (FDLE board packages). An in-page `fetch` in a real browser on this machine succeeded.
- Block pages passing as content: the only failure test is length (`MIN_USEFUL_CONTENT_CHARS = 200`), so a long challenge or login page counts as a fetch.
- Wayback 429s: every failure falls through to Wayback, through Jina, with no throttle or backoff.
- Image-only PDFs (Mesa AZ minutes, Santa Ana agreement) returned empty text.
- `fetch_playwright.py` returns `page.content()` (raw HTML) and cannot read PDFs.

Out of scope: SearXNG engine blocks (separate work), and anything that defeats an interactive CAPTCHA. No stealth patches, user-agent or fingerprint spoofing, or CAPTCHA-solving services.

## Design

### New modules

**`scripts/block_detect.py`.** `classify(status: int | None, content_type: str, text: str) -> Verdict`, where `Verdict` is `ok`, `blocked(reason)`, `missing` or `thin`.
- `blocked`: HTTP 401/403/429/503, or text matching bot-wall markers (`Just a moment`, `Attention Required`, `Verify you are human`, `cf-chl`, Turnstile/hCaptcha/reCAPTCHA widgets, `Access denied`, `Request blocked`) or a sign-in wall. Reasons: `cloudflare`, `captcha`, `login`, `http_<code>`, `denied`.
- `captcha` is terminal for automatic fetchers; it routes to a person.
- `missing`: 404/410. `thin`: under `MIN_USEFUL_CONTENT_CHARS`.
- Markers are a module-level list, so new walls are a one-line addition plus a fixture.

**`scripts/pdf_extract.py`.** `extract(pdf_bytes: bytes, cache_dir: Path) -> (text, info)` with PyMuPDF.
- Pages with near-empty text layers are OCR'd via PyMuPDF's Tesseract integration when Tesseract is available (`info.method = "pdf-ocr"`, otherwise `"pdf-local"`).
- If OCR is unavailable and the document is image-only (most pages empty), save the bytes to `cache_dir/pdf/<sha256>.pdf` and return `info.image_only_pdf = True`, `info.pdf_path`. Text from any pages that do have a layer is still returned.

**`scripts/wayback.py`.** A small client with a process-wide limiter: one request at a time, a 1.5 s minimum gap, exponential backoff on 429/503 (honouring `Retry-After`), at most 4 tries.
- Snapshot lookup via the CDX API (`statuscode:200`, latest), then a direct GET of the raw `.../web/<ts>id_/<url>` copy. No Jina hop.
- HTML is reduced to text with the same extractor the browser path uses; PDFs go to `pdf_extract`.

**`scripts/fetch_playwright.py` (rewritten).** A `BrowserSession` context manager: one Chromium per fetch run, persistent profile under `.cache/browser-profile/`.
- HTML: `goto`, then wait up to ~15 s while `classify` says `cloudflare` (passive challenges clear on their own in a real browser), then return readable text (`document.body.innerText` and title).
- PDF (by response content-type or URL): download with the session's request context, keeping cookies, and pass the bytes to `pdf_extract`.
- If the page still classifies as `captcha`, stop and return `blocked("captcha")`.

### Chain (`fetch_url`)

1. Jina, as now; its result goes through `classify`. If Jina reports a PDF (content-type or `.pdf` URL), fetch the bytes directly and extract locally instead of using Jina's text.
2. `blocked` or `thin`: browser, then Wayback.
3. `missing`: Wayback, then browser.
4. Still `blocked`: the URL goes to `needs_browser` with the reason and attempts. Every other failure goes to `failed`.

`attempts` becomes a real record: a list of `{method, verdict, reason}` for each attempt.

### Two phases per run

Phase 1 runs Jina for all URLs (parallel path with 3 workers, or serial with `fetch_delay`). Phase 2 handles the failures one at a time, with a single `BrowserSession` and the Wayback limiter. This keeps Playwright's sync API on one thread and keeps Wayback calm.

Fold-in cleanup: `process_urls` and `_fetch_single` duplicate the cache-hit and record-building code. Both now call one `_record(...)` helper.

### Output (additive)

`fetch_results` entries:
- `fetch_method` adds `pdf-local`, `pdf-ocr`, `playwright`, `wayback` and `browser`.
- PDF entries add `image_only_pdf` and `pdf_path`.

Top level:
- New `needs_browser: [{url, reason, attempts}]`.
- `stats.needs_browser` gives its count.

The cache stores the same fields, so cache hits keep `image_only_pdf` and `pdf_path`.

### Skill and agents

**Browser pass** (`skills/researcher/SKILL.md`, Stage 4b, new step after the fetch):
- Runs when `needs_browser` is non-empty and the browser pane tools (`mcp__Claude_Browser__*`) are available.
- The orchestrator reads each URL: HTML via navigate plus page text; PDFs via in-page `fetch` plus pdf.js text extraction, the method that worked on the FDLE packages.
- Results are written with the new `rw.py add-browser-fetch --vault V --hop N --topic T --url U --title X --content-file F`. It appends to `fetch_results_hop{N}.json` (removing the URL from `needs_browser`) and writes the cache entry with `fetch_method: "browser"`.

**Manual URLs:**
- Pages that still show a CAPTCHA, or any page when no browser tools are available (plain CLI, the batch workflow), go to run state `manual_urls` via `rw.py add-manual-url`.
- The Stage 10 summary lists them.

**Summarizers:**
- The Claude summarizer prompts (Stage 4d base tier, and `agents/fetch-summarize-runner.md`) gain one rule: when an item has `image_only_pdf`, Read the pages at `pdf_path` and summarize from the images.
- `summarize.py` on the Ollama path marks such items `usable: false`, reason `needs visual read`.

### Setup and tier detection

- `pyproject.toml` adds `playwright`, re-locked with `uv lock`, subject to the 72-hour package-age gate.
- `/researcher-setup` runs `uv run --frozen --project "REPO" playwright install chromium` after `uv sync --frozen`.
- `detect_tier.check_playwright` confirms the Chromium executable exists, not just the import.
- New `check_tesseract` (`shutil.which("tesseract")` or PyMuPDF's tessdata lookup) reports present or missing, with the install line `winget install UB-Mannheim.TesseractOCR`. Optional; it does not change the tier.

### Fold-ins from the Peregrine run (separate commit)

1. `rw.py compute-confidence` accepts a `fetch_results` file (`{"fetched": [...]}`) as well as a bare list of sources, using `fetched` entries that aren't `usable: false`.
2. `state.aggregate_hop_summaries` stamps `topic_key` and `hop` (from the filename) onto each item.
3. `state.quality_gate` and `topic_passes` skip topics with status `superseded`. `rw.py` gains `mark-superseded --topic T --reason R`, which sets the status and records `run["superseded"][topic] = reason`.

## Error handling

- Every fetcher returns a `Verdict`; none raises for an expected block. Unexpected exceptions become `blocked("error:<type>")` or `failed` with the message.
- Wayback stops after its retry budget and never blocks the run.
- If Playwright is missing at runtime, phase 2 skips the browser step and records `playwright_unavailable` in attempts.
- If OCR fails on a page, keep any text found and set the image-only flag so the visual read path still applies.
- SSRF validation (`validate_url`) still runs before every network call, including Wayback raw URLs and browser navigation.

## Testing

- `tests/test_block_detect.py`: saved fixture pages (Cloudflare interstitial, Turnstile, hCaptcha, login wall, access denied, a normal long article, a short real page).
- `tests/test_pdf_extract.py`: a generated text PDF; an image-only PDF (text rendered to an image) with OCR mocked available (text returned) and unavailable (saved file plus flag).
- `tests/test_wayback.py`: mocked CDX response; 429, 429, 200 with `Retry-After`; the limiter's spacing (time mocked); raw `id_` URL construction.
- `tests/test_fetch_chain.py`: `fetch_url` and the two-phase run with mocked fetchers. Covers ordering per verdict, `needs_browser` vs `failed` routing, the `attempts` record, cache round-trip of PDF fields, and full-text cache still truncating only output.
- `tests/test_fetch_playwright.py`, behind `@pytest.mark.browser` (skipped without Chromium): a local `file://` HTML page and a local PDF served by `http.server`.
- `tests/test_rw_cli.py`: `add-browser-fetch`, `add-manual-url`, `mark-superseded`, `compute-confidence` with the fetch_results shape.
- `tests/test_state*.py`: aggregation stamping; gate skips superseded.
- `tests/test_skill_contract.py`: the existing scanners cover the new subcommands; add a check that Stage 4b mentions `needs_browser`.

## Docs

MANIFEST lines for the three new modules and the rewritten fetcher (full rewrite before merge, per repo convention); README setup section (Chromium install, optional Tesseract).
