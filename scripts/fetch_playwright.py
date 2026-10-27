"""fetch_playwright.py — Local Chromium fallback for pages scripted clients can't read.

BrowserSession runs one Chromium per fetch run with a persistent profile under
.cache/browser-profile/, so cookies a site sets (including a cleared Cloudflare
check) carry across URLs and across runs; the profile is safe to delete. If it
is locked, a throwaway temp profile is used instead. HTML pages wait up to
CHALLENGE_WAIT_S while a passive challenge clears, then return readable text. PDFs (by URL, response
content type, or a download navigation) are fetched with the session's request
context, which shares its cookies, and go to pdf_extract. An interactive
CAPTCHA is never touched: it comes back as blocked("captcha"). No fingerprint
patches and no user-agent changes.

Uses Playwright's sync API: one session, one thread.
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from block_detect import FetchResult, Verdict, classify
from pdf_extract import looks_like_pdf, to_result
from text_utils import html_to_text
from url_safety import UnsafeURLError, validate_url

PROFILE_DIR = Path(__file__).parent.parent / ".cache" / "browser-profile"
# Browsers live in the repo cache, not %LOCALAPPDATA%: under the Claude desktop (MSIX) app that
# path is virtualized and full Chromium cannot be spawned from it.
BROWSERS_PATH = Path(__file__).parent.parent / ".cache" / "ms-playwright"


def use_repo_browsers() -> None:
    """Point Playwright at the repo-local browser cache unless the caller already chose one."""
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS_PATH))


NAV_TIMEOUT_MS = 30_000
CHALLENGE_WAIT_S = 15.0
POLL_MS = 1000
MAX_REDIRECTS = 5
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_DOWNLOAD_ERRORS = ("Download is starting", "net::ERR_ABORTED")


def _dispose(response) -> None:
    try:
        response.dispose()
    except Exception:
        pass


class BrowserSession:
    """Context manager; check .available before calling fetch()."""

    def __init__(self, profile_dir: Path = PROFILE_DIR, headless: bool = True):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.available = False
        self._playwright = None
        self._context = None
        self._tmp_profile = None
        self._host_ok: dict[tuple[str, str], bool] = {}
        self._last_nav_url: dict[str, str] = {}

    def __enter__(self):
        use_repo_browsers()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("[fetch_playwright] playwright not installed; browser fallback off", file=sys.stderr)
            return self
        try:
            self._playwright = sync_playwright().start()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._context = self._launch(str(self.profile_dir))
            except Exception as exc:  # most often: profile locked by another run
                print(f"[fetch_playwright] profile launch failed ({exc}); retrying in a temp profile",
                      file=sys.stderr)
                self._tmp_profile = tempfile.mkdtemp(prefix="browser-profile-")
                self._context = self._launch(self._tmp_profile)
            # Every request, including redirects and subresources, passes the SSRF guard.
            self._context.route("**/*", self._guard)
            self.available = True
        except Exception as exc:  # e.g. Chromium not downloaded yet
            print(f"[fetch_playwright] browser unavailable: {exc}", file=sys.stderr)
            self.close()
        return self

    def _launch(self, profile: str):
        # Service workers would bypass the routing guard.
        return self._playwright.chromium.launch_persistent_context(
            profile, headless=self.headless, service_workers="block"
        )

    def _allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return True  # data:, blob:, about: never reach the network
        key = (parsed.scheme, parsed.hostname or "")
        if key not in self._host_ok:
            try:
                validate_url(url)
                self._host_ok[key] = True
            except UnsafeURLError:
                self._host_ok[key] = False
        return self._host_ok[key]

    def _guard(self, route) -> None:
        url = route.request.url
        if self._allowed(url):
            if route.request.is_navigation_request() and urlparse(url).scheme in ("http", "https"):
                self._guard_navigation(route, url)
            else:
                route.continue_()
        else:
            print(f"[fetch_playwright] blocked internal request: {url}", file=sys.stderr)
            route.abort("blockedbyclient")

    def _guard_navigation(self, route, url: str) -> None:
        """Walk the whole redirect chain here: Chromium does not route hops after a fulfilled
        3xx, so every Location is checked before it is requested. A main-frame chain that
        redirected is aborted with the final URL recorded in _last_nav_url, and _fetch
        navigates to that URL so the page runs under its real origin; anything else is
        fulfilled with the final response."""
        request = route.request
        method, post = request.method, request.post_data_buffer
        deadline = time.monotonic() + NAV_TIMEOUT_MS / 1000
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            remaining_ms = (deadline - time.monotonic()) * 1000
            if remaining_ms <= 0:
                route.abort("failed")
                return
            try:
                response = route.fetch(url=current, method=method, post_data=post,
                                       max_redirects=0, timeout=max(1000, int(remaining_ms)))
            except Exception as exc:
                print(f"[fetch_playwright] navigation fetch failed for {current}: {exc}", file=sys.stderr)
                route.abort("failed")
                return
            location = response.headers.get("location")
            if response.status in _REDIRECT_CODES and location:
                status = response.status
                _dispose(response)
                target = urljoin(current, location)
                if not self._allowed(target):
                    print(f"[fetch_playwright] blocked internal request: {target}", file=sys.stderr)
                    route.abort("blockedbyclient")
                    return
                if status == 303 or (status in (301, 302) and method == "POST"):
                    method, post = "GET", None
                current = target
                continue
            if current != url and request.frame.parent_frame is None:
                self._last_nav_url[url] = current
                _dispose(response)
                route.abort("aborted")
            else:
                route.fulfill(response=response)
                _dispose(response)
            return
        route.abort("failed")  # redirect loop

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
        if self._tmp_profile:
            shutil.rmtree(self._tmp_profile, ignore_errors=True)
            self._tmp_profile = None

    def fetch(self, url: str, cache_dir: Path) -> FetchResult:
        """Raises UnsafeURLError, timeouts and network errors; callers wrap with _try."""
        try:
            return self._fetch(url, cache_dir)
        except Exception as exc:
            if "Target" in str(exc) and "closed" in str(exc):
                self.available = False  # browser crashed
            raise

    def _fetch(self, url: str, cache_dir: Path) -> FetchResult:
        validate_url(url)
        if looks_like_pdf(url, ""):
            return self._fetch_pdf(url, cache_dir)
        page = self._context.new_page()
        target = url
        try:
            while True:
                try:
                    response = page.goto(target, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                    break
                except Exception as exc:
                    # The guard aborts a redirected chain and records where it ended: go there
                    # once so the page runs under its real URL.
                    final = self._last_nav_url.pop(target, None)
                    if final and target == url:
                        target = final
                        continue
                    if final:
                        raise RuntimeError(f"{url} redirected again at {target}") from exc
                    if "ERR_BLOCKED_BY_CLIENT" in str(exc):
                        raise UnsafeURLError(f"{url} redirected to an internal address") from exc
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
            if page.url != target and not self._allowed(page.url):
                raise UnsafeURLError(f"{url} ended on internal address {page.url}")
            title = page.title() or title
            if not verdict.ok:
                return FetchResult(verdict, method="playwright", content_type=ctype)
            return FetchResult(verdict, text, title, "playwright", ctype)
        finally:
            page.close()
            self._last_nav_url.pop(url, None)

    def _get(self, url: str):
        """GET through the session, validating every redirect hop."""
        for _ in range(MAX_REDIRECTS + 1):
            validate_url(url)
            response = self._context.request.get(url, timeout=NAV_TIMEOUT_MS, max_redirects=0)
            location = response.headers.get("location")
            if response.status not in _REDIRECT_CODES or not location:
                return response
            url = urljoin(url, location)
        raise RuntimeError(f"too many redirects: {url}")

    def _fetch_pdf(self, url: str, cache_dir: Path) -> FetchResult:
        response = self._get(url)
        ctype = response.headers.get("content-type", "")
        if not response.ok:
            return FetchResult(classify(response.status, ctype, ""), method="playwright", content_type=ctype)
        body = response.body()
        if not looks_like_pdf("", ctype, body[:5]):
            return FetchResult(Verdict("thin", "not_a_pdf"), method="playwright", content_type=ctype)
        return to_result(body, cache_dir)
