# Researcher plugin: Python environment and single-line invocations — Design

**Status:** approved 2026-10-25
**Branch:** `fix/fetch-cache-and-python-env`

## Problem

The `/researcher` skill and its agents run their Python with bare `python`, mostly as multi-line `python -c "..."` blocks (46 call sites in `skills/researcher/SKILL.md`, 7 in `agents/`). Two separate failures follow:

1. **Interpreter.** The plugin ships no environment. Bare `python` resolves to whatever is first on PATH, which may lack `requests`, `pymupdf`, `python-docx`, `rich` and `PyYAML`. The installed plugin runs from `~/.claude/plugins/cache/<marketplace>/researcher/<version>/`, which is replaced on every update, so a repo-level `.venv` or `mise.toml` does nothing for real runs.
2. **Invocation.** Multi-line `python -c` is fragile (shell quoting and backslash mangling) and is blocked outright by shell-guard hooks on some machines. Orchestrators work around it by writing each block to a temp file.

Related: the skill's `{{REPO_ROOT}}` and `{{VAULT_ROOT}}` placeholders, and setup's `{{PYTHON_PATH}}`, are never filled in the installed copy, so every run reconstructs the paths by hand.

## Decision

Run all plugin Python through **`uv run --project <plugin root>`** against a shipped `pyproject.toml` + `uv.lock`, and replace the inline `python -c` blocks with subcommands of one CLI, `scripts/rw.py`. uv becomes a hard requirement.

Rejected alternatives:
- *Setup-created venv recorded in vault config:* needs Python to read the config before it knows which Python to use; unpinned deps; stale when the venv or base interpreter changes.
- *`uv run --no-project --with-requirements requirements.txt` per call:* no new files, but resolves deps every call, unpinned, and long command lines.

## Design

### 1. Packaging

- New `pyproject.toml`: `requires-python = ">=3.12"`, `dependencies` = the current `requirements.txt` set (requests, pymupdf, python-docx, rich, PyYAML), a `dev` dependency group with pytest and pytest-mock, and the existing pytest config if any. Not an installable package (`[tool.uv] package = false`); scripts stay directly runnable.
- Commit `uv.lock`. Delete `requirements.txt` so dependencies are declared once.
- yt-dlp and openai-whisper stay optional and outside the lock; setup keeps offering to install them (now via `uv pip install` into the project env, or `uv tool install yt-dlp`).
- First `uv run` in a fresh plugin cache creates `.venv` inside the plugin folder from the lock; a plugin update gets a fresh folder and uv rebuilds it automatically.

### 2. Locating the plugin and the vault

- `REPO` = two directories above the skill's base directory (`skills/researcher/` → plugin root). Claude Code announces the base directory when the skill loads; the skill states this rule at the top and uses `"$REPO"` everywhere.
- `rw.py locate` walks up from the current working directory to the first directory containing `.researcher/config.json` and prints `{"vault": ..., "repo": ..., "scripts": ..., "state_dir": ..., "cases_dir": ...}`. Exit 1 with a clear message if none is found (user should run `/researcher-setup` or cd into the vault).
- Remove `{{REPO_ROOT}}`, `{{VAULT_ROOT}}` and `{{PYTHON_PATH}}` from all skills and agents.

### 3. `scripts/rw.py`

- One argparse CLI with one subcommand per skill glue block (about 40). Names follow the existing function names, kebab-cased: `locate`, `migrate`, `load-config`, `detect-tier`, `update-index`, `check-run`, `abandon-run`, `create-run`, `update-stage`, `set-strategy`, `init-topics`, `save-stage-output`, `load-stage-output`, `load-learned-patterns`, `record-applied-pattern`, `add-seen-urls`, `apply-hop-decision`, `apply-replan-readmit`, `quality-gate`, `record-user-decision`, `set-low-confidence`, `aggregate-summaries`, `aggregate-fetch-results`, `check-mtime`, `append-written-note`, `complete-run`, `write-case-record`, `analyze-case`, `promote-pattern`, `reject-pattern`, `hold-pattern`, and any others the skill needs. The plan enumerates the exact list from the skill.
- Each subcommand is a thin wrapper: parse args, call the existing function in `state.py` / `confidence.py` / `learned_patterns.py` / `accumulator.py` / `case_analyzer.py` / `vault_index.py` / `config_manager.py` / `detect_tier.py` / `migrate.py`, print JSON. **No pipeline logic moves into rw.py**; any multi-step logic that currently lives only in a skill block (e.g. the Stage 5a per-topic pass/fail check, Stage 6a/7a aggregation) becomes a small function in the module it belongs to, with rw.py calling it.
- Inputs: scalar flags (`--topic`, `--stage`, `--decision`, ...) and **file paths for JSON payloads** (`--plan-file`, `--hop-data-file`, `--specs-file`), never inline JSON. Paths default from `locate` (state dir etc.) so most calls need no path flags.
- Output: JSON on stdout. Errors: message on stderr, non-zero exit. State writes keep using the existing atomic helpers and `acquire_state_lock` where the skill used them.
- Encoding: all file I/O in rw.py and new helpers uses `encoding="utf-8"` explicitly (Windows defaults to cp1252).

### 4. Skill and agent edits

- At the top of `skills/researcher/SKILL.md`: the REPO rule, the invocation pattern `RW="uv run --project \"$REPO\" scripts/rw.py"` (written out literally in each call, since shell vars don't persist between Bash calls), and a Stage 0 uv check (`uv --version`; if missing, stop with install instructions for Windows/macOS/Linux).
- Every inline block becomes a single-line call: `uv run --project "$REPO" "$REPO/scripts/rw.py" <cmd> ...`. Existing scripts: `uv run --project "$REPO" "$REPO/scripts/fetch_and_clean.py" --input ...` (same for summarize, fetch_media, search_searxng, extract_local, produce_output).
- Agents (`classify-agent`, `hop-planner`, `thread-discoverer`, `topic-resolver`, `wikilink-scanner`): same conversion; the orchestrator passes `repo` in the dispatch payload instead of `scripts_dir` + implicit python.
- `skills/researcher-setup/SKILL.md`: drop `{{PYTHON_PATH}}`; check for uv; run `uv sync --project "$REPO"` once to warm the env; optional-tool installs via uv.
- Batch workflow (`lib/` research-batch and `fetch-summarize-runner` agent): replace `python_path: <sys.executable>` with the uv invocation (pass `repo`; the runner builds the command).
- README: requirements (uv, Python 3.12 fetched by uv if absent), dev setup (`uv sync`, `uv run pytest`). MANIFEST: add `rw.py`, `pyproject.toml`, `uv.lock`; drop `requirements.txt`.

### 5. Testing

- `tests/test_rw_cli.py`: for each subcommand, run it against a temp vault (fixture creating `.researcher/config.json` + state dir) via `main([...])` and assert the JSON output and the resulting state. Use monkeypatch to stub network-touching calls (`detect-tier`).
- `tests/test_skill_contract.py` (extend): fail if `skills/**/SKILL.md`, `agents/*.md` or `lib/**` contain `python -c`, a bare `python ` / `python3 ` command outside fenced explanatory prose, a `{{REPO_ROOT}}` / `{{VAULT_ROOT}}` / `{{PYTHON_PATH}}` placeholder, or an `rw.py` subcommand name that rw.py does not define (parse subcommands from the argparse parser).
- Full suite passes with `uv run --project . pytest` from the repo root.

### 6. Out of scope

- Changing pipeline behavior, stage order, agent prompts' substance, or any script's logic beyond extracting skill-only helpers.
- Making the plugin work without uv.

## Implementation

Mechanical conversion by a Sonnet subagent working from the implementation plan; Opus subagent reviews the diff against this design and the plan before commit. Commit attribution names the model that actually wrote each commit.
