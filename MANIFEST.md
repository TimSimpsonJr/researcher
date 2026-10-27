# MANIFEST

## Stack

Python 3.12 (uv, locked) · requests, pymupdf, python-docx, rich, PyYAML, playwright · pytest + pytest-mock (offline; `browser` marker = real Chromium) · Claude Code plugin "researcher" v3.2 · Node >=18 (node:test) · SQLite FTS5 vault index · repo-local Chromium shell in `.cache/ms-playwright` (`PLAYWRIGHT_BROWSERS_PATH`) · optional Tesseract (OCR), Ollama, SearXNG

## Structure

```
.claude-plugin/plugin.json, marketplace.json   # Plugin manifest (name "researcher", librarian dep) + single-plugin marketplace
pyproject.toml, uv.lock, .python-version       # uv env: Python 3.12 + locked deps; pytest markers incl. `browser`
package.json, .gitignore, README.md, CLAUDE.md # node --test harness; ignores; user docs; contributor rules
.gitattributes                                 # eol=lf for .claude/workflows/*.js (CRLF breaks Workflow scriptPath)

skills/researcher/SKILL.md                     # Sonnet orchestrator: inline Stages 0-10 (4b browser pass, 10b manual URLs), batch router 3.5, fan-out 4B
skills/researcher-setup/SKILL.md               # Interactive setup wizard: vault config, tier detection, index build

agents/topic-resolver.md, hop-planner.md       # Sonnet: NL prompt -> plan; between-hop continue/stop/replan
agents/search-agent.md, classify-agent.md      # Haiku: tiered web search; vault folder/tag/wikilink assignment
agents/thread-discoverer.md, wikilink-scanner.md, case-analyzer.md  # Haiku: follow-up leads; link scan; pattern-merge compare
agents/fetch-summarize-runner.md               # Haiku: drives fetch_and_clean.py + summarize.py for the batch workflow

scripts/rw.py                                  # argparse CLI, 45 subcommands, JSON to stdout; sole Python entry for skills/agents
scripts/config_manager.py, migrate.py          # {vault}/.researcher/config.json; .env and dir-rename migrations
scripts/state.py, confidence.py                # v3 state + atomic transitions + lock; depth profiles/confidence (twin of lib/confidence.js)
scripts/accumulator.py, learned_patterns.py    # v3.1 candidate store; graduated-pattern md parser/writer
scripts/pattern_detection.py, score_updates.py, case_analyzer.py  # v3.1 heuristics, W/L scoring, Stage 10d analyzer
scripts/fetch_and_clean.py                     # Two-phase fetch: Jina/direct, then browser/Wayback chosen by verdict, else needs_browser; full-text cache
scripts/block_detect.py                        # classify() -> Verdict/FetchResult via MARKERS: blocked, login wall, JS shell, short, ok
scripts/url_safety.py                          # SSRF guard: scheme/host/IP checks, get_validated() with redirect validation
scripts/fetch_playwright.py                    # BrowserSession headless Chromium; route + websocket guard, redirect-chain validation
scripts/wayback.py                             # Archive fallback: process-wide rate limiter, CDX lookup, raw `id_` snapshot fetch
scripts/pdf_extract.py                         # PyMuPDF text, optional Tesseract OCR, image-only PDF handoff (pdf_path)
scripts/fetch_media.py, extract_local.py       # Media embeds; local file extract
scripts/summarize.py, search_searxng.py, produce_output.py  # Ollama summarize ("needs visual read" for image-only PDFs); SearXNG; output formats
scripts/detect_tier.py, vault_index.py         # base/mid/full tier detection; FTS5 vault search index
scripts/text_utils.py, vault_lint.py, find_broken_links.py  # slug helpers + html_to_text; frontmatter + broken-link gates
scripts/prompts/                               # Summarize/extract/synthesis templates, vault_rules.txt, output_formats/ (README.md inside)

lib/confidence.js, schemas.js                  # JS twins (confidence parity via shared vectors) + agent-return JSON schemas (incl. manual_urls)
.claude/workflows/research-batch.js            # Batch fan-out Workflow; inlines lib/*.js (gitignored, force-added)
tools/reinline-workflow.mjs                    # Refreshes the workflow's inlined confidence/schemas blocks

docker/, assets/, template-vault/              # SearXNG compose+settings; Obsidian CSS snippet; starter-vault scaffold

tests/test_rw_cli.py                           # rw.py CLI: subcommand dispatch and JSON output contract
tests/test_skill_contract.py                   # Skills/agents: only frozen one-line rw.py calls, no placeholders/unknown subcommands
tests/test_manifest_budget.py                  # This file's line/word budget guard
tests/test_*_contract.py                       # Agent prompt contracts (case_analyzer, classify, hop_planner, search, topic_resolver)
tests/test_fetch_chain.py, test_block_detect.py, test_wayback.py  # Fetch fallback chain; verdict classifier; Wayback client
tests/test_fetch_playwright.py, test_pdf_extract.py, test_fetch_and_clean.py  # Browser guard (some `browser`-marked); PDF/OCR; fetch entry point
tests/test_<script>.py                         # One offline module per other script; plus test_v3_compat, test_pattern_evolution, test_research_state_mechanics
tests/conftest.py, fixtures/                   # Shared fixtures; case_learning/, research_integration/ JSON; pages/ (block-page HTML/md samples)
test/*.test.js, test/fixtures/vault/           # node:test: confidence, parity, schemas, workflow contract; 3-note fixture vault

docs/plans/2026-10-27-fetch-fallbacks-design.md  # Fetch-fallback design (this branch); -plan.md alongside is the task list
docs/plans/2026-10-25-python-env-design.md     # uv env + rw.py + frozen-invocation design
docs/plans/                                    # Earlier designs/plans: pipeline rework, methodology, v3.1, batch fan-out, rename audit
docs/TAGGING-REFERENCE.md, docs/handoffs/      # Tag taxonomy; session handoffs
```

## Key Relationships

- Skills and agents run Python only as `uv run --frozen --project "REPO" "REPO/scripts/rw.py" <cmd>`; `test_skill_contract.py` rejects bare/inline Python, unfrozen uv, and unknown subcommands.
- `rw.py` wraps every script in `scripts/`; `test_rw_cli.py` pins its 45 subcommands and JSON output.
- Env is `pyproject.toml` + `uv.lock` + `.python-version`; `--frozen` never re-resolves. Rationale: `docs/plans/2026-10-25-python-env-design.md`.
- Fetch fallbacks (design: `docs/plans/2026-10-27-fetch-fallbacks-design.md`): `fetch_and_clean.py` runs `block_detect.classify`, then `fetch_playwright`/`wayback` per verdict, all URLs gated by `url_safety`.
- `fetch_results.needs_browser` -> SKILL Stage 4b browser pass -> `rw add-browser-fetch` or `add-manual-url` -> Stage 10b lists manual URLs for the user.
- `image_only_pdf`/`pdf_path` (from `pdf_extract`) -> `summarize.py` "needs visual read" -> orchestrator Claude Read of the PDF.
- `compute-confidence` takes `fetch_results` files (repeatable, `--topic`); superseded topics (`mark-superseded`) are skipped by `quality-gate` and case records.
- Batch runner `manual_urls` -> workflow result -> SKILL 4B.3/4B.4 -> `add-manual-url`; schema in `lib/schemas.js`.
- `fetch_and_clean.py` caches full cleaned text; length limits apply when reading from cache, not when writing.
- Inline path: `skills/researcher/SKILL.md` drives `state.py` transitions, `confidence.py`, and `vault_index.py` feeding classify/thread/resolver/wikilink agents.
- Batch path: Stage 3.5 routes `N > batch_threshold` web_research batches to `research-batch.js` (needs `librarian`); inline path handles the rest.
- `research-batch.js` inlines `lib/confidence.js` + `lib/schemas.js`; `test/contract.test.js` guards sync; `fetch-summarize-runner` supplies Bash/Python via `rw.py`.
- v3.1 case learning: `case_analyzer.analyze()` (Stage 10d) wires pattern_detection, accumulator, learned_patterns, score_updates; state in `{vault}/.researcher/` under `state.acquire_state_lock`.
- `migrate.migrate_vault_dir` runs at Stage 0a: one-time `.research-workflow/` -> `.researcher/` rename.
