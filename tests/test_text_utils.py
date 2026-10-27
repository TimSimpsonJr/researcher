"""Tests for text_utils.html_to_text."""

from text_utils import html_to_text


def test_html_to_text_drops_scripts_keeps_title_and_blocks():
    html = (
        "<html><head><title> Board  Agenda </title><style>p{color:red}</style></head>"
        "<body><h1>Item 4</h1><p>Camera contract &amp; renewal.</p>"
        "<script>var x = 'hidden';</script><div>Second block</div></body></html>"
    )
    text, title = html_to_text(html)
    assert title == "Board Agenda"
    assert text == "Item 4\nCamera contract & renewal.\nSecond block"


def test_html_to_text_recovers_from_unclosed_head():
    text, title = html_to_text("<html><head><title>T</title><body><p>Body text</p></body>")
    assert title == "T" and text == "Body text"
