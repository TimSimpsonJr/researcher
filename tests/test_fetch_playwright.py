"""Tests for fetch_playwright.BrowserSession.

Unit tests use a fake page/context. The real-browser test is marked `browser`
and skips when Playwright or its Chromium build is missing."""

import functools
import http.server
import os
import sys
import threading
import types
from urllib.parse import urlparse
from pathlib import Path

import pytest

import fetch_playwright
from block_detect import FetchResult, Verdict
from url_safety import UnsafeURLError

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
    def __init__(self, htmls, response=None, goto_error=None, final_url=None):
        self.htmls = list(htmls)
        self.response = response or FakeResponse()
        self.goto_error = goto_error
        self.final_url = final_url
        self.url = ""
        self.waits = 0
        self.visited = []
        self.closed = False

    def goto(self, url, timeout, wait_until):
        self.visited.append(url)
        self.url = self.final_url or url
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

    def get(self, url, timeout, max_redirects=20):
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


def _reject(monkeypatch, *needles):
    def check(url):
        if any(n in url for n in needles):
            raise UnsafeURLError(f"Blocked: {url}")
    monkeypatch.setattr(fetch_playwright, "validate_url", check)


class FakeRoute:
    def __init__(self, url, navigation=False, fetched=None):
        self.request = types.SimpleNamespace(url=url, is_navigation_request=lambda: navigation)
        self.outcome = None
        self._fetched = fetched
        self.fetch_args = None

    def continue_(self):
        self.outcome = "continue"

    def abort(self, code):
        self.outcome = code

    def fetch(self, **kwargs):
        self.fetch_args = kwargs
        return self._fetched

    def fulfill(self, response=None):
        self.outcome = ("fulfill", response)


def test_navigation_redirect_to_internal_host_is_aborted_before_it_is_sent(monkeypatch):
    _reject(monkeypatch, "127.0.0.1")
    session = fetch_playwright.BrowserSession()
    route = FakeRoute("https://a.gov/r", navigation=True,
                      fetched=FakeResponse(302, {"location": "http://127.0.0.1:9/secret"}))
    session._guard(route)
    assert route.fetch_args == {"max_redirects": 0}
    assert route.outcome == "blockedbyclient"


def test_navigation_redirect_to_public_host_is_fulfilled(monkeypatch):
    _reject(monkeypatch, "127.0.0.1")
    session = fetch_playwright.BrowserSession()
    redirect = FakeResponse(302, {"location": "/next"})
    route = FakeRoute("https://a.gov/r", navigation=True, fetched=redirect)
    session._guard(route)
    assert route.outcome == ("fulfill", redirect)


def test_navigation_without_redirect_is_fulfilled(monkeypatch):
    session = fetch_playwright.BrowserSession()
    page = FakeResponse(200)
    route = FakeRoute("https://a.gov/p", navigation=True, fetched=page)
    session._guard(route)
    assert route.outcome == ("fulfill", page)


def test_non_http_navigation_just_continues(monkeypatch):
    route = FakeRoute("file:///tmp/page.html", navigation=True)
    fetch_playwright.BrowserSession()._guard(route)
    assert route.outcome == "continue" and route.fetch_args is None


def test_non_navigation_requests_keep_continue_and_abort(monkeypatch):
    _reject(monkeypatch, "127.0.0.1")
    session = fetch_playwright.BrowserSession()
    ok, bad = FakeRoute("https://a.gov/img.png"), FakeRoute("http://127.0.0.1:9/img.png")
    session._guard(ok)
    session._guard(bad)
    assert ok.outcome == "continue" and ok.fetch_args is None
    assert bad.outcome == "blockedbyclient"


def test_guard_aborts_internal_hosts_and_caches_lookups(monkeypatch):
    calls = []

    def check(url):
        calls.append(url)
        if "127.0.0.1" in url:
            raise UnsafeURLError(url)

    monkeypatch.setattr(fetch_playwright, "validate_url", check)
    session = fetch_playwright.BrowserSession()
    routes = [FakeRoute(u) for u in (
        "https://a.gov/x", "https://a.gov/y", "http://127.0.0.1:9/z", "data:text/plain,hi")]
    for route in routes:
        session._guard(route)
    assert [r.outcome for r in routes] == ["continue", "continue", "blockedbyclient", "continue"]
    assert sum("a.gov" in c for c in calls) == 1


def test_navigation_ending_on_internal_host_is_refused(tmp_path, monkeypatch):
    _reject(monkeypatch, "127.0.0.1")
    page = FakePage([ARTICLE_HTML], final_url="http://127.0.0.1/admin")
    with pytest.raises(UnsafeURLError):
        _session(FakeContext(page)).fetch("https://a.gov/minutes", tmp_path)
    assert page.closed


def test_blocked_redirect_hop_is_unsafe_not_blocked(tmp_path):
    page = FakePage([""], goto_error=RuntimeError("net::ERR_BLOCKED_BY_CLIENT at https://a.gov/r"))
    with pytest.raises(UnsafeURLError):
        _session(FakeContext(page)).fetch("https://a.gov/r", tmp_path)


class RedirectRequest:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, timeout, max_redirects=20):
        self.calls.append((url, max_redirects))
        return self.responses.pop(0)


def test_pdf_redirects_are_validated_per_hop(tmp_path, monkeypatch, fake_pdf):
    _reject(monkeypatch, "127.0.0.1")
    context = FakeContext(FakePage([""]))
    context.request = RedirectRequest([
        FakeResponse(302, {"location": "/next", "content-type": "text/html"}),
        FakeResponse(302, {"location": "http://127.0.0.1/secret.pdf", "content-type": "text/html"}),
    ])
    with pytest.raises(UnsafeURLError):
        _session(context).fetch("https://a.gov/packet.pdf", tmp_path)
    assert context.request.calls == [("https://a.gov/packet.pdf", 0), ("https://a.gov/next", 0)]
    assert fake_pdf == []


class FakePlaywright:
    def __init__(self, launch_results):
        self.launch_results = list(launch_results)
        self.launches = []
        self.stopped = False
        self.chromium = self

    def start(self):
        return self

    def stop(self):
        self.stopped = True

    def launch_persistent_context(self, profile, **kwargs):
        self.launches.append((profile, kwargs))
        result = self.launch_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class GuardedContext:
    def __init__(self):
        self.routes = []
        self.closed = False

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def close(self):
        self.closed = True


def _install_fake_playwright(monkeypatch, fake):
    api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = lambda: fake
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)


def test_locked_profile_retries_in_a_temp_profile(tmp_path, monkeypatch):
    ctx = GuardedContext()
    fake = FakePlaywright([RuntimeError("profile in use"), ctx])
    _install_fake_playwright(monkeypatch, fake)
    session = fetch_playwright.BrowserSession(profile_dir=tmp_path / "profile")
    with session:
        assert session.available
        temp = Path(session._tmp_profile)
        assert temp.exists() and fake.launches[1][0] == str(temp)
        assert fake.launches[0][1]["service_workers"] == "block"
        assert ctx.routes and ctx.routes[0][0] == "**/*"
    assert not temp.exists() and ctx.closed
    assert session._tmp_profile is None


def test_browsers_live_in_the_repo_cache_unless_overridden(tmp_path, monkeypatch):
    _install_fake_playwright(monkeypatch, FakePlaywright([GuardedContext(), GuardedContext()]))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "x")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    with fetch_playwright.BrowserSession(profile_dir=tmp_path / "p1"):
        assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) == fetch_playwright.BROWSERS_PATH
    assert fetch_playwright.BROWSERS_PATH.parts[-2:] == (".cache", "ms-playwright")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "custom")
    with fetch_playwright.BrowserSession(profile_dir=tmp_path / "p2"):
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "custom"


def test_launch_failing_twice_is_unavailable(tmp_path, monkeypatch):
    fake = FakePlaywright([RuntimeError("no chromium"), RuntimeError("no chromium")])
    _install_fake_playwright(monkeypatch, fake)
    with fetch_playwright.BrowserSession(profile_dir=tmp_path / "profile") as session:
        assert session.available is False
    assert fake.stopped


def test_browser_crash_marks_session_unavailable(tmp_path):
    page = FakePage([""], goto_error=RuntimeError("Target page, context or browser has been closed"))
    session = _session(FakeContext(page))
    with pytest.raises(RuntimeError):
        session.fetch("https://a.gov/x", tmp_path)
    assert session.available is False


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
        pytest.skip("Chromium not installed (uv run --frozen playwright install chromium)")


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


@pytest.mark.browser
def test_real_browser_blocks_subrequests_and_redirects_to_internal_hosts(tmp_path, monkeypatch):
    _chromium_or_skip()
    hits = []

    class B(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x")

        def log_message(self, *args):
            pass

    server_b = http.server.ThreadingHTTPServer(("127.0.0.1", 0), B)
    port_b = server_b.server_address[1]

    class A(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/r":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port_b}/secret")
                self.end_headers()
                return
            body = (f'<html><head><title>P</title></head><body><p>{"Public page text. " * 20}</p>'
                    f'<img src="http://127.0.0.1:{port_b}/ping"></body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server_a = http.server.ThreadingHTTPServer(("127.0.0.1", 0), A)
    port_a = server_a.server_address[1]
    for s in (server_a, server_b):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    # The guard (and its per-host cache) decides by hostname: A is "localhost" (allowed),
    # B is "127.0.0.1" (blocked), so the test can tell the two servers apart.
    def check(url):
        if urlparse(url).hostname == "127.0.0.1":
            raise UnsafeURLError(url)

    monkeypatch.setattr(fetch_playwright, "validate_url", check)
    try:
        with fetch_playwright.BrowserSession(profile_dir=tmp_path / "profile") as session:
            assert session.available
            ok = session.fetch(f"http://localhost:{port_a}/page", tmp_path / "cache")
            assert ok.verdict.ok
            assert hits == []  # the page's <img> subresource was aborted by the context route
            with pytest.raises(UnsafeURLError):
                session.fetch(f"http://localhost:{port_a}/r", tmp_path / "cache")
    finally:
        server_a.shutdown()
        server_b.shutdown()
    # Observed with Playwright 1.63.0 / Chromium 153 (2026-10-27): context.route does NOT see
    # navigation redirect hops on its own (B used to receive the redirected GET). _guard now
    # fetches navigations with max_redirects=0 and checks each Location before fulfilling,
    # so B gets no hit at all.
    assert hits == []
