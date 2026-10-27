"""Tests for pdf_extract.py. Fixture PDFs are generated in-test with PyMuPDF;
OCR is mocked so the suite never needs Tesseract."""

import hashlib

import pymupdf
import pytest

import pdf_extract

LINE = "Board meeting minutes: the council approved the camera contract, page {}."


def _text_pdf(n_pages: int) -> bytes:
    doc = pymupdf.open()
    for i in range(n_pages):
        doc.new_page().insert_text((72, 72), LINE.format(i + 1), fontsize=11)
    return doc.tobytes()


def _image_only_pdf(n_pages: int, text_pages: int = 0) -> bytes:
    """text_pages real text pages, then n_pages pages that are only a picture of text."""
    src = pymupdf.open(stream=_text_pdf(n_pages), filetype="pdf")
    out = pymupdf.open()
    for i in range(text_pages):
        out.new_page().insert_text((72, 72), LINE.format(f"text-{i + 1}"), fontsize=11)
    for page in src:
        pix = page.get_pixmap(dpi=100)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=pix)
    return out.tobytes()


@pytest.fixture
def no_ocr(monkeypatch):
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: False)


def test_text_pdf_is_extracted_locally(tmp_path, no_ocr):
    text, info = pdf_extract.extract(_text_pdf(3), tmp_path)
    assert "camera contract, page 2" in text
    assert info.method == "pdf-local" and info.pages == 3 and info.empty_pages == 0
    assert info.image_only_pdf is False and info.pdf_path is None
    assert not (tmp_path / "pdf").exists()


def test_image_only_pdf_without_ocr_is_saved_and_flagged(tmp_path, no_ocr):
    data = _image_only_pdf(2)
    text, info = pdf_extract.extract(data, tmp_path)
    assert text == ""
    assert info.method == "pdf-local" and info.image_only_pdf is True
    expected = tmp_path / "pdf" / f"{hashlib.sha256(data).hexdigest()}.pdf"
    assert info.pdf_path == str(expected) and expected.read_bytes() == data


def test_image_only_pdf_with_ocr_returns_text(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: True)
    monkeypatch.setattr(pdf_extract, "_ocr_page", lambda page: "Recognised text from a scanned page of minutes.")
    text, info = pdf_extract.extract(_image_only_pdf(2), tmp_path)
    assert text.count("Recognised text") == 2
    assert info.method == "pdf-ocr" and info.image_only_pdf is False and info.pdf_path is None


def test_ocr_failure_keeps_text_and_flags_visual_read(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: True)

    def broken(page):
        raise RuntimeError("tesseract crashed")

    monkeypatch.setattr(pdf_extract, "_ocr_page", broken)
    text, info = pdf_extract.extract(_image_only_pdf(1, text_pages=2), tmp_path)
    assert "page text-1" in text and "page text-2" in text
    assert info.method == "pdf-ocr" and info.image_only_pdf is True and info.pdf_path


def test_mostly_scanned_pdf_keeps_its_text_pages(tmp_path, no_ocr):
    text, info = pdf_extract.extract(_image_only_pdf(3, text_pages=1), tmp_path)
    assert "page text-1" in text
    assert info.pages == 4 and info.empty_pages == 3 and info.image_only_pdf is True


def test_looks_like_pdf():
    assert pdf_extract.looks_like_pdf("https://a.gov/x", "application/pdf; charset=binary")
    assert pdf_extract.looks_like_pdf("https://a.gov/Packet.PDF?v=2", "")
    assert pdf_extract.looks_like_pdf("https://a.gov/download", "application/octet-stream", b"%PDF-")
    assert not pdf_extract.looks_like_pdf("https://a.gov/agenda", "text/html", b"<html")


def test_to_result_verdicts(tmp_path, no_ocr):
    full = pdf_extract.to_result(_text_pdf(5), tmp_path)
    assert full.verdict.ok and full.method == "pdf-local" and full.is_pdf
    thin = pdf_extract.to_result(_text_pdf(1), tmp_path)
    assert thin.verdict.kind == "thin"
    scanned = pdf_extract.to_result(_image_only_pdf(2), tmp_path)
    assert scanned.verdict.ok and scanned.image_only_pdf and scanned.pdf_path
