"""Top-level case analyzer — wires heuristics + accumulator + scoring.

Runs at Stage 10d. See docs/plans/2026-05-27-v3-1-case-learning-design.md Section 6.1.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from accumulator import (
    Accumulator,
    AccumulatorEntry,
    load_accumulator,
    save_accumulator,
    record_observation,
    tick_staleness,
    mark_promotion_pending,
    mark_rejected,
    clear_promotion_pending,
    demote,
    remove_entry,  # NEW for partial-write recovery
)
from learned_patterns import (
    LearnedPattern,
    LearnedPatternsFile,
    load_learned_patterns,
    save_learned_patterns,
)
from pattern_detection import (
    detect_source_tier_dominance,
    detect_hop_pattern_lift,
    detect_query_template_recurrence,
)
from score_updates import (
    compute_run_outcome,
    apply_score,
    find_demotion_targets,
)


@dataclass
class AnalyzerResult:
    promotion_candidates: list[AccumulatorEntry] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    score_updates_applied: int = 0
    demotions_applied: int = 0


def analyze(
    *,
    case_path: Path,
    accumulator_path: Path,
    learned_patterns_path: Path,
    cases_dir: Path,
    cases_window: int = 20,
    confidence_target: float = 0.75,
    promotion_threshold: int = 3,
    promotion_threshold_raised: int = 5,
    haiku_dispatch: Callable | None = None,
) -> AnalyzerResult:
    """Run the analyzer at Stage 10d.

    Returns AnalyzerResult listing promotion-eligible candidates and warnings.

    Side effects: updates accumulator.json and learned_patterns.md
    (atomic writes), BUT refuses to write either file if its load step
    produced a corruption or schema-mismatch warning. This prevents silently
    clobbering user-recoverable state with an empty file. The orchestrator
    surfaces the warning text so the user can manually delete the corrupt
    file when ready to reset that store.

    haiku_dispatch is an optional callable for semantic comparison. None means
    skip the semantic compare (conservative-distinct treatment).
    """
    result = AnalyzerResult()

    if not case_path.exists():
        result.warnings.append(f"case_path missing: {case_path}")
        return result

    case = json.loads(case_path.read_text(encoding="utf-8"))

    # Load state (both helpers return (data, warnings); warnings propagate to result)
    accumulator, acc_warnings = load_accumulator(accumulator_path)
    result.warnings.extend(acc_warnings)
    learned, lp_warnings = load_learned_patterns(learned_patterns_path)
    result.warnings.extend(lp_warnings)

    # Hoisted to outer scope: reused by step 4 (heuristic re-detection skip) and
    # step 5 (partial-write recovery — stale promotion_pending reconciliation).
    learned_pattern_ids = {p.id for p in learned.patterns}

    # Compute corruption flags early — they gate cross-file mutations below
    # (so we don't write to one store while skipping the other and lose state).
    acc_corrupt = any(
        w.startswith("accumulator_corrupted") or w.startswith("accumulator_schema_mismatch")
        for w in result.warnings
    )
    lp_corrupt = any(
        w.startswith("learned_patterns_corrupted") or w.startswith("learned_patterns_schema_mismatch")
        for w in result.warnings
    )

    # 1. Score updates for applied_patterns
    outcome = compute_run_outcome(case, confidence_target=confidence_target)
    pattern_index = {p.id: p for p in learned.patterns}
    for pid in case.get("applied_patterns", []):
        if pid in pattern_index:
            apply_score(pattern_index[pid], outcome)
            result.score_updates_applied += 1

    # 2. Demotion sweep — SKIP if accumulator is corrupt. Demoting a pattern
    # removes it from learned_patterns AND places it back into the accumulator;
    # if the accumulator save is going to be skipped (because corrupt), the
    # demoted pattern would disappear from BOTH stores. Defer demotions until
    # the user repairs the accumulator file.
    if acc_corrupt:
        if find_demotion_targets(learned):
            result.warnings.append(
                "demotion_sweep_skipped: accumulator corrupt; deferring demotions until repair"
            )
        demotion_targets = []
    else:
        demotion_targets = find_demotion_targets(learned)
    for target in demotion_targets:
        # Reconstruction-or-update logic mirrors accumulator.demote() so the
        # 2nd-demotion -> rejected rule still fires whether or not the
        # accumulator currently has an entry for this pattern_id.
        existing = next((e for e in accumulator.entries if e.pattern_id == target.id), None)
        new_demotion_count = target.demotion_count + 1
        if existing is None:
            # Pattern was promoted out and accumulator entry was removed.
            # Reconstruct it, applying the same status rules as accumulator.demote().
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            permanent = new_demotion_count >= 2
            accumulator.entries.append(AccumulatorEntry(
                pattern_id=target.id,
                name=target.name,
                category=target.category,
                target_stage=target.target_stage,
                domain_tags=list(target.domain_tags),
                sessions_seen=0,
                sessions_since_last_seen=0,
                status=("rejected" if permanent else "hold"),
                raised_bar=(not permanent),
                promotion_pending=False,
                demotion_count=new_demotion_count,
                evidence=[],
                proposed_promotion_body=target.body,
                created_at=now,
                last_updated_at=now,
            ))
        else:
            # Accumulator entry exists — sync its demotion_count UP to the
            # learned_pattern's count before deferring to demote(). Without
            # this, a heuristic refresh after re-graduation could have created
            # a fresh accumulator entry with demotion_count=0, masking the
            # fact that learned_pattern already has demotion_count=1; the
            # 2nd-demotion -> rejected rule would then never fire.
            existing.demotion_count = max(existing.demotion_count, target.demotion_count)
            demote(accumulator, target.id)
        # Remove from learned regardless of new status (rejected patterns live in accumulator)
        learned.patterns = [p for p in learned.patterns if p.id != target.id]
        result.demotions_applied += 1

    # 3. Candidate detection
    cases = _load_recent_cases(cases_dir, cases_window)
    if not cases:
        cases = [case]
    candidates = (
        detect_source_tier_dominance(cases)
        + detect_hop_pattern_lift(cases)
        + detect_query_template_recurrence(cases)
    )

    # 4. Record observations into accumulator (with optional Haiku semantic merge)
    # Skip candidates whose pattern_id is already graduated. Otherwise the
    # heuristic would re-create the same pattern in the accumulator after each
    # promotion, leading to "duplicate" re-graduations. Patterns present in
    # learned.patterns are by definition graduated (demotion moves them back
    # to the accumulator and removes them from learned).
    # (learned_pattern_ids hoisted to outer scope above — also used by step 5)
    seen_pattern_ids = set()
    for c in candidates:
        # Cheap exact-match check before the semantic-merge dispatch.
        if c["pattern_id"] in learned_pattern_ids:
            continue
        effective_pid = _match_or_create_pattern_id(c, accumulator, haiku_dispatch, result)
        # Defense-in-depth: if semantic-merge resolved to a graduated id, skip too.
        # In practice _match_or_create_pattern_id only walks non-rejected accumulator
        # entries, and graduated patterns are removed from the accumulator at
        # promotion time — so this branch is only reachable if a future change
        # surfaces a graduated id through that path.
        if effective_pid in learned_pattern_ids:
            continue
        for ev in c["evidence_rows"]:
            record_observation(
                accumulator,
                pattern_id=effective_pid,
                name=c["name"],
                category=c["category"],
                target_stage=c["target_stage"],
                domain_tags=c["domain_tags"],
                evidence_row=ev,
                proposed_promotion_body=c["proposed_promotion_body"],
            )
            break  # only one record_observation per candidate per run
        seen_pattern_ids.add(effective_pid)

    tick_staleness(accumulator, seen_pattern_ids)

    # 5. Promotion eligibility (+ contradiction detection)
    def _contradiction_for(entry: AccumulatorEntry) -> dict | None:
        """Return contradiction dict if entry conflicts with any graduated
        pattern in the same (target_stage, domain_tags) bucket, else None."""
        conflicts = [
            p for p in learned.patterns
            if p.target_stage == entry.target_stage
            and (set(p.domain_tags) & set(entry.domain_tags))
        ]
        if not conflicts:
            return None
        return {
            "candidate_pattern_id": entry.pattern_id,
            "candidate_name": entry.name,
            "conflicting_graduated_ids": [p.id for p in conflicts],
            "conflicting_names": [p.name for p in conflicts],
        }

    for entry in accumulator.entries:
        if entry.status not in ("hold", "promotion_pending"):
            continue
        if entry.status == "promotion_pending":
            if entry.pattern_id in learned_pattern_ids:
                # Cross-file transaction recovery: prior Promote landed the
                # learned pattern but the accumulator save didn't complete,
                # leaving a stale promotion_pending entry. Don't re-prompt
                # the user — silently reconcile by removing the entry.
                remove_entry(accumulator, entry.pattern_id)
                continue
            # Re-surface entries left pending by a prior aborted/dismissed
            # Stage 10e prompt. The threshold was already met when status
            # was first flipped to "promotion_pending"; no need to re-check.
            # Recompute contradictions in case learned.patterns changed
            # since the original eligibility check.
            contradiction = _contradiction_for(entry)
            if contradiction:
                result.contradictions.append(contradiction)
            result.promotion_candidates.append(entry)
            continue
        threshold = promotion_threshold_raised if entry.raised_bar else promotion_threshold
        if entry.sessions_seen >= threshold:
            contradiction = _contradiction_for(entry)
            if contradiction:
                result.contradictions.append(contradiction)
            mark_promotion_pending(accumulator, entry.pattern_id)
            result.promotion_candidates.append(entry)

    # 6. Persist selectively — refuse to clobber files that loaded with warnings.
    #    Write order when both are written: learned_patterns FIRST, then accumulator
    #    (if the accumulator write fails after learned_patterns succeeded, the next
    #    run's analyzer reconciles via TWO protections, both keyed on
    #    `learned_pattern_ids`: step 4 skips heuristic re-detection of graduated
    #    pattern_ids, and step 5 removes any stale promotion_pending entry whose
    #    pattern_id has already graduated. Together these prevent double-graduation
    #    and stale re-prompts; the reverse write order would risk losing graduations).
    if not lp_corrupt:
        save_learned_patterns(learned_patterns_path, learned)
    if not acc_corrupt:
        save_accumulator(accumulator_path, accumulator)

    return result


def _load_recent_cases(cases_dir: Path, window: int) -> list[dict]:
    """Load up to `window` most recent case JSON files."""
    if not cases_dir.exists():
        return []
    case_files = sorted(cases_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for cf in case_files[:window]:
        try:
            out.append(json.loads(cf.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            # Tolerant — skip malformed cases. Route to stderr so the
            # JSON-only stdout contract for Stage 10d isn't broken by a
            # single malformed case file polluting the orchestrator's parse.
            print(f"[analyzer] skipping malformed case {cf.name}: {e}", file=sys.stderr)
    return out


def _match_or_create_pattern_id(
    candidate: dict,
    accumulator: Accumulator,
    haiku_dispatch: Callable | None,
    result: "AnalyzerResult",
) -> str:
    """If an accumulator entry exists in the same (domain_tags, category)
    bucket under a different pattern_id and is not rejected, dispatch the
    Haiku semantic-compare subagent. On is_same=True, reuse the existing
    pattern_id so sessions_seen accumulates on the right entry. Otherwise
    return the candidate's own pattern_id.

    haiku_dispatch=None disables semantic merge entirely (conservative —
    fragments near-duplicates rather than risk wrong merge).
    """
    if haiku_dispatch is None:
        return candidate["pattern_id"]
    cand_tag_set = set(candidate["domain_tags"])
    for existing in accumulator.entries:
        if existing.pattern_id == candidate["pattern_id"]:
            continue
        if existing.status == "rejected":
            continue
        if existing.category != candidate["category"]:
            continue
        if set(existing.domain_tags) != cand_tag_set:
            continue
        try:
            verdict = haiku_dispatch(
                candidate_body=candidate["proposed_promotion_body"],
                existing_body=existing.proposed_promotion_body,
            )
        except Exception as e:
            result.warnings.append(f"haiku_dispatch_error: {e}; treating candidate as distinct")
            continue
        if verdict.get("is_same"):
            return existing.pattern_id
    return candidate["pattern_id"]


def dispatch_semantic_compare(
    *,
    candidate_body: str,
    existing_body: str,
    timeout_s: float = 30.0,
) -> dict:
    """Dispatch the case-analyzer Haiku subagent for semantic comparison.

    Returns {"is_same": bool, "reason": str}. On timeout, returns conservative
    {"is_same": false, "reason": "timeout — treating as distinct"}.

    NOTE: actual Haiku dispatch is via the orchestrator's Task tool at runtime;
    this helper is here for contract testing. In production the orchestrator
    constructs the Task tool dispatch directly. This subprocess.run wrapper
    exists only so the contract test can mock it and pin the prompt shape.
    """
    prompt = (
        "CANDIDATE\n"
        f"{candidate_body}\n\n"
        "EXISTING\n"
        f"{existing_body}\n"
    )
    try:
        proc = subprocess.run(
            ["claude", "task", "--agent", "case-analyzer"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        return {"is_same": False, "reason": "timeout — treating as distinct"}
    except (json.JSONDecodeError, FileNotFoundError) as e:
        return {"is_same": False, "reason": f"dispatch error: {e}"}


# --- Stage 10e graduation (caller holds state.acquire_state_lock) ------------

def _corrupt(name: str, warnings: list[str]) -> str:
    return f"refusing to save over corrupt {name}: {warnings}"


def promote_candidate(
    pattern_id: str, *, accumulator_path: Path, learned_patterns_path: Path
) -> str | None:
    """Promote an accumulator entry into learned_patterns.md.

    Returns None on success, or an abort reason when either store loaded with
    warnings (saving would clobber a user-recoverable corrupt file). Write
    order: learned_patterns first, then accumulator; an id already present in
    learned_patterns is not re-added (recovery from a promotion that wrote one
    file but not the other). Raises KeyError if the id is not in the accumulator.
    """
    lp, lp_warnings = load_learned_patterns(learned_patterns_path)
    if lp_warnings:
        return _corrupt("learned_patterns.md", lp_warnings)
    acc, acc_warnings = load_accumulator(accumulator_path)
    if acc_warnings:
        return _corrupt("accumulator.json", acc_warnings)
    entry = next((e for e in acc.entries if e.pattern_id == pattern_id), None)
    if entry is None:
        raise KeyError(f"pattern not in accumulator: {pattern_id}")
    if not any(p.id == entry.pattern_id for p in lp.patterns):
        lp.patterns.append(LearnedPattern(
            id=entry.pattern_id,
            name=entry.name,
            body=entry.proposed_promotion_body,
            domain_tags=entry.domain_tags,
            target_stage=entry.target_stage,
            category=entry.category,
            wins=0,
            losses=0,
            promoted_at=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            demotion_count=entry.demotion_count,
        ))
        save_learned_patterns(learned_patterns_path, lp)
    remove_entry(acc, pattern_id)
    save_accumulator(accumulator_path, acc)
    return None


def reject_candidate(pattern_id: str, *, accumulator_path: Path) -> str | None:
    """Permanently reject an accumulator entry. Abort reason on a corrupt store."""
    acc, acc_warnings = load_accumulator(accumulator_path)
    if acc_warnings:
        return _corrupt("accumulator.json", acc_warnings)
    mark_rejected(acc, pattern_id)
    save_accumulator(accumulator_path, acc)
    return None


def hold_candidate(pattern_id: str, *, accumulator_path: Path) -> str | None:
    """Clear promotion_pending (status back to hold). Abort reason on a corrupt store."""
    acc, acc_warnings = load_accumulator(accumulator_path)
    if acc_warnings:
        return _corrupt("accumulator.json", acc_warnings)
    clear_promotion_pending(acc, pattern_id)
    save_accumulator(accumulator_path, acc)
    return None
