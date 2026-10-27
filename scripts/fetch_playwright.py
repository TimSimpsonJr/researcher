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
