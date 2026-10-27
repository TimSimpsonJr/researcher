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


def test_check_playwright_and_tesseract(cli, monkeypatch):
    monkeypatch.setattr(rw, "check_playwright", lambda: {"status": "ok"})
    monkeypatch.setattr(rw, "check_tesseract", lambda: {"status": "missing"})
    assert cli("check", "playwright")[1] == {"status": "ok"}
    assert cli("check", "tesseract")[1] == {"status": "missing"}


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


PDF_URL = "https://fdle.example/packet.pdf"


def _needs_browser_results(vault):
    return _write(_state(vault) / "fetch_results_hop1.json", {
        "topic": "T", "fetched": [], "failed": [],
        "needs_browser": [{"url": PDF_URL, "reason": "http_403", "attempts": []}],
        "stats": {"total_urls": 1, "fetched": 0, "failed": 0, "needs_browser": 1,
                  "cache_hits": 0, "total_words": 0},
    })


@pytest.fixture
def no_dns(monkeypatch):
    import fetch_and_clean
    monkeypatch.setattr(fetch_and_clean, "validate_url", lambda url: None)


def test_add_browser_fetch_moves_url_out_of_needs_browser(active_run, cli, tmp_path, no_dns):
    import fetch_and_clean
    results = _needs_browser_results(active_run)
    page = active_run / "page.md"
    page.write_text("Board package text. " * 30, encoding="utf-8")
    cache = tmp_path / "cache"
    code, out, err = cli("add-browser-fetch", "--hop", "1", "--topic", "Greenville ALPR", "--url", PDF_URL,
                         "--title", "Board package", "--content-file", page, "--cache-dir", cache)
    assert code == 0, err
    assert out == {"url": PDF_URL, "hop": "1", "word_count": 90}
    data = json.loads(results.read_text(encoding="utf-8"))
    assert [(f["url"], f["fetch_method"], f["title"]) for f in data["fetched"]] == [(PDF_URL, "browser", "Board package")]
    assert data["needs_browser"] == []
    assert data["stats"] == {"total_urls": 1, "fetched": 1, "failed": 0, "needs_browser": 0,
                             "cache_hits": 0, "total_words": 90}
    cached = fetch_and_clean.load_cache(cache, fetch_and_clean.url_cache_key(PDF_URL))
    assert cached["fetch_method"] == "browser" and cached["content"].startswith("Board package text.")
    assert PDF_URL in _run_file(active_run)["topics"][0]["seen_urls"]


def test_add_browser_fetch_also_clears_failed(active_run, cli, tmp_path, no_dns):
    results = _needs_browser_results(active_run)
    data = json.loads(results.read_text(encoding="utf-8"))
    data["failed"] = [{"url": PDF_URL, "error": "x", "attempts": []}]
    results.write_text(json.dumps(data), encoding="utf-8")
    page = active_run / "page.md"
    page.write_text("some text here", encoding="utf-8")
    code, _, err = cli("add-browser-fetch", "--hop", "1", "--topic", "Greenville ALPR", "--url", PDF_URL,
                       "--content-file", page, "--cache-dir", tmp_path / "cache")
    assert code == 0, err
    data = json.loads(results.read_text(encoding="utf-8"))
    assert data["failed"] == [] and data["stats"]["failed"] == 0 and data["stats"]["fetched"] == 1


def test_add_browser_fetch_rejects_internal_url(active_run, cli, tmp_path):
    results = _needs_browser_results(active_run)
    before = results.read_text(encoding="utf-8")
    page = active_run / "page.md"
    page.write_text("x", encoding="utf-8")
    code, _, err = cli("add-browser-fetch", "--hop", "1", "--topic", "Greenville ALPR",
                       "--url", "http://10.0.0.5/admin", "--content-file", page, "--cache-dir", tmp_path / "cache")
    assert code == 1 and err.startswith("ERROR:") and "Blocked" in err
    assert results.read_text(encoding="utf-8") == before


def test_add_browser_fetch_needs_the_hop_file(active_run, cli, tmp_path, no_dns):
    page = active_run / "page.md"
    page.write_text("x", encoding="utf-8")
    code, _, err = cli("add-browser-fetch", "--hop", "9", "--topic", "Greenville ALPR", "--url", PDF_URL,
                       "--content-file", page, "--cache-dir", tmp_path / "cache")
    assert code == 1 and "fetch_results_hop9.json not found" in err


def _unchanged_after(active_run, cli, tmp_path, content_file, expect):
    results = _needs_browser_results(active_run)
    before = results.read_text(encoding="utf-8")
    code, _, err = cli("add-browser-fetch", "--hop", "1", "--topic", "Greenville ALPR", "--url", PDF_URL,
                       "--content-file", content_file, "--cache-dir", tmp_path / "cache")
    assert code == 1 and expect in err
    assert results.read_text(encoding="utf-8") == before
    assert _run_file(active_run)["topics"][0]["seen_urls"] == []
    assert not (tmp_path / "cache").exists()


def test_add_browser_fetch_missing_content_file_changes_nothing(active_run, cli, tmp_path, no_dns):
    _unchanged_after(active_run, cli, tmp_path, active_run / "nope.md", "ERROR:")


def test_add_browser_fetch_empty_content_file_changes_nothing(active_run, cli, tmp_path, no_dns):
    page = active_run / "empty.md"
    page.write_text("  \n", encoding="utf-8")
    _unchanged_after(active_run, cli, tmp_path, page, "content file is empty")


def test_add_browser_fetch_truncates_output_but_caches_full_text(active_run, cli, tmp_path, no_dns):
    import fetch_and_clean
    results = _needs_browser_results(active_run)
    page = active_run / "big.md"
    page.write_text("a" * (fetch_and_clean.MAX_CONTENT_CHARS + 500), encoding="utf-8")
    cache = tmp_path / "cache"
    code, _, err = cli("add-browser-fetch", "--hop", "1", "--topic", "Greenville ALPR", "--url", PDF_URL,
                       "--content-file", page, "--cache-dir", cache)
    assert code == 0, err
    data = json.loads(results.read_text(encoding="utf-8"))
    assert len(data["fetched"][0]["content"]) == fetch_and_clean.MAX_CONTENT_CHARS
    cached = fetch_and_clean.load_cache(cache, fetch_and_clean.url_cache_key(PDF_URL))
    assert len(cached["content"]) == fetch_and_clean.MAX_CONTENT_CHARS + 500


def test_add_browser_fetch_twice_keeps_one_record(active_run, cli, tmp_path, no_dns):
    results = _needs_browser_results(active_run)
    page = active_run / "page.md"
    page.write_text("some text here", encoding="utf-8")
    for _ in range(2):
        code, _, err = cli("add-browser-fetch", "--hop", "1", "--topic", "Greenville ALPR", "--url", PDF_URL,
                           "--content-file", page, "--cache-dir", tmp_path / "cache")
        assert code == 0, err
    data = json.loads(results.read_text(encoding="utf-8"))
    assert len(data["fetched"]) == 1 and data["stats"]["fetched"] == 1


def test_add_browser_fetch_unknown_topic_leaves_file_alone(active_run, cli, tmp_path, no_dns):
    results = _needs_browser_results(active_run)
    before = results.read_text(encoding="utf-8")
    page = active_run / "page.md"
    page.write_text("x", encoding="utf-8")
    code, _, err = cli("add-browser-fetch", "--hop", "1", "--topic", "Nope", "--url", PDF_URL,
                       "--content-file", page, "--cache-dir", tmp_path / "cache")
    assert code == 1 and "Topic not found" in err
    assert results.read_text(encoding="utf-8") == before


def test_add_manual_url_dedupes(active_run, cli):
    code, out, _ = cli("add-manual-url", "--url", PDF_URL, "--reason", "captcha", "--topic", "Greenville ALPR")
    assert code == 0 and out == {"url": PDF_URL, "reason": "captcha"}
    cli("add-manual-url", "--url", PDF_URL, "--reason", "captcha")
    assert _run_file(active_run)["manual_urls"] == [
        {"url": PDF_URL, "reason": "captcha", "topic": "Greenville ALPR"}]


def test_add_manual_url_without_run_fails(vault, cli):
    code, _, err = cli("add-manual-url", "--url", PDF_URL, "--reason", "captcha")
    assert code == 1 and "No active run" in err


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


def test_compute_confidence_accepts_fetch_results(tmp_path, cli, monkeypatch):
    from confidence import compute_confidence
    data = {
        "search_context": {"selected_urls": [
            {"url": "a", "tier": "T1", "is_primary": True},
            {"url": "b", "tier": "T2"},
            {"url": "c", "tier": "T1", "is_primary": True},
        ]},
        "fetched": [{"url": "a", "content": "x"}, {"url": "b", "content": "y"},
                    {"url": "c", "content": "z", "usable": False}],
    }
    monkeypatch.chdir(tmp_path)
    code, out, _ = cli("compute-confidence", "--sources-file", _write(tmp_path / "fr.json", data),
                       "--depth", "quick")
    expected = compute_confidence([{"tier": "T1", "is_primary": True}, {"tier": "T2"}], depth="quick")
    assert code == 0 and out == pytest.approx(expected)


def _two_topic_results():
    return {
        "search_context": {"selected_urls": [
            {"url": "a", "tier": "T1", "is_primary": True, "topic": "Alpha"},
            {"url": "b", "tier": "T2", "topic": "Alpha"},
            {"url": "c", "tier": "T4", "topic": "Beta"},
        ]},
        "fetched": [{"url": "a", "content": "x"}, {"url": "b", "content": "y"}, {"url": "c", "content": "z"}],
    }


def test_compute_confidence_topic_counts_only_its_own_sources(tmp_path, cli, monkeypatch):
    from confidence import compute_confidence
    path = _write(tmp_path / "fr.json", _two_topic_results())
    monkeypatch.chdir(tmp_path)
    code, alpha, _ = cli("compute-confidence", "--sources-file", path, "--depth", "quick", "--topic", "Alpha")
    assert code == 0 and alpha == pytest.approx(
        compute_confidence([{"tier": "T1", "is_primary": True}, {"tier": "T2"}], depth="quick"))
    code, beta, _ = cli("compute-confidence", "--sources-file", path, "--depth", "quick", "--topic", "Beta")
    assert code == 0 and beta == pytest.approx(compute_confidence([{"tier": "T4"}], depth="quick"))


def test_compute_confidence_without_topic_counts_everything(tmp_path, cli, monkeypatch):
    from confidence import compute_confidence
    monkeypatch.chdir(tmp_path)
    code, out, _ = cli("compute-confidence", "--sources-file", _write(tmp_path / "fr.json", _two_topic_results()),
                       "--depth", "quick")
    expected = compute_confidence([{"tier": "T1", "is_primary": True}, {"tier": "T2"}, {"tier": "T4"}],
                                  depth="quick")
    assert code == 0 and out == pytest.approx(expected)


def test_compute_confidence_topic_without_stamps_warns_and_uses_all(tmp_path, cli, monkeypatch):
    from confidence import compute_confidence
    data = _two_topic_results()
    for s in data["search_context"]["selected_urls"]:
        del s["topic"]
    monkeypatch.chdir(tmp_path)
    code, out, err = cli("compute-confidence", "--sources-file", _write(tmp_path / "fr.json", data),
                         "--depth", "quick", "--topic", "Alpha")
    expected = compute_confidence([{"tier": "T1", "is_primary": True}, {"tier": "T2"}, {"tier": "T4"}],
                                  depth="quick")
    assert code == 0 and out == pytest.approx(expected)
    assert "no topic stamps" in err


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
    assert combined == {"topic": "ALPR", "items": [
        {"url": "a", "topic_key": None, "hop": 1}, {"url": "b", "topic_key": None, "hop": 2}]}


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


def test_aggregate_summaries_dedupes_by_topic_url_title(active_run, cli):
    sd = _state(active_run)
    _write(sd / "summaries_hop1_alpr.json", {"items": [
        {"url": "u", "title": "A", "v": 1}, {"url": "shared", "title": "X"},
        {"url": "shared", "title": "Y"}]})
    _write(sd / "summaries_hop3_alpr.json", {"items": [{"url": "u", "title": "A", "v": 3}]})
    _write(sd / "summaries_hop1_flock.json", {"items": [{"url": "u", "title": "A", "v": 9}]})
    cli("aggregate-summaries", "--project", "P")
    items = json.loads((sd / "summaries.json").read_text(encoding="utf-8"))["items"]
    assert len(items) == 4  # alpr/u once, both shared items, flock/u kept separate
    assert [i["v"] for i in items if i["url"] == "u"] == [3, 9]
    assert {i["title"] for i in items if i["url"] == "shared"} == {"X", "Y"}


def test_aggregate_hop_order_is_numeric(active_run, cli):
    sd = _state(active_run)
    _write(sd / "summaries_hop2.json", {"items": [{"url": "u", "title": "t", "v": 2}]})
    _write(sd / "summaries_hop10.json", {"items": [{"url": "u", "title": "t", "v": 10}]})
    _write(sd / "fetch_results_hop2.json", {"fetched": [{"url": "u", "content": "two"}]})
    _write(sd / "fetch_results_hop10.json", {"fetched": [{"url": "u", "content": "ten"}]})
    cli("aggregate-summaries", "--project", "P")
    cli("aggregate-fetch-results")
    assert json.loads((sd / "summaries.json").read_text(encoding="utf-8"))["items"][0]["v"] == 10
    agg = json.loads((sd / "fetch_results_aggregated.json").read_text(encoding="utf-8"))
    assert agg["by_url"]["u"]["content"] == "ten"


def test_aggregate_summaries_stamps_topic_key_and_hop(active_run, cli):
    sd = _state(active_run)
    _write(sd / "summaries_hop2_alpr.json", {"items": [{"url": "u", "title": "A"},
                                                       {"url": "v", "title": "B", "topic_key": "kept"}]})
    cli("aggregate-summaries", "--project", "P")
    items = json.loads((sd / "summaries.json").read_text(encoding="utf-8"))["items"]
    assert [(i["topic_key"], i["hop"]) for i in items] == [("alpr", 2), ("kept", 2)]


def test_mark_superseded_takes_topic_out_of_the_gate(active_run, cli):
    code, out, _ = cli("mark-superseded", "--topic", "Greenville ALPR", "--reason", "merged into Flock topic")
    assert code == 0 and out == {"topic": "Greenville ALPR", "status": "superseded"}
    resp = _write(active_run / "resp.json", {"decision": "stop", "confidence_score": 0.95, "contradiction_rate": 0.1})
    hop = _write(active_run / "hop.json", {"hop": 1})
    cli("apply-hop-decision", "--topic", "Flock Safety contracts", "--response-file", resp, "--hop-data-file", hop)
    gate = cli("quality-gate")[1]
    assert gate["passed"] is True and gate["superseded"] == ["Greenville ALPR"]
    assert [t["topic"] for t in gate["topics"]] == ["Flock Safety contracts"]
    assert gate["worst_confidence"] == 0.95
    run = _run_file(active_run)
    assert {t["topic"]: t["status"] for t in run["topics"]}["Greenville ALPR"] == "superseded"
    assert run["superseded"] == {"Greenville ALPR": "merged into Flock topic"}


def test_mark_superseded_unknown_topic_fails(active_run, cli):
    code, _, err = cli("mark-superseded", "--topic", "Nope", "--reason", "x")
    assert code == 1 and "Topic not found" in err


def test_mark_superseded_refuses_last_live_topic(active_run, cli):
    assert cli("mark-superseded", "--topic", "Greenville ALPR", "--reason", "x")[0] == 0
    code, _, err = cli("mark-superseded", "--topic", "Flock Safety contracts", "--reason", "y")
    assert code == 1 and "cannot supersede the last live topic" in err


def test_apply_hop_decision_refuses_superseded_topic(active_run, cli):
    cli("mark-superseded", "--topic", "Greenville ALPR", "--reason", "x")
    resp = _write(active_run / "resp.json", {"decision": "stop", "confidence_score": 0.9, "contradiction_rate": 0.1})
    hop = _write(active_run / "hop.json", {"hop": 1})
    code, _, err = cli("apply-hop-decision", "--topic", "Greenville ALPR", "--response-file", resp,
                       "--hop-data-file", hop)
    assert code == 1 and "superseded" in err


def test_build_case_record_ignores_superseded_topics():
    from state import build_case_record
    from score_updates import compute_run_outcome
    topics = [
        {"topic": "old", "depth": "quick", "status": "superseded", "confidence_history": [0.1],
         "contradiction_rate": 0.9, "hop_genealogy": [{}]},
        {"topic": "new", "depth": "quick", "status": "complete", "confidence_history": [0.9],
         "contradiction_rate": 0.1, "hop_genealogy": [{}, {}]},
    ]
    case = build_case_record({"run_id": "r", "topics": topics, "superseded": {"old": "merged"}},
                             {"query": "q"})
    assert case["confidence_per_topic"] == {"new": 0.9}
    assert case["contradiction_rate"] == 0.1
    assert case["hops_executed"] == 3 and case["depths_used"]["quick"] == 2
    assert case["superseded"] == {"old": "merged"}
    assert compute_run_outcome(case) == "W"


def test_compute_confidence_dict_without_fetched_fails(tmp_path, cli, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _, err = cli("compute-confidence", "--sources-file", _write(tmp_path / "x.json", {"a": 1}),
                       "--depth", "quick")
    assert code == 1 and "fetched" in err


def test_compute_confidence_warns_without_selected_urls(tmp_path, cli, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = {"fetched": [{"url": "a", "content": "x"}]}
    code, _, err = cli("compute-confidence", "--sources-file", _write(tmp_path / "x.json", data),
                       "--depth", "quick")
    assert code == 0 and "without tier/primary signals" in err
    data["search_context"] = None
    code, _, err = cli("compute-confidence", "--sources-file", _write(tmp_path / "y.json", data),
                       "--depth", "quick")
    assert code == 0 and "Traceback" not in err


def test_compute_confidence_merges_multiple_files(tmp_path, cli, monkeypatch):
    from confidence import compute_confidence
    sel = [{"url": "a", "tier": "T1", "is_primary": True}, {"url": "b", "tier": "T2"}]
    h1 = {"search_context": {"selected_urls": sel},
          "fetched": [{"url": "a", "content": "1"}, {"url": "b", "content": "1"}]}
    h2 = {"search_context": {"selected_urls": sel}, "fetched": [{"url": "a", "content": "2"}]}
    monkeypatch.chdir(tmp_path)
    code, out, _ = cli("compute-confidence", "--sources-file", _write(tmp_path / "h1.json", h1),
                       "--sources-file", _write(tmp_path / "h2.json", h2), "--depth", "quick")
    expected = compute_confidence([{"tier": "T1", "is_primary": True}, {"tier": "T2"}], depth="quick")
    assert code == 0 and out == pytest.approx(expected)


def test_topic_passes_skips_superseded():
    from state import topic_passes
    assert topic_passes({"depth": "quick", "confidence_history": [], "contradiction_rate": 0.9,
                         "status": "superseded"})


def test_record_user_decision_without_run_fails(vault, cli):
    code, _, err = cli("record-user-decision", "--decision", "continue")
    assert code == 1 and "no active run" in err


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


# --- completion and pattern graduation -----------------------------------------

from accumulator import Accumulator, AccumulatorEntry, load_accumulator, save_accumulator
from learned_patterns import load_learned_patterns


def _entry(pid="p-new"):
    return AccumulatorEntry(
        pattern_id=pid, name="T1 first", category="source-tier-bias", target_stage="search",
        domain_tags=["alpr"], sessions_seen=3, sessions_since_last_seen=0,
        status="promotion_pending", raised_bar=False, promotion_pending=True, demotion_count=0,
        evidence=[{"case_id": "c1", "signal": "T1=8/12"}],
        proposed_promotion_body="Prefer agency data",
        created_at="2026-10-01T00:00:00Z", last_updated_at="2026-10-01T00:00:00Z",
    )


def _seed_accumulator(vault, *entries):
    path = vault / ".researcher" / "accumulator.json"
    save_accumulator(path, Accumulator(entries=list(entries)))
    return path


def test_complete_run_saves_final(active_run, cli):
    cli("record-applied-pattern", "--pattern-id", "p1")
    code, out, _ = cli("complete-run")
    assert code == 0 and out["run_id"] == "r1" and out["completed_at"]
    saved = json.loads((active_run / ".researcher" / "tmp" / "final_run.json").read_text(encoding="utf-8"))
    assert saved == out
    assert (_state(active_run) / "history" / "r1" / "current_run.json").is_file()
    assert cli("check-run")[1] is None


def test_complete_run_without_run_prints_null(vault, cli):
    code, out, _ = cli("complete-run")
    assert code == 0 and out is None
    assert not (vault / ".researcher" / "tmp" / "final_run.json").exists()


def test_complete_run_without_run_deletes_stale_final(vault, cli):
    stale = vault / ".researcher" / "tmp" / "final_run.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("{}", encoding="utf-8")
    code, out, _ = cli("complete-run")
    assert code == 0 and out is None
    assert not stale.exists()


def test_write_case_record_missing_final_errors(vault, cli):
    inputs = _write(vault / "case_inputs.json", {"query": "q"})
    code, _, err = cli("write-case-record", "--inputs-file", inputs)
    assert code == 1 and "final_run.json" in err


def test_write_case_record_builds_case_from_final(active_run, cli):
    cli("record-applied-pattern", "--pattern-id", "p1")
    cli("set-strategy", "--strategy", "unified")
    cli("complete-run")
    inputs = _write(active_run / "case_inputs.json", {
        "query": "ALPR", "domain_tags": ["alpr"],
        "patterns_that_worked": {"hop_chain": ["causal_chain"]}, "patterns_that_failed": {},
        "sources_processed": 7, "notes_created": 2, "notes_updated": 1,
    })
    code, out, _ = cli("write-case-record", "--inputs-file", inputs)
    assert code == 0
    assert Path(out["case_path"]) == active_run.resolve() / ".researcher" / "cases" / "r1.json"
    case = json.loads(Path(out["case_path"]).read_text(encoding="utf-8"))
    assert case["case_id"] == "r1" and case["query"] == "ALPR" and case["strategy_used"] == "unified"
    assert case["applied_patterns"] == ["p1"]
    assert case["depths_used"] == {"quick": 1, "standard": 1, "deep": 0, "exhaustive": 0}
    assert case["hops_executed"] == 0
    assert case["confidence_per_topic"] == {"Greenville ALPR": None, "Flock Safety contracts": None}
    assert case["patterns_that_worked"] == {"hop_chain": ["causal_chain"]}
    assert case["outcomes"]["sources_processed"] == 7


def test_build_case_record_defaults():
    from state import build_case_record
    case = build_case_record({"run_id": "r9", "topics": []}, {"query": "q"})
    assert case["applied_patterns"] == [] and case["strategy_used"] == "unified"
    assert case["contradiction_rate"] == 0.0 and case["outcomes"]["user_decisions"] == []


def test_build_case_record_wraps_non_dict_patterns():
    from state import build_case_record
    case = build_case_record({"run_id": "r9", "topics": []}, {
        "query": "q", "patterns_that_worked": ["T1 sources", "causal chain"],
        "patterns_that_failed": "nothing notable",
    })
    assert case["patterns_that_worked"] == {"notes": ["T1 sources", "causal chain"]}
    assert case["patterns_that_failed"] == {"notes": "nothing notable"}


def test_write_case_record_then_analyze_with_list_patterns(active_run, cli):
    """Regression: list-shaped patterns_that_* used to crash analyze with AttributeError."""
    cli("complete-run")
    inputs = _write(active_run / "case_inputs.json", {
        "query": "ALPR", "domain_tags": ["alpr"],
        "patterns_that_worked": ["T1 agency data", "causal chain"],
        "patterns_that_failed": ["blog sources"],
    })
    case_path = cli("write-case-record", "--inputs-file", inputs)[1]["case_path"]
    code, out, err = cli("analyze-case", "--case-path", case_path)
    assert code == 0, err
    assert isinstance(out["warnings"], list)


def test_analyze_case_runs_under_lock(active_run, cli):
    cli("complete-run")
    inputs = _write(active_run / "case_inputs.json", {"query": "ALPR", "domain_tags": ["alpr"]})
    case_path = cli("write-case-record", "--inputs-file", inputs)[1]["case_path"]
    code, out, _ = cli("analyze-case", "--case-path", case_path)
    assert code == 0
    assert set(out) == {"promotion_candidates", "contradictions", "warnings",
                        "score_updates_applied", "demotions_applied"}
    assert not (active_run / ".researcher" / ".lock").exists()


def test_promote_pattern_moves_entry(vault, cli):
    acc_path = _seed_accumulator(vault, _entry())
    code, out, _ = cli("promote-pattern", "--pattern-id", "p-new")
    assert code == 0 and out == {"status": "promoted"}
    lp, warnings = load_learned_patterns(vault / ".researcher" / "learned_patterns.md")
    assert warnings == [] and [p.id for p in lp.patterns] == ["p-new"]
    assert lp.patterns[0].body == "Prefer agency data"
    assert load_accumulator(acc_path)[0].entries == []
    assert not (vault / ".researcher" / ".lock").exists()


def test_promote_unknown_pattern_fails(vault, cli):
    _seed_accumulator(vault)
    code, _, err = cli("promote-pattern", "--pattern-id", "nope")
    assert code == 1 and "nope" in err


def test_reject_and_hold(vault, cli):
    acc_path = _seed_accumulator(vault, _entry("p1"), _entry("p2"))
    assert cli("reject-pattern", "--pattern-id", "p1")[1] == {"status": "rejected"}
    assert cli("hold-pattern", "--pattern-id", "p2")[1] == {"status": "held"}
    by_id = {e.pattern_id: e for e in load_accumulator(acc_path)[0].entries}
    assert by_id["p1"].status == "rejected"
    assert by_id["p2"].status == "hold" and by_id["p2"].promotion_pending is False


@pytest.mark.parametrize("command", ["promote-pattern", "reject-pattern", "hold-pattern"])
def test_graduation_refuses_corrupt_accumulator(vault, cli, command):
    acc_path = vault / ".researcher" / "accumulator.json"
    acc_path.write_text("{not json", encoding="utf-8")
    code, out, err = cli(command, "--pattern-id", "p1")
    assert code == 0 and out == {"status": "aborted"}
    assert "BRANCH_ABORTED" in err
    assert acc_path.read_text(encoding="utf-8") == "{not json"


def test_promote_refuses_corrupt_learned_patterns(vault, cli):
    _seed_accumulator(vault, _entry())
    lp_path = vault / ".researcher" / "learned_patterns.md"
    lp_path.write_text("---\nversion: 99\n---\n", encoding="utf-8")
    code, out, err = cli("promote-pattern", "--pattern-id", "p-new")
    assert code == 0 and out == {"status": "aborted"} and "learned_patterns" in err
    assert lp_path.read_text(encoding="utf-8") == "---\nversion: 99\n---\n"
