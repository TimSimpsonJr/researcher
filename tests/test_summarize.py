# tests/test_summarize.py
"""Tests for summarize.py — article summarization via Ollama or file output."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ── summarize_article ────────────────────────────

def test_summarize_single_with_ollama():
    from summarize import summarize_article
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"response": json.dumps({
        "summary": "ALPR cameras capture plates automatically.",
        "source_type": "journalism",
        "key_entities": ["Flock Safety"],
        "key_claims": ["422M reads in 2024"],
    })}
    with patch("summarize.requests.post", return_value=mock_response):
        result = summarize_article(
            content="Long article about ALPR surveillance...",
            title="ALPR Report",
            url="https://example.com",
            model="qwen2.5:14b",
        )
    assert result is not None
    assert "summary" in result
    assert isinstance(result["key_entities"], list)


def test_summarize_article_handles_ollama_failure():
    from summarize import summarize_article
    with patch("summarize.requests.post", side_effect=Exception("connection refused")):
        result = summarize_article(
            content="Content", title="Title", url="https://x.com",
            model="qwen2.5:14b",
        )
    assert result is None


def test_summarize_article_handles_invalid_json():
    from summarize import summarize_article
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"response": "not valid json at all"}
    with patch("summarize.requests.post", return_value=mock_response):
        result = summarize_article(
            content="Content", title="Title", url="https://x.com",
            model="qwen2.5:14b",
        )
    assert result is None


def test_summarize_article_passes_correct_payload():
    from summarize import summarize_article
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"response": json.dumps({
        "summary": "Test", "source_type": "other",
        "key_entities": [], "key_claims": [],
    })}
    with patch("summarize.requests.post", return_value=mock_response) as mock_post:
        summarize_article(
            content="Article text here",
            title="My Title",
            url="https://example.com/article",
            model="qwen2.5:14b",
            ollama_url="http://myhost:11434",
        )
    call_args = mock_post.call_args
    assert call_args[0][0] == "http://myhost:11434/api/generate"
    payload = call_args[1].get("json") or call_args[0][1] if len(call_args[0]) > 1 else call_args[1]["json"]
    assert payload["model"] == "qwen2.5:14b"
    assert "Article text here" in payload["prompt"]
    assert payload["stream"] is False


# ── summarize_batch ──────────────────────────────

def test_summarize_batch_processes_all():
    from summarize import summarize_batch
    fetch_results = {
        "fetched": [
            {"url": "https://a.com", "title": "Article A", "content": "Content A about ALPR"},
            {"url": "https://b.com", "title": "Article B", "content": "Content B about surveillance"},
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"response": json.dumps({
        "summary": "Summary", "source_type": "journalism",
        "key_entities": [], "key_claims": [],
    })}
    with patch("summarize.requests.post", return_value=mock_response):
        summaries = summarize_batch(fetch_results, model="qwen2.5:14b")
    assert len(summaries) == 2


def test_summarize_batch_includes_url_and_title():
    from summarize import summarize_batch
    fetch_results = {
        "fetched": [
            {"url": "https://a.com", "title": "Article A", "content": "Content A"},
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"response": json.dumps({
        "summary": "Sum", "source_type": "journalism",
        "key_entities": [], "key_claims": [],
    })}
    with patch("summarize.requests.post", return_value=mock_response):
        summaries = summarize_batch(fetch_results, model="qwen2.5:14b")
    assert summaries[0]["url"] == "https://a.com"
    assert summaries[0]["title"] == "Article A"


def test_summarize_batch_skips_failures():
    from summarize import summarize_batch
    fetch_results = {
        "fetched": [
            {"url": "https://a.com", "title": "A", "content": "Content A"},
            {"url": "https://b.com", "title": "B", "content": "Content B"},
        ]
    }
    with patch("summarize.requests.post", side_effect=Exception("connection refused")):
        summaries = summarize_batch(fetch_results, model="qwen2.5:14b")
    assert len(summaries) == 0


# ── prepare_for_claude_code ──────────────────────

def test_prepare_for_claude_code_writes_files(tmp_path):
    from summarize import prepare_for_claude_code
    fetch_results = {
        "fetched": [
            {"url": "https://a.com", "title": "Article A", "content": "Content A " * 500},
        ]
    }
    output = prepare_for_claude_code(fetch_results, tmp_path)
    assert len(output) == 1
    written_file = tmp_path / output[0]["file"]
    assert written_file.exists()
    text = written_file.read_text()
    assert "Article A" in text
    assert "https://a.com" in text


def test_prepare_for_claude_code_truncates_content(tmp_path):
    from summarize import prepare_for_claude_code
    long_content = "x" * 10000
    fetch_results = {
        "fetched": [{"url": "https://a.com", "title": "Long", "content": long_content}]
    }
    output = prepare_for_claude_code(fetch_results, tmp_path)
    written_file = tmp_path / output[0]["file"]
    text = written_file.read_text()
    # Should be truncated to around 3000 chars of content (plus headers)
    assert len(text) < 5000


def test_prepare_for_claude_code_returns_correct_entries(tmp_path):
    from summarize import prepare_for_claude_code
    fetch_results = {
        "fetched": [
            {"url": "https://a.com", "title": "Article A", "content": "Content A"},
            {"url": "https://b.com", "title": "Article B", "content": "Content B"},
        ]
    }
    output = prepare_for_claude_code(fetch_results, tmp_path)
    assert len(output) == 2
    assert output[0]["url"] == "https://a.com"
    assert output[0]["title"] == "Article A"
    assert output[1]["url"] == "https://b.com"
    # Each entry has a "file" key with a relative filename
    assert output[0]["file"].endswith(".md")
    assert output[1]["file"].endswith(".md")


def test_prepare_for_claude_code_slugifies_filenames(tmp_path):
    from summarize import prepare_for_claude_code
    fetch_results = {
        "fetched": [
            {"url": "https://a.com", "title": "Hello World! Test Article", "content": "Content"},
        ]
    }
    output = prepare_for_claude_code(fetch_results, tmp_path)
    filename = output[0]["file"]
    assert filename.startswith("0-")
    assert "hello-world" in filename.lower()


# ── CLI output shape (Ollama mode) ────────────────────
# Stage 6a in SKILL.md aggregates per-hop summary files via
# `data.get("items", [])`. The Ollama-mode CLI must emit `items` (not
# `summaries`) so the aggregation matches.

def test_cli_ollama_mode_emits_items_key(tmp_path, monkeypatch):
    import subprocess
    fetch = {"fetched": [
        {"url": "https://a.com", "title": "A", "content": "Content A"},
    ]}
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps(fetch))
    out = tmp_path / "out.json"

    # Run summarize.py with Ollama mode but patch the network call so we don't
    # need a real Ollama instance. We do this by importing main() and patching
    # requests.post in the summarize module.
    import sys
    sys.path.insert(0, "scripts")
    import summarize as sm
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"response": json.dumps({
        "summary": "S", "source_type": "journalism",
        "key_entities": [], "key_claims": [],
    })}
    with patch.object(sm, "requests") as mock_req:
        mock_req.post.return_value = mock_response
        argv_backup = sys.argv
        sys.argv = [
            "summarize.py",
            "--input", str(inp),
            "--output", str(out),
            "--model", "test-model",
        ]
        try:
            sm.main()
        finally:
            sys.argv = argv_backup
    result = json.loads(out.read_text())
    assert "items" in result, f"expected 'items' key in CLI output, got: {list(result.keys())}"
    assert "summaries" not in result, "legacy 'summaries' key should be gone"
    assert isinstance(result["items"], list)


IMAGE_PDF = {"url": "https://a.gov/minutes.pdf", "title": "Minutes", "content": "",
             "image_only_pdf": True, "pdf_path": "C:/cache/pdf/ab.pdf"}


def test_summarize_batch_flags_image_only_pdf_without_ollama():
    from summarize import summarize_batch
    with patch("summarize.requests.post") as post:
        summaries = summarize_batch({"fetched": [IMAGE_PDF]}, model="m")
    post.assert_not_called()
    assert summaries == [{"url": "https://a.gov/minutes.pdf", "title": "Minutes", "usable": False,
                          "reason": "needs visual read", "image_only_pdf": True,
                          "pdf_path": "C:/cache/pdf/ab.pdf"}]


def test_cli_stats_count_visual_reads(tmp_path):
    import sys
    import summarize as sm
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps({"fetched": [
        {"url": "https://a.com", "title": "A", "content": "Content A"}, IMAGE_PDF]}), encoding="utf-8")
    out = tmp_path / "out.json"
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"response": json.dumps({
        "summary": "S", "source_type": "journalism", "key_entities": [], "key_claims": []})}
    with patch.object(sm, "requests") as mock_req:
        mock_req.post.return_value = mock_response
        argv_backup = sys.argv
        sys.argv = ["summarize.py", "--input", str(inp), "--output", str(out), "--model", "m"]
        try:
            sm.main()
        finally:
            sys.argv = argv_backup
    result = json.loads(out.read_text(encoding="utf-8"))
    assert mock_req.post.call_count == 1  # only the non-skipped item reaches Ollama
    assert result["stats"] == {"total": 2, "summarized": 1, "needs_visual_read": 1, "failed": 0}


OLLAMA_OK = {"response": json.dumps({
    "summary": "S", "source_type": "government", "key_entities": [], "key_claims": []})}


def test_image_only_pdf_with_text_is_summarized_and_keeps_pdf_fields():
    from summarize import summarize_batch
    item = {**IMAGE_PDF, "content": "Partial OCR text. " * 40}
    resp = MagicMock()
    resp.json.return_value = OLLAMA_OK
    with patch("summarize.requests.post", return_value=resp) as post:
        summaries = summarize_batch({"fetched": [item]}, model="m")
    assert post.call_count == 1
    assert summaries[0]["summary"] == "S"
    assert summaries[0]["image_only_pdf"] is True
    assert summaries[0]["pdf_path"] == "C:/cache/pdf/ab.pdf"
    assert summaries[0].get("usable", True) is True


def test_image_only_flag_without_pdf_path_is_not_a_visual_read(tmp_path):
    from summarize import prepare_for_claude_code, summarize_batch
    item = {**IMAGE_PDF, "pdf_path": None, "content": ""}
    resp = MagicMock()
    resp.json.return_value = OLLAMA_OK
    with patch("summarize.requests.post", return_value=resp) as post:
        summaries = summarize_batch({"fetched": [item]}, model="m")
    assert post.call_count == 1 and summaries[0].get("usable", True) is True
    entry = prepare_for_claude_code({"fetched": [item]}, tmp_path)[0]
    assert "image_only_pdf" not in entry
    assert "Image-only PDF" not in (tmp_path / entry["file"]).read_text(encoding="utf-8")


def test_prepare_writes_windows_pdf_path_verbatim(tmp_path):
    from summarize import prepare_for_claude_code
    p = r"C:\Users\tim\Tim's Vault\cache\pdf\ab.pdf"
    entry = prepare_for_claude_code({"fetched": [{**IMAGE_PDF, "pdf_path": p}]}, tmp_path)[0]
    assert f"read the page images at {p}\n" in (tmp_path / entry["file"]).read_text(encoding="utf-8")
    assert entry["pdf_path"] == p


def test_prepare_for_claude_marks_image_only_pdf(tmp_path):
    from summarize import prepare_for_claude_code
    output = prepare_for_claude_code({"fetched": [IMAGE_PDF]}, tmp_path)
    text = (tmp_path / output[0]["file"]).read_text(encoding="utf-8")
    assert "Image-only PDF: read the page images at C:/cache/pdf/ab.pdf" in text
    assert output[0]["image_only_pdf"] is True and output[0]["pdf_path"] == "C:/cache/pdf/ab.pdf"
