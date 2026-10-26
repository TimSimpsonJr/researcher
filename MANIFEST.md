# MANIFEST

## Stack

Python 3.12 (uv) · requests, pymupdf, python-docx, rich, PyYAML · pytest + pytest-mock (offline) · Claude Code plugin v3.2 "researcher" (Sonnet orchestrator + Sonnet topic-resolver/hop-planner + Haiku for search/classify/summarize/discover/case-analyzer) · Node >=18 (node:test) for batch JS twins · SQLite FTS5 vault index · optional Ollama (mid/full tier) + SearXNG, yt-dlp, Whisper, Playwright (full tier)

## Structure

```
.claude-plugin/plugin.json             # Plugin manifest — name "researcher", v3.2.0, librarian dependency
.claude-plugin/marketplace.json        # Single-plugin marketplace pointer (./ source)
pyproject.toml / uv.lock / .python-version  # uv-managed env: Python 3.12 + locked deps; dev group = pytest

skills/
  researcher/SKILL.md                  # Sonnet orchestrator — inline pipeline (Stages 0-10) + batch router (Stage 3.5) + batch fan-out (Stage 4B)
  researcher-setup/SKILL.md            # Interactive setup wizard — vault config, tier detection, index build

agents/
  topic-resolver.md                    # Sonnet — parses NL prompts into structured plans; no implicit topic cap
  search-agent.md                      # Haiku — web search per topic, T1-T4 source tiering
  classify-agent.md                    # Haiku — vault folder/tag/wikilink assignment (inline path)
  thread-discoverer.md                 # Haiku — scores results for follow-up leads
  wikilink-scanner.md                  # Haiku — scans new notes for wikilink opportunities
  hop-planner.md                       # Sonnet — between-hop reasoning; next-hop judgment
  case-analyzer.md                     # Haiku — semantic-compare for accumulator merge (v3.1)
  fetch-summarize-runner.md            # Haiku — drives fetch_and_clean.py + summarize.py for the batch workflow

scripts/
  rw.py                                # one-line CLI for all skill/agent Python glue; JSON out; run via uv run --frozen --project
  config_manager.py                    # JSON vault config at {vault}/.researcher/config.json; batch_threshold (default 10)
  state.py                             # v3 schema + atomic transitions (apply_hop_decision, apply_replan_readmit, acquire_state_lock)
  accumulator.py / learned_patterns.py # v3.1 candidate store + graduated-pattern md parser/writer at {vault}/.researcher/
  pattern_detection.py / score_updates.py / case_analyzer.py  # v3.1 heuristics, W/L scoring, Stage 10d analyzer wiring
  confidence.py                        # Pure-Python depth profiles + confidence/contradiction formulas (twin of lib/confidence.js)
  migrate.py                           # .env->config.json migration; folder renames; .research-workflow->.researcher dir migration
  detect_tier.py / vault_index.py      # base/mid/full tier detection; SQLite FTS5 vault search index
  fetch_and_clean.py / fetch_playwright.py / fetch_media.py   # Jina->Wayback->Playwright fetch + cache; JS fallback; media embeds
  extract_local.py / summarize.py / search_searxng.py / produce_output.py  # local extraction; Ollama summarize; SearXNG; format transforms
  text_utils.py / vault_lint.py / find_broken_links.py        # slug helpers; frontmatter + broken-link post-write gates

scripts/prompts/                       # Summarize/extract/synthesis templates + auto-appended vault_rules.txt + output_formats/

lib/                                   # Batch-workflow JS twins (node:test, zero deps, type:module)
  confidence.js                        # JS twin of confidence.py — kept value-for-value in sync via shared test vectors
  schemas.js                           # agent-return JSON schemas (SEARCH/SUMMARIES/HOP_NEXT/CLASSIFY/WIKILINK_RESULT/MOC_RESULT/THREADS)
.claude/workflows/research-batch.js    # Batch fan-out Workflow — hop loop via agentType + Librarian write span (gitignored; force-added)
tools/reinline-workflow.mjs            # Refreshes the workflow's inlined confidence/schemas blocks in place (not a bundler)
package.json                           # node --test harness

docker/ assets/ template-vault/        # SearXNG config (full tier); Obsidian CSS snippet; starter-vault scaffold

tests/                                 # pytest — one module per script (offline) + test_skill_contract / test_manifest_budget
test/                                  # node:test — confidence / confidence.parity / schemas / contract + fixtures/vault (3 notes)

docs/plans/, docs/handoffs/            # Design docs (gitignored except force-added)
```

## Key Relationships

- **Inline path (unchanged):** `skills/researcher/SKILL.md` Stages 0-10 orchestrate the single/small pipeline; `state.py` atomic transitions + `confidence.py` (shared by hop-planner via Bash) drive the hop loop; `vault_index.py` FTS5 feeds classify/thread/resolver/wikilink agents.
- **Batch path (new):** Stage 3.5 routes `N > batch_threshold` all-`web_research` batches (when `librarian` is available) to Stage 4B, which dispatches `.claude/workflows/research-batch.js`; everything else stays on the inline path.
- **The workflow** fans the per-topic hop loop out via `agentType` (researcher's search / fetch-summarize-runner / hop-planner; JS `confidence.js`/decide/replan) and delegates the write span to Librarian (classify-agent + the `librarian` skill writer + wikilink-scanner), then thread-discoverer.
- `research-batch.js` inlines `lib/confidence.js` + `lib/schemas.js` (exports stripped); `test/contract.test.js` guards the inline-sync + the Workflow launch contract; `tools/reinline-workflow.mjs` refreshes the blocks.
- `fetch-summarize-runner` wraps `fetch_and_clean.py` + `summarize.py` (the workflow has no Bash/Python) via `uv run --frozen --project <repo>`; the workflow passes `repo` in its config.
- **v3.1 case learning:** `case_analyzer.analyze()` (Stage 10d) wires `pattern_detection.py` + `accumulator.py` + `learned_patterns.py` + `score_updates.py`; state at `{vault}/.researcher/`, serialized by `state.acquire_state_lock`.
- `migrate.migrate_vault_dir` runs at Stage 0a — one-time `.research-workflow/`->`.researcher/` rename for existing vaults.
