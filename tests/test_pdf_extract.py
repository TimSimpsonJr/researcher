"""Tests for pdf_extract.py. Fixture PDFs are generated in-test with PyMuPDF;
OCR is mocked so the suite never needs Tesseract."""

import hashlib

import pymupdf
import pytest

import pdf_extract

LINE = "Board meeting minutes: the council approved the camera contract, page {}."


def _text_pdf(n_pages: int) -> bytes:
    with pymupdf.open() as doc:
        for i in range(n_pages):
            doc.new_page().insert_text((72, 72), LINE.format(i + 1), fontsize=11)
        return doc.tobytes()


def _image_only_pdf(n_pages: int, text_pages: int = 0) -> bytes:
    """text_pages real text pages, then n_pages pages that are only a picture of text."""
    with pymupdf.open(stream=_text_pdf(n_pages), filetype="pdf") as src, pymupdf.open() as out:
        for i in range(text_pages):
            out.new_page().insert_text((72, 72), LINE.format(f"text-{i + 1}"), fontsize=11)
        for page in src:
            pix = page.get_pixmap(dpi=100)
            new = out.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, pixmap=pix)
        return out.tobytes()


def _blank_pdf(n_pages: int) -> bytes:
    with pymupdf.open() as doc:
        for _ in range(n_pages):
            doc.new_page(width=50, height=50)
        return doc.tobytes()


def test_html_bytes_are_not_a_pdf(tmp_path, no_ocr):
    r = pdf_extract.to_result(b"<html><body>Access denied</body></html>", tmp_path)
    assert not r.verdict.ok and r.verdict.reason == "not_pdf"
    assert not (tmp_path / "pdf").exists()


def test_corrupt_pdf_returns_blocked_verdict(tmp_path, no_ocr):
    r = pdf_extract.to_result(b"%PDF-1.4\n1 0 obj", tmp_path)
    assert r.verdict.kind == "blocked" and r.verdict.reason.startswith("error:")
    assert r.error


def test_encrypted_pdf_returns_blocked_verdict(tmp_path, no_ocr):
    with pymupdf.open(stream=_text_pdf(1), filetype="pdf") as doc:
        data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    r = pdf_extract.to_result(data, tmp_path)
    assert r.verdict.kind == "blocked" and r.verdict.reason == "error:ValueError"


def test_ocr_is_capped_at_max_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: True)
    calls = []
    monkeypatch.setattr(pdf_extract, "_ocr_page", lambda page: calls.append(1) or "Recognised text from a scanned page.")
    text, info = pdf_extract.extract(_blank_pdf(pdf_extract.MAX_OCR_PAGES + 10), tmp_path)
    assert len(calls) == pdf_extract.MAX_OCR_PAGES
    assert info.empty_pages == 10 and info.image_only_pdf is True and info.pdf_path


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
    assert thin.verdict.kind == "thin" and thin.content == ""
    scanned = pdf_extract.to_result(_image_only_pdf(2), tmp_path)
    assert scanned.verdict.ok and scanned.image_only_pdf and scanned.pdf_path


def test_to_result_serializes_extraction(tmp_path, monkeypatch):
    import threading
    import time

    state = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def slow_extract(pdf_bytes, cache_dir):
        with guard:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with guard:
            state["now"] -= 1
        return "text " * 100, pdf_extract.PdfInfo(method="pdf-local", pages=1, empty_pages=0)

    monkeypatch.setattr(pdf_extract, "extract", slow_extract)
    threads = [threading.Thread(target=pdf_extract.to_result, args=(b"%PDF-1.7", tmp_path)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 1
