"""pdf_extract.py — PDF bytes to text with PyMuPDF, OCR'ing image-only pages.

Pages whose text layer is (nearly) empty are OCR'd through PyMuPDF's Tesseract
integration when Tesseract is installed. When it isn't, or OCR fails, and most
pages are still empty, the bytes are kept under cache_dir/pdf/<sha256>.pdf and
the result is flagged image_only_pdf so a Claude summarizer can Read the page
images instead. Text from pages that do have a layer is always returned.

Known limit: a genuinely blank PDF is indistinguishable from a scan and is
flagged image-only too.
"""

import functools
import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pymupdf

from block_detect import MIN_USEFUL_CONTENT_CHARS, OK, FetchResult, Verdict, blocked

_PDF_LOCK = threading.Lock()  # ponytail: PyMuPDF is not thread-safe; one extract at a time
EMPTY_PAGE_CHARS = 20  # fewer stripped chars than this = no usable text layer
OCR_DPI = 300
# ponytail: OCR capped at 50 pages per document, pages past the cap go to the visual read;
# raise or make configurable if long scans matter
MAX_OCR_PAGES = 50


@dataclass
class PdfInfo:
    method: str  # "pdf-local" | "pdf-ocr"
    pages: int
    empty_pages: int
    title: str = ""
    image_only_pdf: bool = False
    pdf_path: str | None = None


def looks_like_pdf(url: str, content_type: str, head: bytes = b"") -> bool:
    return (
        "application/pdf" in (content_type or "").lower()
        or urlparse(url).path.lower().endswith(".pdf")
        or head.startswith(b"%PDF")
    )


@functools.lru_cache(maxsize=1)
def tesseract_available() -> bool:
    """PyMuPDF finds Tesseract's language data (TESSDATA_PREFIX or an install)."""
    try:
        pymupdf.get_tessdata()
    except Exception:
        return False
    return True


def _ocr_page(page) -> str:
    textpage = page.get_textpage_ocr(language="eng", dpi=OCR_DPI, full=True)
    return page.get_text(textpage=textpage)


def _is_empty(text: str) -> bool:
    return len(text.strip()) < EMPTY_PAGE_CHARS


def extract(pdf_bytes: bytes, cache_dir: Path) -> tuple[str, PdfInfo]:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if doc.needs_pass and not doc.authenticate(""):
            raise ValueError("encrypted")
        texts = [page.get_text() for page in doc]
        empty = [i for i, t in enumerate(texts) if _is_empty(t)]
        method, ocr_failed, capped = "pdf-local", False, False
        if empty and tesseract_available():
            method = "pdf-ocr"
            capped = len(empty) > MAX_OCR_PAGES
            for i in empty[:MAX_OCR_PAGES]:
                try:
                    texts[i] = _ocr_page(doc[i])
                except Exception:
                    ocr_failed = True
        title = (doc.metadata or {}).get("title") or ""
    still_empty = sum(1 for t in texts if _is_empty(t))
    info = PdfInfo(method=method, pages=len(texts), empty_pages=still_empty, title=title)
    if ocr_failed or capped or (texts and still_empty * 2 > len(texts)):
        path = Path(cache_dir) / "pdf" / f"{hashlib.sha256(pdf_bytes).hexdigest()}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pdf_bytes)
        info.image_only_pdf, info.pdf_path = True, str(path)
    text = "\n\n".join(t.strip() for t in texts if not _is_empty(t))
    return text, info


def to_result(pdf_bytes: bytes, cache_dir: Path) -> FetchResult:
    """extract() as a FetchResult. An image-only PDF is ok: the visual read covers it."""
    if b"%PDF" not in pdf_bytes[:1024]:  # pymupdf happily opens HTML as a "pdf"
        return FetchResult(
            verdict=Verdict("thin", "not_a_pdf"), method="pdf-local", content_type="application/pdf"
        )
    try:
        with _PDF_LOCK:
            text, info = extract(pdf_bytes, cache_dir)
    except (pymupdf.FileDataError, pymupdf.EmptyFileError, ValueError, RuntimeError) as e:
        return FetchResult(
            verdict=blocked(f"error:{type(e).__name__}"),
            method="pdf-local",
            content_type="application/pdf",
            error=str(e),
        )
    if info.image_only_pdf or len(text) >= MIN_USEFUL_CONTENT_CHARS:
        verdict = OK
    else:
        verdict = Verdict("thin", f"{len(text)}_chars")
    return FetchResult(
        verdict=verdict,
        content=text if verdict.ok else "",
        title=info.title,
        method=info.method,
        content_type="application/pdf",
        image_only_pdf=info.image_only_pdf,
        pdf_path=info.pdf_path,
    )
