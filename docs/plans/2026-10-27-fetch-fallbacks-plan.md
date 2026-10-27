# Fetch Fallbacks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop losing sources at the fetch stage: tell bot walls and empty pages apart from real content, fall back to a local Chromium and a rate-limited Wayback client, extract PDFs locally (with OCR or a visual-read handoff for scans), and route pages no script can open to the orchestrator's own browser or to a manual-read list.

**Architecture:** `block_detect.classify()` turns every fetch attempt into a `Verdict` (`ok` / `blocked(reason)` / `missing` / `thin`), carried in a shared `FetchResult`. `fetch_and_clean.py` runs two phases: phase 1 is Jina (plus a direct GET for PDFs) over every uncached URL; phase 2 takes the failures one at a time through one `BrowserSession` (Playwright, persistent profile) and the `wayback` client, ordered by the phase-1 verdict. URLs still behind a wall land in a new `needs_browser` list, which the orchestrator reads with the browser-pane tools and records through `rw.py add-browser-fetch`, or parks with `rw.py add-manual-url`. PDFs go through `pdf_extract` (PyMuPDF, Tesseract OCR when installed, otherwise a saved file flagged `image_only_pdf` for a Claude visual read).

**Tech Stack:** Python 3.12 (uv, locked), requests, PyMuPDF 2.0 (+ optional Tesseract), Playwright (sync API, Chromium), stdlib `html.parser`, pytest + pytest-mock.

**Design:** `docs/plans/2026-10-27-fetch-fallbacks-design.md` (approved).

---

## Environment rules for the implementer (read first)

- Repo: `C:/Users/tim/workspace/research-workflow`, branch `fix/fetch-fallbacks`. Shell is **Git Bash on Windows**. Run every command from the repo root (start the Bash call with `cd C:/Users/tim/workspace/research-workflow && ...` if your cwd was reset).
- **All Python goes through uv.** Tests: `uv run --frozen pytest <args>` from the repo root. Anything else: `uv run --frozen --project C:/Users/tim/workspace/research-workflow <cmd>`. Never run bare `python`, never `python -c`, never heredocs (a shell-guard hook blocks them). Create and change files only with the Write and Edit tools.
- `uv.lock` records `exclude-newer` settings, so uv is always run with `--frozen` except for the single `uv lock` in Task 6 (adding Playwright). Go back to `--frozen` right after.
- **Package-age gate:** versions younger than 72 hours are blocked. Task 6 adds `playwright>=1.48` and lets uv resolve under the gate; it then checks that the locked version is at least 2 weeks old.
- **Commits:** write each message with the Write tool to its own file, `.tmp/commit-taskN.txt` (`.tmp/` is gitignored), then `git commit -F .tmp/commit-taskN.txt`. Never `-m` with multi-line text. Every message ends with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage files by name; never `git add -A`. `docs/` is gitignored and this plan stays uncommitted.
- **Skill and agent text:** every Python call stays a single line of the form `uv run --frozen --project "REPO" "REPO/scripts/rw.py" <sub> [flags]` (or `.../scripts/<script>.py`). `tests/test_skill_contract.py` enforces this and checks that every `rw.py` subcommand a skill or agent names exists in `rw.build_parser()`.
- All new file I/O passes `encoding="utf-8"` (Windows defaults to cp1252).
- **No CAPTCHA solving, stealth patches, user-agent or fingerprint changes anywhere.** A CAPTCHA is reported, never touched.
- One commit per task. The suite is green at the end of every task.

## File Structure

**Create**
- `scripts/block_detect.py`: `Verdict`, `FetchResult`, `classify()` and the bot-wall `MARKERS` list. The one place that decides "is this real content".
- `scripts/pdf_extract.py`: PDF bytes to text with PyMuPDF; OCR of empty pages via Tesseract when present; saves image-only PDFs under `cache_dir/pdf/`; `looks_like_pdf()`, `tesseract_available()`, `to_result()`.
- `scripts/url_safety.py`: `validate_url()` (SSRF guard, moved out of `fetch_and_clean.py` so the new fetchers can import it without a cycle) and `UnsafeURLError`.
- `scripts/wayback.py`: CDX lookup + raw `id_` fetch behind a process-wide limiter with 429/503 backoff.
- `tests/test_block_detect.py`, `tests/fixtures/pages/*` (7 fixture pages).
- `tests/test_pdf_extract.py`, `tests/test_text_utils.py`, `tests/test_wayback.py`.
- `tests/test_fetch_chain.py`: `fetch_url`, the two-phase `process_urls`, routing, attempts, records, cache round-trip.

**Modify**
- `scripts/text_utils.py`: add `html_to_text()` (stdlib), shared by the browser and Wayback paths.
- `scripts/fetch_playwright.py`: rewritten as `BrowserSession`.
- `scripts/fetch_and_clean.py`: rewritten chain, two phases, `_record` helper, `needs_browser`, PDF fields, `add_browser_fetch()`.
- `scripts/fetch_media.py:29`: import `validate_url` from `url_safety`.
- `scripts/detect_tier.py`: `check_playwright` confirms Chromium exists; new `check_tesseract`; tier report lists it.
- `scripts/rw.py`: `check playwright|tesseract`, `add-browser-fetch`, `add-manual-url`, `mark-superseded`; `compute-confidence` accepts a fetch_results file.
- `scripts/state.py`: `add_manual_url`, `mark_superseded`, `create_run` fields, `quality_gate`/`topic_passes` skip superseded, `aggregate_hop_summaries` stamps `topic_key`/`hop`.
- `scripts/summarize.py`: image-only PDFs come back `usable: false`, `needs visual read`; prepared files tell Haiku where the PDF is.
- `pyproject.toml`, `uv.lock`: `playwright` dependency; `browser` pytest marker.
- `skills/researcher/SKILL.md` (Stage 4b browser pass, 4d visual read, 5a superseded, 10b manual URLs), `skills/researcher-setup/SKILL.md` (Chromium install, optional Tesseract), `agents/fetch-summarize-runner.md`.
- `tests/test_fetch_and_clean.py` (old chain tests removed; they move to `test_fetch_chain.py`), `tests/test_fetch_playwright.py` (rewritten), `tests/test_detect_tier.py`, `tests/test_rw_cli.py`, `tests/test_state.py`, `tests/test_summarize.py`, `tests/test_skill_contract.py`.
- `README.md`: setup notes for Chromium and Tesseract.

`MANIFEST.md` is rewritten by the orchestrator before merge (see the final section); no task touches it.

---

### Task 1: block_detect verdicts

**Files:**
- Create: `scripts/block_detect.py`
- Create: `tests/fixtures/pages/cloudflare.html`, `cloudflare_jina.md`, `turnstile.html`, `hcaptcha.html`, `login_wall.md`, `access_denied.md`, `short_real.md`
- Test: `tests/test_block_detect.py`

- [ ] **Step 1: Write the fixture pages**

`tests/fixtures/pages/cloudflare.html`:
```html
<!DOCTYPE html>
<html lang="en-US"><head><title>Just a moment...</title>
<meta http-equiv="refresh" content="390"></head>
<body><div class="main-wrapper" role="main"><div class="main-content">
<h1 class="zone-name-title h1">www.example.gov</h1>
<h2 class="h2" id="challenge-running">Verify you are human by completing the action below.</h2>
<div id="challenge-stage"></div>
<div id="challenge-body-text" class="core-msg spacer">www.example.gov needs to review the security of your connection before proceeding.</div>
</div></div>
<script>(function(){window._cf_chl_opt={cvId: '3', cZone: 'www.example.gov', cType: 'managed'};var a=document.createElement('script');a.src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1';document.head.appendChild(a);}());</script>
</body></html>
```

`tests/fixtures/pages/cloudflare_jina.md`:
```
Title: Just a moment...

URL Source: https://www.example.gov/agenda

Warning: Target URL returned error 403: Forbidden

Markdown Content:
www.example.gov
---------------

Verify you are human by completing the action below.

www.example.gov needs to review the security of your connection before proceeding.
```

`tests/fixtures/pages/turnstile.html`:
```html
<!DOCTYPE html>
<html><head><title>Security check</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script></head>
<body><main><h1>One more step</h1>
<p>Please complete the security check to open the records portal.</p>
<form method="post" action="/check"><div class="cf-turnstile" data-sitekey="1x00000000000000000000AA"></div>
<button type="submit">Continue</button></form></main></body></html>
```

`tests/fixtures/pages/hcaptcha.html`:
```html
<!DOCTYPE html>
<html><head><title>Checking your request</title>
<script src="https://js.hcaptcha.com/1/api.js" async defer></script></head>
<body><h1>Please confirm you are a person</h1>
<form><div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001"></div></form>
</body></html>
```

`tests/fixtures/pages/login_wall.md`:
```
Title: Sign In | Example Document Portal

Markdown Content:
# Sign in to continue

You must be logged in to view this document. Enter the email address and password for your Example Document Portal account.

Email address

Password

[Forgot your password?](https://portal.example.org/reset)
```

`tests/fixtures/pages/access_denied.md`:
```
Title: Access Denied

Markdown Content:
# Access Denied

You don't have permission to access "http://www.example.gov/boards/2026-09-packet.pdf" on this server.

Reference #18.2f3c1702.1761400000.9a1b2c3
```

`tests/fixtures/pages/short_real.md`:
```
Title: Notice of Public Hearing - City of Example

Markdown Content:
# Notice of Public Hearing

The City Council will hold a public hearing on November 12, 2026 at 6:00 p.m. in Council Chambers, 100 Main Street, to receive comments on the proposed automated license plate reader policy. Written comments may be sent to the City Clerk before the hearing.
```

- [ ] **Step 2: Write the failing test**

`tests/test_block_detect.py`:
```python
"""Tests for block_detect.py: content vs bot wall vs gone vs empty."""

from pathlib import Path

import pytest

from block_detect import (
    MIN_USEFUL_CONTENT_CHARS,
    WALL_MAX_CHARS,
    FetchResult,
    Verdict,
    blocked,
    classify,
)

PAGES = Path(__file__).parent / "fixtures" / "pages"
ARTICLE = "The county commission reviewed the license plate reader contract. " * 100


def _page(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name, status, expected", [
    ("cloudflare.html", 403, blocked("cloudflare")),
    ("cloudflare_jina.md", 200, blocked("cloudflare")),
    ("turnstile.html", 200, blocked("captcha")),
    ("hcaptcha.html", 200, blocked("captcha")),
    ("login_wall.md", 200, blocked("login")),
    ("access_denied.md", 403, blocked("denied")),
    ("short_real.md", 200, Verdict("ok")),
])
def test_fixture_pages(name, status, expected):
    assert classify(status, "text/html", _page(name)) == expected


def test_long_article_ignores_wall_words():
    text = ARTICLE + "\nPlease sign in to comment.\n"
    assert len(text) > WALL_MAX_CHARS
    assert classify(200, "text/html", text) == Verdict("ok")


@pytest.mark.parametrize("status", [404, 410])
def test_missing_statuses(status):
    assert classify(status, "text/html", _page("cloudflare.html")) == Verdict("missing", f"http_{status}")


def test_blocked_status_without_markers():
    assert classify(429, "text/html", ARTICLE) == blocked("http_429")
    assert classify(503, "", "") == blocked("http_503")
    assert classify(403, "text/html", "") == blocked("http_403")


def test_thin_page():
    assert classify(200, "text/html", "  tiny  ") == Verdict("thin", "4_chars")
    assert classify(None, "", "x" * (MIN_USEFUL_CONTENT_CHARS - 1)).kind == "thin"


def test_pdf_text_is_never_a_wall():
    text = "Access denied to records older than 1990 under the retention schedule. " * 4
    assert classify(200, "application/pdf", text) == Verdict("ok")


def test_markers_in_html_count_only_when_visible_text_is_short():
    widget = '<div class="h-captcha" data-sitekey="x"></div>'
    short = "Please confirm before continuing to the portal. " * 5
    assert classify(200, "text/html", short, widget) == blocked("captcha")
    assert classify(200, "text/html", ARTICLE, widget) == Verdict("ok")


def test_verdict_and_result_helpers():
    assert Verdict("ok").ok and not blocked("captcha").ok
    assert FetchResult(Verdict("ok"), method="pdf-ocr").is_pdf
    assert not FetchResult(Verdict("ok"), method="jina").is_pdf
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run --frozen pytest tests/test_block_detect.py -v`
Expected: FAIL, collection error `ModuleNotFoundError: No module named 'block_detect'`.

- [ ] **Step 4: Write the implementation**

`scripts/block_detect.py`:
```python
"""block_detect.py — Is a fetched page real content, a bot wall, gone, or empty?

classify() turns (HTTP status, content type, text) into a Verdict. Every fetcher
(Jina, the direct PDF GET, the local browser, Wayback) returns a FetchResult
carrying one, so the fallback chain in fetch_and_clean routes on the verdict
instead of on exceptions or on length alone.
"""

from dataclasses import dataclass

MIN_USEFUL_CONTENT_CHARS = 200  # below this, content is likely empty/blocked
WALL_MAX_CHARS = 5000  # wall pages are short; markers in longer text are ignored
BLOCKED_STATUSES = frozenset({401, 403, 429, 503})
MISSING_STATUSES = frozenset({404, 410})
PDF_METHODS = ("pdf-local", "pdf-ocr")

# (reason, lowercase needle), checked in order; the first hit wins. Cloudflare's
# passive interstitial comes first so the browser waits on it rather than giving
# up. A new wall is one line here plus a fixture in tests/fixtures/pages/.
MARKERS = [
    ("cloudflare", "just a moment..."),
    ("cloudflare", "attention required! | cloudflare"),
    ("cloudflare", "verify you are human"),
    ("cloudflare", "needs to review the security of your connection"),
    ("cloudflare", "checking your browser before accessing"),
    ("cloudflare", "cf-chl"),
    ("cloudflare", "_cf_chl_opt"),
    ("cloudflare", "challenge-platform"),
    ("captcha", "cf-turnstile"),
    ("captcha", "h-captcha"),
    ("captcha", "hcaptcha.com"),
    ("captcha", "g-recaptcha"),
    ("captcha", "www.google.com/recaptcha"),
    ("captcha", "i'm not a robot"),
    ("denied", "access denied"),
    ("denied", "request blocked"),
    ("denied", "you don't have permission to access"),
    ("login", "sign in to continue"),
    ("login", "log in to continue"),
    ("login", "please sign in"),
    ("login", "please log in"),
    ("login", "you must be logged in"),
    ("login", "login required"),
]


@dataclass(frozen=True)
class Verdict:
    kind: str  # "ok" | "blocked" | "missing" | "thin" ("unsafe" from fetch_and_clean)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


OK = Verdict("ok")


def blocked(reason: str) -> Verdict:
    return Verdict("blocked", reason)


@dataclass
class FetchResult:
    """One fetch attempt. content is empty unless the verdict is ok."""

    verdict: Verdict
    content: str = ""
    title: str = ""
    method: str = ""
    content_type: str = ""
    image_only_pdf: bool = False
    pdf_path: str | None = None
    error: str = ""

    @property
    def is_pdf(self) -> bool:
        return self.method in PDF_METHODS


def classify(status: int | None, content_type: str, text: str, html: str = "") -> Verdict:
    """Verdict for one response.

    text is the readable text; html (optional) is the raw markup, scanned for
    widget markers alongside text. Markers only count when the visible text is
    short, so a long article with a reCAPTCHA comment form stays ok. Marker hits
    beat status codes, so a Cloudflare 403 reports "cloudflare", not "http_403".
    """
    if status in MISSING_STATUSES:
        return Verdict("missing", f"http_{status}")
    if "pdf" not in (content_type or "").lower() and len(text) < WALL_MAX_CHARS:
        hay = f"{text}\n{html}".lower()
        for reason, needle in MARKERS:
            if needle in hay:
                return blocked(reason)
    if status in BLOCKED_STATUSES:
        return blocked(f"http_{status}")
    n = len(text.strip())
    if n < MIN_USEFUL_CONTENT_CHARS:
        return Verdict("thin", f"{n}_chars")
    return OK
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run --frozen pytest tests/test_block_detect.py -v`
Expected: PASS (all tests).

- [ ] **Step 6: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

Write `.tmp/commit-task1.txt`:
```
feat(fetch): block_detect verdicts for bot walls, gone and thin pages

classify(status, content_type, text, html="") returns ok, blocked(reason),
missing or thin. Bot-wall markers (Cloudflare interstitial, Turnstile,
hCaptcha, reCAPTCHA, access denied, sign-in walls) are a module-level list
checked only on short pages, so a long article with a captcha form stays ok.
FetchResult is the shared return type for every fetcher.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add scripts/block_detect.py tests/test_block_detect.py tests/fixtures/pages/cloudflare.html tests/fixtures/pages/cloudflare_jina.md tests/fixtures/pages/turnstile.html tests/fixtures/pages/hcaptcha.html tests/fixtures/pages/login_wall.md tests/fixtures/pages/access_denied.md tests/fixtures/pages/short_real.md
git commit -F .tmp/commit-task1.txt
```

---

### Task 2: pdf_extract (local PDFs, OCR, image-only handoff)

**Files:**
- Create: `scripts/pdf_extract.py`
- Test: `tests/test_pdf_extract.py`

- [ ] **Step 1: Write the failing test**

`tests/test_pdf_extract.py`:
```python
"""Tests for pdf_extract.py. Fixture PDFs are generated in-test with PyMuPDF;
OCR is mocked so the suite never needs Tesseract."""

import hashlib

import pymupdf
import pytest

import pdf_extract

LINE = "Board meeting minutes: the council approved the camera contract, page {}."


def _text_pdf(n_pages: int) -> bytes:
    doc = pymupdf.open()
    for i in range(n_pages):
        doc.new_page().insert_text((72, 72), LINE.format(i + 1), fontsize=11)
    return doc.tobytes()


def _image_only_pdf(n_pages: int, text_pages: int = 0) -> bytes:
    """text_pages real text pages, then n_pages pages that are only a picture of text."""
    src = pymupdf.open(stream=_text_pdf(n_pages), filetype="pdf")
    out = pymupdf.open()
    for i in range(text_pages):
        out.new_page().insert_text((72, 72), LINE.format(f"text-{i + 1}"), fontsize=11)
    for page in src:
        pix = page.get_pixmap(dpi=100)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=pix)
    return out.tobytes()


@pytest.fixture
def no_ocr(monkeypatch):
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: False)


def test_text_pdf_is_extracted_locally(tmp_path, no_ocr):
    text, info = pdf_extract.extract(_text_pdf(3), tmp_path)
    assert "camera contract, page 2" in text
    assert info.method == "pdf-local" and info.pages == 3 and info.empty_pages == 0
    assert info.image_only_pdf is False and info.pdf_path is None
    assert not (tmp_path / "pdf").exists()


def test_image_only_pdf_without_ocr_is_saved_and_flagged(tmp_path, no_ocr):
    data = _image_only_pdf(2)
    text, info = pdf_extract.extract(data, tmp_path)
    assert text == ""
    assert info.method == "pdf-local" and info.image_only_pdf is True
    expected = tmp_path / "pdf" / f"{hashlib.sha256(data).hexdigest()}.pdf"
    assert info.pdf_path == str(expected) and expected.read_bytes() == data


def test_image_only_pdf_with_ocr_returns_text(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: True)
    monkeypatch.setattr(pdf_extract, "_ocr_page", lambda page: "Recognised text from a scanned page of minutes.")
    text, info = pdf_extract.extract(_image_only_pdf(2), tmp_path)
    assert text.count("Recognised text") == 2
    assert info.method == "pdf-ocr" and info.image_only_pdf is False and info.pdf_path is None


def test_ocr_failure_keeps_text_and_flags_visual_read(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: True)

    def broken(page):
        raise RuntimeError("tesseract crashed")

    monkeypatch.setattr(pdf_extract, "_ocr_page", broken)
    text, info = pdf_extract.extract(_image_only_pdf(1, text_pages=2), tmp_path)
    assert "page text-1" in text and "page text-2" in text
    assert info.method == "pdf-ocr" and info.image_only_pdf is True and info.pdf_path


def test_mostly_scanned_pdf_keeps_its_text_pages(tmp_path, no_ocr):
    text, info = pdf_extract.extract(_image_only_pdf(3, text_pages=1), tmp_path)
    assert "page text-1" in text
    assert info.pages == 4 and info.empty_pages == 3 and info.image_only_pdf is True


def test_looks_like_pdf():
    assert pdf_extract.looks_like_pdf("https://a.gov/x", "application/pdf; charset=binary")
    assert pdf_extract.looks_like_pdf("https://a.gov/Packet.PDF?v=2", "")
    assert pdf_extract.looks_like_pdf("https://a.gov/download", "application/octet-stream", b"%PDF-")
    assert not pdf_extract.looks_like_pdf("https://a.gov/agenda", "text/html", b"<html")


def test_to_result_verdicts(tmp_path, no_ocr):
    full = pdf_extract.to_result(_text_pdf(5), tmp_path)
    assert full.verdict.ok and full.method == "pdf-local" and full.is_pdf
    thin = pdf_extract.to_result(_text_pdf(1), tmp_path)
    assert thin.verdict.kind == "thin"
    scanned = pdf_extract.to_result(_image_only_pdf(2), tmp_path)
    assert scanned.verdict.ok and scanned.image_only_pdf and scanned.pdf_path
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --frozen pytest tests/test_pdf_extract.py -v`
Expected: FAIL, collection error `ModuleNotFoundError: No module named 'pdf_extract'`.

- [ ] **Step 3: Write the implementation**

`scripts/pdf_extract.py`:
```python
"""pdf_extract.py — PDF bytes to text with PyMuPDF, OCR'ing image-only pages.

Pages whose text layer is (nearly) empty are OCR'd through PyMuPDF's Tesseract
integration when Tesseract is installed. When it isn't, or OCR fails, and most
pages are still empty, the bytes are kept under cache_dir/pdf/<sha256>.pdf and
the result is flagged image_only_pdf so a Claude summarizer can Read the page
images instead. Text from pages that do have a layer is always returned.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pymupdf

from block_detect import MIN_USEFUL_CONTENT_CHARS, OK, FetchResult, Verdict

EMPTY_PAGE_CHARS = 20  # fewer stripped chars than this = no usable text layer
OCR_DPI = 300


@dataclass
class PdfInfo:
    method: str  # "pdf-local" | "pdf-ocr"
    pages: int
    empty_pages: int
    title: str = ""
    image_only_pdf: bool = False
    pdf_path: str | None = None


def looks_like_pdf(url: str, content_type: str, head: bytes = b"") -> bool:
    return (
        "application/pdf" in (content_type or "").lower()
        or urlparse(url).path.lower().endswith(".pdf")
        or head.startswith(b"%PDF")
    )


def tesseract_available() -> bool:
    """PyMuPDF finds Tesseract's language data (TESSDATA_PREFIX or an install)."""
    try:
        pymupdf.get_tessdata()
    except Exception:
        return False
    return True


def _ocr_page(page) -> str:
    textpage = page.get_textpage_ocr(language="eng", dpi=OCR_DPI, full=True)
    return page.get_text(textpage=textpage)


def _is_empty(text: str) -> bool:
    return len(text.strip()) < EMPTY_PAGE_CHARS


def extract(pdf_bytes: bytes, cache_dir: Path) -> tuple[str, PdfInfo]:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        texts = [page.get_text() for page in doc]
        empty = [i for i, t in enumerate(texts) if _is_empty(t)]
        method, ocr_failed = "pdf-local", False
        if empty and tesseract_available():
            method = "pdf-ocr"
            for i in empty:
                try:
                    texts[i] = _ocr_page(doc[i])
                except Exception:
                    ocr_failed = True
        title = (doc.metadata or {}).get("title") or ""
    still_empty = sum(1 for t in texts if _is_empty(t))
    info = PdfInfo(method=method, pages=len(texts), empty_pages=still_empty, title=title)
    if ocr_failed or (texts and still_empty * 2 > len(texts)):
        path = Path(cache_dir) / "pdf" / f"{hashlib.sha256(pdf_bytes).hexdigest()}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pdf_bytes)
        info.image_only_pdf, info.pdf_path = True, str(path)
    text = "\n\n".join(t.strip() for t in texts if not _is_empty(t))
    return text, info


def to_result(pdf_bytes: bytes, cache_dir: Path) -> FetchResult:
    """extract() as a FetchResult. An image-only PDF is ok: the visual read covers it."""
    text, info = extract(pdf_bytes, cache_dir)
    if info.image_only_pdf or len(text) >= MIN_USEFUL_CONTENT_CHARS:
        verdict = OK
    else:
        verdict = Verdict("thin", f"{len(text)}_chars")
    return FetchResult(
        verdict=verdict,
        content=text,
        title=info.title,
        method=info.method,
        content_type="application/pdf",
        image_only_pdf=info.image_only_pdf,
        pdf_path=info.pdf_path,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --frozen pytest tests/test_pdf_extract.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

Write `.tmp/commit-task2.txt`:
```
feat(fetch): pdf_extract with PyMuPDF, Tesseract OCR and image-only handoff

Empty pages are OCR'd when Tesseract is installed (method pdf-ocr). Without
it, or when OCR fails, a mostly-empty PDF is saved under cache_dir/pdf/ and
flagged image_only_pdf with its pdf_path so a Claude summarizer can read the
page images. Text pages are always kept.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add scripts/pdf_extract.py tests/test_pdf_extract.py
git commit -F .tmp/commit-task2.txt
```

---

### Task 3: url_safety, html_to_text and the Wayback client

**Files:**
- Create: `scripts/url_safety.py`, `scripts/wayback.py`
- Modify: `scripts/text_utils.py` (append `html_to_text`), `scripts/fetch_and_clean.py:16-83` (imports; `validate_url` moves out), `scripts/fetch_media.py:29`
- Test: `tests/test_text_utils.py`, `tests/test_wayback.py`, `tests/test_fetch_and_clean.py` (append one test)

- [ ] **Step 1: Write the failing tests**

`tests/test_text_utils.py`:
```python
"""Tests for text_utils.html_to_text."""

from text_utils import html_to_text


def test_html_to_text_drops_scripts_keeps_title_and_blocks():
    html = (
        "<html><head><title> Board  Agenda </title><style>p{color:red}</style></head>"
        "<body><h1>Item 4</h1><p>Camera contract &amp; renewal.</p>"
        "<script>var x = 'hidden';</script><div>Second block</div></body></html>"
    )
    text, title = html_to_text(html)
    assert title == "Board Agenda"
    assert text == "Item 4\nCamera contract & renewal.\nSecond block"


def test_html_to_text_recovers_from_unclosed_head():
    text, title = html_to_text("<html><head><title>T</title><body><p>Body text</p></body>")
    assert title == "T" and text == "Body text"
```

`tests/test_wayback.py`:
```python
"""Tests for wayback.py: CDX lookup, raw id_ fetch, limiter and backoff.
requests and the clock are faked; nothing touches the network."""

import pytest

import wayback
from block_detect import FetchResult, Verdict

ARTICLE_HTML = (
    "<html><head><title>Agenda</title></head><body><p>"
    + "Item 4: license plate reader contract renewal. " * 10
    + "</p></body></html>"
)


class Resp:
    def __init__(self, status=200, json_data=None, text="", content=b"", headers=None):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._json


def cdx_ok():
    return Resp(200, [["timestamp"], ["20250101120000"]], text='[["timestamp"],["20250101120000"]]')


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 100.0, "sleeps": []}

    def sleep(seconds):
        state["sleeps"].append(round(seconds, 3))
        state["t"] += seconds

    monkeypatch.setattr(wayback, "_now", lambda: state["t"])
    monkeypatch.setattr(wayback, "_sleep", sleep)
    monkeypatch.setattr(wayback, "LIMITER", wayback._Limiter(wayback.MIN_GAP_S))
    return state


@pytest.fixture
def net(monkeypatch):
    queue, calls, validated = [], [], []

    def get(url, timeout=60, **kwargs):
        calls.append((url, kwargs.get("params")))
        return queue.pop(0)

    monkeypatch.setattr(wayback.requests, "get", get)
    monkeypatch.setattr(wayback, "validate_url", validated.append)
    return queue, calls, validated


def test_raw_url_uses_id_suffix():
    assert (wayback.raw_url("20250101120000", "https://x.gov/a.pdf")
            == "https://web.archive.org/web/20250101120000id_/https://x.gov/a.pdf")


def test_fetch_html_snapshot(clock, net, tmp_path):
    queue, calls, validated = net
    queue += [cdx_ok(), Resp(200, text=ARTICLE_HTML, headers={"Content-Type": "text/html"})]
    result = wayback.fetch("https://x.gov/agenda", tmp_path)
    assert result.verdict.ok and result.method == "wayback" and result.title == "Agenda"
    assert "license plate reader" in result.content
    cdx_url, params = calls[0]
    assert cdx_url == wayback.CDX_API
    assert params["url"] == "https://x.gov/agenda" and params["filter"] == "statuscode:200"
    assert calls[1][0] == "https://web.archive.org/web/20250101120000id_/https://x.gov/agenda"
    assert validated == ["https://x.gov/agenda", calls[1][0]]
    assert clock["sleeps"] == [1.5]


def test_no_snapshot_is_missing(clock, net, tmp_path):
    queue, calls, _ = net
    queue.append(Resp(200, [], text="[]"))
    assert wayback.fetch("https://x.gov/gone", tmp_path).verdict == Verdict("missing", "no_snapshot")
    assert len(calls) == 1


def test_429_backoff_honours_retry_after(clock, net, tmp_path):
    queue, calls, _ = net
    queue += [Resp(429, headers={"Retry-After": "7"}), Resp(429), cdx_ok(),
              Resp(200, text=ARTICLE_HTML, headers={"Content-Type": "text/html"})]
    assert wayback.fetch("https://x.gov/agenda", tmp_path).verdict.ok
    assert clock["sleeps"] == [7.0, 4.0, 1.5]
    assert len(calls) == 4


def test_gives_up_after_max_tries(clock, net, tmp_path):
    queue, calls, _ = net
    queue += [Resp(429) for _ in range(wayback.MAX_TRIES)]
    assert wayback.fetch("https://x.gov/agenda", tmp_path).verdict == Verdict("blocked", "http_429")
    assert len(calls) == wayback.MAX_TRIES
    assert clock["sleeps"] == [2.0, 4.0, 8.0]


def test_limiter_spaces_requests(clock, net):
    queue, _, _ = net
    queue += [Resp(200), Resp(200)]
    wayback._get("https://web.archive.org/a")
    wayback._get("https://web.archive.org/b")
    assert clock["sleeps"] == [1.5]


def test_pdf_snapshot_goes_to_pdf_extract(clock, net, tmp_path, monkeypatch):
    queue, _, _ = net
    queue += [cdx_ok(), Resp(200, content=b"%PDF-1.7 data", headers={"Content-Type": "application/pdf"})]
    seen = []
    monkeypatch.setattr(wayback, "to_result",
                        lambda body, cache_dir: seen.append(body) or FetchResult(Verdict("ok"), "pdf text", "", "pdf-local"))
    result = wayback.fetch("https://x.gov/packet.pdf", tmp_path)
    assert result.method == "pdf-local" and seen == [b"%PDF-1.7 data"]
```

Append to `tests/test_fetch_and_clean.py` (after the last test):
```python


def test_validate_url_raises_unsafe_url_error():
    from url_safety import UnsafeURLError
    from fetch_and_clean import validate_url
    with pytest.raises(UnsafeURLError):
        validate_url("http://localhost/admin")
    assert issubclass(UnsafeURLError, ValueError)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --frozen pytest tests/test_text_utils.py tests/test_wayback.py tests/test_fetch_and_clean.py -v`
Expected: FAIL: `ImportError: cannot import name 'html_to_text' from 'text_utils'`, `ModuleNotFoundError: No module named 'wayback'`, and `ModuleNotFoundError: No module named 'url_safety'` in the new fetch_and_clean test.

- [ ] **Step 3: Create `scripts/url_safety.py`**

```python
"""url_safety.py — SSRF guard run before every network call.

Moved out of fetch_and_clean.py so wayback.py and fetch_playwright.py can
import it without a circular import. fetch_and_clean re-exports validate_url.
"""

import socket
from ipaddress import ip_address
from urllib.parse import urlparse

_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}


class UnsafeURLError(ValueError):
    """The URL uses a non-http(s) scheme or targets a private/internal address."""


def _is_internal(addr) -> bool:
    return addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local


def validate_url(url: str) -> None:
    """Raise UnsafeURLError for non-http(s) URLs or private/reserved targets."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Blocked URL scheme '{parsed.scheme}': {url}")
    hostname = parsed.hostname or ""
    if hostname in _BLOCKED_HOSTNAMES:
        raise UnsafeURLError(f"Blocked hostname '{hostname}': {url}")
    try:
        addr = ip_address(hostname)
    except ValueError:
        addr = None
    if addr is not None:
        if _is_internal(addr):
            raise UnsafeURLError(f"Blocked private/reserved IP '{hostname}': {url}")
        return
    try:
        resolved = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # let the actual fetch handle DNS failures
    for *_, sockaddr in resolved:
        addr = ip_address(sockaddr[0])
        if _is_internal(addr):
            raise UnsafeURLError(f"Blocked: '{hostname}' resolves to private IP {addr}: {url}")
```

- [ ] **Step 4: Point fetch_and_clean and fetch_media at it**

In `scripts/fetch_and_clean.py`, replace lines 16-83 (from `import argparse` through the end of the `validate_url` function, just before `def url_cache_key`) with:
```python
import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from fetch_playwright import fetch_via_playwright
from url_safety import UnsafeURLError, validate_url  # noqa: F401 (validate_url re-exported)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

DEFAULT_CACHE_DIR = Path(__file__).parent.parent / ".cache" / "fetch"
DEFAULT_TTL_DAYS = 7
DEFAULT_FETCH_DELAY = 1.0  # seconds between fetches
MAX_CONTENT_CHARS = 50_000
MIN_USEFUL_CONTENT_CHARS = 200  # below this, content is likely empty/blocked
JINA_BASE_URL = "https://r.jina.ai"
WAYBACK_API = "https://archive.org/wayback/available"


# ──────────────────────────────────────────────
# URL helpers
# ──────────────────────────────────────────────

```

In `scripts/fetch_media.py`, line 29, replace `from fetch_and_clean import validate_url` with `from url_safety import validate_url`.

- [ ] **Step 5: Append `html_to_text` to `scripts/text_utils.py`**

Add `from html.parser import HTMLParser` below `import re`, then append:
```python


_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
})


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag == "body":
            self._skip = 0  # recover from an unclosed <head>
        elif tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Readable (text, title) from HTML: no scripts/styles, one line per block.

    Shared by the browser path (rendered DOM) and Wayback (archived HTML).
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    text = "\n".join(line for line in lines if line)
    title = " ".join("".join(parser.title_parts).split())
    return text, title
```

- [ ] **Step 6: Create `scripts/wayback.py`**

```python
"""wayback.py — Gentle Wayback Machine client.

A process-wide limiter keeps one request in flight with a MIN_GAP_S minimum
gap, and 429/503 replies back off exponentially (honouring a numeric
Retry-After) for at most MAX_TRIES tries. Snapshot lookup goes through the CDX
API (latest statuscode:200 capture); the copy is fetched raw from
web.archive.org/web/<ts>id_/<url>, with no Jina hop. HTML is reduced with
text_utils.html_to_text, PDFs go to pdf_extract.
"""

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests

from block_detect import FetchResult, Verdict, classify
from pdf_extract import looks_like_pdf, to_result
from text_utils import html_to_text
from url_safety import validate_url

CDX_API = "https://web.archive.org/cdx/search/cdx"
RAW_URL = "https://web.archive.org/web/{ts}id_/{url}"
MIN_GAP_S = 1.5
MAX_TRIES = 4
BACKOFF_BASE_S = 2.0
MAX_RETRY_AFTER_S = 60.0
RETRY_STATUSES = frozenset({429, 503})

_now = time.monotonic
_sleep = time.sleep


class _Limiter:
    """One request at a time, at least min_gap seconds apart."""

    def __init__(self, min_gap: float):
        self.min_gap = min_gap
        self.last: float | None = None
        self.lock = threading.Lock()

    @contextmanager
    def slot(self):
        with self.lock:
            if self.last is not None:
                wait = self.min_gap - (_now() - self.last)
                if wait > 0:
                    _sleep(wait)
            try:
                yield
            finally:
                self.last = _now()


LIMITER = _Limiter(MIN_GAP_S)


def raw_url(timestamp: str, url: str) -> str:
    return RAW_URL.format(ts=timestamp, url=url)


def _retry_delay(response, attempt: int) -> float:
    value = (response.headers.get("Retry-After") or "").strip()
    if value.isdigit():  # ponytail: an HTTP-date Retry-After falls back to backoff
        return min(float(value), MAX_RETRY_AFTER_S)
    return BACKOFF_BASE_S * 2 ** attempt


def _get(url: str, **kwargs):
    """GET through the limiter; retries 429/503. Returns the last response."""
    for attempt in range(MAX_TRIES):
        with LIMITER.slot():
            response = requests.get(url, timeout=60, **kwargs)
        if response.status_code not in RETRY_STATUSES or attempt == MAX_TRIES - 1:
            return response
        _sleep(_retry_delay(response, attempt))


def fetch(url: str, cache_dir: Path) -> FetchResult:
    """Latest archived copy of url. Never raises for an expected miss or block."""
    validate_url(url)
    cdx = _get(CDX_API, params={
        "url": url, "output": "json", "filter": "statuscode:200",
        "fl": "timestamp", "limit": "-1",
    })
    if cdx.status_code != 200:
        return FetchResult(classify(cdx.status_code, "", ""), method="wayback")
    rows = cdx.json() if cdx.text.strip() else []
    if len(rows) < 2:  # first row is the header
        return FetchResult(Verdict("missing", "no_snapshot"), method="wayback")
    archived = raw_url(rows[-1][0], url)
    validate_url(archived)
    response = _get(archived)
    ctype = response.headers.get("Content-Type", "")
    if response.status_code != 200:
        return FetchResult(classify(response.status_code, ctype, ""), method="wayback", content_type=ctype)
    if looks_like_pdf(url, ctype, response.content[:5]):
        return to_result(response.content, cache_dir)
    text, title = html_to_text(response.text)
    verdict = classify(200, ctype, text)
    return FetchResult(verdict, text if verdict.ok else "", title, "wayback", ctype)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_text_utils.py tests/test_wayback.py tests/test_fetch_and_clean.py tests/test_fetch_media.py -v`
Expected: PASS (the existing `validate_url` tests still pass through the re-export).

- [ ] **Step 8: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass.

- [ ] **Step 9: Commit**

Write `.tmp/commit-task3.txt`:
```
feat(fetch): gentle Wayback client, shared html_to_text, url_safety module

wayback.fetch looks up the latest 200 capture via CDX and GETs the raw
id_ copy directly (no Jina hop), behind a process-wide limiter: one request
at a time, 1.5 s apart, 429/503 backoff honouring Retry-After, 4 tries.
validate_url moves to url_safety.py (UnsafeURLError) so the new fetchers
can import it; fetch_and_clean re-exports it and fetch_media imports it
directly. text_utils.html_to_text is the stdlib extractor shared by the
browser and Wayback paths.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add scripts/url_safety.py scripts/wayback.py scripts/text_utils.py scripts/fetch_and_clean.py scripts/fetch_media.py tests/test_text_utils.py tests/test_wayback.py tests/test_fetch_and_clean.py
git commit -F .tmp/commit-task3.txt
```

---

### Task 4: BrowserSession (fetch_playwright rewrite)

**Files:**
- Modify: `scripts/fetch_playwright.py` (whole file), `pyproject.toml:25-26` (pytest marker)
- Test: `tests/test_fetch_playwright.py` (whole file)

Playwright is not installed yet (Task 6). The unit tests drive `BrowserSession` with a fake page and context; the real-browser test is marked `browser` and skips until Chromium exists.

- [ ] **Step 1: Register the marker**

In `pyproject.toml`, replace:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```
with:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "browser: drives a real Chromium (uv run --frozen playwright install chromium --only-shell); skipped when it is missing",
]
```

- [ ] **Step 2: Write the failing test**

Replace `tests/test_fetch_playwright.py` with:
```python
"""Tests for fetch_playwright.BrowserSession.

Unit tests use a fake page/context. The real-browser test is marked `browser`
and skips when Playwright or its Chromium build is missing."""

import functools
import http.server
import sys
import threading
from pathlib import Path

import pytest

import fetch_playwright
from block_detect import FetchResult, Verdict

PAGES = Path(__file__).parent / "fixtures" / "pages"
CF_HTML = (PAGES / "cloudflare.html").read_text(encoding="utf-8")
HCAPTCHA_HTML = (PAGES / "hcaptcha.html").read_text(encoding="utf-8")
ARTICLE_HTML = (
    "<html><head><title>Board Minutes</title></head><body><p>"
    + "The board approved the license plate reader contract. " * 10
    + "</p></body></html>"
)


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self.headers = headers if headers is not None else {"content-type": "text/html"}
        self._body = body

    @property
    def ok(self):
        return 200 <= self.status < 300

    def body(self):
        return self._body


class FakePage:
    def __init__(self, htmls, response=None, goto_error=None):
        self.htmls = list(htmls)
        self.response = response or FakeResponse()
        self.goto_error = goto_error
        self.waits = 0
        self.visited = []
        self.closed = False

    def goto(self, url, timeout, wait_until):
        self.visited.append(url)
        if self.goto_error:
            raise self.goto_error
        return self.response

    def content(self):
        return self.htmls[min(self.waits, len(self.htmls) - 1)]

    def wait_for_timeout(self, ms):
        self.waits += 1

    def title(self):
        return ""

    def close(self):
        self.closed = True


class FakeRequest:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        return self.response


class FakeContext:
    def __init__(self, page=None, pdf_response=None):
        self.page = page
        self.request = FakeRequest(pdf_response or FakeResponse(200, {"content-type": "application/pdf"}, b"%PDF-1.7 x"))

    def new_page(self):
        return self.page


def _session(context):
    session = fetch_playwright.BrowserSession()
    session._context = context
    session.available = True
    return session


@pytest.fixture(autouse=True)
def validated(monkeypatch):
    seen = []
    monkeypatch.setattr(fetch_playwright, "validate_url", seen.append)
    return seen


@pytest.fixture
def fake_pdf(monkeypatch):
    bodies = []
    monkeypatch.setattr(fetch_playwright, "to_result",
                        lambda body, cache_dir: bodies.append(body) or FetchResult(Verdict("ok"), "pdf text", "", "pdf-local"))
    return bodies


def test_html_page_returns_readable_text(tmp_path, validated):
    page = FakePage([ARTICLE_HTML])
    result = _session(FakeContext(page)).fetch("https://a.gov/minutes", tmp_path)
    assert result.verdict.ok and result.method == "playwright" and result.title == "Board Minutes"
    assert "license plate reader contract" in result.content and "<p>" not in result.content
    assert validated == ["https://a.gov/minutes"] and page.closed


def test_passive_challenge_is_waited_out(tmp_path):
    page = FakePage([CF_HTML, CF_HTML, ARTICLE_HTML], response=FakeResponse(403))
    result = _session(FakeContext(page)).fetch("https://a.gov/minutes", tmp_path)
    assert result.verdict.ok and page.waits == 2


def test_challenge_that_never_clears_stays_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_playwright, "CHALLENGE_WAIT_S", 0)
    page = FakePage([CF_HTML], response=FakeResponse(403))
    result = _session(FakeContext(page)).fetch("https://a.gov/minutes", tmp_path)
    assert result.verdict == Verdict("blocked", "cloudflare") and result.content == ""


def test_captcha_is_returned_untouched(tmp_path):
    page = FakePage([HCAPTCHA_HTML])
    result = _session(FakeContext(page)).fetch("https://a.gov/portal", tmp_path)
    assert result.verdict == Verdict("blocked", "captcha") and page.waits == 0


def test_pdf_url_downloads_through_the_session(tmp_path, fake_pdf):
    page = FakePage([ARTICLE_HTML])
    context = FakeContext(page)
    result = _session(context).fetch("https://a.gov/packet.pdf", tmp_path)
    assert result.method == "pdf-local" and fake_pdf == [b"%PDF-1.7 x"]
    assert context.request.urls == ["https://a.gov/packet.pdf"] and page.visited == []


def test_pdf_by_content_type(tmp_path, fake_pdf):
    page = FakePage([""], response=FakeResponse(200, {"content-type": "application/pdf"}))
    result = _session(FakeContext(page)).fetch("https://a.gov/download?id=7", tmp_path)
    assert result.method == "pdf-local" and fake_pdf == [b"%PDF-1.7 x"]


def test_download_navigation_falls_back_to_request(tmp_path, fake_pdf):
    page = FakePage([""], goto_error=RuntimeError("Download is starting"))
    result = _session(FakeContext(page)).fetch("https://a.gov/download?id=7", tmp_path)
    assert result.method == "pdf-local" and page.closed


def test_pdf_403_is_blocked(tmp_path, fake_pdf):
    context = FakeContext(FakePage([""]), pdf_response=FakeResponse(403, {"content-type": "text/html"}))
    result = _session(context).fetch("https://a.gov/packet.pdf", tmp_path)
    assert result.verdict == Verdict("blocked", "http_403") and result.method == "playwright"
    assert fake_pdf == []


def test_session_without_playwright_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with fetch_playwright.BrowserSession() as session:
        assert session.available is False


def test_no_stealth_or_user_agent_override():
    source = Path(fetch_playwright.__file__).read_text(encoding="utf-8").lower()
    assert "user_agent" not in source and "stealth" not in source


def _chromium_or_skip():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        with sync_api.sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
    except Exception as exc:
        pytest.skip(f"playwright unusable: {exc}")
    if not exe.exists():
        pytest.skip("Chromium not installed (uv run --frozen playwright install chromium --only-shell)")


@pytest.mark.browser
def test_real_browser_reads_file_html_and_served_pdf(tmp_path, monkeypatch):
    _chromium_or_skip()
    import pymupdf

    monkeypatch.setattr(fetch_playwright, "validate_url", lambda url: None)  # local test pages
    page_file = tmp_path / "page.html"
    page_file.write_text(
        "<html><head><title>Local Page</title></head><body><p>"
        + "Council minutes approved the camera contract. " * 20 + "</p></body></html>",
        encoding="utf-8",
    )
    site = tmp_path / "site"
    site.mkdir()
    doc = pymupdf.open()
    for i in range(3):
        doc.new_page().insert_text((72, 72), f"Board package page {i}: the sheriff requested twelve additional readers.")
    (site / "packet.pdf").write_bytes(doc.tobytes())
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with fetch_playwright.BrowserSession(profile_dir=tmp_path / "profile") as session:
            assert session.available
            html_result = session.fetch(page_file.as_uri(), tmp_path / "cache")
            pdf_result = session.fetch(f"http://127.0.0.1:{server.server_address[1]}/packet.pdf", tmp_path / "cache")
    finally:
        server.shutdown()
    assert html_result.verdict.ok and html_result.title == "Local Page"
    assert "camera contract" in html_result.content
    assert pdf_result.verdict.ok and pdf_result.method == "pdf-local"
    assert "twelve additional readers" in pdf_result.content
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run --frozen pytest tests/test_fetch_playwright.py -v`
Expected: FAIL with `AttributeError: module 'fetch_playwright' has no attribute 'BrowserSession'` (and the module-level `validate_url` patch fails the same way).

- [ ] **Step 4: Write the implementation**

Replace `scripts/fetch_playwright.py` with:
```python
"""fetch_playwright.py — Local Chromium fallback for pages scripted clients can't read.

BrowserSession runs one Chromium per fetch run with a persistent profile under
.cache/browser-profile/, so cookies a site sets (including a cleared Cloudflare
check) carry across URLs. HTML pages wait up to CHALLENGE_WAIT_S while a
passive challenge clears, then return readable text. PDFs (by URL, response
content type, or a download navigation) are fetched with the session's request
context, which shares its cookies, and go to pdf_extract. An interactive
CAPTCHA is never touched: it comes back as blocked("captcha"). No fingerprint
patches and no user-agent changes.

Uses Playwright's sync API: one session, one thread.
"""

import sys
import time
from pathlib import Path

from block_detect import FetchResult, Verdict, classify
from pdf_extract import looks_like_pdf, to_result
from text_utils import html_to_text
from url_safety import validate_url

PROFILE_DIR = Path(__file__).parent.parent / ".cache" / "browser-profile"
NAV_TIMEOUT_MS = 30_000
CHALLENGE_WAIT_S = 15.0
POLL_MS = 1000
_DOWNLOAD_ERRORS = ("Download is starting", "net::ERR_ABORTED")


class BrowserSession:
    """Context manager; check .available before calling fetch()."""

    def __init__(self, profile_dir: Path = PROFILE_DIR, headless: bool = True):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.available = False
        self._playwright = None
        self._context = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("[fetch_playwright] playwright not installed; browser fallback off", file=sys.stderr)
            return self
        try:
            self._playwright = sync_playwright().start()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless
            )
            self.available = True
        except Exception as exc:  # e.g. Chromium not downloaded yet
            print(f"[fetch_playwright] browser unavailable: {exc}", file=sys.stderr)
            self.close()
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self) -> None:
        for obj, method in ((self._context, "close"), (self._playwright, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        self._context = self._playwright = None
        self.available = False

    def fetch(self, url: str, cache_dir: Path) -> FetchResult:
        validate_url(url)
        if looks_like_pdf(url, ""):
            return self._fetch_pdf(url, cache_dir)
        page = self._context.new_page()
        try:
            try:
                response = page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            except Exception as exc:
                if any(marker in str(exc) for marker in _DOWNLOAD_ERRORS):
                    return self._fetch_pdf(url, cache_dir)
                raise
            status = response.status if response else None
            ctype = response.headers.get("content-type", "") if response else ""
            if looks_like_pdf(url, ctype):
                return self._fetch_pdf(url, cache_dir)
            html = page.content()
            text, title = html_to_text(html)
            verdict = classify(status, ctype, text, html)
            deadline = time.monotonic() + CHALLENGE_WAIT_S
            while verdict.reason == "cloudflare" and time.monotonic() < deadline:
                page.wait_for_timeout(POLL_MS)
                try:
                    html = page.content()
                except Exception:  # mid-navigation after the challenge clears
                    continue
                text, title = html_to_text(html)
                # The challenge's own 403/503 is stale once it clears: drop the status.
                verdict = classify(None, ctype, text, html)
            title = page.title() or title
            if not verdict.ok:
                return FetchResult(verdict, method="playwright", content_type=ctype)
            return FetchResult(verdict, text, title, "playwright", ctype)
        finally:
            page.close()

    def _fetch_pdf(self, url: str, cache_dir: Path) -> FetchResult:
        response = self._context.request.get(url, timeout=NAV_TIMEOUT_MS)
        ctype = response.headers.get("content-type", "")
        if not response.ok:
            return FetchResult(classify(response.status, ctype, ""), method="playwright", content_type=ctype)
        body = response.body()
        if not looks_like_pdf("", ctype, body[:5]):
            return FetchResult(Verdict("thin", "not_a_pdf"), method="playwright", content_type=ctype)
        return to_result(body, cache_dir)


def fetch_via_playwright(url: str) -> tuple[str, str]:
    """One-shot (content, title) fetch in a throwaway session. Raises RuntimeError."""
    with BrowserSession() as session:
        if not session.available:
            raise RuntimeError("playwright not available")
        result = session.fetch(url, PROFILE_DIR.parent / "fetch")
    if not result.verdict.ok:
        raise RuntimeError(f"playwright fetch failed: {result.verdict.kind} {result.verdict.reason}")
    return result.content, result.title
```

`fetch_via_playwright` keeps `fetch_and_clean.fetch_url` working until Task 5 replaces the chain and deletes it.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run --frozen pytest tests/test_fetch_playwright.py -v`
Expected: PASS for the unit tests; `test_real_browser_reads_file_html_and_served_pdf` SKIPPED (`could not import 'playwright.sync_api'`).

- [ ] **Step 6: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass, 1 skipped.

- [ ] **Step 7: Commit**

Write `.tmp/commit-task4.txt`:
```
feat(fetch): BrowserSession, one Chromium per run with a persistent profile

HTML waits up to 15 s while a passive Cloudflare check clears, then returns
html_to_text of the rendered DOM. PDFs (URL, content type or a download
navigation) are fetched through the session's request context, keeping its
cookies, and go to pdf_extract. A captcha comes back blocked("captcha")
untouched. No fingerprint or user-agent changes. Adds the `browser` pytest
marker for the real-Chromium test, which skips until Chromium is installed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add scripts/fetch_playwright.py tests/test_fetch_playwright.py pyproject.toml
git commit -F .tmp/commit-task4.txt
```

---

### Task 5: fetch chain, two phases, records and needs_browser

**Files:**
- Modify: `scripts/fetch_and_clean.py` (whole file), `scripts/fetch_playwright.py` (delete `fetch_via_playwright`)
- Modify: `tests/test_fetch_and_clean.py` (delete the old chain/process tests)
- Test: `tests/test_fetch_chain.py`

- [ ] **Step 1: Remove the superseded tests**

In `tests/test_fetch_and_clean.py`, delete everything from the line `# ── fetch_via_jina (mocked) ─────────────────` (around line 96) up to, but not including, the line `# ── validate_url (SSRF protection) ────────────`. That removes the old `fetch_via_jina`, `fetch_url`, Playwright-fallback, `process_urls`, `_fetch_single` and `process_urls_parallel` tests; `tests/test_fetch_chain.py` replaces them. The URL, cache and `validate_url` tests stay.

- [ ] **Step 2: Write the failing test**

`tests/test_fetch_chain.py`:
```python
"""Tests for the fetch_and_clean chain: first pass, fallbacks, routing,
attempts records, output records and the cache round-trip."""

import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import fetch_and_clean
from block_detect import FetchResult, Verdict, blocked
from url_safety import UnsafeURLError

BODY = "Real article text about the camera contract. " * 10


def ok(method="jina", content=BODY, **extra):
    return FetchResult(Verdict("ok"), content, "Title", method, **extra)


def bad(kind, reason="", method="jina"):
    return FetchResult(Verdict(kind, reason), method=method)


def att(method, kind, reason=""):
    return {"method": method, "verdict": kind, "reason": reason}


class FakeSession:
    def __init__(self, result=None, available=True, log=None, by_url=None):
        self.result, self.available, self.by_url = result, available, by_url or {}
        self.log = log if log is not None else []
        self.calls, self.entered = [], 0

    def fetch(self, url, cache_dir):
        self.calls.append(url)
        self.log.append("playwright")
        return self.by_url.get(url, self.result)

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        return False


def _resp(status=200, text="", headers=None, content=b""):
    r = MagicMock()
    r.status_code, r.text, r.headers, r.content = status, text, headers or {}, content
    return r


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(fetch_and_clean, "validate_url", lambda url: None)


@pytest.fixture
def chain(monkeypatch):
    """Patch Jina, the direct PDF GET and Wayback; calls records run order."""
    calls, results = [], {}

    def jina(url, key=None):
        calls.append("jina")
        return results["jina"]

    def direct(url, cache_dir):
        calls.append("direct")
        return results["direct"]

    def wb(url, cache_dir):
        calls.append("wayback")
        return results["wayback"]

    monkeypatch.setattr(fetch_and_clean, "fetch_via_jina", jina)
    monkeypatch.setattr(fetch_and_clean, "fetch_pdf_direct", direct)
    monkeypatch.setattr(fetch_and_clean.wayback, "fetch", wb)
    return calls, results


# ── fetchers ──────────────────────────────────

def test_fetch_via_jina_ok_with_title(monkeypatch):
    monkeypatch.setattr(fetch_and_clean.requests, "get",
                        lambda *a, **k: _resp(200, "# My Article\n\n" + BODY, {"Content-Type": "text/markdown"}))
    r = fetch_and_clean.fetch_via_jina("https://a.example/x")
    assert r.verdict.ok and r.method == "jina" and r.title == "My Article" and BODY in r.content


def test_fetch_via_jina_classifies_a_wall(monkeypatch):
    wall = "Title: Just a moment...\n\nVerify you are human by completing the action below."
    monkeypatch.setattr(fetch_and_clean.requests, "get", lambda *a, **k: _resp(200, wall))
    r = fetch_and_clean.fetch_via_jina("https://a.example/x")
    assert r.verdict == blocked("cloudflare") and r.content == ""


def test_fetch_via_jina_sends_key(monkeypatch):
    seen = {}

    def get(url, headers, timeout):
        seen.update(url=url, headers=headers)
        return _resp(200, BODY)

    monkeypatch.setattr(fetch_and_clean.requests, "get", get)
    fetch_and_clean.fetch_via_jina("https://a.example/x", "k1")
    assert seen["url"] == "https://r.jina.ai/https://a.example/x"
    assert seen["headers"]["Authorization"] == "Bearer k1"


def test_fetch_pdf_direct(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_and_clean.requests, "get",
                        lambda *a, **k: _resp(403, "", {"Content-Type": "text/html"}))
    assert fetch_and_clean.fetch_pdf_direct("https://a.example/p.pdf", tmp_path).verdict == blocked("http_403")

    monkeypatch.setattr(fetch_and_clean.requests, "get",
                        lambda *a, **k: _resp(200, "", {"Content-Type": "text/html"}, b"<html>login</html>"))
    assert fetch_and_clean.fetch_pdf_direct("https://a.example/p.pdf", tmp_path).verdict == Verdict("thin", "not_a_pdf")

    monkeypatch.setattr(fetch_and_clean.requests, "get",
                        lambda *a, **k: _resp(200, "", {"Content-Type": "application/pdf"}, b"%PDF-1.7 bytes"))
    monkeypatch.setattr(fetch_and_clean, "to_result", lambda body, cache_dir: ok("pdf-local", body.decode()))
    r = fetch_and_clean.fetch_pdf_direct("https://a.example/p.pdf", tmp_path)
    assert r.method == "pdf-local" and r.content == "%PDF-1.7 bytes"


# ── fetch_url ordering and attempts ───────────

def test_jina_ok_stops_there(chain, tmp_path):
    calls, results = chain
    results["jina"] = ok()
    result, attempts = fetch_and_clean.fetch_url("https://a.example/x", cache_dir=tmp_path,
                                                 session=FakeSession(log=calls))
    assert result.method == "jina" and calls == ["jina"]
    assert attempts == [att("jina", "ok")]


@pytest.mark.parametrize("first", [blocked("cloudflare"), Verdict("thin", "12_chars")])
def test_blocked_or_thin_tries_browser_then_wayback(chain, tmp_path, first):
    calls, results = chain
    results["jina"] = FetchResult(first, method="jina")
    results["wayback"] = ok("wayback")
    session = FakeSession(bad("blocked", "cloudflare", "playwright"), log=calls)
    result, attempts = fetch_and_clean.fetch_url("https://a.example/x", cache_dir=tmp_path, session=session)
    assert calls == ["jina", "playwright", "wayback"] and result.method == "wayback"
    assert attempts == [att("jina", first.kind, first.reason), att("playwright", "blocked", "cloudflare"),
                        att("wayback", "ok")]


def test_missing_tries_wayback_then_browser(chain, tmp_path):
    calls, results = chain
    results["jina"] = bad("missing", "http_404")
    results["wayback"] = bad("missing", "no_snapshot", "wayback")
    session = FakeSession(ok("playwright"), log=calls)
    result, _ = fetch_and_clean.fetch_url("https://a.example/x", cache_dir=tmp_path, session=session)
    assert calls == ["jina", "wayback", "playwright"] and result.method == "playwright"


def test_captcha_skips_the_browser(chain, tmp_path):
    calls, results = chain
    results["jina"] = bad("blocked", "captcha")
    results["wayback"] = bad("missing", "no_snapshot", "wayback")
    session = FakeSession(ok("playwright"), log=calls)
    result, attempts = fetch_and_clean.fetch_url("https://a.example/x", cache_dir=tmp_path, session=session)
    assert result is None and calls == ["jina", "wayback"] and session.calls == []
    assert [a["method"] for a in attempts] == ["jina", "wayback"]


def test_unavailable_browser_is_recorded(chain, tmp_path):
    calls, results = chain
    results["jina"] = bad("thin", "0_chars")
    results["wayback"] = bad("missing", "no_snapshot", "wayback")
    result, attempts = fetch_and_clean.fetch_url("https://a.example/x", cache_dir=tmp_path,
                                                 session=FakeSession(available=False))
    assert result is None
    assert attempts[1] == att("playwright", "skipped", "playwright_unavailable")


def test_fetcher_exception_becomes_error_verdict(chain, tmp_path, monkeypatch):
    calls, results = chain

    def boom(url, key=None):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(fetch_and_clean, "fetch_via_jina", boom)
    results["wayback"] = ok("wayback")
    session = FakeSession(bad("thin", "0_chars", "playwright"), log=calls)
    result, attempts = fetch_and_clean.fetch_url("https://a.example/x", cache_dir=tmp_path, session=session)
    assert attempts[0] == att("jina", "blocked", "error:TimeoutError") and result.method == "wayback"


def test_unsafe_url_stops_the_chain(chain, tmp_path, monkeypatch):
    def unsafe(url, key=None):
        raise UnsafeURLError("Blocked hostname 'localhost'")

    monkeypatch.setattr(fetch_and_clean, "fetch_via_jina", unsafe)
    session = FakeSession(ok("playwright"))
    result, attempts = fetch_and_clean.fetch_url("http://localhost/x", cache_dir=tmp_path, session=session)
    assert result is None and attempts == [att("jina", "unsafe", "ssrf")] and session.calls == []


def test_pdf_url_is_extracted_locally(chain, tmp_path):
    calls, results = chain
    results["jina"] = ok()
    results["direct"] = ok("pdf-local")
    result, _ = fetch_and_clean.fetch_url("https://a.example/packet.pdf?v=2", cache_dir=tmp_path)
    assert calls == ["jina", "direct"] and result.method == "pdf-local"


def test_pdf_direct_403_keeps_jina_text(chain, tmp_path):
    calls, results = chain
    results["jina"] = ok()
    results["direct"] = bad("blocked", "http_403", "direct")
    result, _ = fetch_and_clean.fetch_url("https://a.example/packet.pdf", cache_dir=tmp_path)
    assert result.method == "jina"


def test_pdf_403_after_jina_failure_goes_to_browser(chain, tmp_path):
    calls, results = chain
    results["jina"] = bad("thin", "0_chars")
    results["direct"] = bad("blocked", "http_403", "direct")
    session = FakeSession(ok("pdf-local"), log=calls)
    result, _ = fetch_and_clean.fetch_url("https://a.example/packet.pdf", cache_dir=tmp_path, session=session)
    assert calls == ["jina", "direct", "playwright"] and result.method == "pdf-local"


# ── process_urls: phases, routing, records ────

def _first(monkeypatch, by_url):
    def fake(url, key, cache_dir):
        r = by_url[url]
        return r, [att("jina", r.verdict.kind, r.verdict.reason)]

    monkeypatch.setattr(fetch_and_clean, "first_pass", fake)


def _no_first(monkeypatch):
    monkeypatch.setattr(fetch_and_clean, "first_pass",
                        lambda *a: pytest.fail("first_pass should not run on a cache hit"))


def test_process_urls_routes_failures(monkeypatch, tmp_path):
    good, wall, gone = "https://a.example/ok", "https://a.example/wall", "https://a.example/gone"
    _first(monkeypatch, {good: ok(), wall: bad("blocked", "cloudflare"), gone: bad("missing", "http_404")})
    session = FakeSession(by_url={wall: bad("blocked", "cloudflare", "playwright"),
                                  gone: bad("missing", "http_404", "playwright")})
    monkeypatch.setattr(fetch_and_clean, "BrowserSession", lambda: session)
    monkeypatch.setattr(fetch_and_clean.wayback, "fetch",
                        lambda url, cache_dir: bad("missing", "no_snapshot", "wayback"))
    fetched, failed, needs_browser = fetch_and_clean.process_urls(
        [{"url": u} for u in (good, wall, gone)], tmp_path, 7, None, fetch_delay=0)
    assert [f["url"] for f in fetched] == [good]
    assert needs_browser == [{"url": wall, "reason": "cloudflare", "attempts": [
        att("jina", "blocked", "cloudflare"), att("playwright", "blocked", "cloudflare"),
        att("wayback", "missing", "no_snapshot")]}]
    assert [f["url"] for f in failed] == [gone]
    assert failed[0]["error"].startswith("All fetch methods failed")
    assert [a["method"] for a in failed[0]["attempts"]] == ["jina", "wayback", "playwright"]
    assert session.entered == 1


def test_no_browser_when_nothing_failed(monkeypatch, tmp_path):
    _first(monkeypatch, {"https://a.example/1": ok()})
    monkeypatch.setattr(fetch_and_clean, "BrowserSession", lambda: pytest.fail("browser opened"))
    fetched, failed, needs_browser = fetch_and_clean.process_urls(
        [{"url": "https://a.example/1"}], tmp_path, 7, None)
    assert len(fetched) == 1 and failed == [] and needs_browser == []


def test_cache_hit_skips_fetch(monkeypatch, tmp_path):
    url = "https://a.example/cached"
    fetch_and_clean.save_cache(tmp_path, fetch_and_clean.url_cache_key(url), {
        "url": url, "content": "cached content", "title": "Cached", "fetch_method": "jina",
        "fetched_at": datetime.now(timezone.utc).isoformat()})
    _no_first(monkeypatch)
    fetched, _, _ = fetch_and_clean.process_urls([{"url": url}], tmp_path, 7, None)
    assert fetched[0]["cache_hit"] is True and fetched[0]["content"] == "cached content"


def test_expired_cache_refetches(monkeypatch, tmp_path):
    url = "https://a.example/old"
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    fetch_and_clean.save_cache(tmp_path, fetch_and_clean.url_cache_key(url), {
        "url": url, "content": "old", "title": "", "fetch_method": "jina", "fetched_at": old})
    _first(monkeypatch, {url: ok(content="refreshed content " * 20)})
    fetched, _, _ = fetch_and_clean.process_urls([{"url": url}], tmp_path, 7, None)
    assert fetched[0]["cache_hit"] is False and fetched[0]["content"].startswith("refreshed")


def test_pdf_fields_round_trip_through_cache(monkeypatch, tmp_path):
    url = "https://a.example/minutes.pdf"
    _first(monkeypatch, {url: ok("pdf-local", image_only_pdf=True, pdf_path="C:/cache/pdf/ab.pdf")})
    first, _, _ = fetch_and_clean.process_urls([{"url": url}], tmp_path, 7, None)
    assert first[0]["fetch_method"] == "pdf-local"
    assert first[0]["image_only_pdf"] is True and first[0]["pdf_path"] == "C:/cache/pdf/ab.pdf"
    _no_first(monkeypatch)
    again, _, _ = fetch_and_clean.process_urls([{"url": url}], tmp_path, 7, None)
    assert again[0]["cache_hit"] is True
    assert again[0]["image_only_pdf"] is True and again[0]["pdf_path"] == "C:/cache/pdf/ab.pdf"


def test_html_records_have_no_pdf_fields(monkeypatch, tmp_path):
    _first(monkeypatch, {"https://a.example/1": ok()})
    fetched, _, _ = fetch_and_clean.process_urls([{"url": "https://a.example/1"}], tmp_path, 7, None)
    assert "image_only_pdf" not in fetched[0] and "pdf_path" not in fetched[0]


def test_output_truncated_cache_keeps_full_text(monkeypatch, tmp_path):
    url = "https://a.example/long"
    cap = fetch_and_clean.MAX_CONTENT_CHARS
    _first(monkeypatch, {url: ok(content="x" * (cap + 1000))})
    fetched, _, _ = fetch_and_clean.process_urls([{"url": url}], tmp_path, 7, None)
    assert len(fetched[0]["content"]) == cap
    cached = fetch_and_clean.load_cache(tmp_path, fetch_and_clean.url_cache_key(url))
    assert len(cached["content"]) == cap + 1000


def test_cache_hit_serves_full_content_under_raised_cap(monkeypatch, tmp_path):
    url = "https://a.example/long-rerun"
    cap = fetch_and_clean.MAX_CONTENT_CHARS
    _first(monkeypatch, {url: ok(content="z" * (cap + 1000))})
    fetch_and_clean.process_urls([{"url": url}], tmp_path, 7, None)
    monkeypatch.setattr(fetch_and_clean, "MAX_CONTENT_CHARS", cap * 2)
    _no_first(monkeypatch)
    fetched, _, _ = fetch_and_clean.process_urls([{"url": url}], tmp_path, 7, None)
    assert len(fetched[0]["content"]) == cap + 1000


def test_parallel_first_pass(monkeypatch, tmp_path):
    urls = [f"https://a.example/{i}" for i in range(5)]
    _first(monkeypatch, {u: ok() for u in urls})
    fetched, failed, _ = fetch_and_clean.process_urls([{"url": u} for u in urls], tmp_path, 7, None, workers=3)
    assert sorted(f["url"] for f in fetched) == sorted(urls) and failed == []


def test_serial_first_pass_spaces_requests(monkeypatch, tmp_path):
    urls = [f"https://a.example/{i}" for i in range(3)]
    _first(monkeypatch, {u: ok() for u in urls})
    sleeps = []
    monkeypatch.setattr(fetch_and_clean.time, "sleep", sleeps.append)
    fetch_and_clean.process_urls([{"url": u} for u in urls], tmp_path, 7, None, fetch_delay=0.5)
    assert sleeps == [0.5, 0.5]


def test_main_writes_needs_browser_and_stats(monkeypatch, tmp_path):
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps({"topic": "T", "selected_urls": ["https://a.example/x"]}), encoding="utf-8")
    out = tmp_path / "out.json"
    nb = [{"url": "https://a.example/x", "reason": "cloudflare", "attempts": []}]
    monkeypatch.setattr(fetch_and_clean, "process_urls", lambda urls, cache_dir, ttl, key, workers=1: ([], [], nb))
    monkeypatch.setattr(sys, "argv", ["fetch_and_clean.py", "--input", str(ctx), "--output", str(out),
                                      "--cache-dir", str(tmp_path / "cache")])
    fetch_and_clean.main()
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["needs_browser"] == nb
    assert result["stats"] == {"total_urls": 1, "fetched": 0, "failed": 0, "needs_browser": 1,
                               "cache_hits": 0, "total_words": 0}
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run --frozen pytest tests/test_fetch_chain.py -v`
Expected: FAIL, e.g. `AttributeError: module 'fetch_and_clean' has no attribute 'fetch_pdf_direct'` and `... has no attribute 'wayback'`.

- [ ] **Step 4: Rewrite `scripts/fetch_and_clean.py`**

Replace the whole file with:
```python
"""
fetch_and_clean.py — URL Fetch, Clean, and Cache

Purpose: Tier 2 of the research pipeline. Accepts a search_context JSON,
fetches each URL, caches results by SHA-256(url), and outputs fetch_results JSON.

Two phases per run:
  1. Jina Reader for every uncached URL (serial with a delay, or --workers N in
     parallel). URLs that look like PDFs are also fetched directly and
     extracted locally (pdf_extract).
  2. The failures, one at a time: local Chromium (fetch_playwright.BrowserSession)
     and the rate-limited Wayback client (wayback.py), ordered by the first
     verdicts (block_detect.classify). A URL still behind a wall goes to
     needs_browser for the orchestrator's own browser; the rest go to failed.

Usage:
    uv run --frozen --project REPO REPO/scripts/fetch_and_clean.py --input search_context.json --output fetch_results.json
    uv run --frozen --project REPO REPO/scripts/fetch_and_clean.py --input search_context.json --dry-run
"""

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

import wayback
from block_detect import FetchResult, Verdict, blocked, classify
from fetch_playwright import BrowserSession
from pdf_extract import looks_like_pdf, to_result
from url_safety import UnsafeURLError, validate_url  # validate_url is re-exported

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

DEFAULT_CACHE_DIR = Path(__file__).parent.parent / ".cache" / "fetch"
DEFAULT_TTL_DAYS = 7
DEFAULT_FETCH_DELAY = 1.0  # seconds between phase-1 fetches (serial path)
MAX_CONTENT_CHARS = 50_000
JINA_BASE_URL = "https://r.jina.ai"
LIVE_METHODS = frozenset({"jina", "direct", "playwright"})  # hit the site itself


# ──────────────────────────────────────────────
# URL helpers
# ──────────────────────────────────────────────

def url_cache_key(url: str) -> str:
    """Return SHA-256 hash of URL as cache filename key."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    """Normalize URL for deduplication: lowercase, strip trailing slash."""
    return url.rstrip("/").lower()


def _normalize_url_items(urls: list) -> list[dict]:
    """Normalize URL entries to dicts with a 'url' key.

    Accepts mixed input:
    - Strings: "https://example.com" -> {"url": "https://example.com"}
    - Dicts with 'url' key: passed through as-is
    """
    result: list[dict] = []
    for item in urls:
        if isinstance(item, str):
            result.append({"url": item})
        elif isinstance(item, dict) and "url" in item:
            result.append(item)
        else:
            print(f"[fetch_and_clean] WARNING: skipping unrecognized URL entry: {item!r}",
                  file=sys.stderr)
    return result


def deduplicate_urls(urls: list[dict]) -> list[dict]:
    """Remove duplicate URLs (case-insensitive, trailing-slash agnostic). Preserves order."""
    seen: set[str] = set()
    result: list[dict] = []
    for item in urls:
        key = normalize_url(item["url"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


# ──────────────────────────────────────────────
# Cache helpers
# ──────────────────────────────────────────────

def _content_hash(content: str) -> str:
    """SHA-256 hash of content for cache integrity verification."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_cache(cache_dir: Path, cache_key: str) -> dict | None:
    """Return cached entry dict, or None if missing, corrupt, or tampered."""
    cache_file = cache_dir / f"{cache_key}.json"
    if not cache_file.exists():
        return None
    try:
        entry = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    stored_hash = entry.get("content_hash")
    if stored_hash and _content_hash(entry.get("content", "")) != stored_hash:
        print(f"[fetch_and_clean] WARNING: cache integrity check failed for {cache_key}", file=sys.stderr)
        return None
    return entry


def is_expired(entry: dict, ttl_days: int) -> bool:
    """Return True if cache entry is older than ttl_days."""
    try:
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched_at > timedelta(days=ttl_days)
    except (KeyError, ValueError, TypeError):
        return True  # Treat unparseable entries as expired


def save_cache(cache_dir: Path, cache_key: str, entry: dict) -> None:
    """Write entry JSON to cache file with integrity hash. Creates cache_dir if needed."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    if "content" in entry:
        entry["content_hash"] = _content_hash(entry["content"])
    cache_file = cache_dir / f"{cache_key}.json"
    cache_file.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")


# ──────────────────────────────────────────────
# Fetchers (each returns a FetchResult)
# ──────────────────────────────────────────────

def fetch_via_jina(url: str, api_key: str | None = None) -> FetchResult:
    """Fetch URL via Jina Reader (https://r.jina.ai) and classify the reply."""
    validate_url(url)
    headers = {"Accept": "text/markdown"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = requests.get(f"{JINA_BASE_URL}/{url}", headers=headers, timeout=30)
    ctype = response.headers.get("Content-Type", "")
    content = response.text
    verdict = classify(response.status_code, ctype, content)
    title = next((line[2:].strip() for line in content.splitlines() if line.startswith("# ")), "")
    return FetchResult(verdict, content if verdict.ok else "", title, "jina", ctype)


def fetch_pdf_direct(url: str, cache_dir: Path) -> FetchResult:
    """GET a PDF's bytes ourselves and extract locally (pdf_extract)."""
    validate_url(url)
    response = requests.get(url, timeout=60)
    ctype = response.headers.get("Content-Type", "")
    if response.status_code != 200:
        return FetchResult(classify(response.status_code, ctype, ""), method="direct", content_type=ctype)
    if not looks_like_pdf("", ctype, response.content[:5]):
        return FetchResult(Verdict("thin", "not_a_pdf"), method="direct", content_type=ctype)
    return to_result(response.content, cache_dir)


# ──────────────────────────────────────────────
# Chain
# ──────────────────────────────────────────────

def _try(attempts: list[dict], method: str, fn) -> FetchResult:
    """Run one fetcher; record {method, verdict, reason}. Never raises."""
    try:
        result = fn()
    except UnsafeURLError as exc:
        result = FetchResult(Verdict("unsafe", "ssrf"), method=method, error=str(exc))
    except Exception as exc:
        result = FetchResult(blocked(f"error:{type(exc).__name__}"), method=method, error=str(exc))
    if result.error:
        print(f"[fetch_and_clean] {method} failed: {result.error}", file=sys.stderr)
    attempts.append({"method": method, "verdict": result.verdict.kind, "reason": result.verdict.reason})
    return result


def first_pass(url: str, jina_api_key: str | None, cache_dir: Path) -> tuple[FetchResult, list[dict]]:
    """Phase 1 for one URL: Jina, plus a direct GET + local extract for PDFs."""
    attempts: list[dict] = []
    result = _try(attempts, "jina", lambda: fetch_via_jina(url, jina_api_key))
    if result.verdict.kind != "unsafe" and looks_like_pdf(url, result.content_type):
        direct = _try(attempts, "direct", lambda: fetch_pdf_direct(url, cache_dir))
        if direct.verdict.ok:
            return direct, attempts
    return result, attempts


def _fallback_order(attempts: list[dict]) -> list[str]:
    kinds = {a["verdict"] for a in attempts}
    if "missing" in kinds and "blocked" not in kinds:
        return ["wayback", "playwright"]
    return ["playwright", "wayback"]


def fallback(url: str, first: FetchResult, attempts: list[dict], session, cache_dir: Path) -> FetchResult | None:
    """Phase 2 for one URL. Returns the first ok result, else None (attempts grows)."""
    if first.verdict.ok:
        return first
    if first.verdict.kind == "unsafe":
        return None
    for step in _fallback_order(attempts):
        if step == "playwright":
            if any(a["reason"] == "captcha" for a in attempts):
                continue  # captcha is terminal for automatic live-site fetchers
            if session is None or not session.available:
                attempts.append({"method": "playwright", "verdict": "skipped", "reason": "playwright_unavailable"})
                continue
            result = _try(attempts, "playwright", lambda: session.fetch(url, cache_dir))
        else:
            result = _try(attempts, "wayback", lambda: wayback.fetch(url, cache_dir))
        if result.verdict.ok:
            return result
    return None


def fetch_url(url: str, jina_api_key: str | None = None, cache_dir: Path = DEFAULT_CACHE_DIR,
              session=None) -> tuple[FetchResult | None, list[dict]]:
    """The whole chain for one URL: (ok result or None, attempts)."""
    first, attempts = first_pass(url, jina_api_key, cache_dir)
    return fallback(url, first, attempts, session, cache_dir), attempts


def _live_blocks(attempts: list[dict]) -> list[dict]:
    """Walls the site itself put up (not fetch errors, not Wayback throttling)."""
    return [a for a in attempts
            if a["method"] in LIVE_METHODS and a["verdict"] == "blocked"
            and not a["reason"].startswith("error:")]


def _block_reason(attempts: list[dict]) -> str:
    reasons = [a["reason"] for a in _live_blocks(attempts)]
    return "captcha" if "captcha" in reasons else reasons[-1]


def _failure_summary(attempts: list[dict]) -> str:
    parts = [f"{a['method']}={a['verdict']}" + (f"({a['reason']})" if a["reason"] else "") for a in attempts]
    return "All fetch methods failed: " + ", ".join(parts)


# ──────────────────────────────────────────────
# Records
# ──────────────────────────────────────────────

def _record(entry: dict, cache_hit: bool) -> dict:
    """Output record from a cache entry. MAX_CONTENT_CHARS caps output only."""
    content = entry["content"][:MAX_CONTENT_CHARS]
    record = {
        "url": entry["url"],
        "title": entry.get("title", ""),
        "content": content,
        "fetch_method": entry.get("fetch_method", "cached"),
        "cache_hit": cache_hit,
        "fetched_at": entry["fetched_at"],
        "word_count": len(content.split()),
    }
    if "image_only_pdf" in entry:
        record["image_only_pdf"] = entry["image_only_pdf"]
        record["pdf_path"] = entry.get("pdf_path")
    return record


def _cached_record(cache_dir: Path, url: str, ttl_days: int) -> dict | None:
    cached = load_cache(cache_dir, url_cache_key(url))
    if cached is None or is_expired(cached, ttl_days):
        return None
    return _record(cached, cache_hit=True)


def _store(cache_dir: Path, url: str, result: FetchResult) -> dict:
    """Cache the full text (and PDF fields) and return the output record."""
    entry = {
        "url": url,
        "title": result.title,
        "content": result.content,
        "fetch_method": result.method,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if result.is_pdf:
        entry["image_only_pdf"] = result.image_only_pdf
        entry["pdf_path"] = result.pdf_path
    save_cache(cache_dir, url_cache_key(url), entry)
    return _record(entry, cache_hit=False)


def _stats(fetched: list[dict], failed: list[dict], needs_browser: list[dict]) -> dict:
    return {
        "fetched": len(fetched),
        "failed": len(failed),
        "needs_browser": len(needs_browser),
        "cache_hits": sum(1 for f in fetched if f["cache_hit"]),
        "total_words": sum(f["word_count"] for f in fetched),
    }


# ──────────────────────────────────────────────
# Main processing
# ──────────────────────────────────────────────

def _run_first_pass(urls: list[str], jina_api_key: str | None, cache_dir: Path,
                    fetch_delay: float, workers: int) -> list[tuple[FetchResult, list[dict]]]:
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda u: first_pass(u, jina_api_key, cache_dir), urls))
    results = []
    for i, url in enumerate(urls):
        if i and fetch_delay > 0:
            time.sleep(fetch_delay)
        results.append(first_pass(url, jina_api_key, cache_dir))
    return results


def process_urls(
    urls: list[dict],
    cache_dir: Path,
    ttl_days: int,
    jina_api_key: str | None,
    fetch_delay: float = DEFAULT_FETCH_DELAY,
    workers: int = 1,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch and cache URL dicts (each with a "url" key).

    Returns (fetched, failed, needs_browser). Never raises for a fetch failure.
    """
    fetched: list[dict] = []
    misses: list[str] = []
    for item in urls:
        record = _cached_record(cache_dir, item["url"], ttl_days)
        if record is not None:
            fetched.append(record)
        else:
            misses.append(item["url"])

    pending = []
    for url, (result, attempts) in zip(misses, _run_first_pass(misses, jina_api_key, cache_dir, fetch_delay, workers)):
        if result.verdict.ok:
            fetched.append(_store(cache_dir, url, result))
        else:
            pending.append((url, result, attempts))

    failed: list[dict] = []
    needs_browser: list[dict] = []
    if pending:
        with BrowserSession() as session:
            for url, first, attempts in pending:
                result = fallback(url, first, attempts, session, cache_dir)
                if result is not None:
                    fetched.append(_store(cache_dir, url, result))
                elif _live_blocks(attempts):
                    needs_browser.append({"url": url, "reason": _block_reason(attempts), "attempts": attempts})
                else:
                    error = _failure_summary(attempts)
                    print(f"[fetch_and_clean] FAILED {url}: {error}", file=sys.stderr)
                    failed.append({"url": url, "error": error, "attempts": attempts})
    return fetched, failed, needs_browser


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and cache URLs for the research pipeline (Tier 2)."
    )
    parser.add_argument("--input", required=True, help="Path to search_context.json")
    parser.add_argument("--output", help="Output path for fetch_results.json (stdout if omitted)")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
    parser.add_argument("--workers", type=int, default=1, help="parallel Jina fetches in phase 1")
    parser.add_argument("--dry-run", action="store_true", help="Print URLs; do not fetch")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[fetch_and_clean] ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    search_context: dict = json.loads(input_path.read_text(encoding="utf-8"))
    raw_urls = search_context.get("selected_urls", [])
    urls = deduplicate_urls(_normalize_url_items(raw_urls))

    if args.dry_run:
        print(f"[fetch_and_clean] Dry run — {len(urls)} URL(s) would be fetched:", file=sys.stderr)
        for item in urls:
            print(f"  {item['url']}", file=sys.stderr)
        return

    jina_api_key: str | None = os.environ.get("JINA_API_KEY")
    cache_dir = Path(args.cache_dir)

    fetched, failed, needs_browser = process_urls(urls, cache_dir, args.ttl_days, jina_api_key,
                                                  workers=args.workers)

    result = {
        "topic": search_context.get("topic", ""),
        "search_context": search_context,
        "fetched": fetched,
        "failed": failed,
        "needs_browser": needs_browser,
        "stats": {"total_urls": len(urls), **_stats(fetched, failed, needs_browser)},
    }

    output_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output_json, encoding="utf-8")
        print(f"[fetch_and_clean] Results written to {output_path}", file=sys.stderr)
    else:
        print(output_json)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Delete the one-shot shim**

In `scripts/fetch_playwright.py`, delete the whole `fetch_via_playwright` function at the end of the file (nothing imports it any more).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_fetch_chain.py tests/test_fetch_and_clean.py tests/test_fetch_playwright.py -v`
Expected: PASS (the real-browser test still SKIPPED).

- [ ] **Step 7: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass, 1 skipped.

- [ ] **Step 8: Commit**

Write `.tmp/commit-task5.txt`:
```
feat(fetch): two-phase fetch chain with verdict routing and needs_browser

Phase 1 runs Jina (plus a direct GET + local extract for PDFs) for every
uncached URL; phase 2 takes the failures one at a time through a single
BrowserSession and the Wayback limiter. blocked/thin try the browser first,
missing tries Wayback first; a captcha skips the browser. attempts is now a
real {method, verdict, reason} record. URLs still behind a site wall go to
needs_browser (stats.needs_browser); everything else to failed. PDF entries
carry image_only_pdf and pdf_path through the cache. process_urls and
_fetch_single shared their cache-hit/record code; both now go through
_record, and process_urls_parallel folds into process_urls(workers=N).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add scripts/fetch_and_clean.py scripts/fetch_playwright.py tests/test_fetch_chain.py tests/test_fetch_and_clean.py
git commit -F .tmp/commit-task5.txt
```

---

### Task 6: Playwright dependency, Chromium check, Tesseract check

**Files:**
- Modify: `pyproject.toml:6-12`, `uv.lock` (regenerated)
- Modify: `scripts/detect_tier.py:94-100` (`check_playwright`), add `check_tesseract`, `build_tier_report` components (~line 254-268)
- Modify: `scripts/rw.py` (imports ~line 26-37; `cmd_check` ~line 164-174; `check` parser choices ~line 481)
- Test: `tests/test_detect_tier.py:97-109` (rewrite the two Playwright tests), `tests/test_rw_cli.py` (append)

- [ ] **Step 1: Write the failing tests**

In `tests/test_detect_tier.py`, replace the two tests `test_playwright_detection_when_unavailable` and `test_playwright_detection_when_available` (lines 97-109) with:
```python
def _fake_playwright(monkeypatch, exe_path):
    import types
    sync_api = types.ModuleType("playwright.sync_api")

    class _Manager:
        chromium = types.SimpleNamespace(executable_path=str(exe_path))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    sync_api.sync_playwright = lambda: _Manager()
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


def test_playwright_detection_when_unavailable(monkeypatch):
    from detect_tier import check_playwright
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    result = check_playwright()
    assert result["status"] == "missing" and "not installed" in result["reason"]


def test_playwright_detection_needs_chromium(monkeypatch, tmp_path):
    from detect_tier import check_playwright
    _fake_playwright(monkeypatch, tmp_path / "chrome.exe")
    result = check_playwright()
    assert result["status"] == "missing" and "playwright install chromium --only-shell" in result["install"]


def test_playwright_detection_when_available(monkeypatch, tmp_path):
    from detect_tier import check_playwright
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    _fake_playwright(monkeypatch, exe)
    assert check_playwright() == {"status": "ok", "executable": str(exe)}


def test_check_tesseract(monkeypatch):
    import detect_tier
    import pdf_extract
    monkeypatch.setattr(detect_tier.shutil, "which", lambda name: None)
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: False)
    result = detect_tier.check_tesseract()
    assert result == {"status": "missing", "optional": True,
                      "install": "winget install UB-Mannheim.TesseractOCR"}
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: True)
    assert detect_tier.check_tesseract() == {"status": "ok"}
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: False)
    monkeypatch.setattr(detect_tier.shutil, "which", lambda name: "C:/Program Files/Tesseract-OCR/tesseract.exe")
    assert detect_tier.check_tesseract() == {"status": "ok"}


def test_tier_report_lists_tesseract_without_changing_tier(monkeypatch):
    import detect_tier
    monkeypatch.setattr(detect_tier, "check_ollama", lambda: {"installed": False, "running": False})
    monkeypatch.setattr(detect_tier, "ensure_searxng", lambda url, root: {"available": False, "url": None})
    monkeypatch.setattr(detect_tier, "check_ytdlp", lambda: {"installed": False})
    monkeypatch.setattr(detect_tier, "check_whisper", lambda: {"installed": False})
    monkeypatch.setattr(detect_tier, "check_playwright", lambda: {"status": "ok"})
    monkeypatch.setattr(detect_tier, "check_tesseract", lambda: {"status": "missing", "optional": True})
    report = detect_tier.build_tier_report(None)
    assert report["components"]["tesseract"]["status"] == "missing"
    assert report["components"]["playwright"]["status"] == "ok"
    assert report["tier"] == "base"
    assert not any("esseract" in m for m in report["missing_for_full"])
```

Append to `tests/test_rw_cli.py` (after `test_check_dispatches_by_component`, in the infrastructure section):
```python
def test_check_playwright_and_tesseract(cli, monkeypatch):
    monkeypatch.setattr(rw, "check_playwright", lambda: {"status": "ok"})
    monkeypatch.setattr(rw, "check_tesseract", lambda: {"status": "missing"})
    assert cli("check", "playwright")[1] == {"status": "ok"}
    assert cli("check", "tesseract")[1] == {"status": "missing"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --frozen pytest tests/test_detect_tier.py tests/test_rw_cli.py -v -k "playwright or tesseract"`
Expected: FAIL: the old `check_playwright` reports `ok` for any importable package (AssertionError), `AttributeError: module 'detect_tier' has no attribute 'check_tesseract'`, and the rw test exits 2 (`invalid choice: 'playwright'`).

- [ ] **Step 3: Implement the checks**

In `scripts/detect_tier.py`, add `import pdf_extract` below `import requests`, then replace `check_playwright` (lines 94-100) with:
```python
PLAYWRIGHT_INSTALL = 'uv run --frozen --project "<plugin root>" playwright install chromium --only-shell'
TESSERACT_INSTALL = "winget install UB-Mannheim.TesseractOCR"


def check_playwright() -> dict:
    """Playwright importable AND its Chromium build downloaded."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"status": "missing", "reason": "playwright package not installed"}
    try:
        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
    except Exception as e:
        return {"status": "error", "reason": f"playwright failed to start: {e}"}
    if not exe.exists():
        return {"status": "missing", "reason": "Chromium not downloaded", "install": PLAYWRIGHT_INSTALL}
    return {"status": "ok", "executable": str(exe)}


def check_tesseract() -> dict:
    """Optional OCR for image-only PDFs. Never changes the tier."""
    if shutil.which("tesseract") is not None or pdf_extract.tesseract_available():
        return {"status": "ok"}
    return {"status": "missing", "optional": True, "install": TESSERACT_INSTALL}
```
In `build_tier_report`, add a line after `"playwright": check_playwright(),`:
```python
        "tesseract": check_tesseract(),
```

In `scripts/rw.py`, add `check_playwright,` and `check_tesseract,` to the `from detect_tier import (...)` list (alphabetical, after `check_ollama,`). In `cmd_check`, add to the `checks` dict:
```python
        "playwright": check_playwright,
        "tesseract": check_tesseract,
```
In `build_parser`, change the `check` choices to:
```python
    p.add_argument("component", choices=["ollama", "ytdlp", "whisper", "searxng", "platform",
                                         "playwright", "tesseract"])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_detect_tier.py tests/test_rw_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Add the dependency and re-lock**

In `pyproject.toml`, add `"playwright>=1.48",` to `dependencies` after `"PyYAML",`:
```toml
dependencies = [
    "requests",
    "pymupdf",
    "python-docx",
    "rich",
    "PyYAML",
    "playwright>=1.48",
]
```
Run (the one non-frozen uv call in this plan):
```bash
uv lock --project C:/Users/tim/workspace/research-workflow
```
Expected: `Resolved N packages`, with `Added playwright v1.x.y` plus its dependencies (`greenlet`, `pyee`, and possibly `typing-extensions`). No other package changes version.

Check the result:
```bash
git diff --stat uv.lock
grep -n -A3 'name = "playwright"' uv.lock
```
Expected: only additions in the diff. The wheel `upload-time` for playwright must be on or before 2026-10-13 (two weeks before today). If it is newer, change the requirement to `"playwright>=1.48,<X.Y"` where `X.Y` is the resolved minor version, and run `uv lock --project C:/Users/tim/workspace/research-workflow` again. If the age gate refuses every candidate, stop and report it.

Then go back to frozen:
```bash
uv sync --frozen --project C:/Users/tim/workspace/research-workflow
```
Expected: `Installed N packages` including playwright.

- [ ] **Step 6: (Needs the user's OK) download Chromium and run the browser test**

This downloads about 150 MB from Playwright's CDN. Run it only if the user approved the download in this session; otherwise skip this step and say so in your report (the browser test keeps skipping).
```bash
uv run --frozen --project C:/Users/tim/workspace/research-workflow playwright install chromium --only-shell
uv run --frozen pytest -m browser -v
```
Expected: `test_real_browser_reads_file_html_and_served_pdf PASSED`.

- [ ] **Step 7: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass (the browser test passes or skips).

- [ ] **Step 8: Commit**

Write `.tmp/commit-task6.txt`:
```
build: add playwright; tier checks confirm Chromium and report Tesseract

check_playwright now starts Playwright and confirms the Chromium executable
exists, not just the import, and names the install command when it is
missing. check_tesseract (optional, never changes the tier) reports whether
OCR is available, with the winget install line. rw.py check gains
playwright and tesseract. uv.lock re-resolved under the package-age gate.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add pyproject.toml uv.lock scripts/detect_tier.py scripts/rw.py tests/test_detect_tier.py tests/test_rw_cli.py
git commit -F .tmp/commit-task6.txt
```

---

### Task 7: rw add-browser-fetch and add-manual-url

**Files:**
- Modify: `scripts/fetch_and_clean.py` (append `add_browser_fetch` after `process_urls`)
- Modify: `scripts/state.py` (`create_run` ~line 584-601; add `add_manual_url` after `add_seen_urls` ~line 261)
- Modify: `scripts/rw.py` (imports; two commands after `cmd_add_seen_urls` ~line 294; parser entries after `add-seen-urls` ~line 519)
- Test: `tests/test_rw_cli.py` (append to the hop-loop section), `tests/test_state.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rw_cli.py`, after `test_add_seen_urls_unknown_topic_fails`:
```python
PDF_URL = "https://fdle.example/packet.pdf"


def _needs_browser_results(vault):
    return _write(_state(vault) / "fetch_results_hop1.json", {
        "topic": "T", "fetched": [], "failed": [],
        "needs_browser": [{"url": PDF_URL, "reason": "http_403", "attempts": []}],
        "stats": {"total_urls": 1, "fetched": 0, "failed": 0, "needs_browser": 1,
                  "cache_hits": 0, "total_words": 0},
    })


def test_add_browser_fetch_moves_url_out_of_needs_browser(active_run, cli, tmp_path):
    import fetch_and_clean
    results = _needs_browser_results(active_run)
    page = active_run / "page.md"
    page.write_text("Board package text. " * 30, encoding="utf-8")
    cache = tmp_path / "cache"
    code, out, err = cli("add-browser-fetch", "--hop", "1", "--topic", "Greenville ALPR", "--url", PDF_URL,
                         "--title", "Board package", "--content-file", page, "--cache-dir", cache)
    assert code == 0, err
    assert out == {"url": PDF_URL, "hop": "1", "word_count": 90}
    data = json.loads(results.read_text(encoding="utf-8"))
    assert [(f["url"], f["fetch_method"], f["title"]) for f in data["fetched"]] == [(PDF_URL, "browser", "Board package")]
    assert data["needs_browser"] == []
    assert data["stats"] == {"total_urls": 1, "fetched": 1, "failed": 0, "needs_browser": 0,
                             "cache_hits": 0, "total_words": 90}
    cached = fetch_and_clean.load_cache(cache, fetch_and_clean.url_cache_key(PDF_URL))
    assert cached["fetch_method"] == "browser" and cached["content"].startswith("Board package text.")
    assert PDF_URL in _run_file(active_run)["topics"][0]["seen_urls"]


def test_add_browser_fetch_needs_the_hop_file(active_run, cli, tmp_path):
    page = active_run / "page.md"
    page.write_text("x", encoding="utf-8")
    code, _, err = cli("add-browser-fetch", "--hop", "9", "--topic", "Greenville ALPR", "--url", PDF_URL,
                       "--content-file", page, "--cache-dir", tmp_path / "cache")
    assert code == 1 and "fetch_results_hop9.json not found" in err


def test_add_browser_fetch_unknown_topic_leaves_file_alone(active_run, cli, tmp_path):
    results = _needs_browser_results(active_run)
    before = results.read_text(encoding="utf-8")
    page = active_run / "page.md"
    page.write_text("x", encoding="utf-8")
    code, _, err = cli("add-browser-fetch", "--hop", "1", "--topic", "Nope", "--url", PDF_URL,
                       "--content-file", page, "--cache-dir", tmp_path / "cache")
    assert code == 1 and "Topic not found" in err
    assert results.read_text(encoding="utf-8") == before


def test_add_manual_url_dedupes(active_run, cli):
    code, out, _ = cli("add-manual-url", "--url", PDF_URL, "--reason", "captcha", "--topic", "Greenville ALPR")
    assert code == 0 and out == {"url": PDF_URL, "reason": "captcha"}
    cli("add-manual-url", "--url", PDF_URL, "--reason", "captcha")
    assert _run_file(active_run)["manual_urls"] == [
        {"url": PDF_URL, "reason": "captcha", "topic": "Greenville ALPR"}]


def test_add_manual_url_without_run_fails(vault, cli):
    code, _, err = cli("add-manual-url", "--url", PDF_URL, "--reason", "captcha")
    assert code == 1 and "No active run" in err
```

Append to `tests/test_state.py`:
```python


def test_create_run_starts_manual_and_superseded_empty(tmp_path):
    from state import create_run
    run = create_run(tmp_path, "r1", "base")
    assert run["manual_urls"] == [] and run["superseded"] == {}


def test_add_manual_url_on_a_resumed_run_without_the_field(tmp_path):
    from state import add_manual_url, create_run, load_run, save_state
    run = create_run(tmp_path, "r1", "base")
    del run["manual_urls"]
    save_state(tmp_path, run)
    add_manual_url(tmp_path, "https://a.example/x", "no_browser")
    assert load_run(tmp_path)["manual_urls"] == [
        {"url": "https://a.example/x", "reason": "no_browser", "topic": None}]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --frozen pytest tests/test_rw_cli.py tests/test_state.py -v -k "browser_fetch or manual"`
Expected: FAIL: rw exits 2 with `invalid choice: 'add-browser-fetch'` / `'add-manual-url'`; state tests fail with `KeyError: 'manual_urls'` and `ImportError: cannot import name 'add_manual_url'`.

- [ ] **Step 3: Implement the state helpers**

In `scripts/state.py` `create_run`, add two keys to the `run` dict after `"applied_patterns": [],`:
```python
        "manual_urls": [],
        "superseded": {},
```
After `add_seen_urls`, add:
```python
def add_manual_url(state_dir: Path, url: str, reason: str, topic: str | None = None) -> None:
    """List a URL for the user to read by hand (a CAPTCHA, or no browser tools).

    Stage 10b prints run["manual_urls"]. Deduplicated by URL.
    """
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    manual = run.setdefault("manual_urls", [])
    if any(m["url"] == url for m in manual):
        return
    manual.append({"url": url, "reason": reason, "topic": topic})
    save_state(state_dir, run)
```

- [ ] **Step 4: Implement `add_browser_fetch`**

Append to `scripts/fetch_and_clean.py`, after `process_urls` and before the CLI section:
```python
def add_browser_fetch(results_path: Path, url: str, title: str, content: str, cache_dir: Path) -> dict:
    """Record a page the orchestrator read in its own browser (SKILL Stage 4b).

    Caches it with fetch_method "browser", appends it to the hop's fetched list,
    drops the URL from needs_browser and failed, and refreshes stats.
    """
    data = json.loads(results_path.read_text(encoding="utf-8"))
    entry = {
        "url": url,
        "title": title,
        "content": content,
        "fetch_method": "browser",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    save_cache(cache_dir, url_cache_key(url), entry)
    record = _record(entry, cache_hit=False)
    data["fetched"] = [f for f in data.get("fetched", []) if f["url"] != url] + [record]
    for key in ("failed", "needs_browser"):
        data[key] = [f for f in data.get(key, []) if f["url"] != url]
    data["stats"] = {**data.get("stats", {}), **_stats(data["fetched"], data["failed"], data["needs_browser"])}
    tmp = results_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(results_path)
    return record
```

- [ ] **Step 5: Add the rw subcommands**

In `scripts/rw.py`, add `import fetch_and_clean` after `import config_manager`, and add `add_manual_url,` to the `from state import (...)` list (after `abandon_run,`... keep it alphabetical: after `acquire_state_lock,` comes `add_manual_url,` then `add_seen_urls,`).

After `cmd_add_seen_urls`, add:
```python
def cmd_add_browser_fetch(args):
    sd = _state_dir(args)
    results = sd / f"fetch_results_hop{args.hop}.json"
    if not results.is_file():
        raise CLIError(f"{results.name} not found; run fetch_and_clean.py for hop {args.hop} first")
    add_seen_urls(sd, topic_name=args.topic, urls=[args.url])  # validates the topic first
    content = Path(args.content_file).read_text(encoding="utf-8")
    record = fetch_and_clean.add_browser_fetch(results, args.url, args.title, content, Path(args.cache_dir))
    _out({"url": args.url, "hop": args.hop, "word_count": record["word_count"]})


def cmd_add_manual_url(args):
    add_manual_url(_state_dir(args), args.url, args.reason, args.topic)
    _out({"url": args.url, "reason": args.reason})
```
In `build_parser`, after the `add-seen-urls` entry, add:
```python
    p = add("add-browser-fetch", cmd_add_browser_fetch,
            "record a page read in the orchestrator's browser into fetch_results_hop<N>.json")
    p.add_argument("--hop", required=True)
    p.add_argument("--topic", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--content-file", required=True)
    p.add_argument("--cache-dir", default=str(fetch_and_clean.DEFAULT_CACHE_DIR))
    p = add("add-manual-url", cmd_add_manual_url, "list a URL for the user to read by hand (Stage 10b)")
    p.add_argument("--url", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--topic")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_rw_cli.py tests/test_state.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

Write `.tmp/commit-task7.txt`:
```
feat(rw): add-browser-fetch and add-manual-url for the Stage 4b browser pass

add-browser-fetch writes a page the orchestrator read in its browser into
fetch_results_hop<N>.json (fetch_method "browser"), drops it from
needs_browser/failed, refreshes stats, caches it and marks it seen for the
topic. add-manual-url lists pages behind a CAPTCHA, or read with no browser
available, in run state manual_urls for the Stage 10 summary.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add scripts/fetch_and_clean.py scripts/state.py scripts/rw.py tests/test_rw_cli.py tests/test_state.py
git commit -F .tmp/commit-task7.txt
```

---

### Task 8: Fold-ins from the Peregrine run

**Files:**
- Modify: `scripts/rw.py` (`cmd_compute_confidence` ~line 315; new `cmd_mark_superseded`; parser)
- Modify: `scripts/state.py` (`topic_passes`, `quality_gate` ~line 452-485; `aggregate_hop_summaries` ~line 499-509; new `mark_superseded`)
- Modify: `skills/researcher/SKILL.md` Stage 5a (one paragraph)
- Test: `tests/test_rw_cli.py` (update `test_aggregate_summaries`; append tests)

- [ ] **Step 1: Write the failing tests**

In `tests/test_rw_cli.py`, in `test_aggregate_summaries`, replace the last line:
```python
    assert combined == {"topic": "ALPR", "items": [{"url": "a"}, {"url": "b"}]}
```
with:
```python
    assert combined == {"topic": "ALPR", "items": [
        {"url": "a", "topic_key": None, "hop": 1}, {"url": "b", "topic_key": None, "hop": 2}]}
```

Append after `test_compute_confidence_needs_no_vault`:
```python
def test_compute_confidence_accepts_fetch_results(tmp_path, cli, monkeypatch):
    from confidence import compute_confidence
    data = {
        "search_context": {"selected_urls": [
            {"url": "a", "tier": "T1", "is_primary": True},
            {"url": "b", "tier": "T2"},
            {"url": "c", "tier": "T1", "is_primary": True},
        ]},
        "fetched": [{"url": "a", "content": "x"}, {"url": "b", "content": "y"},
                    {"url": "c", "content": "z", "usable": False}],
    }
    monkeypatch.chdir(tmp_path)
    code, out, _ = cli("compute-confidence", "--sources-file", _write(tmp_path / "fr.json", data),
                       "--depth", "quick")
    expected = compute_confidence([{"tier": "T1", "is_primary": True}, {"tier": "T2"}], depth="quick")
    assert code == 0 and out == pytest.approx(expected)
```

Append after `test_aggregate_hop_order_is_numeric`:
```python
def test_aggregate_summaries_stamps_topic_key_and_hop(active_run, cli):
    sd = _state(active_run)
    _write(sd / "summaries_hop2_alpr.json", {"items": [{"url": "u", "title": "A"},
                                                       {"url": "v", "title": "B", "topic_key": "kept"}]})
    cli("aggregate-summaries", "--project", "P")
    items = json.loads((sd / "summaries.json").read_text(encoding="utf-8"))["items"]
    assert [(i["topic_key"], i["hop"]) for i in items] == [("alpr", 2), ("kept", 2)]


def test_mark_superseded_takes_topic_out_of_the_gate(active_run, cli):
    code, out, _ = cli("mark-superseded", "--topic", "Greenville ALPR", "--reason", "merged into Flock topic")
    assert code == 0 and out == {"topic": "Greenville ALPR", "status": "superseded"}
    resp = _write(active_run / "resp.json", {"decision": "stop", "confidence_score": 0.95, "contradiction_rate": 0.1})
    hop = _write(active_run / "hop.json", {"hop": 1})
    cli("apply-hop-decision", "--topic", "Flock Safety contracts", "--response-file", resp, "--hop-data-file", hop)
    gate = cli("quality-gate")[1]
    assert gate["passed"] is True and gate["superseded"] == ["Greenville ALPR"]
    assert [t["topic"] for t in gate["topics"]] == ["Flock Safety contracts"]
    assert gate["worst_confidence"] == 0.95
    run = _run_file(active_run)
    assert run["topics"][0]["status"] == "superseded"
    assert run["superseded"] == {"Greenville ALPR": "merged into Flock topic"}


def test_mark_superseded_unknown_topic_fails(active_run, cli):
    code, _, err = cli("mark-superseded", "--topic", "Nope", "--reason", "x")
    assert code == 1 and "Topic not found" in err


def test_topic_passes_skips_superseded():
    from state import topic_passes
    assert topic_passes({"depth": "quick", "confidence_history": [], "contradiction_rate": 0.9,
                         "status": "superseded"})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --frozen pytest tests/test_rw_cli.py -v -k "aggregate_summaries or fetch_results or superseded"`
Expected: FAIL: the aggregate tests miss `topic_key`/`hop`; compute-confidence raises `AttributeError: 'str' object has no attribute 'get'` (it iterates the dict's keys); rw exits 2 for `mark-superseded`; `topic_passes` returns False.

- [ ] **Step 3: Implement the state changes**

In `scripts/state.py`, replace `topic_passes` and `quality_gate` with:
```python
def topic_passes(topic: dict) -> bool:
    """Stage 5a: latest confidence >= the depth's confidence_target AND
    contradiction_rate <= 0.3. A topic with no confidence yet scores 0.0.
    A superseded topic is out of the gate and always passes."""
    if topic.get("status") == "superseded":
        return True
    target = get_depth_profile(topic["depth"])["confidence_target"]
    return (_latest_confidence(topic) >= target
            and topic["contradiction_rate"] <= QUALITY_MAX_CONTRADICTION)


def quality_gate(run: dict) -> dict:
    """Stage 5a per-topic pass/fail for the run, ignoring superseded topics.

    worst_confidence (the lowest latest confidence, 0.0 if none) is the
    OVERALL_CONFIDENCE surfaced in Stage 5c prompts and 5d's
    final_confidence_score -- depths are per-topic, so there is no single
    run target.
    """
    live = [t for t in run["topics"] if t.get("status") != "superseded"]
    topics = [
        {
            "topic": t["topic"],
            "depth": t["depth"],
            "confidence": _latest_confidence(t),
            "contradiction_rate": t["contradiction_rate"],
            "replan_hint": t.get("replan_hint"),
            "passes": topic_passes(t),
        }
        for t in live
    ]
    return {
        "passed": all(t["passes"] for t in topics),
        "failing_topics": [t for t in topics if not t["passes"]],
        "topics": topics,
        "superseded": [t["topic"] for t in run["topics"] if t.get("status") == "superseded"],
        "worst_confidence": min((t["confidence"] for t in topics), default=0.0),
        "replan_count": run["replan_count"],
    }


def mark_superseded(state_dir: Path, topic_name: str, reason: str) -> None:
    """Retire a topic another one replaced; the quality gate then skips it."""
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    for t in run["topics"]:
        if t["topic"] == topic_name:
            t["status"] = "superseded"
            break
    else:
        raise KeyError(f"Topic not found: {topic_name}")
    run.setdefault("superseded", {})[topic_name] = reason
    save_state(state_dir, run)
```
Replace `aggregate_hop_summaries` with:
```python
def aggregate_hop_summaries(state_dir: Path, project: str) -> dict:
    """Stage 6a: merge every summaries_hop*.json's items into summaries.json.

    Each item gets topic_key and hop from its filename (summaries_hop<N>[_<key>])
    unless it already carries them.
    """
    seen: dict = {}  # insertion order = first-seen; later hops overwrite the value
    for hop_file in _hop_files(state_dir, "summaries"):
        m = _HOP_RE.search(hop_file.stem)
        key = (m.group(3) or "") if m else ""
        hop = int(m.group(1)) if m else None
        for item in json.loads(hop_file.read_text(encoding="utf-8")).get("items", []):
            item.setdefault("topic_key", key or None)
            item.setdefault("hop", hop)
            seen[(key, item.get("url"), item.get("title"))] = item
    combined = {"topic": project, "items": list(seen.values())}
    save_stage_output(state_dir, "summaries", combined)
    return combined
```

- [ ] **Step 4: Implement the rw changes**

In `scripts/rw.py`, add `mark_superseded,` to the `from state import (...)` list (after `load_stage_output,`). Replace `cmd_compute_confidence` with:
```python
def _confidence_sources(data) -> list[dict]:
    """A bare sources list, or a fetch_results file ({"fetched": [...]}).

    From fetch_results, use the fetched entries not marked usable: false, with
    tier / is_primary / credibility_score copied from the matching
    search_context.selected_urls entry (fetched entries don't carry them).
    """
    if isinstance(data, list):
        return data
    selected = {
        s["url"]: s for s in data.get("search_context", {}).get("selected_urls", [])
        if isinstance(s, dict) and "url" in s
    }
    sources = []
    for entry in data.get("fetched", []):
        if entry.get("usable") is False:
            continue
        signals = {k: v for k, v in selected.get(entry["url"], {}).items()
                   if k in ("tier", "is_primary", "credibility_score")}
        sources.append({**signals, **entry})
    return sources


def cmd_compute_confidence(args):
    _out(compute_confidence(_confidence_sources(_read_json(args.sources_file)), depth=args.depth))
```
After `cmd_quality_gate`, add:
```python
def cmd_mark_superseded(args):
    mark_superseded(_state_dir(args), args.topic, args.reason)
    _out({"topic": args.topic, "status": "superseded"})
```
In `build_parser`, change the `compute-confidence` help text to `"confidence score for a sources list or fetch_results JSON file"`, and after `add("quality-gate", ...)` add:
```python
    p = add("mark-superseded", cmd_mark_superseded, "retire a topic; the quality gate skips it")
    p.add_argument("--topic", required=True)
    p.add_argument("--reason", required=True)
```

- [ ] **Step 5: Document mark-superseded in Stage 5a**

In `skills/researcher/SKILL.md`, replace:
```
If `failing_topics` is empty: proceed to Stage 6 (classify).
```
with:
````
If `failing_topics` is empty: proceed to Stage 6 (classify).

A topic another one replaced (for example, merged into a sibling after a replan) can be retired instead of failing the gate:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" mark-superseded --topic "TOPIC" --reason "REASON"
```

`quality-gate` then leaves it out of `topics` and `failing_topics` and lists it under `superseded`.
````

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_rw_cli.py tests/test_skill_contract.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

Write `.tmp/commit-task8.txt`:
```
fix: Peregrine fold-ins (confidence input, summary stamps, superseded topics)

compute-confidence accepts a fetch_results file as well as a sources list,
using fetched entries not marked usable: false with credibility signals
joined from search_context.selected_urls. aggregate-summaries stamps
topic_key and hop from each filename. The quality gate skips superseded
topics; rw.py mark-superseded sets the status and records the reason in
run["superseded"].

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add scripts/rw.py scripts/state.py skills/researcher/SKILL.md tests/test_rw_cli.py
git commit -F .tmp/commit-task8.txt
```

---

### Task 9: summarize.py hands image-only PDFs to a visual read

**Files:**
- Modify: `scripts/summarize.py` (`summarize_batch` ~line 221-249, `prepare_for_claude_code` ~line 256-295, `main` stats ~line 334-343)
- Test: `tests/test_summarize.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_summarize.py`:
```python


IMAGE_PDF = {"url": "https://a.gov/minutes.pdf", "title": "Minutes", "content": "",
             "image_only_pdf": True, "pdf_path": "C:/cache/pdf/ab.pdf"}


def test_summarize_batch_flags_image_only_pdf_without_ollama():
    from summarize import summarize_batch
    with patch("summarize.requests.post") as post:
        summaries = summarize_batch({"fetched": [IMAGE_PDF]}, model="m")
    post.assert_not_called()
    assert summaries == [{"url": "https://a.gov/minutes.pdf", "title": "Minutes", "usable": False,
                          "reason": "needs visual read", "image_only_pdf": True,
                          "pdf_path": "C:/cache/pdf/ab.pdf"}]


def test_cli_stats_count_visual_reads(tmp_path):
    import sys
    import summarize as sm
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps({"fetched": [
        {"url": "https://a.com", "title": "A", "content": "Content A"}, IMAGE_PDF]}), encoding="utf-8")
    out = tmp_path / "out.json"
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"response": json.dumps({
        "summary": "S", "source_type": "journalism", "key_entities": [], "key_claims": []})}
    with patch.object(sm, "requests") as mock_req:
        mock_req.post.return_value = mock_response
        argv_backup = sys.argv
        sys.argv = ["summarize.py", "--input", str(inp), "--output", str(out), "--model", "m"]
        try:
            sm.main()
        finally:
            sys.argv = argv_backup
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["stats"] == {"total": 2, "summarized": 1, "needs_visual_read": 1, "failed": 0}


def test_prepare_for_claude_marks_image_only_pdf(tmp_path):
    from summarize import prepare_for_claude_code
    output = prepare_for_claude_code({"fetched": [IMAGE_PDF]}, tmp_path)
    text = (tmp_path / output[0]["file"]).read_text(encoding="utf-8")
    assert "Image-only PDF: read the page images at C:/cache/pdf/ab.pdf" in text
    assert output[0]["image_only_pdf"] is True and output[0]["pdf_path"] == "C:/cache/pdf/ab.pdf"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --frozen pytest tests/test_summarize.py -v -k "image_only or visual"`
Expected: FAIL: `summarize_batch` still sends the image-only item to Ollama (the first test errors or asserts), stats lack `needs_visual_read`, and the prepared file has no `Image-only PDF` line.

- [ ] **Step 3: Implement**

In `scripts/summarize.py`, replace `summarize_batch` with:
```python
def summarize_batch(
    fetch_results: dict,
    model: str,
    ollama_url: str = "http://localhost:11434",
) -> list[dict]:
    """Summarize all fetched articles via Ollama.

    Iterates over fetch_results["fetched"], calls summarize_article for each,
    and returns a list of summary dicts with url, title, and summary fields merged.
    Failures (None returns) are filtered out. Image-only PDFs (image_only_pdf)
    have no text for Ollama: they come back usable: false with reason
    "needs visual read" and their pdf_path, for a Claude summarizer to Read.
    """
    summaries: list[dict] = []

    for item in fetch_results.get("fetched", []):
        if item.get("image_only_pdf"):
            summaries.append({
                "url": item["url"],
                "title": item["title"],
                "usable": False,
                "reason": "needs visual read",
                "image_only_pdf": True,
                "pdf_path": item.get("pdf_path"),
            })
            continue
        result = summarize_article(
            content=item["content"],
            title=item["title"],
            url=item["url"],
            model=model,
            ollama_url=ollama_url,
        )
        if result is not None:
            summaries.append({
                "url": item["url"],
                "title": item["title"],
                **result,
            })

    return summaries
```
In `prepare_for_claude_code`, replace the loop body from `truncated_content = ...` through `entries.append({...})` with:
```python
        truncated_content = item["content"][:MAX_CONTENT_CHARS_CLAUDE]
        visual = (
            f"Image-only PDF: read the page images at {item.get('pdf_path')}\n\n"
            if item.get("image_only_pdf") else ""
        )

        file_content = (
            f"# {item['title']}\n\n"
            f"Source: {item['url']}\n\n"
            f"{visual}"
            f"---\n\n"
            f"{truncated_content}\n"
        )

        (output_dir / filename).write_text(file_content, encoding="utf-8")

        entry = {"url": item["url"], "title": item["title"], "file": filename}
        if item.get("image_only_pdf"):
            entry["image_only_pdf"] = True
            entry["pdf_path"] = item.get("pdf_path")
        entries.append(entry)
```
In `main`, replace the Ollama-mode `result = {...}` block with:
```python
        total = len(fetch_results.get("fetched", []))
        usable = [s for s in summaries if s.get("usable", True)]
        result = {
            "mode": "ollama",
            "model": args.model,
            "items": summaries,
            "stats": {
                "total": total,
                "summarized": len(usable),
                "needs_visual_read": len(summaries) - len(usable),
                "failed": total - len(summaries),
            },
        }
```
(Keep the comment above it about emitting `items`.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --frozen pytest tests/test_summarize.py -v`
Expected: PASS (old and new tests).

- [ ] **Step 5: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

Write `.tmp/commit-task9.txt`:
```
feat(summarize): image-only PDFs go to a visual read instead of Ollama

On the Ollama path an image_only_pdf item comes back usable: false with
reason "needs visual read" and its pdf_path; stats count them separately.
Base-tier prepared files name the PDF path so the Haiku summarizer reads
the page images.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add scripts/summarize.py tests/test_summarize.py
git commit -F .tmp/commit-task9.txt
```

---

### Task 10: Skill and agent instructions, with contract tests

**Files:**
- Modify: `skills/researcher/SKILL.md` (Stage 4b ~line 475, Stage 4d ~lines 499-509, Stage 10b ~line 1116)
- Modify: `skills/researcher-setup/SKILL.md` (Step 0 ~line 32, after Step 7 ~line 292, Step 12 ~line 484)
- Modify: `agents/fetch-summarize-runner.md` (steps 2-4 and Output)
- Test: `tests/test_skill_contract.py` (append)

- [ ] **Step 1: Write the failing contract tests**

Append to `tests/test_skill_contract.py`:
```python


_SETUP_MD = _REPO / "skills" / "researcher-setup" / "SKILL.md"
_RUNNER_MD = _REPO / "agents" / "fetch-summarize-runner.md"


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def test_stage_4b_browser_pass_routes_needs_browser():
    s = _section(_SKILL_MD.read_text(encoding="utf-8"), "### 4b. Fetch", "### 4c. Media")
    for needle in (
        "needs_browser", "add-browser-fetch", "add-manual-url", "CAPTCHA",
        "mcp__Claude_Browser__navigate", "mcp__Claude_Browser__get_page_text",
        "mcp__Claude_Browser__javascript_tool",
        "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.0.379/pdf.min.mjs",
    ):
        assert needle in s, needle


def test_stage_4d_and_runner_read_image_only_pdfs():
    s = _section(_SKILL_MD.read_text(encoding="utf-8"), "### 4d. Summarize", "### 4e.")
    assert "image_only_pdf" in s and "pdf_path" in s and "needs visual read" in s
    runner = _RUNNER_MD.read_text(encoding="utf-8")
    assert "image_only_pdf" in runner and "pdf_path" in runner and "needs_browser" in runner


def test_stage_10b_lists_manual_urls():
    s = _section(_SKILL_MD.read_text(encoding="utf-8"), "### 10b. Print summary", "### 10c.")
    assert "manual_urls" in s


def test_setup_installs_chromium_and_checks_tesseract():
    setup = _SETUP_MD.read_text(encoding="utf-8")
    assert ('PLAYWRIGHT_BROWSERS_PATH="REPO/.cache/ms-playwright" '
            'uv run --frozen --project "REPO" playwright install chromium --only-shell') in setup
    assert 'rw.py" check playwright' in setup and 'rw.py" check tesseract' in setup
    assert "winget install UB-Mannheim.TesseractOCR" in setup
```

**Scanner change (part of this task):** the existing single-line `uv run` scanner in `tests/test_skill_contract.py` must accept a leading `PLAYWRIGHT_BROWSERS_PATH=...` env prefix on a command line (for example `PLAYWRIGHT_BROWSERS_PATH="REPO/.cache/ms-playwright" uv run --frozen --project "REPO" playwright install chromium --only-shell`). Update its regex and add a test that such a line is accepted while other leading text is still rejected. Browsers are installed repo-locally because `%LOCALAPPDATA%` is virtualized under the Claude desktop (MSIX) app.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --frozen pytest tests/test_skill_contract.py -v -k "4b or 4d or 10b or chromium"`
Expected: FAIL with `AssertionError: needs_browser` (and similar for the others).

- [ ] **Step 3: Add the Stage 4b browser pass**

In `skills/researcher/SKILL.md`, replace:
```
Update each topic's `seen_urls` with the URLs that were successfully fetched
```
with:
````
**Browser pass.** `fetch_results_hop{N}.json` has a `needs_browser` list: `{url, reason, attempts}` entries the scripted fetchers could not get past (bot walls, 403s on government PDFs). If it is empty, skip to the `seen_urls` step below. Otherwise read each entry yourself, before moving on:

- **Browser pane tools available** (`mcp__Claude_Browser__navigate`, `mcp__Claude_Browser__get_page_text`, `mcp__Claude_Browser__javascript_tool`):
  - **HTML page:** `mcp__Claude_Browser__navigate` to the URL, then `mcp__Claude_Browser__get_page_text`. A passive "checking your browser" page may clear by itself; wait a few seconds and read again.
  - **PDF** (the URL ends in `.pdf`, or the page shows a PDF viewer): navigate to a page on the PDF's own site, then run the pdf.js script below with `mcp__Claude_Browser__javascript_tool`, with `PDF_URL` replaced. It fetches the file in-page with the browser's cookies and returns the text of every page.
  - If the page still shows a CAPTCHA or an interactive "verify you are human" check, do not click it or try to solve it. Record the URL as manual (below) and move on.
  - Otherwise write the text you read to `STATE_DIR/browser_hop{N}_{index}.md` with the Write tool and record it (TOPIC is the topic whose selected URLs held this URL):

    ```bash
    uv run --frozen --project "REPO" "REPO/scripts/rw.py" add-browser-fetch --vault "VAULT" --hop N --topic "TOPIC" --url "URL" --title "TITLE" --content-file "STATE_DIR/browser_hop{N}_{index}.md"
    ```

    It appends the page to `fetch_results_hop{N}.json` (`fetch_method: "browser"`), removes it from `needs_browser`, caches it and adds it to the topic's `seen_urls`.
- **No browser tools** (plain CLI): record every `needs_browser` entry as manual.

Recording a manual URL (REASON is the entry's `reason`, or `captcha`):

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" add-manual-url --vault "VAULT" --url "URL" --reason "REASON" --topic "TOPIC"
```

Stage 10b lists them for the user. Never change the browser's user agent, use stealth tricks, or use a CAPTCHA-solving service.

The pdf.js script for `mcp__Claude_Browser__javascript_tool`:

```js
const pdfjs = await import('https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.0.379/pdf.min.mjs');
pdfjs.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.0.379/pdf.worker.min.mjs';
const buf = await (await fetch('PDF_URL', { credentials: 'include' })).arrayBuffer();
const doc = await pdfjs.getDocument({ data: buf }).promise;
const pages = [];
for (let i = 1; i <= doc.numPages; i++) {
  const content = await (await doc.getPage(i)).getTextContent();
  pages.push(content.items.map((item) => item.str).join(' '));
}
pages.join('\n\n');
```

If the script returns little or no text, the PDF is a scan: navigate to the PDF itself and read it from the screen, or record it as manual.

Update each topic's `seen_urls` with the URLs that were successfully fetched
````

- [ ] **Step 4: Add the Stage 4d visual-read rules**

In `skills/researcher/SKILL.md`, replace:
```
**If Ollama is NOT available (base tier):**
```
with:
```
Items that come back `usable: false` with reason `needs visual read` are image-only PDFs (the fetch entry has `image_only_pdf: true` and a `pdf_path`). For each, dispatch a Haiku subagent with the base-tier prompt below plus the image-only rule, and replace the item in `summaries_hop{N}.json` with its summary.

**If Ollama is NOT available (base tier):**
```
Then replace:
```
Then dispatch a Haiku subagent per article file (same prompt as v2 base-tier path), collect
```
with:
```
Then dispatch a Haiku subagent per article file (same prompt as v2 base-tier path, plus this image-only rule: when the file has an `Image-only PDF:` line, i.e. the fetch entry has `image_only_pdf: true`, Read the PDF at its `pdf_path` with the Read tool, up to 20 pages per call, and summarize from the page images), collect
```

- [ ] **Step 5: Add the manual URL list to Stage 10b**

In `skills/researcher/SKILL.md`, replace:
```
{if threads approved:}
```
with:
```
{if final.manual_urls:}
Needs a manual read ({count}): pages behind a CAPTCHA, or read with no browser available.
{for each in final.manual_urls:}
  - {url} ({reason})
{end}
{end}

{if threads approved:}
```

- [ ] **Step 6: Update the setup wizard**

In `skills/researcher-setup/SKILL.md`, replace:
```
This creates `REPO/.venv` from `REPO/uv.lock` (downloading Python 3.12 if needed). If it fails, report the error and attempt to continue — the Python environment may need troubleshooting.
```
with:
````
This creates `REPO/.venv` from `REPO/uv.lock` (downloading Python 3.12 if needed). If it fails, report the error and attempt to continue — the Python environment may need troubleshooting.

Then check the local browser fallback, which reads pages and PDFs that block scripted fetchers:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check playwright
```
If `status` is not `ok`, use AskUserQuestion: "Download Chromium for the local browser fallback (about 150 MB)? [yes / skip]". On yes:
```bash
PLAYWRIGHT_BROWSERS_PATH="REPO/.cache/ms-playwright" uv run --frozen --project "REPO" playwright install chromium --only-shell
```
Then run the check again and report the result. On skip, note that pages behind bot walls will go to the orchestrator's browser or the manual-read list instead.
````
After the line `Set \`whisper_available = False\`. Do not offer to auto-install — it's heavy.`, add:
````


---

## Step 7b: Tesseract (optional OCR)

Check availability. Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check tesseract
```

**If `status` is `ok`:** Report `Tesseract is available: scanned PDFs are OCR'd locally.`

**If missing:** Report:
```
Tesseract is not installed. It OCRs scanned (image-only) PDFs. Without it,
those PDFs are saved and Claude reads the page images instead, which costs
more tokens.

To install later:
  Windows:  winget install UB-Mannheim.TesseractOCR
  macOS:    brew install tesseract
  Linux:    sudo apt install tesseract-ocr
```
Do not install it automatically. It does not change the tier.
````
In Step 12's summary block, replace:
```
  - Whisper: {available/not available}
```
with:
```
  - Whisper: {available/not available}
  - Chromium (local browser fallback): {ok/missing}
  - Tesseract (PDF OCR, optional): {ok/missing}
```

- [ ] **Step 7: Update the fetch-summarize runner**

In `agents/fetch-summarize-runner.md`, replace:
```
Read `W/fetch_results.json`. It has `{ "fetched": [{url, title, content, ...}], "failed": [...] }`.
If `fetched` is empty, return `{ "items": [] }` and stop.
```
with:
```
Read `W/fetch_results.json`. It has `{ "fetched": [{url, title, content, ...}], "failed": [...], "needs_browser": [{url, reason, attempts}] }`.
You have no browser: copy every `needs_browser` entry into your output's `manual_urls` as `{url, reason}`.
If `fetched` is empty, return `{ "items": [], "manual_urls": [...] }` and stop.
```
Replace:
```
  Read `W/summaries.json`; its `items` already carry `summary`, `source_type`,
  `key_entities`, `key_claims`.
```
with:
```
  Read `W/summaries.json`; its `items` already carry `summary`, `source_type`,
  `key_entities`, `key_claims`. Items with `usable: false` and reason
  `needs visual read` are image-only PDFs: summarize each yourself with the
  image-only rule below and replace the item.
```
Replace:
```
  factual summary, the source type, the named entities, and the notable claims.
```
with:
```
  factual summary, the source type, the named entities, and the notable claims.

- **Image-only rule (both paths):** when a fetched entry has `image_only_pdf: true`
  (its prepared file has an `Image-only PDF:` line), Read the PDF at its `pdf_path`
  with the Read tool, up to 20 pages per call, and summarize from the page images.
```
In the Output example, the object currently ends with the `items` array closing on a line `  ]` followed by `}`. Replace those two lines with:
```
  ],
  "manual_urls": []
}
```
and add to **Field notes**:
```
- `manual_urls` lists `{url, reason}` for every `needs_browser` entry; `[]` when none.
```

- [ ] **Step 8: Run the contract tests**

Run: `uv run --frozen pytest tests/test_skill_contract.py -v`
Expected: PASS, including `test_every_rw_subcommand_referenced_exists` (it now sees `add-browser-fetch`, `add-manual-url`, `mark-superseded`, `check`), `test_uv_calls_are_frozen` and `test_no_inline_or_bare_python`.

- [ ] **Step 9: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass.

- [ ] **Step 10: Commit**

Write `.tmp/commit-task10.txt`:
```
docs(skill): Stage 4b browser pass, visual PDF reads, manual URL list

Stage 4b reads needs_browser entries with the browser-pane tools (pages
via navigate + page text, PDFs via in-page fetch + pdf.js) and records them
with rw.py add-browser-fetch; CAPTCHA pages and no-browser sessions go to
rw.py add-manual-url, which Stage 10b lists. Stage 4d and the
fetch-summarize runner read image_only_pdf files from pdf_path. Setup
offers the Chromium download and reports optional Tesseract. Contract
tests pin each of these.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add skills/researcher/SKILL.md skills/researcher-setup/SKILL.md agents/fetch-summarize-runner.md tests/test_skill_contract.py
git commit -F .tmp/commit-task10.txt
```

---

### Task 11: README setup notes

**Files:**
- Modify: `README.md` (after the tier table, ~line 97; "For developers" optional line ~line 122)

README is reader-facing: invoke the `copydesk:write` skill before editing it and run the text below through it (keep the commands exactly as written).

- [ ] **Step 1: Add the section**

In `README.md`, replace:
```
`/researcher-setup` checks what you have and can start the optional pieces for you.
```
with:
````
`/researcher-setup` checks what you have and can start the optional pieces for you.

### Pages that block scripts, and scanned PDFs

Some sites put up a bot check or refuse scripted downloads. Researcher then tries a local Chromium, then the Wayback Machine (gently, one request at a time). Setup offers to download Chromium (about 150 MB); you can also do it yourself:

```
PLAYWRIGHT_BROWSERS_PATH=<plugin root>/.cache/ms-playwright uv run --frozen --project <plugin root> playwright install chromium --only-shell
```

Pages that still won't open are read in Claude Code's browser pane when it's available. Pages behind a CAPTCHA are never solved for you: they're listed at the end of the run so you can open them yourself.

Scanned PDFs need [Tesseract](https://github.com/tesseract-ocr/tesseract) for OCR. It's optional; without it, Claude reads the page images instead, which uses more tokens.

```
winget install UB-Mannheim.TesseractOCR     # Windows
brew install tesseract                      # macOS
sudo apt install tesseract-ocr              # Debian/Ubuntu
```
````
Replace:
```
**Optional (full tier):** Docker (for SearXNG), Ollama, Playwright, `yt-dlp`, `openai-whisper`.
```
with:
```
**Optional (full tier):** Docker (for SearXNG), Ollama, Playwright's Chromium (`PLAYWRIGHT_BROWSERS_PATH=<plugin root>/.cache/ms-playwright uv run --frozen --project <plugin root> playwright install chromium --only-shell`), Tesseract, `yt-dlp`, `openai-whisper`. The real-browser test (`uv run --frozen pytest -m browser`) skips until Chromium is installed.
```

- [ ] **Step 2: Run the full suite**

Run: `uv run --frozen pytest -q`
Expected: all pass.

- [ ] **Step 3: Commit**

Write `.tmp/commit-task11.txt`:
```
docs(readme): Chromium and Tesseract setup for the fetch fallbacks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
Run:
```bash
git add README.md
git commit -F .tmp/commit-task11.txt
```

---

## After the tasks (orchestrator, not a task)

- Rewrite `MANIFEST.md` from scratch to its budget before the PR merges (repo convention; `tests/test_manifest_budget.py` guards it). New lines: `scripts/block_detect.py`, `scripts/pdf_extract.py`, `scripts/wayback.py`, `scripts/url_safety.py`, the rewritten `scripts/fetch_playwright.py` (BrowserSession) and `scripts/fetch_and_clean.py` (two-phase chain, needs_browser); `text_utils.py` now holds `html_to_text`; Stack gains Playwright and optional Tesseract; `rw.py` subcommand count changes (adds `add-browser-fetch`, `add-manual-url`, `mark-superseded`).
- Follow-up issue (label `design-input-needed`): the batch workflow (`research-batch.js`) does not yet surface the runner's `manual_urls` in its completion summary.
