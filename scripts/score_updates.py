"""Run-level W/L computation + score updates on learned_patterns.md.

See docs/plans/2026-05-27-v3-1-case-learning-design.md Section 6.1.
"""

from __future__ import annotations

from learned_patterns import LearnedPattern, LearnedPatternsFile


def compute_run_outcome(case: dict, *, confidence_target: float = 0.75) -> str:
    """Compute a W or L verdict for a completed run.

    Win = avg confidence across topics >= target
        AND no user abandonment at quality gate
        AND contradiction_rate <= 0.3.
    Loss = any of those fail.
    """
    cpt = case.get("confidence_per_topic", {}) or {}
    if cpt:
        vals = [v for v in cpt.values() if isinstance(v, (int, float))]
        avg_conf = sum(vals) / len(vals) if vals else 0.0
    else:
        avg_conf = 0.0
    if avg_conf < confidence_target:
        return "L"

    contradiction = case.get("contradiction_rate", 0.0)
    if contradiction > 0.3:
        return "L"

    decisions = (case.get("outcomes", {}) or {}).get("user_decisions", []) or []
    for d in decisions:
        if d.get("decision") == "abandon":  # key written by state.record_user_decision
            return "L"
    return "W"


def apply_score(pattern: LearnedPattern, outcome: str) -> None:
    """Increment wins or losses on a LearnedPattern in place."""
    if outcome == "W":
        pattern.wins += 1
    elif outcome == "L":
        pattern.losses += 1
    else:
        raise ValueError(f"outcome must be 'W' or 'L', got {outcome!r}")


def find_demotion_targets(
    file: LearnedPatternsFile,
    *,
    min_uses: int = 5,
    max_loss_ratio: float = 0.4,
) -> list[LearnedPattern]:
    """Return patterns eligible for demotion: total uses >= min_uses AND
    W / (W+L) < max_loss_ratio.
    """
    targets = []
    for p in file.patterns:
        total = p.wins + p.losses
        if total < min_uses:
            continue
        ratio = p.wins / total if total > 0 else 0.0
        if ratio < max_loss_ratio:
            targets.append(p)
    return targets
