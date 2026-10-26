"""Tests for scripts/rw.py: each subcommand against a temp vault via rw.main()."""

import json
import os
import time
from pathlib import Path

import pytest

import rw
from config_manager import default_config


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / ".researcher").mkdir(parents=True)
    (v / ".researcher" / "config.json").write_text(
        json.dumps(default_config(str(v))), encoding="utf-8"
    )
    (v / "Projects").mkdir()
    (v / "Projects" / "Alpha.md").write_text(
        "---\ntitle: Alpha\ntags: [research]\n---\nALPR cameras in Greenville.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(v)
    return v


@pytest.fixture
def cli(capsys):
    def _run(*argv):
        code = rw.main([str(a) for a in argv])
        cap = capsys.readouterr()
        out = json.loads(cap.out) if cap.out.strip() else None
        return code, out, cap.err
    return _run


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- vault, config, index ---------------------------------------------------

def test_locate_walks_up_from_subfolder(vault, cli, monkeypatch):
    monkeypatch.chdir(vault / "Projects")
    code, out, _ = cli("locate")
    root = vault.resolve()
    assert code == 0
    assert out == {
        "vault": str(root),
        "repo": str(rw.REPO_ROOT),
        "scripts": str(rw.REPO_ROOT / "scripts"),
        "state_dir": str(root / ".researcher" / "state"),
        "cases_dir": str(root / ".researcher" / "cases"),
    }


def test_locate_fails_outside_a_vault(tmp_path, cli, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = cli("locate")
    assert code == 1
    assert out is None
    assert "/researcher-setup" in err


def test_locate_honors_vault_flag(tmp_path, cli, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = cli("locate", "--vault", tmp_path / "elsewhere")
    assert code == 0
    assert out["vault"] == str((tmp_path / "elsewhere").resolve())


def test_find_vault_accepts_pre_rename_dir(tmp_path):
    from config_manager import find_vault
    old = tmp_path / ".research-workflow"
    old.mkdir()
    (old / "config.json").write_text("{}", encoding="utf-8")
    assert find_vault(tmp_path / "sub" / "dir") == tmp_path
    sibling = tmp_path.parent / (tmp_path.name + "-sibling")
    sibling.mkdir()
    assert find_vault(sibling) is None


def test_migrate_renames_old_dir_keeps_stdout_json(tmp_path, cli, monkeypatch):
    old = tmp_path / ".research-workflow"
    old.mkdir()
    (old / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, err = cli("migrate")
    assert code == 0
    assert out == "migrated"
    assert (tmp_path / ".researcher" / "config.json").is_file()
    assert "Migrated" in err


def test_load_config_prints_config_or_null(vault, cli, tmp_path):
    code, out, _ = cli("load-config")
    assert code == 0 and out["vault_root"] == str(vault)
    code, out, _ = cli("load-config", "--vault", tmp_path / "no-config-here")
    assert code == 0 and out is None


def test_save_config_writes_vault_config(tmp_path, cli):
    new_vault = tmp_path / "new"
    cfg = default_config("C:/vaults/new")
    src = _write(tmp_path / "setup_config.json", cfg)
    code, out, _ = cli("save-config", "--vault", new_vault, "--config-file", src)
    assert code == 0 and out == cfg
    saved = json.loads((new_vault / ".researcher" / "config.json").read_text(encoding="utf-8"))
    assert saved == cfg


def test_update_and_build_index(vault, cli):
    code, out, _ = cli("update-index")
    assert code == 0 and out == {"added": 1, "updated": 0, "removed": 0}
    code, out, _ = cli("build-index")
    assert code == 0 and out["notes_indexed"] == 1
    assert Path(out["db_path"]).is_file()


def test_vault_search_and_list_notes(vault, cli):
    cli("update-index")
    code, out, _ = cli("vault-search", "--query", "Alpha", "--limit", "5")
    assert code == 0 and [r["path"] for r in out] == ["Projects/Alpha.md"]
    assert cli("list-notes", "--folder", "Projects")[1] == ["Projects/Alpha.md"]
    assert cli("list-notes", "--folder", "Other")[1] == []
    assert cli("list-notes")[1] == ["Projects/Alpha.md"]
    assert cli("list-notes", "--folder", "Proj")[1] == []  # prefix collision
    (vault / "Projects" / "Sub").mkdir()
    (vault / "Projects" / "Sub" / "B.md").write_text("---\ntitle: B\n---\nb\n", encoding="utf-8")
    cli("update-index")
    assert cli("list-notes", "--folder", "Projects/Sub")[1] == ["Projects/Sub/B.md"]
    assert cli("list-notes", "--folder", "Projects\\Sub")[1] == ["Projects/Sub/B.md"]


# --- infrastructure ---------------------------------------------------------

import types


def test_tier_report_uses_config_searxng_and_repo(vault, cli, monkeypatch):
    seen = []
    monkeypatch.setattr(
        rw, "build_tier_report", lambda url, repo: seen.append((url, repo)) or {"tier": "base"}
    )
    code, out, _ = cli("tier-report")
    assert code == 0 and out == {"tier": "base"}
    assert seen == [(None, rw.REPO_ROOT)]


def test_detect_tier_passes_url(cli, monkeypatch):
    monkeypatch.setattr(rw, "detect_tier", lambda url: "mid" if url else "base")
    assert cli("detect-tier")[1] == "base"
    assert cli("detect-tier", "--searxng-url", "http://localhost:8888")[1] == "mid"


def test_check_dispatches_by_component(cli, monkeypatch):
    monkeypatch.setattr(rw, "check_ollama", lambda: {"installed": False})
    monkeypatch.setattr(rw, "check_searxng", lambda url: {"available": False, "url": url})
    assert cli("check", "ollama")[1] == {"installed": False}
    assert cli("check", "searxng", "--url", "http://x")[1] == {"available": False, "url": "http://x"}
    assert set(cli("check", "platform")[1]) == {"os", "arch", "is_wsl"}


def test_recommend_model(cli):
    out = cli("recommend-model", "--ram-gb", "32", "--vram-gb", "16")[1]
    assert out["model"] == "qwen2.5:14b"


def test_hardware_info_and_benchmark_wrappers(cli, monkeypatch):
    monkeypatch.setattr(
        rw, "get_hardware_info", lambda: {"total_ram_gb": 16.0, "gpu_vram_gb": 0, "gpu_name": "unknown"}
    )
    monkeypatch.setattr(
        rw, "benchmark_ollama", lambda model: {"elapsed_ms": 5, "output": model, "success": True}
    )
    assert cli("hardware-info")[1]["total_ram_gb"] == 16.0
    assert cli("benchmark-ollama", "--model", "qwen2.5:7b")[1]["output"] == "qwen2.5:7b"


def test_scan_vault_cli(vault, cli):
    out = cli("scan-vault")[1]
    assert out["total_notes"] == 1 and out["top_folders"] == ["Projects"]


def test_get_hardware_info_without_nvidia_smi(monkeypatch):
    import detect_tier
    original = detect_tier.subprocess.check_output

    def no_smi(cmd, *a, **k):
        if cmd[0] == "nvidia-smi":
            raise FileNotFoundError(cmd[0])
        return original(cmd, *a, **k)

    monkeypatch.setattr(detect_tier.subprocess, "check_output", no_smi)
    info = detect_tier.get_hardware_info()
    assert set(info) == {"total_ram_gb", "gpu_vram_gb", "gpu_name"}
    assert info["gpu_vram_gb"] == 0 and info["gpu_name"] == "unknown"
    assert info["total_ram_gb"] >= 0


def test_benchmark_ollama_reports_timing(monkeypatch):
    import detect_tier
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="A fox jumps.\n")

    monkeypatch.setattr(detect_tier.subprocess, "run", fake_run)
    r = detect_tier.benchmark_ollama("qwen2.5:7b")
    assert r["success"] is True and r["output"] == "A fox jumps." and r["elapsed_ms"] >= 0
    assert calls[0][:3] == ["ollama", "run", "qwen2.5:7b"]


def test_benchmark_ollama_missing_binary(monkeypatch):
    import detect_tier

    def boom(cmd, **kw):
        raise FileNotFoundError("ollama")

    monkeypatch.setattr(detect_tier.subprocess, "run", boom)
    assert detect_tier.benchmark_ollama("m")["success"] is False


def test_scan_conventions(tmp_path):
    from vault_index import scan_conventions
    (tmp_path / "Projects").mkdir()
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / "_Index.md").write_text("---\ntitle: Index\ntags: [index]\n---\nbody\n", encoding="utf-8")
    (tmp_path / "Projects" / "A.md").write_text(
        "---\ntitle: A\ntags: [research]\nsource: x\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "Loose.md").write_text("no frontmatter #inline-tag here\n", encoding="utf-8")
    (tmp_path / ".obsidian" / "skip.md").write_text("x", encoding="utf-8")
    r = scan_conventions(tmp_path)
    assert r["total_notes"] == 3
    assert r["top_folders"] == ["Projects"]
    assert r["moc_files"] == ["_Index.md"]
    assert r["tag_format_counts"]["list"] == 2
    assert r["tag_format_counts"]["inline"] == 1
    assert r["dominant_tag_format"] == "list"
    assert r["frontmatter_fields"] == ["title", "tags", "source"]
    assert r["moc_pattern"] == "^_|MOC|Index|Hub"


# --- run lifecycle ----------------------------------------------------------

PLAN = {
    "project": "ALPR",
    "topics": [
        {"topic": "Greenville ALPR", "mode": "web_research", "depth": "quick"},
        {"topic": "Flock Safety contracts", "mode": "web_research", "depth": "standard"},
    ],
}


def _state(vault):
    return vault / ".researcher" / "state"


def _run_file(vault):
    return json.loads((_state(vault) / "current_run.json").read_text(encoding="utf-8"))


@pytest.fixture
def active_run(vault, cli):
    assert cli("create-run", "--run-id", "r1", "--tier", "base")[0] == 0
    assert cli("init-topics", "--plan-file", _write(vault / "plan.json", PLAN))[0] == 0
    return vault


def test_check_run_null_then_active(vault, cli):
    code, out, _ = cli("check-run")
    assert code == 0 and out is None
    code, out, _ = cli("create-run", "--run-id", "r1", "--tier", "base")
    assert code == 0 and out["run_id"] == "r1" and out["stage"] == "triage"
    out = cli("check-run")[1]
    assert out["run_id"] == "r1" and out["is_stale"] is False


def test_create_run_refuses_second_active_run(vault, cli):
    cli("create-run", "--run-id", "r1", "--tier", "base")
    code, _, err = cli("create-run", "--run-id", "r2", "--tier", "base")
    assert code == 1 and "Active incomplete run" in err


def test_set_strategy(active_run, cli):
    assert cli("set-strategy", "--strategy", "unified")[1] == {"strategy": "unified"}
    assert _run_file(active_run)["strategy"] == "unified"


def test_init_topics_sets_topics_and_stage(active_run, cli):
    run = _run_file(active_run)
    assert [t["topic"] for t in run["topics"]] == ["Greenville ALPR", "Flock Safety contracts"]
    assert [t["max_hops"] for t in run["topics"]] == [1, 3]
    assert run["stage"] == "hop_loop"
    assert not (_state(active_run) / "research_plan.json").exists()


def test_init_topics_save_plan(vault, cli):
    cli("create-run", "--run-id", "r1", "--tier", "base")
    code, out, _ = cli("init-topics", "--plan-file", _write(vault / "plan.json", PLAN), "--save-plan")
    assert code == 0 and out == {"topics": 2, "stage": "hop_loop"}
    saved = json.loads((_state(vault) / "research_plan.json").read_text(encoding="utf-8"))
    assert saved == PLAN


def test_update_stage_with_progress(active_run, cli):
    assert cli("update-stage", "--stage", "resolve")[1] == {"stage": "resolve"}
    assert _run_file(active_run)["stage"] == "resolve"
    cli("update-stage", "--stage", "write", "--total", "5", "--completed", "2", "--current", "Alpha note")
    run = _run_file(active_run)
    assert run["stage"] == "write"
    assert run["stage_progress"] == {"total_notes": 5, "completed": 2, "current": "Alpha note"}


def test_save_and_load_stage_output(active_run, cli):
    classification = {"notes_to_create": [{"title": "Alpha"}]}
    src = _write(active_run / "classify_response.json", classification)
    code, out, _ = cli("save-stage-output", "--name", "classification", "--file", src, "--stage", "write")
    assert code == 0 and out["stage"] == "write"
    assert _run_file(active_run)["stage"] == "write"
    assert cli("load-stage-output", "--name", "classification")[1] == classification
    assert cli("load-stage-output", "--name", "written_notes")[1] is None


def test_abandon_run_archives(active_run, cli):
    code, out, _ = cli("abandon-run")
    archived = _state(active_run).resolve() / "history" / "r1"
    assert code == 0 and out == {"archived": str(archived)}
    assert (archived / "current_run.json").is_file()
    assert cli("check-run")[1] is None


def test_abandon_run_at_gate_marks_run(active_run, cli):
    out = cli("abandon-run", "--at-gate")[1]
    archived = json.loads((Path(out["archived"]) / "current_run.json").read_text(encoding="utf-8"))
    assert archived["abandoned_at_gate"] is True


def test_abandon_run_without_run(vault, cli):
    assert cli("abandon-run")[1] == {"archived": None}


def test_state_command_without_run_fails(vault, cli):
    code, _, err = cli("set-strategy", "--strategy", "unified")
    assert code == 1 and "no active run" in err


def test_update_stage_completed_or_current_requires_total(active_run, cli):
    for extra in (["--completed", "2"], ["--current", "Alpha"]):
        code, _, err = cli("update-stage", "--stage", "write", *extra)
        assert code == 1 and "--total" in err


# --- hop loop ---------------------------------------------------------------

from learned_patterns import LearnedPattern, LearnedPatternsFile, save_learned_patterns


def _seed_learned(vault):
    save_learned_patterns(
        vault / ".researcher" / "learned_patterns.md",
        LearnedPatternsFile(patterns=[
            LearnedPattern(id="p-search", name="T1 first", body="Prefer agency data",
                           domain_tags=["alpr"], target_stage="search"),
            LearnedPattern(id="p-search-offtopic", name="Zoning first", body="Check zoning maps",
                           domain_tags=["zoning"], target_stage="search"),
            LearnedPattern(id="p-hop", name="Follow money", body="Trace contracts",
                           domain_tags=["flock"], target_stage="hop_planner"),
            LearnedPattern(id="p-other", name="Unrelated", body="x",
                           domain_tags=["zoning"], target_stage="classify"),
        ]),
    )


def test_learned_patterns_groups_ids_by_stage(active_run, cli):
    _seed_learned(active_run)
    code, out, _ = cli("learned-patterns")
    assert code == 0
    assert out == {"search": ["p-search"], "hop_planner": ["p-hop"], "classify": []}


def test_learned_patterns_stage_records(active_run, cli):
    _seed_learned(active_run)
    out = cli("learned-patterns", "--stage", "search")[1]
    # p-search-offtopic is also a search-stage pattern but its tag matches no run topic
    assert out == [{"id": "p-search", "name": "T1 first", "body": "Prefer agency data"}]


def test_learned_patterns_missing_file(active_run, cli):
    assert cli("learned-patterns")[1] == {"search": [], "hop_planner": [], "classify": []}


def test_record_applied_pattern_dedupes(active_run, cli):
    code, _, _ = cli("record-applied-pattern", "--pattern-id", "p1", "p1", "p2")
    assert code == 0
    assert _run_file(active_run)["applied_patterns"] == ["p1", "p2"]


def test_add_seen_urls_dedupes(active_run, cli):
    cli("add-seen-urls", "--topic", "Greenville ALPR", "--urls", "https://a.example/1", "https://a.example/2")
    cli("add-seen-urls", "--topic", "Greenville ALPR", "--urls", "https://a.example/2", "https://a.example/3")
    topic = _run_file(active_run)["topics"][0]
    assert topic["seen_urls"] == ["https://a.example/1", "https://a.example/2", "https://a.example/3"]


def test_add_seen_urls_unknown_topic_fails(active_run, cli):
    code, _, err = cli("add-seen-urls", "--topic", "Nope", "--urls", "https://a.example/1")
    assert code == 1 and "Topic not found" in err


HOP_RESPONSE = {
    "topic": "Greenville ALPR",
    "decision": "continue",
    "confidence_score": 0.55,
    "contradiction_rate": 0.1,
    "next_hop": {"pattern": "causal_chain", "from": "state funding"},
}


def test_apply_hop_decision_continue(active_run, cli):
    resp = _write(active_run / "hop_response.json", HOP_RESPONSE)
    hop = _write(active_run / "hop_data.json", {"hop": 1, "pattern": "initial", "sources_kept": 4})
    code, out, _ = cli("apply-hop-decision", "--topic", "Greenville ALPR",
                       "--response-file", resp, "--hop-data-file", hop)
    assert code == 0 and out == {"topic": "Greenville ALPR", "decision": "continue"}
    t = _run_file(active_run)["topics"][0]
    assert t["current_hop"] == 1 and t["status"] == "active"
    assert t["confidence_history"] == [0.55] and t["next_hop"]["pattern"] == "causal_chain"
    assert t["hop_genealogy"] == [{"hop": 1, "pattern": "initial", "sources_kept": 4}]


def test_apply_hop_decision_early_terminated_override(active_run, cli):
    resp = _write(active_run / "hop_response.json", HOP_RESPONSE)
    hop = _write(active_run / "hop_data.json", {"hop": 1})
    out = cli("apply-hop-decision", "--topic", "Greenville ALPR", "--response-file", resp,
              "--hop-data-file", hop, "--decision", "early_terminated")[1]
    assert out["decision"] == "early_terminated"
    t = _run_file(active_run)["topics"][0]
    assert t["status"] == "early_terminated" and t["next_hop"] is None


def test_compute_confidence_needs_no_vault(tmp_path, cli, monkeypatch):
    from confidence import compute_confidence
    sources = [
        {"url": "a", "tier": "T1", "is_primary": True, "credibility_score": 0.95},
        {"url": "b", "tier": "T2", "is_primary": False, "credibility_score": 0.8},
    ]
    monkeypatch.chdir(tmp_path)
    code, out, _ = cli("compute-confidence", "--sources-file", _write(tmp_path / "s.json", sources),
                       "--depth", "quick")
    assert code == 0 and out == pytest.approx(compute_confidence(sources, depth="quick"))


# --- quality gate, aggregation, write ---------------------------------------

def test_topic_passes_boundaries():
    from state import topic_passes
    base = {"depth": "quick", "confidence_history": [0.6], "contradiction_rate": 0.3}
    assert topic_passes(base)
    assert not topic_passes({**base, "contradiction_rate": 0.31})
    assert not topic_passes({**base, "confidence_history": [0.59]})
    assert not topic_passes({**base, "confidence_history": []})


def test_quality_gate_fails_topics_without_confidence(active_run, cli):
    code, out, _ = cli("quality-gate")
    assert code == 0 and out["passed"] is False
    assert [t["topic"] for t in out["failing_topics"]] == ["Greenville ALPR", "Flock Safety contracts"]
    assert out["worst_confidence"] == 0.0 and out["replan_count"] == 0


def test_quality_gate_passes_when_targets_met(active_run, cli):
    resp = _write(active_run / "resp.json",
                  {"decision": "stop", "confidence_score": 0.95, "contradiction_rate": 0.1})
    hop = _write(active_run / "hop.json", {"hop": 1, "pattern": "initial"})
    for topic in ("Greenville ALPR", "Flock Safety contracts"):
        assert cli("apply-hop-decision", "--topic", topic, "--response-file", resp,
                   "--hop-data-file", hop)[0] == 0
    out = cli("quality-gate")[1]
    assert out["passed"] is True and out["failing_topics"] == [] and out["worst_confidence"] == 0.95


def test_apply_replan_readmit(active_run, cli):
    specs = _write(active_run / "replan_specs.json",
                   [{"topic_name": "Greenville ALPR", "replan_hint": {"issue": "thin sources"}}])
    cli("update-stage", "--stage", "quality_gate")
    code, out, _ = cli("apply-replan-readmit", "--specs-file", specs)
    assert code == 0 and out == {"replan_count": 1, "stage": "hop_loop"}
    run = _run_file(active_run)
    assert run["stage"] == "hop_loop"
    assert run["topics"][0]["max_hops"] == 2
    assert run["topics"][0]["replan_hint"] == {"issue": "thin sources"}


def test_record_user_decision(active_run, cli):
    assert cli("record-user-decision", "--decision", "continue", "--confidence", "0.42")[0] == 0
    entry = _run_file(active_run)["user_decisions"][-1]
    assert entry["decision"] == "continue" and entry["confidence"] == 0.42


def test_set_low_confidence(active_run, cli):
    cli("set-low-confidence", "--confidence", "0.42")
    run = _run_file(active_run)
    assert run["low_confidence"] is True and run["final_confidence_score"] == 0.42


def test_aggregate_summaries(active_run, cli):
    sd = _state(active_run)
    _write(sd / "summaries_hop1.json", {"topic": "ALPR", "items": [{"url": "a"}]})
    _write(sd / "summaries_hop2.json", {"topic": "ALPR", "items": [{"url": "b"}]})
    code, out, _ = cli("aggregate-summaries", "--project", "ALPR")
    assert code == 0 and out["items"] == 2
    combined = json.loads((sd / "summaries.json").read_text(encoding="utf-8"))
    assert combined == {"topic": "ALPR", "items": [{"url": "a"}, {"url": "b"}]}


def test_aggregate_fetch_results_later_hop_wins(active_run, cli):
    sd = _state(active_run)
    _write(sd / "fetch_results_hop1.json", {"fetched": [{"url": "a", "content": "old"}], "failed": []})
    _write(sd / "fetch_results_hop2.json",
           {"fetched": [{"url": "a", "content": "new"}, {"url": "b", "content": "b"}]})
    code, out, _ = cli("aggregate-fetch-results")
    assert code == 0 and out["fetched"] == 2
    agg = json.loads((sd / "fetch_results_aggregated.json").read_text(encoding="utf-8"))
    assert agg["by_url"]["a"]["content"] == "new"
    assert [e["url"] for e in agg["fetched"]] == ["a", "b"]
    assert not (sd / "fetch_results_aggregated.tmp").exists()


def test_check_mtime(active_run, cli):
    note = active_run / "Projects" / "Alpha.md"
    future = time.time() + 60
    os.utime(note, (future, future))
    assert cli("check-mtime", "--path", note)[1]["conflict"] is True
    assert cli("check-mtime", "--path", note, "--since", "2999-01-01T00:00:00+00:00")[1]["conflict"] is False
    assert cli("check-mtime", "--path", active_run / "missing.md")[1] == {"conflict": False, "mtime": None}


def test_append_written_note(active_run, cli):
    cli("append-written-note", "--topic", "Greenville ALPR", "--path", "Projects/Alpha.md", "--model", "sonnet")
    written = cli("load-stage-output", "--name", "written_notes")[1]
    assert [(n["topic"], n["path"], n["model"]) for n in written["completed"]] == [
        ("Greenville ALPR", "Projects/Alpha.md", "sonnet")
    ]
