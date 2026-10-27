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
