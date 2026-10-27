# tests/test_state.py
"""Tests for state.py — pipeline state management."""

import json
import pytest
from pathlib import Path
from datetime import datetime, timezone


def test_create_run_writes_current_run(tmp_path):
    from state import create_run
    run = create_run(tmp_path, "sc-alpr", "mid")
    assert (tmp_path / "current_run.json").exists()
    assert run["run_id"] == "sc-alpr"
    assert run["stage"] == "triage"
    assert run["tier_detected"] == "mid"


def test_state_version_constant():
    from state import STATE_VERSION
    assert STATE_VERSION == 3


def test_create_run_writes_version(tmp_path):
    from state import create_run
    run = create_run(tmp_path, run_id="2026-05-26-test", tier="full")
    assert run["version"] == 3


def test_create_run_initial_stage_is_triage(tmp_path):
    from state import create_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    assert run["stage"] == "triage"


def test_create_run_has_empty_applied_patterns(tmp_path):
    """v3.1.0 runs initialize with an empty applied_patterns list."""
    from state import create_run, load_run
    run = create_run(tmp_path, run_id="test-v31-applied", tier="base")
    assert "applied_patterns" in run
    assert run["applied_patterns"] == []
    # Round-trip via load_run too
    persisted = load_run(tmp_path)
    assert persisted["applied_patterns"] == []


def test_create_run_fails_if_run_exists(tmp_path):
    from state import create_run
    create_run(tmp_path, "run1", "base")
    with pytest.raises(FileExistsError):
        create_run(tmp_path, "run2", "base")


def test_load_run_returns_none_when_no_run(tmp_path):
    from state import load_run
    assert load_run(tmp_path) is None


def test_load_run_returns_current_run(tmp_path):
    from state import create_run, load_run
    create_run(tmp_path, "test-run", "base")
    run = load_run(tmp_path)
    assert run["run_id"] == "test-run"


def test_update_stage_changes_stage(tmp_path):
    from state import create_run, update_stage, load_run
    create_run(tmp_path, "test", "base")
    update_stage(tmp_path, "search")
    run = load_run(tmp_path)
    assert run["stage"] == "search"


def test_save_stage_output_writes_atomically(tmp_path):
    from state import create_run, save_stage_output
    create_run(tmp_path, "test", "base")
    data = {"results": [1, 2, 3]}
    save_stage_output(tmp_path, "search_results", data)
    output_file = tmp_path / "search_results.json"
    assert output_file.exists()
    assert json.loads(output_file.read_text())["results"] == [1, 2, 3]


def test_load_stage_output_returns_none_when_missing(tmp_path):
    from state import load_stage_output
    assert load_stage_output(tmp_path, "search_results") is None


def test_load_stage_output_returns_data(tmp_path):
    from state import create_run, save_stage_output, load_stage_output
    create_run(tmp_path, "test", "base")
    save_stage_output(tmp_path, "fetch_results", {"data": "yes"})
    loaded = load_stage_output(tmp_path, "fetch_results")
    assert loaded["data"] == "yes"


def test_append_written_note(tmp_path):
    from state import create_run, append_written_note, load_stage_output
    create_run(tmp_path, "test", "base")
    append_written_note(tmp_path, "Greenville ALPR", "Projects/Surveillance/Greenville.md", "sonnet")
    append_written_note(tmp_path, "Charleston ALPR", "Projects/Surveillance/Charleston.md", "sonnet")
    written = load_stage_output(tmp_path, "written_notes")
    assert len(written["completed"]) == 2
    assert written["completed"][0]["topic"] == "Greenville ALPR"


def test_abandon_run_archives_state(tmp_path):
    from state import create_run, save_stage_output, abandon_run, load_run
    create_run(tmp_path, "old-run", "base")
    save_stage_output(tmp_path, "search_results", {"data": True})
    abandon_run(tmp_path)
    assert load_run(tmp_path) is None
    history_dir = tmp_path / "history" / "old-run"
    assert history_dir.exists()
    assert (history_dir / "current_run.json").exists()
    assert (history_dir / "search_results.json").exists()


def test_complete_run_archives_state(tmp_path):
    from state import create_run, complete_run, load_run
    create_run(tmp_path, "done-run", "base")
    complete_run(tmp_path)
    assert load_run(tmp_path) is None
    history_dir = tmp_path / "history" / "done-run"
    assert history_dir.exists()


def test_is_stale_run(tmp_path):
    from state import create_run, is_stale_run
    create_run(tmp_path, "test", "base")
    assert is_stale_run(tmp_path, max_age_hours=24) is False


def test_load_run_drops_old_schema(tmp_path, capsys):
    from state import load_run
    # Write a fake v2-era state file
    state_file = tmp_path / "current_run.json"
    state_file.write_text(json.dumps({"run_id": "old", "version": 2, "tier": "full"}))
    result = load_run(tmp_path)
    assert result is None
    err = capsys.readouterr().err   # message goes to stderr (see implementation)
    assert "older schema" in err.lower()
    # The state file should have been moved out (abandoned to history/)
    assert not (tmp_path / "current_run.json").exists()


def test_load_run_drops_missing_version(tmp_path, capsys):
    from state import load_run
    state_file = tmp_path / "current_run.json"
    state_file.write_text(json.dumps({"run_id": "old", "tier": "full"}))  # no version
    result = load_run(tmp_path)
    assert result is None


def test_create_run_topic_initialization(tmp_path):
    from state import create_run, init_topic
    run = create_run(tmp_path, run_id="r1", tier="full")
    topic = init_topic("SC ALPR programs", mode="web_research", depth="standard")
    assert topic == {
        "topic": "SC ALPR programs",
        "mode": "web_research",
        "depth": "standard",
        "max_hops": 3,
        "current_hop": 0,
        "status": "active",
        "hop_genealogy": [],
        "confidence_history": [],
        "contradiction_rate": 0.0,
        "seen_urls": [],
        "replan_hint": None,
        "next_hop": None,
    }


def test_record_hop_appends_to_genealogy(tmp_path):
    from state import create_run, init_topic, record_hop, save_state
    run = create_run(tmp_path, run_id="r1", tier="full")
    topic = init_topic("X", mode="web_research", depth="standard")
    run["topics"] = [topic]
    save_state(tmp_path, run)

    hop_data = {
        "hop": 1,
        "pattern": None,
        "queries": ["q1"],
        "sources_found": 12,
        "sources_kept": 7,
        "ended_at": "2026-05-26T14:25:00Z",
    }
    record_hop(tmp_path, topic_name="X", hop_data=hop_data)

    from state import load_run
    reloaded = load_run(tmp_path)
    assert reloaded["topics"][0]["hop_genealogy"] == [hop_data]
    assert reloaded["topics"][0]["current_hop"] == 1


def test_mark_topic_status(tmp_path):
    from state import create_run, init_topic, mark_topic_status, save_state, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    run["topics"] = [init_topic("X", mode="web_research", depth="standard")]
    save_state(tmp_path, run)

    mark_topic_status(tmp_path, topic_name="X", status="complete")
    assert load_run(tmp_path)["topics"][0]["status"] == "complete"


def test_append_confidence(tmp_path):
    from state import create_run, init_topic, append_confidence, save_state, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    run["topics"] = [init_topic("X", mode="web_research", depth="standard")]
    save_state(tmp_path, run)

    append_confidence(tmp_path, topic_name="X", score=0.42)
    append_confidence(tmp_path, topic_name="X", score=0.71)
    assert load_run(tmp_path)["topics"][0]["confidence_history"] == [0.42, 0.71]


def test_set_contradiction_rate(tmp_path):
    from state import create_run, init_topic, set_contradiction_rate, save_state, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    run["topics"] = [init_topic("X", mode="web_research", depth="standard")]
    save_state(tmp_path, run)

    set_contradiction_rate(tmp_path, topic_name="X", rate=0.18)
    assert load_run(tmp_path)["topics"][0]["contradiction_rate"] == 0.18

    # Overwrites with newer value
    set_contradiction_rate(tmp_path, topic_name="X", rate=0.32)
    assert load_run(tmp_path)["topics"][0]["contradiction_rate"] == 0.32


def test_set_replan_hint(tmp_path):
    from state import create_run, init_topic, set_replan_hint, save_state, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    run["topics"] = [init_topic("X", mode="web_research", depth="standard")]
    save_state(tmp_path, run)

    hint = {"issue": "thin sources", "suggested_pattern": "entity_expansion",
            "suggested_query_focus": "official agency data"}
    set_replan_hint(tmp_path, topic_name="X", hint=hint)
    assert load_run(tmp_path)["topics"][0]["replan_hint"] == hint


def test_bump_max_hops(tmp_path):
    """Bumping max_hops lets a topic re-enter the hop loop after exhausting its budget."""
    from state import create_run, init_topic, bump_max_hops, save_state, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    run["topics"] = [init_topic("X", mode="web_research", depth="standard")]  # max_hops=3
    save_state(tmp_path, run)

    bump_max_hops(tmp_path, topic_name="X", increment=1)
    assert load_run(tmp_path)["topics"][0]["max_hops"] == 4


def test_add_seen_urls_appends_dedup(tmp_path):
    """add_seen_urls preserves order, skips duplicates, can be called repeatedly."""
    from state import create_run, init_topic, add_seen_urls, save_state, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    run["topics"] = [init_topic("X", mode="web_research", depth="standard")]
    save_state(tmp_path, run)

    add_seen_urls(tmp_path, topic_name="X", urls=["https://a", "https://b"])
    add_seen_urls(tmp_path, topic_name="X", urls=["https://b", "https://c"])  # b is dup

    assert load_run(tmp_path)["topics"][0]["seen_urls"] == ["https://a", "https://b", "https://c"]


def test_add_seen_urls_unknown_topic_raises(tmp_path):
    import pytest
    from state import create_run, init_topic, save_state, add_seen_urls
    run = create_run(tmp_path, run_id="r1", tier="full")
    run["topics"] = [init_topic("X", mode="web_research", depth="standard")]
    save_state(tmp_path, run)

    with pytest.raises(KeyError, match="Topic not found"):
        add_seen_urls(tmp_path, topic_name="missing", urls=["https://a"])


def test_apply_hop_decision_continue_is_atomic(tmp_path):
    """apply_hop_decision applies hop record + routing + quality signals + status in one save."""
    from state import create_run, init_topic, save_state, apply_hop_decision, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    topic = init_topic("X", mode="web_research", depth="standard")
    topic["replan_hint"] = {"issue": "stale"}  # simulate a prior replan
    run["topics"] = [topic]
    save_state(tmp_path, run)

    hop_data = {"hop": 1, "pattern": None, "queries": [], "sources_kept": 5,
                "ended_at": "2026-05-26T15:00:00Z"}
    next_hop = {"pattern": "entity_expansion", "from": "Flock Safety", "rationale": "..."}
    apply_hop_decision(tmp_path, topic_name="X", hop_data=hop_data,
                       decision="continue", confidence_score=0.68,
                       contradiction_rate=0.12, next_hop=next_hop)

    t = load_run(tmp_path)["topics"][0]
    assert t["hop_genealogy"] == [hop_data]
    assert t["current_hop"] == 1
    assert t["confidence_history"] == [0.68]
    assert t["contradiction_rate"] == 0.12
    assert t["next_hop"] == next_hop
    assert t["replan_hint"] is None  # cleared atomically
    assert t["status"] == "active"


def test_apply_hop_decision_stop_marks_complete(tmp_path):
    from state import create_run, init_topic, save_state, apply_hop_decision, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    run["topics"] = [init_topic("X", mode="web_research", depth="standard")]
    save_state(tmp_path, run)

    apply_hop_decision(tmp_path, topic_name="X",
                       hop_data={"hop": 1, "ended_at": "..."},
                       decision="stop", confidence_score=0.82,
                       contradiction_rate=0.05)

    t = load_run(tmp_path)["topics"][0]
    assert t["current_hop"] == 1
    assert t["confidence_history"] == [0.82]
    assert t["contradiction_rate"] == 0.05
    assert t["next_hop"] is None
    assert t["status"] == "complete"


def test_apply_hop_decision_replan_stores_hint(tmp_path):
    from state import create_run, init_topic, save_state, apply_hop_decision, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    run["topics"] = [init_topic("X", mode="web_research", depth="standard")]
    save_state(tmp_path, run)

    hint = {"issue": "thin sources", "suggested_pattern": "entity_expansion",
            "suggested_query_focus": "official data"}
    apply_hop_decision(tmp_path, topic_name="X",
                       hop_data={"hop": 1, "ended_at": "..."},
                       decision="replan", confidence_score=0.41,
                       contradiction_rate=0.38, replan_hint=hint)

    t = load_run(tmp_path)["topics"][0]
    assert t["current_hop"] == 1
    assert t["confidence_history"] == [0.41]
    assert t["contradiction_rate"] == 0.38
    assert t["next_hop"] is None
    assert t["replan_hint"] == hint
    assert t["status"] == "replan_pending"


def test_apply_hop_decision_unknown_raises(tmp_path):
    from state import create_run, init_topic, save_state, apply_hop_decision
    run = create_run(tmp_path, run_id="r1", tier="full")
    run["topics"] = [init_topic("X", mode="web_research", depth="standard")]
    save_state(tmp_path, run)

    with pytest.raises(ValueError, match="Unknown decision"):
        apply_hop_decision(tmp_path, topic_name="X",
                           hop_data={"hop": 1},
                           decision="bogus",
                           confidence_score=0.0, contradiction_rate=0.0)


def test_apply_replan_readmit_is_atomic(tmp_path):
    """apply_replan_readmit re-admits failing topics, bumps max_hops, sets
    replan_hints, increments replan_count, and sets stage='hop_loop' in one
    load->mutate->save cycle so a crash mid-readmission cannot leave topics
    flagged active with stage stuck at 'quality_gate'."""
    from state import (
        create_run, init_topic, save_state, update_stage,
        apply_replan_readmit, load_run,
    )
    run = create_run(tmp_path, run_id="r1", tier="full")
    # Two topics that already failed at the gate
    t1 = init_topic("alpha", mode="web_research", depth="standard")
    t1["status"] = "complete"
    t1["max_hops"] = 3
    t1["current_hop"] = 3
    t1["replan_hint"] = {"issue": "prior", "suggested_pattern": "entity_expansion"}
    t2 = init_topic("beta", mode="web_research", depth="standard")
    t2["status"] = "replan_pending"
    t2["max_hops"] = 3
    t2["current_hop"] = 3
    run["topics"] = [t1, t2]
    save_state(tmp_path, run)
    # The orchestrator advanced to the quality gate
    update_stage(tmp_path, "quality_gate")

    synth_hint = {"issue": "thin sources", "suggested_pattern": "source_widening"}
    apply_replan_readmit(
        tmp_path,
        failing_topic_specs=[
            # alpha keeps its prior hint (None means "don't overwrite")
            {"topic_name": "alpha", "replan_hint": None},
            # beta gets a freshly synthesized hint
            {"topic_name": "beta",  "replan_hint": synth_hint},
        ],
    )

    reloaded = load_run(tmp_path)
    by_name = {t["topic"]: t for t in reloaded["topics"]}

    # Both topics re-admitted to the hop loop
    assert by_name["alpha"]["status"] == "active"
    assert by_name["beta"]["status"] == "active"
    # Hop budget bumped by 1
    assert by_name["alpha"]["max_hops"] == 4
    assert by_name["beta"]["max_hops"] == 4
    # Existing hint preserved when spec hint is None; new hint set otherwise
    assert by_name["alpha"]["replan_hint"] == {"issue": "prior", "suggested_pattern": "entity_expansion"}
    assert by_name["beta"]["replan_hint"] == synth_hint
    # Replan counter incremented and stage rolled back to hop_loop in the same save
    assert reloaded["replan_count"] == 1
    assert reloaded["stage"] == "hop_loop"


def test_apply_replan_readmit_skip_increment(tmp_path):
    """increment_replan_counter=False leaves replan_count untouched (used when
    the counter was already bumped earlier in the flow)."""
    from state import (
        create_run, init_topic, save_state, apply_replan_readmit, load_run,
    )
    run = create_run(tmp_path, run_id="r1", tier="full")
    t = init_topic("alpha", mode="web_research", depth="standard")
    t["status"] = "complete"
    t["current_hop"] = 3
    run["topics"] = [t]
    run["replan_count"] = 2
    save_state(tmp_path, run)

    apply_replan_readmit(
        tmp_path,
        failing_topic_specs=[{"topic_name": "alpha", "replan_hint": None}],
        increment_replan_counter=False,
    )
    reloaded = load_run(tmp_path)
    assert reloaded["replan_count"] == 2  # unchanged
    assert reloaded["stage"] == "hop_loop"
    assert reloaded["topics"][0]["status"] == "active"


def test_apply_replan_readmit_unknown_topic_raises_no_partial_disk_state(tmp_path):
    """An unknown topic_name in the middle of the spec list aborts the whole
    transition before save_state runs. On disk the prior valid mutation must
    NOT be visible — that's the whole point of the one-save-at-end design."""
    import pytest
    from state import (
        create_run, init_topic, save_state, apply_replan_readmit, load_run,
    )
    run = create_run(tmp_path, run_id="r1", tier="full")
    t = init_topic("alpha", mode="web_research", depth="standard")
    t["status"] = "complete"
    t["current_hop"] = 3
    run["topics"] = [t]
    save_state(tmp_path, run)

    with pytest.raises(KeyError, match="Topic not found"):
        apply_replan_readmit(
            tmp_path,
            failing_topic_specs=[
                # Valid spec first — would mutate in-memory if save reached.
                {"topic_name": "alpha", "replan_hint": {"issue": "x"}},
                # Then a bogus one that raises mid-loop.
                {"topic_name": "ghost", "replan_hint": None},
            ],
        )

    # On disk: state unchanged — the in-memory mutation never reached save_state.
    reloaded = load_run(tmp_path)
    assert reloaded["replan_count"] == 0
    assert reloaded["stage"] == "triage"
    assert reloaded["topics"][0]["status"] == "complete"  # not re-admitted
    assert reloaded["topics"][0]["max_hops"] == 3         # not bumped
    assert reloaded["topics"][0]["replan_hint"] is None   # not overwritten


def test_add_usage_starts_at_zero(tmp_path):
    from state import create_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    assert run["usage"] == {
        "haiku":  {"calls": 0, "in_tokens": 0, "out_tokens": 0},
        "sonnet": {"calls": 0, "in_tokens": 0, "out_tokens": 0},
        "opus":   {"calls": 0, "in_tokens": 0, "out_tokens": 0},
        "ollama": {"calls": 0},
    }


def test_add_usage_accumulates(tmp_path):
    from state import create_run, add_usage, load_run, save_state
    run = create_run(tmp_path, run_id="r1", tier="full")
    save_state(tmp_path, run)

    add_usage(tmp_path, model="haiku", in_tokens=1000, out_tokens=200, stage="search")
    add_usage(tmp_path, model="haiku", in_tokens=500,  out_tokens=100, stage="summarize")
    add_usage(tmp_path, model="ollama", in_tokens=0, out_tokens=0, stage="summarize")

    usage = load_run(tmp_path)["usage"]
    assert usage["haiku"] == {"calls": 2, "in_tokens": 1500, "out_tokens": 300}
    assert usage["ollama"]["calls"] == 1


def test_replan_count_starts_at_zero(tmp_path):
    from state import create_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    assert run["replan_count"] == 0
    assert run["user_decisions"] == []


def test_increment_replan(tmp_path):
    from state import create_run, save_state, increment_replan, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    save_state(tmp_path, run)
    increment_replan(tmp_path)
    increment_replan(tmp_path)
    assert load_run(tmp_path)["replan_count"] == 2


def test_record_user_decision(tmp_path):
    from state import create_run, save_state, record_user_decision, load_run
    run = create_run(tmp_path, run_id="r1", tier="full")
    save_state(tmp_path, run)
    record_user_decision(tmp_path, decision="continue_anyway", confidence=0.52)
    decisions = load_run(tmp_path)["user_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "continue_anyway"
    assert decisions[0]["confidence"] == 0.52
    assert "at" in decisions[0]


def test_record_applied_pattern_appends_unique(tmp_path):
    """record_applied_pattern adds a pattern_id; duplicates are no-ops."""
    from state import create_run, record_applied_pattern, load_run
    create_run(tmp_path, run_id="t", tier="base")
    record_applied_pattern(tmp_path, "civic-alpr-t1-dominance-3f7a")
    record_applied_pattern(tmp_path, "tech-entity-h2-9c2b")
    record_applied_pattern(tmp_path, "civic-alpr-t1-dominance-3f7a")  # dup
    run = load_run(tmp_path)
    assert run["applied_patterns"] == [
        "civic-alpr-t1-dominance-3f7a",
        "tech-entity-h2-9c2b",
    ]


def test_record_user_decision_rejects_reserved_keys(tmp_path):
    """Caller-supplied 'decision' or 'at' in **details would silently override canonical fields."""
    import pytest
    from state import create_run, save_state, record_user_decision
    run = create_run(tmp_path, run_id="r1", tier="full")
    save_state(tmp_path, run)

    with pytest.raises(TypeError, match="reserved keys"):
        record_user_decision(tmp_path, decision="continue", at="2020-01-01T00:00:00Z")


def test_add_usage_rejects_unknown_model(tmp_path):
    """Typos in model name (e.g. 'haku') would create ghost buckets — guard at the call site."""
    import pytest
    from state import create_run, save_state, add_usage
    run = create_run(tmp_path, run_id="r1", tier="full")
    save_state(tmp_path, run)

    with pytest.raises(ValueError, match="unknown model"):
        add_usage(tmp_path, model="haku", in_tokens=100, out_tokens=10, stage="search")


def test_abandon_run_sweeps_hop_intermediate_files(tmp_path):
    from state import create_run, abandon_run
    create_run(tmp_path, run_id="2026-05-26-hop-test", tier="full")

    # Simulate per-hop intermediate files
    (tmp_path / "fetch_results_hop1.json").write_text("{}")
    (tmp_path / "fetch_results_hop2.json").write_text("{}")
    (tmp_path / "summaries_hop1.json").write_text("{}")
    (tmp_path / "search_context_hop1.json").write_text("{}")

    abandon_run(tmp_path)

    # All per-hop files land alongside current_run.json under history/{run_id}/
    history_dir = tmp_path / "history" / "2026-05-26-hop-test"
    assert history_dir.exists()
    assert (history_dir / "fetch_results_hop1.json").exists()
    assert (history_dir / "fetch_results_hop2.json").exists()
    assert (history_dir / "summaries_hop1.json").exists()
    assert (history_dir / "search_context_hop1.json").exists()
    # Active run file should be gone
    assert not (tmp_path / "current_run.json").exists()


def test_complete_run_returns_run_data(tmp_path):
    from state import create_run, complete_run
    create_run(tmp_path, run_id="2026-05-26-complete-test", tier="full")

    result = complete_run(tmp_path)

    assert result is not None
    assert result["run_id"] == "2026-05-26-complete-test"
    assert "completed_at" in result
    # The file is gone (archived)
    assert not (tmp_path / "current_run.json").exists()


def test_write_case_record(tmp_path):
    from state import write_case_record
    case_data = {
        "case_id": "2026-05-26-test",
        "version": 1,
        "query": "test research",
        "domain_tags": ["test"],
        "outcomes": {"sources_processed": 5},
    }
    # cases_dir is .researcher/cases under the vault root
    cases_dir = tmp_path / "cases"
    write_case_record(cases_dir, case_data)

    case_file = cases_dir / "2026-05-26-test.json"
    assert case_file.exists()
    assert json.loads(case_file.read_text()) == case_data


def test_write_shared_state_atomically_dict(tmp_path):
    """write_shared_state_atomically writes a dict as JSON via temp-rename."""
    from state import write_shared_state_atomically
    target = tmp_path / "accumulator.json"
    write_shared_state_atomically(target, {"version": 1, "entries": []})
    assert target.exists()
    import json
    data = json.loads(target.read_text())
    assert data == {"version": 1, "entries": []}


def test_write_shared_state_atomically_str(tmp_path):
    """write_shared_state_atomically writes a string verbatim."""
    from state import write_shared_state_atomically
    target = tmp_path / "learned_patterns.md"
    body = "---\nversion: 1\n---\n\n## civic / alpr\n"
    write_shared_state_atomically(target, body)
    assert target.read_text() == body


def test_write_shared_state_atomically_creates_parent(tmp_path):
    """write_shared_state_atomically creates parent dir if missing."""
    from state import write_shared_state_atomically
    target = tmp_path / "nested" / "deeper" / "file.json"
    write_shared_state_atomically(target, {"x": 1})
    assert target.exists()


def test_write_shared_state_atomically_preserves_unicode(tmp_path):
    """Unicode characters survive without being escaped to \\uXXXX."""
    from state import write_shared_state_atomically
    import json
    target = tmp_path / "accumulator.json"
    payload = {
        "name": "T1 sources dominate — civic ALPR",
        "note": "Smart “quotes” round-trip cleanly"
    }
    write_shared_state_atomically(target, payload)
    raw = target.read_text(encoding="utf-8")
    # Body should contain the literal Unicode characters, not escaped \uXXXX sequences
    assert "—" in raw, f"em-dash should be literal, got: {raw}"
    assert "“" in raw, f"smart quote should be literal, got: {raw}"
    # Round-trip is also fine
    assert json.loads(raw) == payload


def test_acquire_state_lock_succeeds_when_unheld(tmp_path):
    """acquire_state_lock returns a context manager that holds the lock."""
    from state import acquire_state_lock
    with acquire_state_lock(tmp_path, timeout_s=1):
        assert (tmp_path / ".lock").exists()
    assert not (tmp_path / ".lock").exists()


def test_acquire_state_lock_times_out_when_held(tmp_path):
    """acquire_state_lock raises TimeoutError when another process holds it."""
    from state import acquire_state_lock, LockTimeoutError
    import pytest
    # Simulate another holder: write a lock file with current PID + fresh timestamp
    import os
    from datetime import datetime, timezone
    (tmp_path / ".lock").write_text(f"{os.getpid() + 99999}\n{datetime.now(timezone.utc).isoformat()}\n")
    with pytest.raises(LockTimeoutError):
        with acquire_state_lock(tmp_path, timeout_s=0.5):
            pass


def test_acquire_state_lock_breaks_stale_lock(tmp_path):
    """Stale locks (timestamp >1hr old) are forcibly cleared."""
    from state import acquire_state_lock
    import os
    from datetime import datetime, timezone, timedelta
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    (tmp_path / ".lock").write_text(f"{os.getpid() + 99999}\n{stale_ts}\n")
    with acquire_state_lock(tmp_path, timeout_s=1):
        pass  # should succeed, lock was stale


def test_acquire_state_lock_releases_on_exception(tmp_path):
    """Lock file is removed when an exception fires inside the locked block."""
    from state import acquire_state_lock
    import pytest
    with pytest.raises(RuntimeError, match="boom"):
        with acquire_state_lock(tmp_path, timeout_s=1):
            raise RuntimeError("boom")
    assert not (tmp_path / ".lock").exists()
    # Next acquirer can take the lock cleanly
    with acquire_state_lock(tmp_path, timeout_s=1):
        pass


def test_create_run_starts_manual_and_superseded_empty(tmp_path):
    from state import create_run
    run = create_run(tmp_path, "r1", "base")
    assert run["manual_urls"] == [] and run["superseded"] == {}


def test_add_manual_url_on_a_resumed_run_without_the_field(tmp_path):
    from state import add_manual_url, create_run, load_run, save_state
    run = create_run(tmp_path, "r1", "base")
    del run["manual_urls"]
    save_state(tmp_path, run)
    add_manual_url(tmp_path, "https://a.example/x", "no_browser")
    assert load_run(tmp_path)["manual_urls"] == [
        {"url": "https://a.example/x", "reason": "no_browser", "topic": None}]
