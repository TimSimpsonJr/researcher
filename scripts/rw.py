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
from detect_tier import (
    benchmark_ollama,
    build_tier_report,
    check_ollama,
    check_searxng,
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
    create_run,
    init_topic,
    is_stale_run,
    load_run,
    load_stage_output,
    save_stage_output,
    save_state,
    update_stage,
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
    p.add_argument("component", choices=["ollama", "ytdlp", "whisper", "searxng", "platform"])
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
