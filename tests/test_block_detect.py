"""Tests for block_detect.py: content vs bot wall vs gone vs empty."""

import re
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
from text_utils import html_to_text

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
    page = _page(name)
    if name.endswith(".html"):
        assert classify(status, "text/html", "", page) == expected
    else:
        assert classify(status, "text/html", page) == expected


def test_cloudflare_jsd_script_on_real_page_is_ok():
    html = _page("cloudflare_jsd_real.html")
    visible = re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<script.*?</script>", "", html))
    assert "challenge-platform" in html and 200 < len(visible) < WALL_MAX_CHARS
    assert classify(200, "text/html", visible, html) == Verdict("ok")


@pytest.mark.parametrize("prompt", [
    "Please sign in to comment.",
    "Please log in to reply.",
    "You must be logged in to post a comment.",
])
def test_comment_login_prompt_on_short_real_page_is_ok(prompt):
    body = "The county commission reviewed the license plate reader contract. " * 5
    html = (f'<nav><a href="/login">Sign in</a></nav><main><p>{body}</p></main>'
            f'<section id="respond"><p>{prompt}</p></section>')
    text, _ = html_to_text(html)
    assert prompt in text and len(text) < WALL_MAX_CHARS
    assert classify(200, "text/html", text, html) == Verdict("ok")


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
