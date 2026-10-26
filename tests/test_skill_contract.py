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

from pathlib import Path


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
