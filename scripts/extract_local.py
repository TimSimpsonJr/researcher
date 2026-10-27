"""
extract_local.py -- Local file text extraction.

Pure extraction: file -> {content, title, file_type, source_path}.
No vault writing, no frontmatter, no Claude calls.
Used by the research pipeline when local files are provided as input.
"""

from pathlib import Path

import pymupdf
import docx  # python-docx

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}


def _extract_pdf(file_path: Path) -> dict:
    """Extract text from PDF via pymupdf, one section per page."""
    doc = pymupdf.open(str(file_path))
    pages = []
    for page in doc:
        text = page.get_text().strip()
        if text:
            pages.append(text)
    content = "\n\n---\n\n".join(pages)
    return {
        "content": content,
        "title": file_path.stem,
        "file_type": "pdf",
        "source_path": str(file_path),
    }


def _extract_docx(file_path: Path) -> dict:
    """Extract text from .docx via python-docx."""
    doc = docx.Document(str(file_path))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    content = "\n\n".join(paragraphs)
    return {
        "content": content,
        "title": file_path.stem,
        "file_type": "docx",
        "source_path": str(file_path),
    }


def _extract_doc(file_path: Path) -> dict:
    """Try python-docx first (handles mislabeled OOXML), then warn."""
    try:
        return _extract_docx(file_path)
    except Exception:
        return {
            "content": f"[Could not extract .doc file: {file_path.name}. "
                       "Install LibreOffice or use .docx format.]",
            "title": file_path.stem,
            "file_type": "doc",
            "source_path": str(file_path),
        }


def extract_file(file_path: Path) -> dict | None:
    """Extract text from a local file. Returns None for unsupported types."""
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        return _extract_pdf(file_path)
    elif ext == ".docx":
        return _extract_docx(file_path)
    elif ext == ".doc":
        return _extract_doc(file_path)
    return None


def extract_folder(folder_path: Path, recursive: bool = False) -> list[dict]:
    """Extract text from all supported files in a folder."""
    results = []
    pattern = "**/*" if recursive else "*"
    for f in folder_path.glob(pattern):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
            result = extract_file(f)
            if result is not None:
                results.append(result)
    return results
