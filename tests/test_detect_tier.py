"""Tests for detect_tier.py — infrastructure detection."""

import sys
from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock


def test_check_ollama_not_installed():
    from detect_tier import check_ollama
    with patch("detect_tier.shutil.which", return_value=None):
        result = check_ollama()
    assert result["installed"] is False
    assert result["recommended_model"] is None


def test_check_ollama_installed_but_not_running():
    from detect_tier import check_ollama
    with patch("detect_tier.shutil.which", return_value="/usr/bin/ollama"):
        with patch("detect_tier.subprocess.run", side_effect=Exception("connection refused")):
            result = check_ollama()
    assert result["installed"] is True
    assert result["running"] is False


def test_recommend_model_low_ram():
    from detect_tier import recommend_model
    result = recommend_model(ram_gb=6, vram_gb=0)
    assert result["recommendation"] == "skip"


def test_recommend_model_medium_ram():
    from detect_tier import recommend_model
    result = recommend_model(ram_gb=16, vram_gb=0)
    assert result["recommendation"] == "use"
    assert "7b" in result["model"] or "8b" in result["model"]


def test_recommend_model_high_vram():
    from detect_tier import recommend_model
    result = recommend_model(ram_gb=32, vram_gb=12)
    assert result["recommendation"] == "use"
    assert "14b" in result["model"] or "32b" in result["model"]


def test_check_searxng_not_available():
    from detect_tier import check_searxng
    result = check_searxng(url=None)
    assert result["available"] is False


def test_check_searxng_url_unreachable():
    from detect_tier import check_searxng
    with patch("detect_tier.requests.get", side_effect=Exception("connection refused")):
        result = check_searxng(url="http://localhost:8888")
    assert result["available"] is False


def test_check_ytdlp_installed():
    from detect_tier import check_ytdlp
    with patch("detect_tier.shutil.which", return_value="/usr/bin/yt-dlp"):
        result = check_ytdlp()
    assert result["installed"] is True


def test_check_ytdlp_not_installed():
    from detect_tier import check_ytdlp
    with patch("detect_tier.shutil.which", return_value=None):
        result = check_ytdlp()
    assert result["installed"] is False


def test_detect_tier_base():
    from detect_tier import detect_tier
    with patch("detect_tier.check_ollama", return_value={"installed": False, "running": False, "recommended_model": None}):
        with patch("detect_tier.check_searxng", return_value={"available": False}):
            tier = detect_tier(searxng_url=None)
    assert tier == "base"


def test_detect_tier_mid():
    from detect_tier import detect_tier
    with patch("detect_tier.check_ollama", return_value={"installed": True, "running": True, "recommended_model": "qwen2.5:14b"}):
        with patch("detect_tier.check_searxng", return_value={"available": False}):
            tier = detect_tier(searxng_url=None)
    assert tier == "mid"


def test_detect_tier_full():
    from detect_tier import detect_tier
    with patch("detect_tier.check_ollama", return_value={"installed": True, "running": True, "recommended_model": "qwen2.5:14b"}):
        with patch("detect_tier.check_searxng", return_value={"available": True}):
            tier = detect_tier(searxng_url="http://localhost:8888")
    assert tier == "full"


def _fake_playwright(monkeypatch, exe_path):
    import types
    sync_api = types.ModuleType("playwright.sync_api")

    class _Manager:
        chromium = types.SimpleNamespace(executable_path=str(exe_path))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    sync_api.sync_playwright = lambda: _Manager()
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


def test_playwright_detection_when_unavailable(monkeypatch):
    from detect_tier import check_playwright
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    result = check_playwright()
    assert result["status"] == "missing" and "not installed" in result["reason"]


def test_playwright_detection_needs_chromium(monkeypatch, tmp_path):
    from detect_tier import check_playwright
    _fake_playwright(monkeypatch, tmp_path / "chrome.exe")
    result = check_playwright()
    assert result["status"] == "missing" and "playwright install chromium" in result["install"]


def test_playwright_check_uses_the_repo_browser_cache(monkeypatch, tmp_path):
    import os
    from detect_tier import check_playwright
    _fake_playwright(monkeypatch, tmp_path / "chrome.exe")
    result = check_playwright()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"].replace("\\", "/").endswith(".cache/ms-playwright")
    assert "PLAYWRIGHT_BROWSERS_PATH=" in result["install"] and ".cache/ms-playwright" in result["install"]


def test_importing_detect_tier_or_rw_does_not_load_pymupdf_or_playwright():
    import subprocess
    scripts = str(Path(__file__).parent.parent / "scripts")
    for module in ("detect_tier", "rw"):
        code = (f"import sys; sys.path.insert(0, {scripts!r}); import {module}; "
                "print(sorted(m for m in ('pymupdf', 'fitz', 'playwright') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]", f"{module} imported {out.stdout.strip()}"


def test_playwright_detection_when_available(monkeypatch, tmp_path):
    from detect_tier import check_playwright
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    _fake_playwright(monkeypatch, exe)
    assert check_playwright() == {"status": "ok", "executable": str(exe)}


def test_check_tesseract(monkeypatch):
    import detect_tier
    import pdf_extract
    monkeypatch.setattr(detect_tier.shutil, "which", lambda name: None)
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: False)
    result = detect_tier.check_tesseract()
    assert result == {"status": "missing", "optional": True,
                      "install": "winget install UB-Mannheim.TesseractOCR"}
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: True)
    assert detect_tier.check_tesseract() == {"status": "ok"}
    monkeypatch.setattr(pdf_extract, "tesseract_available", lambda: False)
    monkeypatch.setattr(detect_tier.shutil, "which", lambda name: "C:/Program Files/Tesseract-OCR/tesseract.exe")
    assert detect_tier.check_tesseract() == {"status": "ok"}


def test_tier_report_lists_tesseract_without_changing_tier(monkeypatch):
    import detect_tier
    monkeypatch.setattr(detect_tier, "check_ollama", lambda: {"installed": False, "running": False})
    monkeypatch.setattr(detect_tier, "ensure_searxng", lambda url, root: {"available": False, "url": None})
    monkeypatch.setattr(detect_tier, "check_ytdlp", lambda: {"installed": False})
    monkeypatch.setattr(detect_tier, "check_whisper", lambda: {"installed": False})
    monkeypatch.setattr(detect_tier, "check_playwright", lambda: {"status": "ok"})
    monkeypatch.setattr(detect_tier, "check_tesseract", lambda: {"status": "missing", "optional": True})
    report = detect_tier.build_tier_report(None)
    assert report["components"]["tesseract"]["status"] == "missing"
    assert report["components"]["playwright"]["status"] == "ok"
    assert report["tier"] == "base"
    assert not any("esseract" in m for m in report["missing_for_full"])
