# tests/test_extract_local.py
"""Tests for extract_local.py -- local file text extraction."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}


def test_extract_pdf(tmp_path):
    from extract_local import extract_file
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"fake pdf")
    with patch("extract_local.pymupdf") as mock_fitz:
        mock_doc = MagicMock()
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Page 1 content about surveillance"
        mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_fitz.open.return_value = mock_doc
        result = extract_file(pdf_path)
    assert result["content"] == "Page 1 content about surveillance"
    assert result["file_type"] == "pdf"
    assert result["title"] == "test"


def test_extract_docx(tmp_path):
    from extract_local import extract_file
    docx_path = tmp_path / "test.docx"
    docx_path.write_bytes(b"fake docx")
    with patch("extract_local.docx") as mock_docx:
        mock_doc = MagicMock()
        mock_doc.paragraphs = [MagicMock(text="Paragraph one"), MagicMock(text="Paragraph two")]
        mock_docx.Document.return_value = mock_doc
        result = extract_file(docx_path)
    assert "Paragraph one" in result["content"]
    assert "Paragraph two" in result["content"]
    assert result["file_type"] == "docx"


def test_extract_unsupported_type(tmp_path):
    from extract_local import extract_file
    bad_path = tmp_path / "test.xyz"
    bad_path.write_text("content")
    result = extract_file(bad_path)
    assert result is None


def test_extract_folder_processes_all_supported(tmp_path):
    from extract_local import extract_folder
    (tmp_path / "a.pdf").write_bytes(b"fake")
    (tmp_path / "b.docx").write_bytes(b"fake")
    (tmp_path / "c.txt").write_text("ignored")
    with patch("extract_local.extract_file") as mock_extract:
        mock_extract.return_value = {"content": "text", "file_type": "pdf", "title": "test", "source_path": "test"}
        results = extract_folder(tmp_path, recursive=False)
    # Should attempt a.pdf and b.docx, skip c.txt
    assert mock_extract.call_count == 2


def test_extract_folder_recursive(tmp_path):
    from extract_local import extract_folder
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "a.pdf").write_bytes(b"fake")
    (sub / "b.pdf").write_bytes(b"fake")
    with patch("extract_local.extract_file") as mock_extract:
        mock_extract.return_value = {"content": "text", "file_type": "pdf", "title": "test", "source_path": "test"}
        results = extract_folder(tmp_path, recursive=True)
    assert mock_extract.call_count == 2


def test_extract_folder_filters_none_results(tmp_path):
    from extract_local import extract_folder
    (tmp_path / "a.pdf").write_bytes(b"fake")
    (tmp_path / "b.xyz").write_text("bad")
    # mock extract_file to return None for .xyz (which won't even be called)
    # and a real result for .pdf
    def side_effect(path):
        if path.suffix == ".pdf":
            return {"content": "pdf text", "file_type": "pdf", "title": "a", "source_path": str(path)}
        return None
    with patch("extract_local.extract_file", side_effect=side_effect):
        results = extract_folder(tmp_path, recursive=False)
    # .xyz files are not in SUPPORTED_EXTENSIONS so extract_file is not called for them
    assert len(results) == 1
    assert results[0]["file_type"] == "pdf"
