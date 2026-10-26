# tests/test_fetch_and_clean.py
"""Tests for fetch_and_clean.py"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock


# ── url_cache_key ────────────────────────────

def test_url_cache_key_is_sha256():
    from fetch_and_clean import url_cache_key
    import hashlib
    url = "https://example.com/article"
    expected = hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert url_cache_key(url) == expected


def test_url_cache_key_different_urls_differ():
    from fetch_and_clean import url_cache_key
    assert url_cache_key("https://a.com") != url_cache_key("https://b.com")


# ── normalize_url + deduplicate_urls ─────────

def test_normalize_url_strips_trailing_slash():
    from fetch_and_clean import normalize_url
    assert normalize_url("https://example.com/") == "https://example.com"


def test_normalize_url_lowercases():
    from fetch_and_clean import normalize_url
    assert normalize_url("HTTPS://EXAMPLE.COM") == "https://example.com"


def test_deduplicate_urls_removes_duplicates():
    from fetch_and_clean import deduplicate_urls
    urls = [
        {"url": "https://example.com/"},
        {"url": "https://EXAMPLE.COM/"},
        {"url": "https://other.com"},
    ]
    result = deduplicate_urls(urls)
    assert len(result) == 2
    assert result[1]["url"] == "https://other.com"


def test_deduplicate_urls_preserves_order():
    from fetch_and_clean import deduplicate_urls
    urls = [
        {"url": "https://a.com"},
        {"url": "https://b.com"},
        {"url": "https://a.com"},
    ]
    result = deduplicate_urls(urls)
    assert [r["url"] for r in result] == ["https://a.com", "https://b.com"]


# ── Cache load/save/expiry ───────────────────

def test_load_cache_returns_none_when_missing(tmp_path):
    from fetch_and_clean import load_cache
    assert load_cache(tmp_path, "nonexistent_key") is None


def test_load_cache_returns_entry_when_present(tmp_path):
    from fetch_and_clean import load_cache, save_cache
    entry = {"url": "https://example.com", "content": "hello", "fetched_at": datetime.now(timezone.utc).isoformat(), "title": "Test", "fetch_method": "jina"}
    save_cache(tmp_path, "testkey", entry)
    loaded = load_cache(tmp_path, "testkey")
    assert loaded["content"] == "hello"


def test_is_expired_returns_false_for_fresh_entry():
    from fetch_and_clean import is_expired
    entry = {"fetched_at": datetime.now(timezone.utc).isoformat()}
    assert is_expired(entry, ttl_days=7) is False


def test_is_expired_returns_true_for_old_entry():
    from fetch_and_clean import is_expired
    old_time = datetime.now(timezone.utc) - timedelta(days=8)
    entry = {"fetched_at": old_time.isoformat()}
    assert is_expired(entry, ttl_days=7) is True


def test_save_cache_creates_file(tmp_path):
    from fetch_and_clean import save_cache
    entry = {"url": "u", "content": "c", "fetched_at": "2026-01-01T00:00:00+00:00", "title": "", "fetch_method": "jina"}
    save_cache(tmp_path, "mykey", entry)
    assert (tmp_path / "mykey.json").exists()


# ── fetch_via_jina (mocked) ─────────────────

def test_fetch_via_jina_returns_content_and_title():
    from fetch_and_clean import fetch_via_jina
    mock_response = MagicMock()
    mock_response.text = "# My Article\n\nSome content here."
    mock_response.raise_for_status = MagicMock()
    with patch("fetch_and_clean.requests.get", return_value=mock_response):
        content, title = fetch_via_jina("https://example.com")
    assert content == "# My Article\n\nSome content here."
    assert title == "My Article"


def test_fetch_via_jina_title_empty_when_no_heading():
    from fetch_and_clean import fetch_via_jina
    mock_response = MagicMock()
    mock_response.text = "Just some text, no heading."
    mock_response.raise_for_status = MagicMock()
    with patch("fetch_and_clean.requests.get", return_value=mock_response):
        content, title = fetch_via_jina("https://example.com")
    assert title == ""


# ── fetch_url fallback logic (mocked) ────────

def test_fetch_url_uses_jina_first():
    from fetch_and_clean import fetch_url
    long_body = "Real Jina content. " * 20  # 380 chars
    with patch("fetch_and_clean.fetch_via_jina", return_value=(long_body, "title")) as mock_jina:
        content, title, method = fetch_url("https://example.com")
    assert method == "jina"
    mock_jina.assert_called_once()


def test_fetch_url_falls_back_to_wayback_when_jina_fails():
    from fetch_and_clean import fetch_url
    long_archived = "Archived content from the Wayback Machine. " * 10  # 430 chars
    with patch("fetch_and_clean.fetch_via_jina", side_effect=Exception("timeout")):
        with patch("fetch_and_clean.fetch_via_wayback", return_value=(long_archived, "archived title")) as mock_wb:
            content, title, method = fetch_url("https://example.com")
    assert method == "wayback"
    assert content == long_archived


def test_fetch_url_raises_when_both_fail():
    from fetch_and_clean import fetch_url
    with patch("fetch_and_clean.fetch_via_jina", side_effect=Exception("jina fail")):
        with patch("fetch_and_clean.fetch_via_wayback", side_effect=Exception("wayback fail")):
            with patch("fetch_and_clean.fetch_via_playwright", side_effect=Exception("playwright fail")):
                with pytest.raises(RuntimeError, match="All fetch methods failed"):
                    fetch_url("https://example.com")


# ── Playwright fallback (Phase 3) ─────────────

MIN_USEFUL_CONTENT_CHARS = 200


def test_fetch_url_falls_back_to_playwright_when_jina_and_wayback_fail(monkeypatch):
    from fetch_and_clean import fetch_url

    def jina_fails(url, key=None):
        raise RuntimeError("jina down")
    def wayback_fails(url, key=None):
        raise RuntimeError("no wayback snapshot")
    def playwright_succeeds(url, **kw):
        return ("Real JS-page content here, longer than 200 chars. " * 10, "JS Page Title")

    monkeypatch.setattr("fetch_and_clean.fetch_via_jina", jina_fails)
    monkeypatch.setattr("fetch_and_clean.fetch_via_wayback", wayback_fails)
    monkeypatch.setattr("fetch_and_clean.fetch_via_playwright", playwright_succeeds)

    content, title, method = fetch_url("https://example-spa.com")
    assert "Real JS-page content" in content
    assert title == "JS Page Title"
    assert method == "playwright"


def test_fetch_url_thin_jina_content_falls_through_to_playwright(monkeypatch):
    from fetch_and_clean import fetch_url

    monkeypatch.setattr("fetch_and_clean.fetch_via_jina", lambda url, key=None: ("", ""))
    monkeypatch.setattr("fetch_and_clean.fetch_via_wayback", lambda url, key=None: ("", ""))
    monkeypatch.setattr(
        "fetch_and_clean.fetch_via_playwright",
        lambda url, **kw: ("Real content from JS execution. " * 20, "JS Title"),
    )

    content, title, method = fetch_url("https://example-spa.com")
    assert method == "playwright"
    assert "Real content" in content


def test_fetch_url_skips_playwright_when_jina_returns_useful_content(monkeypatch):
    from fetch_and_clean import fetch_url

    monkeypatch.setattr(
        "fetch_and_clean.fetch_via_jina",
        lambda url, key=None: ("Jina content. " * 50, "Jina Title"),
    )
    called = []
    monkeypatch.setattr(
        "fetch_and_clean.fetch_via_playwright",
        lambda url, **kw: called.append(url) or ("", ""),
    )

    content, title, method = fetch_url("https://example.com")
    assert method == "jina"
    assert called == []


# ── process_urls — cache hit / miss / failure ─

def test_process_urls_cache_hit_skips_fetch(tmp_path):
    from fetch_and_clean import process_urls, save_cache, url_cache_key
    url = "https://example.com/article"
    cache_key = url_cache_key(url)
    entry = {
        "url": url,
        "content": "cached content",
        "title": "Cached",
        "fetch_method": "jina",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    save_cache(tmp_path, cache_key, entry)

    with patch("fetch_and_clean.fetch_url") as mock_fetch:
        fetched, failed = process_urls(
            urls=[{"url": url}],
            cache_dir=tmp_path,
            ttl_days=7,
            jina_api_key=None,
        )
    mock_fetch.assert_not_called()
    assert len(fetched) == 1
    assert fetched[0]["cache_hit"] is True
    assert fetched[0]["content"] == "cached content"


def test_process_urls_fetches_when_cache_miss(tmp_path):
    from fetch_and_clean import process_urls
    with patch("fetch_and_clean.fetch_url", return_value=("fresh content", "Fresh Title", "jina")):
        fetched, failed = process_urls(
            urls=[{"url": "https://example.com/new"}],
            cache_dir=tmp_path,
            ttl_days=7,
            jina_api_key=None,
        )
    assert len(fetched) == 1
    assert fetched[0]["cache_hit"] is False
    assert fetched[0]["content"] == "fresh content"


def test_process_urls_fetch_failure_goes_to_failed_list(tmp_path):
    from fetch_and_clean import process_urls
    with patch("fetch_and_clean.fetch_url", side_effect=RuntimeError("all methods failed")):
        fetched, failed = process_urls(
            urls=[{"url": "https://broken.com"}],
            cache_dir=tmp_path,
            ttl_days=7,
            jina_api_key=None,
        )
    assert len(fetched) == 0
    assert len(failed) == 1
    assert failed[0]["url"] == "https://broken.com"


def test_process_urls_content_truncated_at_50k(tmp_path):
    from fetch_and_clean import process_urls, MAX_CONTENT_CHARS
    long_content = "x" * (MAX_CONTENT_CHARS + 1000)
    with patch("fetch_and_clean.fetch_url", return_value=(long_content, "title", "jina")):
        fetched, _ = process_urls(
            urls=[{"url": "https://example.com/long"}],
            cache_dir=tmp_path,
            ttl_days=7,
            jina_api_key=None,
        )
    assert len(fetched[0]["content"]) == MAX_CONTENT_CHARS


def test_process_urls_caches_full_content_not_truncated(tmp_path):
    # The output cap must not reach the cache: a later run with a higher cap
    # (or a reader of the cache) needs the whole document.
    from fetch_and_clean import process_urls, load_cache, url_cache_key, MAX_CONTENT_CHARS
    url = "https://example.com/long-packet"
    long_content = "x" * (MAX_CONTENT_CHARS + 1000)
    with patch("fetch_and_clean.fetch_url", return_value=(long_content, "title", "jina")):
        fetched, _ = process_urls(urls=[{"url": url}], cache_dir=tmp_path, ttl_days=7, jina_api_key=None)
    assert len(fetched[0]["content"]) == MAX_CONTENT_CHARS
    assert len(load_cache(tmp_path, url_cache_key(url))["content"]) == MAX_CONTENT_CHARS + 1000


def test_fetch_single_caches_full_content_not_truncated(tmp_path):
    from fetch_and_clean import _fetch_single, load_cache, url_cache_key, MAX_CONTENT_CHARS
    url = "https://example.com/long-packet-parallel"
    long_content = "y" * (MAX_CONTENT_CHARS + 1000)
    with patch("fetch_and_clean.fetch_url", return_value=(long_content, "title", "jina")):
        result, failed = _fetch_single({"url": url}, tmp_path, 7, None)
    assert failed is None
    assert len(result["content"]) == MAX_CONTENT_CHARS
    assert len(load_cache(tmp_path, url_cache_key(url))["content"]) == MAX_CONTENT_CHARS + 1000


def test_cache_hit_serves_full_content_under_raised_cap(tmp_path, monkeypatch):
    import fetch_and_clean
    from fetch_and_clean import process_urls, url_cache_key, MAX_CONTENT_CHARS
    url = "https://example.com/long-packet-rerun"
    long_content = "z" * (MAX_CONTENT_CHARS + 1000)
    with patch("fetch_and_clean.fetch_url", return_value=(long_content, "title", "jina")):
        process_urls(urls=[{"url": url}], cache_dir=tmp_path, ttl_days=7, jina_api_key=None)
    monkeypatch.setattr(fetch_and_clean, "MAX_CONTENT_CHARS", MAX_CONTENT_CHARS * 2)
    with patch("fetch_and_clean.fetch_url") as mock_fetch:
        fetched, _ = process_urls(urls=[{"url": url}], cache_dir=tmp_path, ttl_days=7, jina_api_key=None)
    mock_fetch.assert_not_called()
    assert len(fetched[0]["content"]) == MAX_CONTENT_CHARS + 1000


def test_process_urls_expired_cache_refetches(tmp_path):
    from fetch_and_clean import process_urls, save_cache, url_cache_key
    url = "https://example.com/old"
    cache_key = url_cache_key(url)
    old_time = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    entry = {"url": url, "content": "old content", "title": "", "fetch_method": "jina", "fetched_at": old_time}
    save_cache(tmp_path, cache_key, entry)

    with patch("fetch_and_clean.fetch_url", return_value=("refreshed content", "title", "jina")) as mock_fetch:
        fetched, failed = process_urls(
            urls=[{"url": url}],
            cache_dir=tmp_path,
            ttl_days=7,
            jina_api_key=None,
        )
    mock_fetch.assert_called_once()
    assert fetched[0]["cache_hit"] is False
    assert fetched[0]["content"] == "refreshed content"


# ── process_urls_parallel ──────────────────────

def test_process_urls_parallel_fetches_concurrently(tmp_path):
    from fetch_and_clean import process_urls_parallel
    urls = [{"url": f"https://example.com/{i}"} for i in range(5)]
    with patch("fetch_and_clean.fetch_url", return_value=("content", "title", "jina")):
        fetched, failed = process_urls_parallel(
            urls=urls, cache_dir=tmp_path, ttl_days=7,
            jina_api_key=None, max_workers=3,
        )
    assert len(fetched) == 5
    assert len(failed) == 0


def test_process_urls_parallel_handles_mixed_success_failure(tmp_path):
    from fetch_and_clean import process_urls_parallel
    urls = [{"url": "https://good.com"}, {"url": "https://bad.com"}]

    def mock_fetch(url, key=None):
        if "bad" in url:
            raise RuntimeError("fail")
        return ("content", "title", "jina")

    with patch("fetch_and_clean.fetch_url", side_effect=mock_fetch):
        fetched, failed = process_urls_parallel(
            urls=urls, cache_dir=tmp_path, ttl_days=7,
            jina_api_key=None, max_workers=2,
        )
    assert len(fetched) == 1
    assert len(failed) == 1


def test_process_urls_parallel_respects_cache(tmp_path):
    from fetch_and_clean import process_urls_parallel, save_cache, url_cache_key
    from datetime import datetime, timezone
    url = "https://cached.com/article"
    cache_key = url_cache_key(url)
    save_cache(tmp_path, cache_key, {
        "url": url, "content": "cached", "title": "Cached",
        "fetch_method": "jina", "fetched_at": datetime.now(timezone.utc).isoformat(),
    })
    with patch("fetch_and_clean.fetch_url") as mock:
        fetched, _ = process_urls_parallel(
            urls=[{"url": url}], cache_dir=tmp_path, ttl_days=7,
            jina_api_key=None, max_workers=2,
        )
    mock.assert_not_called()
    assert fetched[0]["cache_hit"] is True


# ── validate_url (SSRF protection) ────────────

def test_validate_url_blocks_ftp_scheme():
    from fetch_and_clean import validate_url
    with pytest.raises(ValueError, match="Blocked URL scheme"):
        validate_url("ftp://example.com/file.txt")


def test_validate_url_blocks_file_scheme():
    from fetch_and_clean import validate_url
    with pytest.raises(ValueError, match="Blocked URL scheme"):
        validate_url("file:///etc/passwd")


def test_validate_url_blocks_localhost():
    from fetch_and_clean import validate_url
    with pytest.raises(ValueError, match="Blocked hostname"):
        validate_url("http://localhost/admin")


def test_validate_url_blocks_loopback_ip():
    from fetch_and_clean import validate_url
    with pytest.raises(ValueError, match="Blocked private/reserved IP"):
        validate_url("http://127.0.0.1/secret")


def test_validate_url_blocks_private_ip():
    from fetch_and_clean import validate_url
    with pytest.raises(ValueError, match="Blocked private/reserved IP"):
        validate_url("http://192.168.1.1/admin")


def test_validate_url_blocks_10_net():
    from fetch_and_clean import validate_url
    with pytest.raises(ValueError, match="Blocked private/reserved IP"):
        validate_url("http://10.0.0.1/internal")


def test_validate_url_allows_public_https():
    from fetch_and_clean import validate_url
    # Should not raise
    validate_url("https://example.com/article")


def test_validate_url_blocks_dns_to_private(monkeypatch):
    import socket
    from fetch_and_clean import validate_url

    def fake_getaddrinfo(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.168.1.1', 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="resolves to private IP"):
        validate_url("http://evil.example.com/steal")
