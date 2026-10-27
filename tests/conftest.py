"""Add scripts directory to sys.path so tests can import modules."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def _isolate_playwright_browsers_path(monkeypatch):
    """use_repo_browsers() sets the variable for the process; undo it after every test."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "x")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
