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
