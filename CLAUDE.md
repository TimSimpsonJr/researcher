# Research Workflow — Project Guide

## What this is

A Claude Code plugin for deep research into Obsidian vaults. The `/researcher` skill is the main entry point — it orchestrates a multi-hop pipeline with depth profiles, confidence-based replanning, and source credibility tiering. Stages: triage, resolve, hop loop (search/fetch/media/summarize/hop-planner per hop), quality gate, classify, write, wikilink scan, discover threads, complete. Three modes: single topic, batch, and thread-pull. Three planning strategies: planning_only (clear queries), intent_planning (ambiguous), unified (batch / full plan presentation).

## Architecture

- **Plugin** (`plugin.json`): Declares skills and agents for Claude Code discovery. No direct Claude API calls — everything goes through Claude Code's Task tool and Bash.
- **Skills** (`skills/`): Claude Code skill definitions (Markdown). The orchestrator (`researcher`) dispatches Haiku subagents defined in `agents/`. The setup wizard (`researcher-setup`) handles first-run configuration.
- **Agents** (`agents/`): Subagent definitions read by the research skill at runtime and passed as prompts via the Task tool. Six agents: topic-resolver (Sonnet), hop-planner (Sonnet), search-agent (Haiku), classify-agent (Haiku), thread-discoverer (Haiku), wikilink-scanner (Haiku). Sonnet handles reasoning-heavy stages (intent parsing, between-hop decisions); Haiku handles parallel-friendly stages (search, classify, summarize fallback, discover, wikilink scan).
- **Scripts** (`scripts/`): Python tools for I/O, caching, extraction, and pure-Python math. No Claude API calls. `confidence.py` is the pure-Python formula library (depth profiles, confidence/contradiction scoring) shared by the hop-planner agent and `state.py`.
- **Config** (`config_manager.py`): JSON-based vault config stored at `{vault}/.researcher/config.json`. Replaces the old `config.py` + `.env` pattern.
- **State** (`state.py`): Pipeline checkpoints with crash recovery. The skill checkpoints after every stage and can resume from the last completed stage.
- **Prompts** (`scripts/prompts/`): Text templates for summarization and synthesis. See `scripts/prompts/README.md` for the assembly pattern.

## Pattern Learning (v3.1.0)

After each run, Stage 10d's case analyzer scans recent case records for recurring heuristic signals (source-tier dominance, hop-pattern dominance, query-template recurrence) and accumulates candidate patterns at `{vault}/.researcher/accumulator.json`. Candidates earn promotion to `{vault}/.researcher/learned_patterns.md` after 3 observations (5 under a raised bar following demotion). Promoted patterns get injected into search-agent, hop-planner, and classify-agent prompts at Stages 4a/4e/6, biasing future runs toward what's worked before. W/L scoring runs end-of-run; patterns with W/(W+L) < 0.4 over >=5 uses are demoted. Two demotions -> permanent rejection. State writes are serialized via `state.acquire_state_lock` so concurrent /researcher runs can't race on the two vault files.

## Infrastructure tiers

`detect_tier.py` determines what's available at startup:

- **Base**: Claude Code only — full pipeline via subagents
- **Mid**: + Ollama — local summarization, faster classify
- **Full**: + SearXNG (Docker) — private web search

## Key conventions

- **No anthropic SDK, no `claude -p`**: The pipeline does not import or call the Anthropic API directly, and does not shell out to `claude -p`. All LLM work goes through Claude Code subagents (Task tool) or Ollama. The legacy `config.py` + `utils.py` + `claude_pipe.py` pattern has been fully removed.
- **Scripts are I/O only**: Python scripts handle fetching, caching, file extraction, and vault indexing. They do not make Claude API calls.
- **Model allocation**: Haiku for search/classification (cheap, parallel). Sonnet for orchestration. Subagent dispatch via Task tool.
- **State checkpoints**: Every pipeline stage writes state. Crash recovery resumes from the last checkpoint.

## Working on this project

- Tests: `uv run --project . pytest` — all tests run offline (no API key needed). The env is `pyproject.toml` + `uv.lock` (`uv sync` to set up).
- Config is generated per-vault by `researcher-setup` and stored in `{vault}/.researcher/config.json`
- Skills reference `{{VAULT_ROOT}}` and `{{REPO_ROOT}}` placeholders filled during plugin setup
- The `.claude/` and `docs/` directories are gitignored (force-add specific docs files when needed)

## Don't

- Don't add direct Claude API calls to skill files or scripts — use subagents (Task tool) or Ollama
- Don't put vault rules in individual prompt templates — they go in `vault_rules.txt` and are auto-included
