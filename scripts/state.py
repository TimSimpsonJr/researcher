"""
state.py — Pipeline state management with crash recovery.

Tracks pipeline progress via JSON checkpoints. Supports resume,
restart, and abandon flows. State lives in the vault at
{vault}/.researcher/state/.
"""

import json
import os
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from confidence import get_depth_profile

CURRENT_RUN_FILE = "current_run.json"
STATE_VERSION = 3


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically via temp file + rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def write_shared_state_atomically(target: Path, content: dict | str) -> None:
    """Write content to target via temp-file-then-rename. Creates parent dir.

    Accepts dict (serialized as JSON with indent=2) or str (written verbatim).
    Reuses _atomic_write's discipline: no partial writes survive a crash.

    Public helper for vault-level shared state files (accumulator.json,
    learned_patterns.md). The private _atomic_write remains dedicated to the
    per-run state file (current_run.json).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict):
        body = json.dumps(content, indent=2, ensure_ascii=False)
    elif isinstance(content, str):
        body = content
    else:
        raise TypeError(f"content must be dict or str, got {type(content).__name__}")
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(target)


class LockTimeoutError(Exception):
    """Raised when state lock can't be acquired within the timeout."""


@contextmanager
def acquire_state_lock(state_root: Path, timeout_s: float = 5.0,
                       stale_after_hours: int = 1):
    """Acquire an exclusive lock for shared-state writes (accumulator,
    learned_patterns). Returns a context manager.

    Lock file at {state_root}/.lock contains: {pid}\\n{iso_timestamp}\\n

    Stale locks (timestamp older than stale_after_hours) are forcibly cleared.
    Otherwise, waits up to timeout_s for the lock to free up.
    Raises LockTimeoutError on timeout.
    """
    import time
    state_root.mkdir(parents=True, exist_ok=True)
    lock_file = state_root / ".lock"
    deadline = time.monotonic() + timeout_s

    while True:
        # Check for stale lock
        if lock_file.exists():
            try:
                content = lock_file.read_text().strip().split("\n")
                if len(content) == 2:
                    _, ts_str = content
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age = datetime.now(timezone.utc) - ts
                    if age > timedelta(hours=stale_after_hours):
                        lock_file.unlink()  # break stale lock
            except (ValueError, OSError):
                # malformed lock file — treat as stale
                try:
                    lock_file.unlink()
                except OSError:
                    pass

        # Try to acquire
        try:
            # Atomic create with O_EXCL semantics via exclusive write
            with open(lock_file, "x") as f:
                f.write(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n")
            try:
                yield
            finally:
                try:
                    lock_file.unlink()
                except OSError:
                    pass
            return
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise LockTimeoutError(
                    f"Could not acquire state lock at {lock_file} within "
                    f"{timeout_s}s — another /research run may be in progress."
                )
            time.sleep(0.1)


def save_state(state_dir: Path, run: dict) -> None:
    """Save the active run state atomically. Public wrapper around _atomic_write."""
    _atomic_write(state_dir / CURRENT_RUN_FILE, run)


def record_hop(state_dir: Path, topic_name: str, hop_data: dict) -> None:
    """Append a hop record to the topic's genealogy and increment current_hop."""
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    for t in run["topics"]:
        if t["topic"] == topic_name:
            t["hop_genealogy"].append(hop_data)
            t["current_hop"] += 1
            break
    else:
        raise KeyError(f"Topic not found: {topic_name}")
    save_state(state_dir, run)


def mark_topic_status(state_dir: Path, topic_name: str, status: str) -> None:
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    for t in run["topics"]:
        if t["topic"] == topic_name:
            t["status"] = status
            break
    else:
        raise KeyError(f"Topic not found: {topic_name}")
    save_state(state_dir, run)


def append_confidence(state_dir: Path, topic_name: str, score: float) -> None:
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    for t in run["topics"]:
        if t["topic"] == topic_name:
            t["confidence_history"].append(score)
            break
    else:
        raise KeyError(f"Topic not found: {topic_name}")
    save_state(state_dir, run)


def set_contradiction_rate(state_dir: Path, topic_name: str, rate: float) -> None:
    """Overwrite the topic's contradiction_rate with the latest measurement.

    Setter rather than history-tracker — the rate is recomputed from all hops'
    summaries each pass, so only the latest value is meaningful.
    """
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    for t in run["topics"]:
        if t["topic"] == topic_name:
            t["contradiction_rate"] = rate
            break
    else:
        raise KeyError(f"Topic not found: {topic_name}")
    save_state(state_dir, run)


def set_replan_hint(state_dir: Path, topic_name: str, hint: dict | None) -> None:
    """Set or clear the topic's replan_hint (read by Stage 5b auto-replan)."""
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    for t in run["topics"]:
        if t["topic"] == topic_name:
            t["replan_hint"] = hint
            break
    else:
        raise KeyError(f"Topic not found: {topic_name}")
    save_state(state_dir, run)


def set_next_hop(state_dir: Path, topic_name: str, next_hop: dict | None) -> None:
    """Set or clear the topic's next_hop (read by Stage 4a on the next iteration).

    Called after a hop-planner decision="continue" to record the pattern/from/rationale
    that should drive the next search. Cleared after consumption in Stage 4a.
    """
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    for t in run["topics"]:
        if t["topic"] == topic_name:
            t["next_hop"] = next_hop
            break
    else:
        raise KeyError(f"Topic not found: {topic_name}")
    save_state(state_dir, run)


def bump_max_hops(state_dir: Path, topic_name: str, increment: int = 1) -> None:
    """Increase the topic's hop budget.

    Used by Stage 5b auto-replan to give a topic another hop after it exhausts
    its initial budget; without this, the Stage 4 admission check
    (current_hop < max_hops) would silently filter the topic out.
    """
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    for t in run["topics"]:
        if t["topic"] == topic_name:
            t["max_hops"] += increment
            break
    else:
        raise KeyError(f"Topic not found: {topic_name}")
    save_state(state_dir, run)


def increment_replan(state_dir: Path) -> None:
    """Increment the run's replan counter. Silently no-ops if no active run."""
    run = load_run(state_dir)
    if run is None:
        return
    run["replan_count"] += 1
    save_state(state_dir, run)


def add_seen_urls(state_dir: Path, topic_name: str, urls: list[str]) -> None:
    """Extend a topic's seen_urls with newly-fetched URLs, deduplicated.

    Called by Stage 4b after fetch_and_clean.py reports which URLs succeeded.
    The list ordering is preserved (existing entries first, new entries appended),
    and duplicates are skipped — the orchestrator can pass the raw fetch output
    without pre-filtering.
    """
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    for t in run["topics"]:
        if t["topic"] == topic_name:
            existing = set(t["seen_urls"])
            for url in urls:
                if url not in existing:
                    t["seen_urls"].append(url)
                    existing.add(url)
            break
    else:
        raise KeyError(f"Topic not found: {topic_name}")
    save_state(state_dir, run)


_RESERVED_DECISION_KEYS = {"decision", "at"}


def record_user_decision(state_dir: Path, decision: str, **details) -> None:
    """Append a user-decision entry to the run's user_decisions log.

    Extra details (e.g., confidence, reason) are merged into the entry alongside
    the decision label and timestamp. Silently no-ops if no active run.

    Raises TypeError if `details` contains reserved keys ("decision", "at") —
    those would silently override the canonical fields via dict-spread order.
    """
    reserved = _RESERVED_DECISION_KEYS & details.keys()
    if reserved:
        raise TypeError(
            f"record_user_decision: details cannot contain reserved keys: {sorted(reserved)}"
        )
    run = load_run(state_dir)
    if run is None:
        return
    run["user_decisions"].append({
        "decision": decision,
        "at": datetime.now(timezone.utc).isoformat(),
        **details,
    })
    save_state(state_dir, run)


def record_applied_pattern(state_dir: Path, pattern_id: str) -> None:
    """Append a pattern_id to the current run's applied_patterns list.

    Idempotent: duplicate pattern_ids are not re-added. Used by the
    orchestrator at each subagent dispatch to record which learned patterns
    were injected into a prompt during the run. At end of run, the analyzer
    reads applied_patterns from the case to compute W/L scores.

    Silently no-ops if there is no active run (telemetry shouldn't block
    the pipeline). Uses setdefault so v3.0.0 runs resumed under v3.1.0
    code still work even if the field was never initialized.
    """
    run = load_run(state_dir)
    if run is None:
        return
    applied = run.setdefault("applied_patterns", [])
    if pattern_id not in applied:
        applied.append(pattern_id)
        _atomic_write(state_dir / CURRENT_RUN_FILE, run)


_KNOWN_USAGE_MODELS = {"haiku", "sonnet", "opus", "ollama"}


def add_usage(state_dir: Path, model: str, in_tokens: int, out_tokens: int, stage: str) -> None:
    """Increment per-model usage counters for the active run.

    Safe against missing keys on the ollama bucket (which lacks token fields by
    design — local inference has no token cost). Uses dict.get(key, 0) + rather
    than += to keep ollama incrementing safely. Silently no-ops if there is no
    active run (telemetry shouldn't block the pipeline).

    Raises ValueError on unknown model names — prevents typos like "haku" from
    silently creating ghost buckets that would distort cost estimates.

    `stage` is currently unused but kept in the signature for future
    per-stage breakdown.
    """
    if model not in _KNOWN_USAGE_MODELS:
        raise ValueError(
            f"add_usage: unknown model {model!r}; expected one of {sorted(_KNOWN_USAGE_MODELS)}"
        )
    run = load_run(state_dir)
    if run is None:
        return
    bucket = run["usage"].setdefault(model, {"calls": 0})
    bucket["calls"] = bucket.get("calls", 0) + 1
    if model != "ollama":
        bucket["in_tokens"] = bucket.get("in_tokens", 0) + in_tokens
        bucket["out_tokens"] = bucket.get("out_tokens", 0) + out_tokens
    save_state(state_dir, run)


def apply_replan_readmit(
    state_dir: Path,
    failing_topic_specs: list[dict],
    *,
    increment_replan_counter: bool = True,
) -> None:
    """Atomic re-admission for Stage 5b/5c. Re-marks topics active, bumps max_hops,
    sets replan_hints (when provided), increments replan_count, sets stage='hop_loop'.

    All mutations in one load->mutate->save cycle so a crash during re-admission
    cannot leave the run with topics flagged active but stage stuck at quality_gate.

    `failing_topic_specs` is a list of dicts shaped:
        {"topic_name": str, "replan_hint": dict | None}

    If `replan_hint` is None the topic's existing hint is preserved (don't clobber
    a prior hop-planner replan_hint with None). If `replan_hint` is a dict it
    overwrites whatever was there.

    Raises KeyError if any topic_name does not exist in the run.
    Raises RuntimeError if there is no active run.
    """
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    topics_by_name = {t["topic"]: t for t in run["topics"]}
    for spec in failing_topic_specs:
        name = spec["topic_name"]
        if name not in topics_by_name:
            raise KeyError(f"Topic not found: {name}")
        t = topics_by_name[name]
        if spec.get("replan_hint") is not None:
            t["replan_hint"] = spec["replan_hint"]
        t["max_hops"] += 1
        t["status"] = "active"
    if increment_replan_counter:
        run["replan_count"] += 1
    run["stage"] = "hop_loop"
    save_state(state_dir, run)


def apply_hop_decision(
    state_dir: Path,
    topic_name: str,
    hop_data: dict,
    decision: str,
    confidence_score: float,
    contradiction_rate: float,
    next_hop: dict | None = None,
    replan_hint: dict | None = None,
) -> None:
    """Atomically apply the full state transition for one hop-planner decision.

    Combines record_hop + set_next_hop + set_replan_hint + mark_topic_status
    + append_confidence + set_contradiction_rate into a single
    load -> mutate -> save cycle. On disk, the transition is all-or-nothing
    (the single save_state at the end writes atomically via temp+rename).
    A crash mid-function — before save_state runs — discards the entire
    transition; resume re-runs the hop from its prior on-disk state.

    `decision` is one of: "continue", "stop", "early_terminated", "replan".

    Stage 4e of the orchestrator should always call this rather than the
    per-field setters when applying a hop-planner response. The quality
    signals (confidence_score, contradiction_rate) come directly from the
    hop-planner JSON and are required arguments.
    """
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    for t in run["topics"]:
        if t["topic"] == topic_name:
            # Always: record the hop, advance current_hop, persist quality signals
            t["hop_genealogy"].append(hop_data)
            t["current_hop"] += 1
            t["confidence_history"].append(confidence_score)
            t["contradiction_rate"] = contradiction_rate
            # Decision-specific transitions
            if decision == "continue":
                t["next_hop"] = next_hop
                t["replan_hint"] = None   # clear any stale replan_hint
                # status stays "active"
            elif decision == "stop":
                t["next_hop"] = None
                t["status"] = "complete"
            elif decision == "early_terminated":
                t["next_hop"] = None
                t["status"] = "early_terminated"
            elif decision == "replan":
                t["next_hop"] = None
                t["replan_hint"] = replan_hint
                t["status"] = "replan_pending"
            else:
                raise ValueError(f"Unknown decision: {decision!r}")
            break
    else:
        raise KeyError(f"Topic not found: {topic_name}")
    save_state(state_dir, run)


QUALITY_MAX_CONTRADICTION = 0.3


def _latest_confidence(topic: dict) -> float:
    return topic["confidence_history"][-1] if topic["confidence_history"] else 0.0


def topic_passes(topic: dict) -> bool:
    """Stage 5a: latest confidence >= the depth's confidence_target AND
    contradiction_rate <= 0.3. A topic with no confidence yet scores 0.0."""
    target = get_depth_profile(topic["depth"])["confidence_target"]
    return (_latest_confidence(topic) >= target
            and topic["contradiction_rate"] <= QUALITY_MAX_CONTRADICTION)


def quality_gate(run: dict) -> dict:
    """Stage 5a per-topic pass/fail for the run.

    worst_confidence (the lowest latest confidence, 0.0 if none) is the
    OVERALL_CONFIDENCE surfaced in Stage 5c prompts and 5d's
    final_confidence_score -- depths are per-topic, so there is no single
    run target.
    """
    topics = [
        {
            "topic": t["topic"],
            "depth": t["depth"],
            "confidence": _latest_confidence(t),
            "contradiction_rate": t["contradiction_rate"],
            "replan_hint": t.get("replan_hint"),
            "passes": topic_passes(t),
        }
        for t in run["topics"]
    ]
    return {
        "passed": all(t["passes"] for t in topics),
        "failing_topics": [t for t in topics if not t["passes"]],
        "topics": topics,
        "worst_confidence": min((t["confidence"] for t in topics), default=0.0),
        "replan_count": run["replan_count"],
    }


def aggregate_hop_summaries(state_dir: Path, project: str) -> dict:
    """Stage 6a: merge every summaries_hop*.json's items into summaries.json."""
    items: list = []
    for hop_file in sorted(state_dir.glob("summaries_hop*.json")):
        items.extend(json.loads(hop_file.read_text(encoding="utf-8")).get("items", []))
    combined = {"topic": project, "items": items}
    save_stage_output(state_dir, "summaries", combined)
    return combined


def aggregate_fetch_results(state_dir: Path) -> dict:
    """Stage 7a: merge fetch_results_hop*.json into fetch_results_aggregated.json
    as {fetched, by_url}. Later hops win on URL collisions. Written atomically
    (temp + rename) so a crash never leaves a half-written file for resume."""
    by_url: dict = {}
    for hop_file in sorted(state_dir.glob("fetch_results_hop*.json")):
        for entry in json.loads(hop_file.read_text(encoding="utf-8")).get("fetched", []):
            by_url[entry["url"]] = entry
    combined = {"fetched": list(by_url.values()), "by_url": by_url}
    save_stage_output(state_dir, "fetch_results_aggregated", combined)
    return combined


def note_modified_since(path: Path, since_iso: str) -> dict:
    """Stage 7c.i mtime guard: {'conflict': bool, 'mtime': str | None}.

    conflict is True when the note exists and was modified after since_iso
    (a naive timestamp is treated as UTC).
    """
    if not path.exists():
        return {"conflict": False, "mtime": None}
    since = datetime.fromisoformat(since_iso)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    file_time = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return {"conflict": file_time > since, "mtime": str(file_time)}


def init_topic(topic: str, mode: str, depth: str) -> dict:
    """Create a fresh topic state entry for the run.

    The 12 fields cover identity (topic/mode/depth), hop-loop budget
    (max_hops/current_hop), lifecycle (status), audit trail (hop_genealogy),
    quality signals (confidence_history/contradiction_rate), dedup (seen_urls),
    and forward routing (next_hop for "continue", replan_hint for "replan").
    next_hop and replan_hint are mutually exclusive in practice.
    """
    profile = get_depth_profile(depth)
    return {
        "topic": topic,
        "mode": mode,
        "depth": depth,
        "max_hops": profile["max_hops"],
        "current_hop": 0,
        "status": "active",
        "hop_genealogy": [],
        "confidence_history": [],
        "contradiction_rate": 0.0,
        "seen_urls": [],
        "replan_hint": None,
        "next_hop": None,
    }


def create_run(state_dir: Path, run_id: str, tier: str) -> dict:
    """Create a new run. Archives any completed previous run automatically.

    Raises FileExistsError only if an *incomplete* run is still active.
    Completed runs are archived to history/ and a fresh run starts.
    """
    run_file = state_dir / CURRENT_RUN_FILE
    if run_file.exists():
        existing = json.loads(run_file.read_text(encoding="utf-8"))
        if existing.get("completed_at"):
            # Previous run finished — archive it and continue
            _archive_run(state_dir)
        else:
            raise FileExistsError(
                f"Active incomplete run exists: {existing.get('run_id', 'unknown')}. "
                f"Resume, restart, or abandon it first."
            )
    state_dir.mkdir(parents=True, exist_ok=True)
    run = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stage": "triage",
        "version": STATE_VERSION,
        "stage_progress": {},
        "tier_detected": tier,
        "plan_approved": False,
        "usage": {
            "haiku":  {"calls": 0, "in_tokens": 0, "out_tokens": 0},
            "sonnet": {"calls": 0, "in_tokens": 0, "out_tokens": 0},
            "opus":   {"calls": 0, "in_tokens": 0, "out_tokens": 0},
            "ollama": {"calls": 0},
        },
        "replan_count": 0,
        "user_decisions": [],
        "applied_patterns": [],
    }
    _atomic_write(run_file, run)
    return run


def load_run(state_dir: Path) -> dict | None:
    """Load current run, or None if no active run.

    If the on-disk schema version does not match STATE_VERSION, archive the
    stale file in-place and return None. We archive directly here (without
    calling abandon_run -> _archive_run -> load_run) to avoid infinite
    recursion on schema-mismatch files.
    """
    state_file = state_dir / CURRENT_RUN_FILE
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if data.get("version") != STATE_VERSION:
        print(
            f"Your in-flight run was on an older schema (v{data.get('version', 'unknown')}) "
            f"and has been abandoned. Run /research to start fresh.",
            file=sys.stderr,
        )
        # Archive in-place WITHOUT recursing through abandon_run -> _archive_run -> load_run
        old_id = data.get("run_id", "unparseable")
        history_dir = state_dir / "history" / f"{old_id}-stale-v{data.get('version', 'unknown')}"
        history_dir.mkdir(parents=True, exist_ok=True)
        for f in state_dir.glob("*.json"):
            shutil.move(str(f), str(history_dir / f.name))
        return None
    return data


def update_stage(state_dir: Path, stage: str, progress: dict | None = None) -> None:
    """Update the current run's stage and optional progress."""
    run = load_run(state_dir)
    if run is None:
        raise RuntimeError("No active run")
    run["stage"] = stage
    if progress is not None:
        run["stage_progress"] = progress
    _atomic_write(state_dir / CURRENT_RUN_FILE, run)


def save_stage_output(state_dir: Path, name: str, data: dict) -> None:
    """Save a stage's output file atomically."""
    _atomic_write(state_dir / f"{name}.json", data)


def load_stage_output(state_dir: Path, name: str) -> dict | None:
    """Load a stage's output file, or None if missing."""
    path = state_dir / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def append_written_note(state_dir: Path, topic: str, path: str, model: str) -> None:
    """Append a completed note to written_notes.json."""
    written = load_stage_output(state_dir, "written_notes") or {"completed": []}
    written["completed"].append({
        "topic": topic,
        "path": path,
        "model": model,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    save_stage_output(state_dir, "written_notes", written)


def _archive_run(state_dir: Path) -> None:
    """Move all state files to history/{run_id}/."""
    run = load_run(state_dir)
    if run is None:
        return
    history_dir = state_dir / "history" / run["run_id"]
    history_dir.mkdir(parents=True, exist_ok=True)
    for f in state_dir.glob("*.json"):
        shutil.move(str(f), str(history_dir / f.name))


def abandon_run(state_dir: Path) -> None:
    """Archive incomplete run to history."""
    _archive_run(state_dir)


def complete_run(state_dir: Path) -> dict | None:
    """Archive completed run to history. Returns the final run dict (with
    completed_at) before archiving so Stage 10 callers can read telemetry,
    hop genealogy, and case-record data from the in-memory snapshot without
    having to re-load from history/."""
    run = load_run(state_dir)
    if run:
        run["completed_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(state_dir / CURRENT_RUN_FILE, run)
    _archive_run(state_dir)
    return run


def is_stale_run(state_dir: Path, max_age_hours: int = 24) -> bool:
    """Check if the current run is older than max_age_hours."""
    run = load_run(state_dir)
    if run is None:
        return False
    started = datetime.fromisoformat(run["started_at"])
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started > timedelta(hours=max_age_hours)


def write_case_record(cases_dir: Path, case_data: dict) -> None:
    """Write a case record JSON to cases_dir/{case_id}.json.

    Stage A (case prep) writes case records at completion. Stage B will read
    them later for retrospective analysis; nothing reads them yet. Creates
    the cases directory if it doesn't exist.
    """
    cases_dir.mkdir(parents=True, exist_ok=True)
    case_id = case_data["case_id"]
    case_file = cases_dir / f"{case_id}.json"
    case_file.write_text(json.dumps(case_data, indent=2), encoding="utf-8", newline="\n")
