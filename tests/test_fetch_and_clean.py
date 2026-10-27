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


def test_validate_url_raises_unsafe_url_error():
    from url_safety import UnsafeURLError
    from fetch_and_clean import validate_url
    with pytest.raises(UnsafeURLError):
        validate_url("http://localhost/admin")
    assert issubclass(UnsafeURLError, ValueError)
