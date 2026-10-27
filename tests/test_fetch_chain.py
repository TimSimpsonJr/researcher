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
        out = self.by_url.get(url, self.result)
        if isinstance(out, Exception):
            raise out
        return out

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


def test_browser_ssrf_ends_chain_and_lands_in_failed(chain, tmp_path):
    calls, results = chain
    results["jina"] = bad("blocked", "cloudflare")
    results["wayback"] = ok("wayback")
    session = FakeSession(UnsafeURLError("redirected to an internal address"), log=calls)
    result, attempts = fetch_and_clean.fetch_url("https://a.example/x", cache_dir=tmp_path, session=session)
    assert result is None and calls == ["jina", "playwright"]
    assert attempts[-1] == att("playwright", "unsafe", "ssrf")


def test_browser_ssrf_is_failed_not_needs_browser(monkeypatch, tmp_path):
    url = "https://a.example/redir"
    _first(monkeypatch, {url: bad("blocked", "cloudflare")})
    session = FakeSession(UnsafeURLError("redirected to an internal address"))
    monkeypatch.setattr(fetch_and_clean, "BrowserSession", lambda: session)
    fetched, failed, needs_browser = fetch_and_clean.process_urls([{"url": url}], tmp_path, 7, None)
    assert needs_browser == [] and [f["url"] for f in failed] == [url]


def test_crashed_browser_is_skipped_for_the_rest(monkeypatch, tmp_path):
    a, b = "https://a.example/a", "https://a.example/b"
    _first(monkeypatch, {a: bad("thin", "0_chars"), b: bad("thin", "0_chars")})

    class Crashing(FakeSession):
        def fetch(self, url, cache_dir):
            self.calls.append(url)
            self.available = False
            raise RuntimeError("Target page, context or browser has been closed")

    session = Crashing()
    monkeypatch.setattr(fetch_and_clean, "BrowserSession", lambda: session)
    monkeypatch.setattr(fetch_and_clean.wayback, "fetch",
                        lambda url, cache_dir: bad("missing", "no_snapshot", "wayback"))
    _, failed, _ = fetch_and_clean.process_urls([{"url": a}, {"url": b}], tmp_path, 7, None)
    assert session.calls == [a]
    by_url = {f["url"]: f for f in failed}
    assert att("playwright", "skipped", "playwright_unavailable") in by_url[b]["attempts"]


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
