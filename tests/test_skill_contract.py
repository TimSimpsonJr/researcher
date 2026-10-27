# tests/test_skill_contract.py
"""Contract tests for skills/researcher/SKILL.md.

SKILL.md is interpreted by the orchestrator at runtime as instructions for
how to assemble bash snippets and gate stage transitions. The Python code in
this repo cannot exercise that markdown directly, so we pin the load-bearing
shape with text-level assertions. A future edit that silently drops one of
these fields will fail the contract test.

These tests are regressions for the codex impl-review findings on v3.1.0
case-based pattern learning:
- score-loop cluster: the Stage 10c case record must include `applied_patterns`
  (now built by state.build_case_record behind rw.py write-case-record)
- graduation-write-path cluster: Stage 10d/10e must defend against
  accumulator_* warnings in addition to learned_patterns_* warnings.
"""

import argparse
import re
from pathlib import Path

import pytest

import rw


_SKILL_MD = Path(__file__).parent.parent / "skills" / "researcher" / "SKILL.md"


def test_skill_md_stage_10c_case_record_carries_applied_patterns():
    """Stage 10c builds the case via rw.py write-case-record ->
    state.build_case_record, which must carry the run's applied_patterns.
    Without it the Stage 10d score/demotion loop is a no-op in production
    (case.get('applied_patterns', []) returns []). Regression for
    codex-impl-review finding score-loop cluster.
    """
    from state import build_case_record
    text = _SKILL_MD.read_text(encoding="utf-8")
    assert "write-case-record" in text, "Stage 10c must call rw.py write-case-record"
    case = build_case_record({"run_id": "r1", "topics": [], "applied_patterns": ["p1"]}, {"query": "q"})
    assert case["applied_patterns"] == ["p1"]


def test_skill_md_stage_10d_gates_on_accumulator_warnings():
    """Stage 10d must skip Stage 10e on accumulator_* warnings, not just
    learned_patterns_* warnings. Regression for codex-impl-review finding
    graduation-write-path cluster.
    """
    text = _SKILL_MD.read_text(encoding="utf-8")
    # The Stage 10e gating condition must reference both warning families
    # Find the gating sentence (heuristic: search for "skip Stage 10e")
    gating_idx = text.find("skip Stage 10e")
    assert gating_idx > 0, "Stage 10e gating instruction not found"
    # Look at the surrounding ~500 chars for both keywords
    context = text[max(0, gating_idx - 250):gating_idx + 250]
    assert "learned_patterns_" in context, "gate must mention learned_patterns_* warnings"
    assert "accumulator_" in context, "gate must mention accumulator_* warnings"


def test_skill_md_stage_10e_branches_use_guarded_commands():
    """All three Stage 10e branches (Promote, Reject, Hold) run rw.py
    promote-/reject-/hold-pattern, whose case_analyzer functions refuse to
    save over a corrupt store (BRANCH_ABORTED). The refusal itself is
    tested in tests/test_rw_cli.py.
    """
    text = _SKILL_MD.read_text(encoding="utf-8")
    for cmd in ("promote-pattern", "reject-pattern", "hold-pattern"):
        assert f'rw.py" {cmd} --pattern-id' in text, f"Stage 10e must call rw.py {cmd}"
    assert "BRANCH_ABORTED" in text


_REPO = Path(__file__).parent.parent
_SCANNED = sorted([
    *(_REPO / "skills").glob("*/SKILL.md"),
    *(_REPO / "agents").glob("*.md"),
    *(_REPO / "lib").rglob("*.js"),
    *(_REPO / ".claude" / "workflows").glob("*.js"),
])
_PLACEHOLDERS = ("{{REPO_ROOT}}", "{{VAULT_ROOT}}", "{{PYTHON_PATH}}", "{{SCRIPTS_DIR}}")
# A python/python3 command: followed by a flag, a quote, or a .py path.
_BARE_PYTHON = re.compile(r"(?<![\w./-])python3?(?:\.exe)?[ \t]+(?:-|\"|'|\S+\.py\b)")
_RW_CALL = re.compile(r"rw\.py[\"']?[ \t]+([a-z][a-z-]*)")
# A real uv invocation: `uv run ...` / `uv sync ...` at a command position.
_UV_CALL = re.compile(r"(?<![\w./-])uv[ \t]+(?:run|sync)\b")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\|")


def _bare_python_hits(text: str) -> list[str]:
    return [line for line in text.splitlines() if _BARE_PYTHON.search(line)]


def _unfrozen_uv_hits(text: str) -> list[str]:
    hits = []
    for line in text.splitlines():
        for seg in _SEGMENT_SPLIT.split(line):
            m = _UV_CALL.search(seg)
            if m and "--frozen" not in seg[m.start():]:
                hits.append(seg.strip())
    return hits


def _rw_names(text: str) -> list[str]:
    # Only uv invocations (a segment split on shell operators and backticks that
    # holds `uv run`); skips prose like "rw.py cannot find it" on the same line.
    return [
        n
        for line in text.splitlines()
        for seg in re.split(r"&&|\|\||;|\||`", line)
        if _UV_CALL.search(seg)
        for n in _RW_CALL.findall(seg)
    ]


def _rw_subcommands() -> set[str]:
    parser = rw.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(sub.choices)


def _rel(p: Path) -> str:
    return p.relative_to(_REPO).as_posix()


def test_scanner_catches_known_bad_lines():
    bad = [
        'python -c "import sys"',
        'python "SCRIPTS/fetch_and_clean.py" --input x',
        '  python3 -m pip install yt-dlp',
        'Run `python migrate.py --vault v`',
    ]
    good = [
        "```python",
        "researcher Python scripts",
        'uv run --frozen --project "REPO" "REPO/scripts/rw.py" locate',
        "neither `python` nor the scripts dir",
    ]
    assert all(_bare_python_hits(line) for line in bad)
    assert not any(_bare_python_hits(line) for line in good)
    assert _unfrozen_uv_hits('uv run --project "R" "R/scripts/rw.py" locate')
    assert _unfrozen_uv_hits('uv sync --project "R"')
    assert not _unfrozen_uv_hits('uv run --frozen --project "R" "R/scripts/rw.py" locate')
    assert not _unfrozen_uv_hits("uv --version")
    assert _unfrozen_uv_hits('uv sync --project R && uv run --frozen --project R x.py')
    assert _unfrozen_uv_hits('uv run --frozen --project R x.py; uv run --project R y.py')
    assert not _unfrozen_uv_hits('uv sync --frozen --project R && uv run --frozen x.py | cat')
    assert _rw_names('uv run --frozen --project R R/scripts/rw.py bogus-cmd') == ["bogus-cmd"]
    assert _rw_names("uv run --frozen --project R 'R/scripts/rw.py' bogus-cmd") == ["bogus-cmd"]
    assert _rw_names('uv run --frozen --project R "R/scripts/rw.py" locate') == ["locate"]
    assert _rw_names("so rw.py cannot find it") == []


def test_scanned_files_present():
    names = {p.name for p in _SCANNED}
    assert {"SKILL.md", "hop-planner.md", "fetch-summarize-runner.md", "research-batch.js"} <= names


@pytest.mark.parametrize("path", _SCANNED, ids=_rel)
def test_no_inline_or_bare_python(path):
    text = path.read_text(encoding="utf-8")
    assert "python -c" not in text and "python3 -c" not in text
    assert _bare_python_hits(text) == []
    assert "python_path" not in text and "sys.executable" not in text


@pytest.mark.parametrize("path", _SCANNED, ids=_rel)
def test_uv_calls_are_frozen(path):
    assert _unfrozen_uv_hits(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", _SCANNED, ids=_rel)
def test_no_unfilled_placeholders(path):
    text = path.read_text(encoding="utf-8")
    assert [p for p in _PLACEHOLDERS if p in text] == []


def test_every_rw_subcommand_referenced_exists():
    known = _rw_subcommands()
    used: dict[str, str] = {}
    for path in _SCANNED:
        for name in _rw_names(path.read_text(encoding="utf-8")):
            used.setdefault(name, _rel(path))
    assert used, "expected rw.py calls in skills/agents"
    assert {n: f for n, f in used.items() if n not in known} == {}


_SETUP_MD = _REPO / "skills" / "researcher-setup" / "SKILL.md"
_RUNNER_MD = _REPO / "agents" / "fetch-summarize-runner.md"
_INSTALL = ('PLAYWRIGHT_BROWSERS_PATH="REPO/.cache/ms-playwright" '
            'uv run --frozen --project "REPO" playwright install chromium')


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def test_scanner_accepts_env_prefix_but_still_rejects_bad_lines():
    assert _unfrozen_uv_hits(_INSTALL) == []
    assert _bare_python_hits(_INSTALL) == []
    assert _unfrozen_uv_hits(_INSTALL.replace("--frozen ", "")) != []
    assert _unfrozen_uv_hits('PLAYWRIGHT_BROWSERS_PATH="R" uv sync --project "R"') != []
    assert _bare_python_hits('PLAYWRIGHT_BROWSERS_PATH="R" python -c "x"') != []
    assert _bare_python_hits('PLAYWRIGHT_BROWSERS_PATH="R" python tool.py') != []
    assert _rw_names(
        'PLAYWRIGHT_BROWSERS_PATH="R" uv run --frozen --project "R" "R/scripts/rw.py" bogus-cmd'
    ) == ["bogus-cmd"]


def test_stage_4b_browser_pass_routes_needs_browser():
    s = _section(_SKILL_MD.read_text(encoding="utf-8"), "### 4b. Fetch", "### 4c. Media")
    for needle in (
        "needs_browser", "add-browser-fetch", "add-manual-url", "CAPTCHA",
        "mcp__Claude_Browser__navigate", "mcp__Claude_Browser__get_page_text",
        "mcp__Claude_Browser__javascript_tool",
        "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.0.379/pdf.min.mjs",
        "pdf.worker.min.mjs", "after the browser pass",
    ):
        assert needle in s, needle


def test_stage_4d_and_runner_read_image_only_pdfs():
    s = _section(_SKILL_MD.read_text(encoding="utf-8"), "### 4d. Summarize", "### 4e.")
    assert "image_only_pdf" in s and "pdf_path" in s and "needs visual read" in s
    runner = _RUNNER_MD.read_text(encoding="utf-8")
    assert "image_only_pdf" in runner and "pdf_path" in runner and "needs_browser" in runner


def test_stage_6a_drops_unread_visual_items():
    s = _section(_SKILL_MD.read_text(encoding="utf-8"), "### 6a. Aggregate", "### 6b.")
    assert "needs visual read" in s and "usable" in s


def test_stage_10b_lists_manual_urls():
    s = _section(_SKILL_MD.read_text(encoding="utf-8"), "### 10b. Print summary", "### 10c.")
    assert "manual_urls" in s


def test_setup_installs_chromium_and_checks_tesseract():
    setup = _SETUP_MD.read_text(encoding="utf-8")
    assert _INSTALL in setup
    assert 'rw.py" check playwright' in setup and 'rw.py" check tesseract' in setup
    assert "winget install UB-Mannheim.TesseractOCR" in setup
