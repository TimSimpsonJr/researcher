"""rw.py — one-line CLI for the researcher skill's Python glue.

Each subcommand is a thin wrapper: parse flags, call a function in one of the
scripts/ modules, print JSON on stdout. Failures print ``ERROR: ...`` on stderr
and exit 1. JSON payloads arrive as file paths (--plan-file, --specs-file,
...), never inline. The vault is the first directory at or above the current
directory holding .researcher/config.json, unless --vault is given.

Run: uv run --frozen --project <repo> <repo>/scripts/rw.py <subcommand> [flags]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

import config_manager
import migrate as migrate_mod
import vault_index
from case_analyzer import analyze, hold_candidate, promote_candidate, reject_candidate
from confidence import compute_confidence
from learned_patterns import filter_by_topic_text, group_by_stage, load_learned_patterns
from detect_tier import (
    benchmark_ollama,
    build_tier_report,
    check_ollama,
    check_playwright,
    check_searxng,
    check_tesseract,
    check_whisper,
    check_ytdlp,
    detect_tier,
    get_hardware_info,
    get_platform_info,
    recommend_model,
)
from state import (
    LockTimeoutError,
    abandon_run,
    acquire_state_lock,
    add_manual_url,
    add_seen_urls,
    aggregate_fetch_results,
    aggregate_hop_summaries,
    append_written_note,
    apply_hop_decision,
    apply_replan_readmit,
    build_case_record,
    complete_run,
    create_run,
    init_topic,
    is_stale_run,
    load_run,
    load_stage_output,
    mark_superseded,
    note_modified_since,
    quality_gate,
    record_applied_pattern,
    record_user_decision,
    save_stage_output,
    save_state,
    update_stage,
    write_case_record,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class CLIError(Exception):
    """User-facing failure: printed to stderr, exit 1."""


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _vault(args) -> Path:
    if args.vault:
        return Path(args.vault).resolve()
    found = config_manager.find_vault(Path.cwd().resolve())
    if found is None:
        raise CLIError(
            "no researcher vault found: no .researcher/config.json in the current "
            "directory or any parent. Run /researcher-setup, or start Claude Code "
            "inside your vault."
        )
    return found


def _researcher_dir(args) -> Path:
    return _vault(args) / config_manager.CONFIG_DIR_NAME


def _state_dir(args) -> Path:
    return _researcher_dir(args) / "state"


# --- vault, config, index ---------------------------------------------------

def cmd_locate(args):
    rd = _researcher_dir(args)
    _out({
        "vault": str(rd.parent),
        "repo": str(REPO_ROOT),
        "scripts": str(REPO_ROOT / "scripts"),
        "state_dir": str(rd / "state"),
        "cases_dir": str(rd / "cases"),
    })


def cmd_migrate(args):
    vault = _vault(args)
    # migrate.py reports through a rich console on stdout; keep stdout pure JSON.
    with contextlib.redirect_stdout(sys.stderr):
        result = migrate_mod.migrate_vault_dir(vault)
    _out(result)


def cmd_load_config(args):
    _out(config_manager.load_config(_vault(args)))


def cmd_save_config(args):
    config = _read_json(args.config_file)
    config_manager.save_config(_vault(args), config)
    _out(config)


def cmd_update_index(args):
    _out(vault_index.update_index(_vault(args)))


def cmd_build_index(args):
    vault = _vault(args)
    db = vault_index.build_index(vault)
    _out({"db_path": str(db), "notes_indexed": len(vault_index.list_notes(vault))})


def cmd_vault_search(args):
    _out(vault_index.search(_vault(args), args.query, limit=args.limit))


def cmd_list_notes(args):
    folder = args.folder.replace("\\", "/").strip("/")
    prefix = f"{folder}/" if folder else ""
    notes = vault_index.list_notes(_vault(args))
    _out([n["path"] for n in notes if n["path"].startswith(prefix)])


# --- infrastructure ---------------------------------------------------------

def cmd_tier_report(args):
    cfg = config_manager.load_config(_vault(args)) or {}
    _out(build_tier_report(cfg.get("searxng_url"), REPO_ROOT))


def cmd_detect_tier(args):
    _out(detect_tier(args.searxng_url))


def cmd_check(args):
    if args.component == "searxng":
        _out(check_searxng(args.url))
        return
    checks = {
        "ollama": check_ollama,
        "ytdlp": check_ytdlp,
        "whisper": check_whisper,
        "playwright": check_playwright,
        "tesseract": check_tesseract,
        "platform": get_platform_info,
    }
    _out(checks[args.component]())


def cmd_hardware_info(args):
    _out(get_hardware_info())


def cmd_recommend_model(args):
    _out(recommend_model(args.ram_gb, args.vram_gb))


def cmd_benchmark_ollama(args):
    _out(benchmark_ollama(args.model))


def cmd_scan_vault(args):
    _out(vault_index.scan_conventions(_vault(args)))


# --- run lifecycle ----------------------------------------------------------

def _active_run(state_dir: Path) -> dict:
    run = load_run(state_dir)
    if run is None:
        raise CLIError("no active run")
    return run


def cmd_check_run(args):
    sd = _state_dir(args)
    run = load_run(sd)
    if run is not None:
        run["is_stale"] = is_stale_run(sd)
    _out(run)


def cmd_abandon_run(args):
    sd = _state_dir(args)
    run = load_run(sd)
    if run is None:
        _out({"archived": None})
        return
    if args.at_gate:
        run["abandoned_at_gate"] = True
        save_state(sd, run)
    abandon_run(sd)
    _out({"archived": str(sd / "history" / run["run_id"])})


def cmd_create_run(args):
    sd = _state_dir(args)
    create_run(sd, args.run_id, args.tier)
    update_stage(sd, "triage")
    _out(load_run(sd))


def cmd_set_strategy(args):
    sd = _state_dir(args)
    run = _active_run(sd)
    run["strategy"] = args.strategy
    save_state(sd, run)
    _out({"strategy": args.strategy})


def cmd_init_topics(args):
    sd = _state_dir(args)
    plan = _read_json(args.plan_file)
    run = _active_run(sd)
    run["topics"] = [init_topic(t["topic"], t["mode"], t["depth"]) for t in plan["topics"]]
    save_state(sd, run)
    update_stage(sd, "hop_loop")
    if args.save_plan:
        save_stage_output(sd, "research_plan", plan)
    _out({"topics": len(run["topics"]), "stage": "hop_loop"})


def cmd_update_stage(args):
    if args.total is None and (args.completed is not None or args.current is not None):
        raise CLIError("--completed/--current require --total")
    progress = None
    if args.total is not None:
        progress = {"total_notes": args.total, "completed": args.completed, "current": args.current}
    update_stage(_state_dir(args), args.stage, progress)
    _out({"stage": args.stage})


def cmd_save_stage_output(args):
    sd = _state_dir(args)
    save_stage_output(sd, args.name, _read_json(args.file))
    if args.stage:
        update_stage(sd, args.stage)
    _out({"saved": str(sd / f"{args.name}.json"), "stage": args.stage})


def cmd_load_stage_output(args):
    _out(load_stage_output(_state_dir(args), args.name))


# --- hop loop -----------------------------------------------------------------

def cmd_learned_patterns(args):
    run = load_run(_state_dir(args))
    topics = [t["topic"] for t in run.get("topics", [])] if run else []
    lp, warnings = load_learned_patterns(_researcher_dir(args) / "learned_patterns.md")
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    grouped = group_by_stage(filter_by_topic_text(lp, topics=topics))
    if args.stage:
        _out([{"id": p.id, "name": p.name, "body": p.body} for p in grouped.get(args.stage, [])])
    else:
        _out({stage: [p.id for p in patterns] for stage, patterns in grouped.items()})


def cmd_record_applied_pattern(args):
    sd = _state_dir(args)
    for pid in args.pattern_id:
        record_applied_pattern(sd, pid)
    _out({"recorded": args.pattern_id})


def cmd_add_seen_urls(args):
    add_seen_urls(_state_dir(args), topic_name=args.topic, urls=args.urls)
    _out({"topic": args.topic})


def cmd_add_browser_fetch(args):
    sd = _state_dir(args)
    results = sd / f"fetch_results_hop{args.hop}.json"
    if not results.is_file():
        raise CLIError(f"{results.name} not found; run fetch_and_clean.py for hop {args.hop} first")
    # fetch_and_clean pulls in pymupdf/playwright; import it only for this command.
    import fetch_and_clean
    fetch_and_clean.validate_url(args.url)  # same SSRF guard as every other fetch
    content = Path(args.content_file).read_text(encoding="utf-8")  # before any state write
    if not content.strip():
        raise CLIError("content file is empty")
    add_seen_urls(sd, topic_name=args.topic, urls=[args.url])  # validates the topic first
    record = fetch_and_clean.add_browser_fetch(results, args.url, args.title, content, Path(args.cache_dir or fetch_and_clean.DEFAULT_CACHE_DIR))
    _out({"url": args.url, "hop": args.hop, "word_count": record["word_count"]})


def cmd_add_manual_url(args):
    add_manual_url(_state_dir(args), args.url, args.reason, args.topic)
    _out({"url": args.url, "reason": args.reason})


def cmd_apply_hop_decision(args):
    resp = _read_json(args.response_file)
    decision = args.decision or resp["decision"]
    apply_hop_decision(
        _state_dir(args),
        topic_name=args.topic,
        hop_data=_read_json(args.hop_data_file),
        decision=decision,
        confidence_score=resp["confidence_score"],
        contradiction_rate=resp["contradiction_rate"],
        next_hop=resp.get("next_hop"),
        replan_hint=resp.get("replan_hint"),
    )
    _out({"topic": args.topic, "decision": decision})


def _confidence_sources(data) -> list[dict]:
    """A bare sources list, or a fetch_results file ({"fetched": [...]}).

    From fetch_results, use the fetched entries not marked usable: false, with
    tier / is_primary / credibility_score copied from the matching
    search_context.selected_urls entry (fetched entries don't carry them).
    """
    if isinstance(data, list):
        return data
    if "fetched" not in data:
        raise ValueError("sources file must be a list or a fetch_results object with 'fetched'")
    selected = {
        s["url"]: s for s in (data.get("search_context") or {}).get("selected_urls", [])
        if isinstance(s, dict) and "url" in s
    }
    if data["fetched"] and not selected:
        print("warning: fetch_results has no search_context.selected_urls; "
              "sources are scored without tier/primary signals", file=sys.stderr)
    sources = []
    for entry in data.get("fetched", []):
        if entry.get("usable") is False:
            continue
        signals = {k: v for k, v in selected.get(entry["url"], {}).items()
                   if k in ("tier", "is_primary", "credibility_score")}
        sources.append({**signals, **entry})
    return sources


def cmd_compute_confidence(args):
    files = args.sources_file
    sources = [s for path in files for s in _confidence_sources(_read_json(path))]
    if len(files) > 1:  # multi-hop: one source per URL, later file wins
        sources = list({s.get("url", id(s)): s for s in sources}.values())
    _out(compute_confidence(sources, depth=args.depth))


# --- quality gate, aggregation, write -----------------------------------------

def cmd_quality_gate(args):
    _out(quality_gate(_active_run(_state_dir(args))))


def cmd_mark_superseded(args):
    mark_superseded(_state_dir(args), args.topic, args.reason)
    _out({"topic": args.topic, "status": "superseded"})


def cmd_apply_replan_readmit(args):
    sd = _state_dir(args)
    apply_replan_readmit(sd, _read_json(args.specs_file))
    _out({"replan_count": _active_run(sd)["replan_count"], "stage": "hop_loop"})


def cmd_record_user_decision(args):
    sd = _state_dir(args)
    _active_run(sd)
    details = {} if args.confidence is None else {"confidence": args.confidence}
    record_user_decision(sd, decision=args.decision, **details)
    _out({"decision": args.decision})


def cmd_set_low_confidence(args):
    sd = _state_dir(args)
    run = _active_run(sd)
    run["low_confidence"] = True
    run["final_confidence_score"] = args.confidence
    save_state(sd, run)
    _out({"low_confidence": True, "final_confidence_score": args.confidence})


def cmd_aggregate_summaries(args):
    sd = _state_dir(args)
    combined = aggregate_hop_summaries(sd, args.project)
    _out({"path": str(sd / "summaries.json"), "items": len(combined["items"])})


def cmd_aggregate_fetch_results(args):
    sd = _state_dir(args)
    combined = aggregate_fetch_results(sd)
    _out({"path": str(sd / "fetch_results_aggregated.json"), "fetched": len(combined["fetched"])})


def cmd_check_mtime(args):
    since = args.since or _active_run(_state_dir(args))["started_at"]
    _out(note_modified_since(Path(args.path), since))


def cmd_append_written_note(args):
    append_written_note(_state_dir(args), args.topic, args.path, args.model)
    _out({"path": args.path})


# --- completion and pattern graduation ----------------------------------------

def _final_path(args) -> Path:
    return _researcher_dir(args) / "tmp" / "final_run.json"


def cmd_complete_run(args):
    final = complete_run(_state_dir(args))
    path = _final_path(args)
    if final is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)  # never leave a stale final for write-case-record
    _out(final)


def cmd_write_case_record(args):
    final_path = Path(args.final_file) if args.final_file else _final_path(args)
    if not final_path.is_file():
        raise CLIError(f"{final_path} not found; run complete-run first")
    final = _read_json(final_path)
    case = build_case_record(final, _read_json(args.inputs_file))
    cases_dir = _researcher_dir(args) / "cases"
    write_case_record(cases_dir, case)
    _out({"case_path": str(cases_dir / f"{case['case_id']}.json")})


def cmd_analyze_case(args):
    rd = _researcher_dir(args)
    with acquire_state_lock(rd):
        result = analyze(
            case_path=Path(args.case_path),
            accumulator_path=rd / "accumulator.json",
            learned_patterns_path=rd / "learned_patterns.md",
            cases_dir=rd / "cases",
        )
    _out({
        "promotion_candidates": [
            {"pattern_id": c.pattern_id, "name": c.name,
             "proposed_promotion_body": c.proposed_promotion_body, "evidence": c.evidence}
            for c in result.promotion_candidates
        ],
        "contradictions": result.contradictions,
        "warnings": result.warnings,
        "score_updates_applied": result.score_updates_applied,
        "demotions_applied": result.demotions_applied,
    })


def _report_branch(reason: str | None, done: str) -> None:
    if reason:
        print(f"BRANCH_ABORTED: {reason}", file=sys.stderr)
        _out({"status": "aborted"})
    else:
        _out({"status": done})


def cmd_promote_pattern(args):
    rd = _researcher_dir(args)
    with acquire_state_lock(rd):
        reason = promote_candidate(args.pattern_id, accumulator_path=rd / "accumulator.json",
                                   learned_patterns_path=rd / "learned_patterns.md")
    _report_branch(reason, "promoted")


def cmd_reject_pattern(args):
    rd = _researcher_dir(args)
    with acquire_state_lock(rd):
        reason = reject_candidate(args.pattern_id, accumulator_path=rd / "accumulator.json")
    _report_branch(reason, "rejected")


def cmd_hold_pattern(args):
    rd = _researcher_dir(args)
    with acquire_state_lock(rd):
        reason = hold_candidate(args.pattern_id, accumulator_path=rd / "accumulator.json")
    _report_branch(reason, "held")


# --- entry point --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--vault", help="vault root (default: search upward from the current directory)"
    )
    parser = argparse.ArgumentParser(prog="rw.py", description="researcher skill CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    add("locate", cmd_locate, "print vault, repo, scripts, state_dir and cases_dir")
    add("migrate", cmd_migrate, "rename .research-workflow/ to .researcher/ (one-time)")
    add("load-config", cmd_load_config, "print the vault config, or null")
    p = add("save-config", cmd_save_config, "save a config JSON file as the vault config")
    p.add_argument("--config-file", required=True)
    add("update-index", cmd_update_index, "incrementally update the vault FTS index")
    add("build-index", cmd_build_index, "rebuild the vault FTS index from scratch")
    p = add("vault-search", cmd_vault_search, "full-text search the vault index")
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=20)
    p = add("list-notes", cmd_list_notes, "vault-relative paths of indexed notes")
    p.add_argument("--folder", default="", help="only notes under this vault-relative folder")
    add("tier-report", cmd_tier_report, "Stage 0b tier report (auto-starts SearXNG)")
    p = add("detect-tier", cmd_detect_tier, "highest available tier, no auto-start")
    p.add_argument("--searxng-url")
    p = add("check", cmd_check, "check one component")
    p.add_argument("component", choices=["ollama", "ytdlp", "whisper", "searxng", "platform",
                                         "playwright", "tesseract"])
    p.add_argument("--url", help="SearXNG URL (for: check searxng)")
    add("hardware-info", cmd_hardware_info, "total RAM and GPU VRAM in GB")
    p = add("recommend-model", cmd_recommend_model, "Ollama model for this hardware")
    p.add_argument("--ram-gb", type=float, required=True)
    p.add_argument("--vram-gb", type=float, required=True)
    p = add("benchmark-ollama", cmd_benchmark_ollama, "time one short Ollama prompt")
    p.add_argument("--model", required=True)
    add("scan-vault", cmd_scan_vault, "detect vault folder/tag/frontmatter/MOC conventions")
    add("check-run", cmd_check_run, "active run (with is_stale) or null")
    p = add("abandon-run", cmd_abandon_run, "archive the active run to history/")
    p.add_argument("--at-gate", action="store_true", help="mark abandoned_at_gate first (Stage 5c)")
    p = add("create-run", cmd_create_run, "create the run and set stage triage")
    p.add_argument("--run-id", required=True)
    p.add_argument("--tier", required=True)
    p = add("set-strategy", cmd_set_strategy, "persist the resolver strategy on the run")
    p.add_argument("--strategy", required=True, choices=["planning_only", "intent_planning", "unified"])
    p = add("init-topics", cmd_init_topics, "init topics from a plan file; stage becomes hop_loop")
    p.add_argument("--plan-file", required=True)
    p.add_argument("--save-plan", action="store_true", help="also save research_plan.json (Stage 3b)")
    p = add("update-stage", cmd_update_stage, "set the run stage, optionally with write progress")
    p.add_argument("--stage", required=True)
    p.add_argument("--total", type=int)
    p.add_argument("--completed", type=int)
    p.add_argument("--current")
    p = add("save-stage-output", cmd_save_stage_output, "save a JSON file as STATE_DIR/<name>.json")
    p.add_argument("--name", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--stage", help="then set this stage")
    p = add("load-stage-output", cmd_load_stage_output, "print STATE_DIR/<name>.json or null")
    p.add_argument("--name", required=True)
    p = add("learned-patterns", cmd_learned_patterns,
            "learned-pattern ids per stage for the run's topics, or one stage's records")
    p.add_argument("--stage", choices=["search", "hop_planner", "classify"])
    p = add("record-applied-pattern", cmd_record_applied_pattern, "record injected pattern ids on the run")
    p.add_argument("--pattern-id", required=True, nargs="+")
    p = add("add-seen-urls", cmd_add_seen_urls, "append fetched URLs to a topic's seen_urls")
    p.add_argument("--topic", required=True)
    p.add_argument("--urls", required=True, nargs="+")
    p = add("add-browser-fetch", cmd_add_browser_fetch,
            "record a page read in the orchestrator's browser into fetch_results_hop<N>.json")
    p.add_argument("--hop", required=True)
    p.add_argument("--topic", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--content-file", required=True)
    p.add_argument("--cache-dir", help="fetch cache (default: the repo's .cache/fetch)")
    p = add("add-manual-url", cmd_add_manual_url, "list a URL for the user to read by hand (Stage 10b)")
    p.add_argument("--url", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--topic")
    p = add("apply-hop-decision", cmd_apply_hop_decision, "atomic Stage 4e hop transition")
    p.add_argument("--topic", required=True)
    p.add_argument("--response-file", required=True, help="the hop-planner JSON response")
    p.add_argument("--hop-data-file", required=True, help="the just-completed hop record")
    p.add_argument("--decision", choices=["continue", "stop", "early_terminated", "replan"],
                   help="override the planner decision (early_terminated)")
    p = add("compute-confidence", cmd_compute_confidence, "confidence score for a sources list or fetch_results JSON file")
    p.add_argument("--sources-file", required=True, action="append",
                   help="repeatable; with several files sources are merged by URL, later file wins")
    p.add_argument("--depth", required=True, choices=["quick", "standard", "deep", "exhaustive"])
    add("quality-gate", cmd_quality_gate, "Stage 5a per-topic pass/fail and worst confidence")
    p = add("mark-superseded", cmd_mark_superseded, "retire a topic; the quality gate skips it")
    p.add_argument("--topic", required=True)
    p.add_argument("--reason", required=True)
    p = add("apply-replan-readmit", cmd_apply_replan_readmit, "atomic Stage 5b/5c re-admission")
    p.add_argument("--specs-file", required=True,
                   help="JSON list of {topic_name, replan_hint (object or null)}")
    p = add("record-user-decision", cmd_record_user_decision, "log a quality-gate user decision")
    p.add_argument("--decision", required=True)
    p.add_argument("--confidence", type=float)
    p = add("set-low-confidence", cmd_set_low_confidence, "flag the run low-confidence (Stage 5d)")
    p.add_argument("--confidence", type=float, required=True)
    p = add("aggregate-summaries", cmd_aggregate_summaries, "merge summaries_hop*.json into summaries.json")
    p.add_argument("--project", required=True)
    add("aggregate-fetch-results", cmd_aggregate_fetch_results,
        "merge fetch_results_hop*.json into fetch_results_aggregated.json")
    p = add("check-mtime", cmd_check_mtime, "was this note modified after the run started?")
    p.add_argument("--path", required=True)
    p.add_argument("--since", help="ISO timestamp (default: the run's started_at)")
    p = add("append-written-note", cmd_append_written_note, "record a written note")
    p.add_argument("--topic", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--model", required=True)
    add("complete-run", cmd_complete_run, "archive the run; print and save the final run dict")
    p = add("write-case-record", cmd_write_case_record, "Stage 10c case record from final + inputs")
    p.add_argument("--inputs-file", required=True)
    p.add_argument("--final-file", help="default: VAULT/.researcher/tmp/final_run.json")
    p = add("analyze-case", cmd_analyze_case, "Stage 10d case analyzer, under the state lock")
    p.add_argument("--case-path", required=True)
    for name, func, help_text in (
        ("promote-pattern", cmd_promote_pattern, "Stage 10e: promote a candidate pattern"),
        ("reject-pattern", cmd_reject_pattern, "Stage 10e: permanently reject a candidate"),
        ("hold-pattern", cmd_hold_pattern, "Stage 10e: hold a candidate for later"),
    ):
        p = add(name, func, help_text)
        p.add_argument("--pattern-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (CLIError, LockTimeoutError, RuntimeError, KeyError, ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
