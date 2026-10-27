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

    disposed = False

    def dispose(self):
        self.disposed = True


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


class AbortingPage(FakePage):
    """goto raises net::ERR_ABORTED for the first `aborts` navigations, like a guard-aborted redirect."""

    def __init__(self, htmls, aborts=1):
        super().__init__(htmls)
        self.aborts = aborts

    def goto(self, url, timeout, wait_until):
        self.visited.append(url)
        self.url = url
        if len(self.visited) <= self.aborts:
            raise RuntimeError(f"Page.goto: net::ERR_ABORTED at {url}")
        return self.response


def test_guard_aborted_redirect_renavigates_once_to_the_final_url(tmp_path):
    page = AbortingPage([ARTICLE_HTML])
    session = _session(FakeContext(page))
    session._last_nav_url["https://a.gov/r"] = "https://b.gov/final"
    result = session.fetch("https://a.gov/r", tmp_path)
    assert result.verdict.ok and page.visited == ["https://a.gov/r", "https://b.gov/final"]
    assert session._last_nav_url == {} and session._goto_target is None


def test_second_guard_abort_is_not_followed_again(tmp_path):
    page = AbortingPage([ARTICLE_HTML], aborts=2)
    session = _session(FakeContext(page))
    session._last_nav_url["https://a.gov/r"] = "https://b.gov/final"
    session._last_nav_url["https://b.gov/final"] = "https://c.gov/again"
    with pytest.raises(RuntimeError):
        session.fetch("https://a.gov/r", tmp_path)
    assert page.visited == ["https://a.gov/r", "https://b.gov/final"] and page.closed
    assert session._last_nav_url == {}


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
    def __init__(self, url, navigation=False, fetched=None, method="GET", post=None, main_frame=True,
                 headers=None):
        self.request = types.SimpleNamespace(
            url=url, is_navigation_request=lambda: navigation, method=method, post_data_buffer=post,
            headers=headers or {}, frame=types.SimpleNamespace(parent_frame=None if main_frame else object()))
        self.outcome = None
        self._fetched = fetched  # a response, a {url: response} dict, or an Exception
        self.fetch_calls = []

    @property
    def fetch_args(self):
        return self.fetch_calls[-1] if self.fetch_calls else None

    def continue_(self):
        self.outcome = "continue"

    def abort(self, code):
        self.outcome = code

    def fetch(self, **kwargs):
        self.fetch_calls.append(kwargs)
        if isinstance(self._fetched, Exception):
            raise self._fetched
        if isinstance(self._fetched, dict):
            return self._fetched[kwargs["url"]]
        return self._fetched

    def fulfill(self, response=None):
        self.outcome = ("fulfill", response)


def test_navigation_redirect_to_internal_host_is_aborted_before_it_is_sent(monkeypatch):
    _reject(monkeypatch, "127.0.0.1")
    session = fetch_playwright.BrowserSession()
    route = FakeRoute("https://a.gov/r", navigation=True,
                      fetched=FakeResponse(302, {"location": "http://127.0.0.1:9/secret"}))
    session._guard(route)
    assert [(c["url"], c["max_redirects"]) for c in route.fetch_calls] == [("https://a.gov/r", 0)]
    assert route.outcome == "blockedbyclient" and route._fetched.disposed
    assert session._last_nav_url == {}


def test_two_hop_chain_to_internal_host_is_aborted_without_fetching_it(monkeypatch):
    _reject(monkeypatch, "127.0.0.1")
    session = fetch_playwright.BrowserSession()
    route = FakeRoute("https://a.gov/r1", navigation=True, fetched={
        "https://a.gov/r1": FakeResponse(302, {"location": "/r2"}),
        "https://a.gov/r2": FakeResponse(301, {"location": "http://127.0.0.1:9/secret"}),
    })
    session._guard(route)
    assert [c["url"] for c in route.fetch_calls] == ["https://a.gov/r1", "https://a.gov/r2"]
    assert route.outcome == "blockedbyclient"


def test_redirected_public_chain_is_aborted_and_the_final_url_recorded(monkeypatch):
    _reject(monkeypatch, "127.0.0.1")
    session = fetch_playwright.BrowserSession()
    session._goto_target = "https://a.gov/r1"  # a goto() from _fetch, which can re-navigate
    hops = [FakeResponse(302, {"location": "/r2"}), FakeResponse(302, {"location": "https://b.gov/page"}),
            FakeResponse(200)]
    route = FakeRoute("https://a.gov/r1", navigation=True, fetched={
        "https://a.gov/r1": hops[0], "https://a.gov/r2": hops[1], "https://b.gov/page": hops[2]})
    session._guard(route)
    assert [(c["url"], c["max_redirects"]) for c in route.fetch_calls] == [
        (u, 0) for u in ("https://a.gov/r1", "https://a.gov/r2", "https://b.gov/page")]
    # The page must run under the real URL, so the browser re-navigates to it.
    assert route.outcome == "aborted"
    assert session._last_nav_url == {"https://a.gov/r1": "https://b.gov/page"}
    assert all(h.disposed for h in hops)


def test_script_initiated_redirected_navigation_is_fulfilled_not_recorded(monkeypatch):
    # e.g. a challenge form POST -> 302: nothing re-navigates, so the page must get the response.
    session = fetch_playwright.BrowserSession()
    session._goto_target = "https://a.gov/challenge"
    final = FakeResponse(200)
    route = FakeRoute("https://a.gov/post", navigation=True, method="POST", post=b"x=1", fetched={
        "https://a.gov/post": FakeResponse(302, {"location": "/final"}), "https://a.gov/final": final})
    session._guard(route)
    assert route.outcome == ("fulfill", final) and session._last_nav_url == {}
    idle = fetch_playwright.BrowserSession()  # no goto in flight at all
    route = FakeRoute("https://a.gov/r", navigation=True, fetched={
        "https://a.gov/r": FakeResponse(302, {"location": "/f"}), "https://a.gov/f": FakeResponse(200)})
    idle._guard(route)
    assert route.outcome[0] == "fulfill" and idle._last_nav_url == {}


def test_redirect_to_the_pages_own_url_after_goto_is_fulfilled(monkeypatch):
    session = fetch_playwright.BrowserSession()
    session._goto_target = None  # goto() has returned
    final = FakeResponse(200)
    route = FakeRoute("https://a.gov/page", navigation=True, fetched={
        "https://a.gov/page": FakeResponse(302, {"location": "/other"}), "https://a.gov/other": final})
    session._guard(route)
    assert route.outcome == ("fulfill", final) and session._last_nav_url == {}


def test_goto_target_is_cleared_as_soon_as_goto_returns(tmp_path):
    seen = []

    class SpyPage(FakePage):
        def content(self):
            seen.append(session._goto_target)
            return super().content()

    session = _session(FakeContext(SpyPage([ARTICLE_HTML])))
    session.fetch("https://a.gov/minutes", tmp_path)
    assert seen == [None]


def test_redirected_subframe_navigation_is_fulfilled_not_recorded(monkeypatch):
    session = fetch_playwright.BrowserSession()
    final = FakeResponse(200)
    route = FakeRoute("https://a.gov/r", navigation=True, main_frame=False, fetched={
        "https://a.gov/r": FakeResponse(302, {"location": "/f"}), "https://a.gov/f": final})
    session._guard(route)
    assert route.outcome == ("fulfill", final) and session._last_nav_url == {}


def test_post_is_not_resent_after_a_redirect(monkeypatch):
    session = fetch_playwright.BrowserSession()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "3", "X-A": "1"}
    route = FakeRoute("https://a.gov/form", navigation=True, method="POST", post=b"a=1", headers=headers, fetched={
        "https://a.gov/form": FakeResponse(302, {"location": "/done"}), "https://a.gov/done": FakeResponse(200)})
    session._guard(route)
    assert [(c["method"], c["post_data"]) for c in route.fetch_calls] == [("POST", b"a=1"), ("GET", None)]
    assert "headers" not in route.fetch_calls[0]  # first hop: the request's own headers
    assert route.fetch_calls[1]["headers"] == {"X-A": "1"}


def test_post_keeps_method_on_307_and_becomes_get_on_303(monkeypatch):
    for status, expected in ((307, "POST"), (303, "GET")):
        session = fetch_playwright.BrowserSession()
        route = FakeRoute("https://a.gov/form", navigation=True, method="POST", post=b"a=1", fetched={
            "https://a.gov/form": FakeResponse(status, {"location": "/done"}), "https://a.gov/done": FakeResponse(200)})
        session._guard(route)
        assert route.fetch_calls[1]["method"] == expected
    get = FakeRoute("https://a.gov/g", navigation=True, fetched={
        "https://a.gov/g": FakeResponse(303, {"location": "/h"}), "https://a.gov/h": FakeResponse(200)})
    fetch_playwright.BrowserSession()._guard(get)
    assert [c["method"] for c in get.fetch_calls] == ["GET", "GET"]


def test_route_fetch_uses_a_shared_deadline(monkeypatch):
    session = fetch_playwright.BrowserSession()
    route = FakeRoute("https://a.gov/r", navigation=True, fetched={
        "https://a.gov/r": FakeResponse(302, {"location": "/p"}), "https://a.gov/p": FakeResponse(200)})
    session._guard(route)
    timeouts = [c["timeout"] for c in route.fetch_calls]
    assert all(1000 <= t <= fetch_playwright.NAV_TIMEOUT_MS for t in timeouts)
    assert timeouts[1] <= timeouts[0]
    monkeypatch.setattr(fetch_playwright, "NAV_TIMEOUT_MS", 0)
    expired = FakeRoute("https://a.gov/p", navigation=True, fetched=FakeResponse(200))
    session._guard(expired)
    assert expired.outcome == "failed" and expired.fetch_calls == []


def test_redirect_loop_is_aborted(monkeypatch):
    session = fetch_playwright.BrowserSession()
    route = FakeRoute("https://a.gov/r", navigation=True,
                      fetched=FakeResponse(302, {"location": "/r"}))
    session._guard(route)
    assert route.outcome == "failed" and len(route.fetch_calls) == fetch_playwright.MAX_REDIRECTS + 1


def test_route_fetch_error_aborts_the_navigation(monkeypatch):
    session = fetch_playwright.BrowserSession()
    route = FakeRoute("https://a.gov/p", navigation=True, fetched=RuntimeError("connection refused"))
    session._guard(route)
    assert route.outcome == "failed"


def test_navigation_without_redirect_is_fulfilled(monkeypatch):
    session = fetch_playwright.BrowserSession()
    page = FakeResponse(200)
    route = FakeRoute("https://a.gov/p", navigation=True, fetched=page)
    session._guard(route)
    assert route.outcome == ("fulfill", page) and page.disposed and session._last_nav_url == {}


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
        self.ws_routes = []
        self.closed = False

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def route_web_socket(self, pattern, handler):
        self.ws_routes.append((pattern, handler))

    def close(self):
        self.closed = True


class FakeWebSocket:
    def __init__(self, url):
        self.url = url
        self.connected = self.closed = False

    def connect_to_server(self):
        self.connected = True

    def close(self):
        self.closed = True


def test_websockets_pass_the_ssrf_guard(tmp_path, monkeypatch):
    import url_safety
    monkeypatch.setattr(fetch_playwright, "validate_url", url_safety.validate_url)
    ctx = GuardedContext()
    _install_fake_playwright(monkeypatch, FakePlaywright([ctx]))
    with fetch_playwright.BrowserSession(profile_dir=tmp_path / "profile"):
        assert len(ctx.ws_routes) == 1
        handler = ctx.ws_routes[0][1]
        public = FakeWebSocket("wss://93.184.216.34/feed")
        handler(public)
        internal = FakeWebSocket("ws://127.0.0.1:9222/devtools")
        handler(internal)
    assert public.connected and not public.closed
    assert internal.closed and not internal.connected


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
    from detect_tier import check_playwright
    pytest.importorskip("playwright.sync_api")
    result = check_playwright()  # accepts the headless shell (--only-shell) as well as full Chromium
    if result["status"] != "ok":
        pytest.skip("Chromium not installed (PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright "
                    "uv run --frozen playwright install chromium --only-shell)")


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


@pytest.mark.browser
def test_real_browser_blocks_a_two_hop_redirect_chain_to_an_internal_host(tmp_path, monkeypatch):
    # Chromium follows a fulfilled 3xx without routing the next hop, so _guard walks the whole
    # chain itself: localhost:A/r1 -> localhost:A/r2 -> 127.0.0.1:B/secret must never reach B.
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
            location = {"/r1": "/r2", "/r2": f"http://127.0.0.1:{port_b}/secret"}[self.path]
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *args):
            pass

    server_a = http.server.ThreadingHTTPServer(("127.0.0.1", 0), A)
    port_a = server_a.server_address[1]
    for s in (server_a, server_b):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    def check(url):
        if urlparse(url).hostname == "127.0.0.1":
            raise UnsafeURLError(url)

    monkeypatch.setattr(fetch_playwright, "validate_url", check)
    try:
        with fetch_playwright.BrowserSession(profile_dir=tmp_path / "profile") as session:
            assert session.available
            with pytest.raises(UnsafeURLError):
                session.fetch(f"http://localhost:{port_a}/r1", tmp_path / "cache")
    finally:
        server_a.shutdown()
        server_b.shutdown()
    assert hits == []


@pytest.mark.browser
def test_real_browser_redirected_page_runs_under_its_final_url(tmp_path, monkeypatch):
    # After a redirect the page must live at the real URL, so relative resources resolve there.
    _chromium_or_skip()
    paths = []

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            if self.path == "/r":
                self.send_response(302)
                self.send_header("Location", "/sub/final")
                self.end_headers()
                return
            if self.path == "/sub/final":
                body = (f'<html><head><title>Final</title></head><body><p>{"Public page text. " * 20}</p>'
                        '<script src="rel.js"></script></body></html>').encode()  # blocks DOMContentLoaded
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
            else:
                body = b""
                self.send_response(404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(fetch_playwright, "validate_url", lambda url: None)
    try:
        with fetch_playwright.BrowserSession(profile_dir=tmp_path / "profile") as session:
            assert session.available
            result = session.fetch(f"http://localhost:{server.server_address[1]}/r", tmp_path / "cache")
    finally:
        server.shutdown()
    assert result.verdict.ok and result.title == "Final"
    assert "/sub/rel.js" in paths and "/rel.js" not in paths


@pytest.mark.browser
def test_real_browser_challenge_form_post_redirect_still_clears(tmp_path, monkeypatch):
    # A script-submitted form POST that 302s to the clean page must reach the page, not be aborted.
    _chromium_or_skip()
    posts = []

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/challenge":
                body = (b"<html><head><title>Just a moment...</title></head><body>"
                        b"<h2>Verify you are human by completing the action below.</h2>"
                        b'<form id="f" method="post" action="/post"><input name="t" value="1"></form>'
                        b"<script>document.getElementById('f').submit()</script></body></html>")
            elif self.path == "/final":
                body = (f"<html><head><title>Done</title></head><body><p>CLEARED "
                        f"{'Public page text. ' * 20}</p></body></html>").encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            posts.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(fetch_playwright, "validate_url", lambda url: None)
    try:
        with fetch_playwright.BrowserSession(profile_dir=tmp_path / "profile") as session:
            assert session.available
            result = session.fetch(f"http://localhost:{server.server_address[1]}/challenge", tmp_path / "cache")
    finally:
        server.shutdown()
    assert posts == ["/post"]
    assert result.verdict.ok and "CLEARED" in result.content
