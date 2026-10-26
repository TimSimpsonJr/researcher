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
from state import LockTimeoutError

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
