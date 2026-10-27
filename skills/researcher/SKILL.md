---
name: researcher
description: 'Deep research pipeline for Obsidian vaults. Usage: /researcher "topic or natural language prompt". Supports batch research, thread-pulling from vault notes, local file ingestion, and multi-hop investigation with confidence-based replanning.'
---

# Research -- v3 Orchestrator (Multi-Hop Pipeline)

You are the orchestrator. You run a stateful multi-stage research pipeline that searches, fetches, summarizes, classifies, and writes vault notes -- with optional multi-hop investigation gated by confidence scoring. You dispatch Haiku subagents for cheap parallel work, Sonnet for resolver and hop-planner, and write final notes yourself (or escalate to Opus for synthesis notes).

## Bootstrap Constants

- `REPO` = the plugin root. When this skill loads, Claude Code prints its base directory (ending in `skills/researcher`); `REPO` is the directory two levels above it. Write that absolute path wherever `REPO` appears below, inside the quotes.
- `VAULT` and `STATE_DIR` = the `vault` and `state_dir` values printed by `rw.py locate` (Stage 0, Pre-flight).

## Running Python

All Python runs through uv against the plugin's locked environment (`REPO/pyproject.toml` + `REPO/uv.lock`). Every call is a single line:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" <subcommand> [flags]
```

The pipeline scripts run the same way, e.g. `uv run --frozen --project "REPO" "REPO/scripts/fetch_and_clean.py" --input ...`. Never invoke the Python interpreter directly and never write inline Python one-liners or multi-line Python blocks. Shell variables do not persist between Bash calls, so write each command out in full.

`rw.py` prints JSON on stdout. On failure it prints `ERROR: ...` on stderr and exits 1. JSON payloads (plans, hop records, replan specs, classification) are passed as files: write the JSON with the Write tool under `STATE_DIR/` and pass the path. `rw.py` finds the vault by walking up from the current directory to the first folder containing `.researcher/config.json`.

---

## Stage 0: Load Config and Detect Tier

### Pre-flight: uv and the vault

Check that uv is installed. Run via Bash:
```bash
uv --version
```
If the command is not found, stop and tell the user:
```
Researcher needs uv to run its Python scripts. Install it, then re-run /researcher:
  Windows (PowerShell):  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  macOS / Linux:         curl -LsSf https://astral.sh/uv/install.sh | sh
```
The first uv call after a plugin install or update builds the plugin's `.venv` from the lockfile (downloading Python 3.12 if needed). That takes up to a minute once; later calls start immediately.

Then locate the vault. Run via Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" locate
```
Store `VAULT` and `STATE_DIR` from the printed JSON. If it exits non-zero, show its message and stop: the user needs to run `/researcher-setup` or start Claude Code inside the vault.

### 0a. Load config

First, migrate any existing vault state from the pre-rename directory name
(`.research-workflow/` -> `.researcher/`). This is one-time and idempotent -- a
no-op for new vaults or vaults already migrated. Run via Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" migrate
```
This prints `"migrated"`, `"noop"`, or `"conflict"`. If it prints `"conflict"`, warn the
user that both `.research-workflow/` and `.researcher/` exist under the vault and
a manual merge is needed, then continue (config loads from `.researcher/`).

Then load config. Run via Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" load-config
```

Parse the JSON output. Extract and store:
- `ASSETS_DIR` = `VAULT/{cfg.assets}` (typically `VAULT/assets`)
- `OLLAMA_MODEL` = `cfg.ollama_model` (may be null)
- `SEARXNG_URL` = `cfg.searxng_url` (may be null)

If it prints `null`, output `ERROR: No config found. Run /researcher-setup first.` and stop.

### 0b. Detect tier (with auto-start and degradation alert)

Run via Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" tier-report
```

It reads `searxng_url` from the vault config and passes the plugin root for the SearXNG auto-start.

Parse the JSON. Extract and store:
- `TIER` = `report.tier`
- `OLLAMA_AVAILABLE` = `report.components.ollama.status == "ok"`
- `RECOMMENDED_MODEL` = `report.components.ollama.model` (if present)
- `SEARXNG_AVAILABLE` = `report.components.searxng.status == "ok"`
- `YTDLP_AVAILABLE` = `report.components.ytdlp.status == "ok"`
- `WHISPER_AVAILABLE` = `report.components.whisper.status == "ok"`

**If `report.degraded` is true**, show the user a tier alert:

```
⚠️  Running at {TIER} tier (max available: full)

Missing:
{for each item in missing_for_full:}
  - {item}
{end}

Impact:
  - mid tier: No SearXNG search merging — search uses Claude WebSearch only
  - base tier: No local summarization — all summarization uses Haiku (higher API cost)

Continue at {TIER} tier? [yes / fix and retry / cancel]
```

Wait for user response:
- **yes:** Continue with the degraded tier.
- **fix and retry:** Stop the pipeline. The user will fix the issue and re-run `/researcher`.
- **cancel:** Stop the pipeline entirely.

If SearXNG was auto-started (check `report.components.searxng.auto_started`), log:
`SearXNG container auto-started successfully.`

**If `report.degraded` is false** (tier is `full`), continue silently.

### 0c. Update vault index

Run via Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" update-index
```

This ensures the FTS5 index is current before any agent queries it. Log the stats but do not block on them.

---

## Stage 1: Check for Active Run

Run via Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check-run
```

**If `null`:** No active run. Proceed to Stage 2.

**If a run exists:**

Show the user:
```
Incomplete run detected: "{run_id}"
Stage: {stage}
Started: {started_at}
{if is_stale: "WARNING: This run is over 24 hours old."}

Resume / Restart / Abandon?
```

Use the user's response:
- **Resume:** Skip to the stage recorded in `stage`. Load any saved stage outputs from `STATE_DIR` and continue from there.
- **Restart:** Run `uv run --frozen --project "REPO" "REPO/scripts/rw.py" abandon-run` via Bash, then proceed to Stage 2.
- **Abandon:** Run the same abandon command and stop.

If `state.load_run()` returned None and the user just ran /researcher (no other reason for that), it may be that an old-schema (v2) run was abandoned silently. Stage 0's config load already printed the migration message -- no further action needed.

---

## Stage 2: Triage

The resolver classifies the prompt's strategy before resolving topics. The run is created at the START of this stage so that any downstream state-writing helper (`save_state`, `record_hop`, `add_usage`, etc.) has somewhere to write to -- even when the planning_only path bypasses Stage 3's full approval flow.

### 2a. Create the run

Generate a run ID from the current date and a slugified version of the user's input (e.g., `2026-03-05-sc-alpr-research`). Then:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" create-run --run-id "RUN_ID" --tier "TIER"
```

The run begins with no topics; topics are populated after the resolver returns (in 2c.iv or Stage 3).

### 2b. Dispatch topic-resolver agent (Sonnet)

Read the agent definition: `REPO/agents/topic-resolver.md`

Dispatch via the Task tool:
- `subagent_type`: `general-purpose`
- `model`: `sonnet`
- `prompt`: The full contents of `agents/topic-resolver.md`, followed by a `---` separator, followed by:

```
prompt: {the user's original input}
vault_root: {VAULT}
repo: {REPO}
```

### 2c. Parse strategy and persist it

The agent returns a JSON object. Read the top-level `strategy` field.

**Persist the strategy immediately** -- every path through Stage 2 ends up needing `run["strategy"]` set, so save it before branching. (`intent_planning` runs may re-dispatch in 2d and produce a different strategy; that re-dispatch overwrites this value.)

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" set-strategy --strategy STRATEGY_VALUE_FROM_RESOLVER
```

Then branch by strategy:

- `planning_only`: continue to Stage 2e (one-line confirm) with the resolved topics from this response, then Stage 2f.
- `intent_planning`: continue to Stage 2d (Q&A loop).
- `unified`: continue to Stage 3 (full resolve+plan flow with depth column), then Stage 2f before Stage 4. Note: the resolver has ALREADY produced topics; Stage 3 uses them rather than re-dispatching.

### 2d. Intent-planning Q&A (if applicable)

If strategy is `intent_planning`, the response contains `clarifying_questions[]` and topics are not yet resolved. For each question (max 3 for single-topic, max 1 for batch):

1. Present the question to the user.
2. Wait for the user's response.
3. Append the answer to the running prompt context.

After all questions are answered, re-dispatch the topic-resolver with the augmented prompt:

```
prompt: {original input}
clarifying_qa: {array of {question, answer} pairs}
vault_root: {VAULT}
repo: {REPO}
```

Expect the new response to have `strategy: "unified"` (rare cases: `planning_only`).

**Re-persist the strategy from the second response.** The first dispatch persisted `"intent_planning"` in 2c; that value is now stale because the clarified resolver may have returned a different strategy.

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" set-strategy --strategy NEW_STRATEGY_FROM_SECOND_DISPATCH
```

Then continue to 2e or Stage 3 accordingly.

If the user abandons mid-Q&A (cancel / explicit abort), run `uv run --frozen --project "REPO" "REPO/scripts/rw.py" abandon-run` and stop.

### 2e. Planning-only confirm (skip if strategy is unified)

Only entered when strategy is `planning_only`. Show the user:

```
Researching {project} at depth {topic.depth}, ~{estimated_minutes}min. Proceed? [yes / edit / cancel]
```

- `yes`: initialize topics from the resolver response (see snippet below), then run Stage 2f, then skip to Stage 4 (hop loop). Stage 3 is bypassed. Strategy was already persisted in 2c.
- `edit`: upgrade to `unified` strategy -- present the full plan as in Stage 3a below. The run is already created; Stage 3 will populate topics and approve. Also run `uv run --frozen --project "REPO" "REPO/scripts/rw.py" set-strategy --strategy unified` since the user chose to upgrade.
- `cancel`: run `uv run --frozen --project "REPO" "REPO/scripts/rw.py" abandon-run` and stop.

When `yes`: initialize topics now (since Stage 3 is being skipped), then advance the stage marker to `hop_loop` so a crash before Stage 4 resumes at the right place (not back at `triage`):

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" init-topics --plan-file "STATE_DIR/resolver_response.json"
```

`STATE_DIR/resolver_response.json` is the resolver's JSON response from Stage 2b/2d; write it with the Write tool first. `init-topics` creates one `init_topic` entry per `topics[]` item, saves the run, and sets the stage to `hop_loop`.

Note: strategy persistence now happens once in Stage 2c (immediately after the resolver returns), so every path through Stage 2 ends with `run["strategy"]` set.

### 2f. Load learned patterns (v3.1.0)

**When to run:** Once per /researcher invocation, after Stage 2 has selected a strategy and Stage 3 has produced the resolved topics list. Every path through Stage 2 (planning_only via 2e, intent_planning via 2d->2e, unified via Stage 3) must run Stage 2f BEFORE entering Stage 4 -- Stage 4a/4e and Stage 6 rely on `LEARNED_BY_STAGE` being populated by 2f. If you got here from 2e's `yes` branch, run 2f next, then enter Stage 4. If you got here from Stage 3b, run 2f, then enter Stage 4.

Run via Bash:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" learned-patterns
```

It reads `VAULT/.researcher/learned_patterns.md` and matches it against the topic strings already saved on the run in Stage 2e / 3b (each topic's `topic` field).

**Why topic-text matching, not `domain_tags` matching at this stage:** v3.0.0 only derives `domain_tags` at case-write time (Stage 10c), computed from tags assigned to written notes. At Stage 2 no notes exist yet. Topic strings are the strongest signal we have for relevance. Stage 10d's analyzer uses real `domain_tags` from the just-written case via `filter_relevant`.

**Known limitation of substring-only matching:** patterns tagged with high-level concepts that don't appear verbatim in topic text (e.g., a pattern tagged `["civic"]` from a prior run won't match the topic `"ALPR programs in Greenville"` because "civic" isn't in the topic string). This is intentional for v3.1.0 -- broader semantic matching would require an additional classification step at Stage 2 (cost we don't want to pay yet). The user benefits less from learned patterns early in a run but gets full credit at Stage 10b scoring once `domain_tags` are derived from written notes. Revisit for v3.2.0 if real usage shows this is too lossy.

Parse the JSON output and store:
- `LEARNED_BY_STAGE` = the returned dict mapping `search` / `hop_planner` / `classify` -> list of pattern IDs

If the file doesn't exist or returns empty, set `LEARNED_BY_STAGE = {"search": [], "hop_planner": [], "classify": []}`. Continue silently.

---

## Stage 3: Resolve

Stage 3 runs ONLY for `unified` strategy. The run was already created in Stage 2a and the resolver already dispatched in Stage 2b -- here we just present the plan and save the approval. Transition stage first:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" update-stage --stage resolve
```

### 3a. Present plan for approval

Show the user (using the resolver response from Stage 2c):
```
Research Plan: {project}

Topics ({count}):
{for each topic:}
  - [{depth}] {topic} ({mode})
{end}

{if local_sources:}
Local files ({count}):
{for each source:}
  - {path} ({type})
{end}
{end}

{if thread_pulls:}
Thread pulls ({count}):
{for each pull:}
  - From: {source_note}
    Leads: {extracted_leads joined}
{end}
{end}

Estimated usage:
  Search:    {search_agents} Haiku agents
  Summarize: {summarize_calls} calls ({if OLLAMA_AVAILABLE: "Ollama" else: "Haiku"})
  Classify:  {classify_agents} Haiku agent
  Write:     {write_messages}
  Total:     {total_claude_messages} Claude messages

Tier: {TIER}

Proceed? [yes / edit / cancel]
```

Wait for user response via the conversation:
- **yes / proceed:** Continue to 3b.
- **edit:** Let the user modify topics, modes, and depths, then re-display the plan.
- **cancel:** Abandon the run and stop.

### 3b. Save plan

Initialize per-topic state using `init_topic` and save the research plan. The run already exists (created in Stage 2a), so we append topics to it rather than re-create:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" init-topics --plan-file "STATE_DIR/resolver_response.json" --save-plan
```

Where `STATE_DIR/resolver_response.json` holds the approved plan (the resolver's JSON; if the user edited the plan in 3a, write the edited version first). `--save-plan` also saves it as `research_plan.json`.

Run Stage 2f before entering Stage 4.

---

## Stage 3.5: Batch Routing Decision

Run this AFTER Stage 2f and BEFORE entering Stage 4. It chooses one of two
execution paths for this run:

- **Inline path (default):** Stages 4–10 below, exactly as always. This is the
  route for single topics and normal-sized batches, and is **unchanged**.
- **Batch fan-out path:** **Stage 4B** (a Workflow-tool orchestration that fans
  the per-topic hop loop out in parallel and auto-queues large batches). This is
  an accelerator for *large web-research batches only*.

From the approved plan and the Stage 0 config, compute:
- `N` = number of entries in `PLAN_JSON['topics']`.
- `batch_threshold` = `config['batch_threshold']` (default `10` if the key is absent).
- `all_web_research` = every topic's `mode` is `web_research` (i.e. no
  `local_extraction` or `thread_pull` topics anywhere in the batch).

**Take the BATCH FAN-OUT path (Stage 4B) only if ALL THREE hold:**

1. `N > batch_threshold` — more topics than the vault's threshold, AND
2. `all_web_research` is true — the batch is purely web-research topics, AND
3. the `librarian` plugin is available in this session (its `librarian` skill is
   installed and invokable). The batch write span delegates note-writing to
   Librarian and has **no inline fallback inside the workflow**, so Librarian is
   a hard precondition — the same availability notion as Stage 7.0's gate.

If all three hold → **go to Stage 4B (Batch Fan-Out); skip Stages 4–10.**

**Otherwise → continue to Stage 4 below (the inline path).** When the batch was
*over* threshold but a condition failed, tell the user one line so the choice is
transparent:

- **Mixed modes** (over threshold, but some topics are `local_extraction` /
  `thread_pull`):
  > Batch has non-web topics; running the interactive pipeline instead of batch fan-out.
- **Librarian absent** (over threshold, all `web_research`, but `librarian` not
  installed/invokable):
  > The librarian plugin isn't available; running the interactive pipeline. Install `librarian` (Fieldwork marketplace) to enable large-batch fan-out.

When `N <= batch_threshold`, the inline path is the expected route — no note needed.

---

## Stage 4: Hop Loop

For each hop level from 1 to the maximum `max_hops` across all topics, run the search->fetch->media->summarize->hop-planner sequence for all topics that are still active at this level.

Loop structure:

```python
hop_level = 1
while any(t.status == "active" and t.current_hop < t.max_hops for t in run.topics):
    active = [t for t in run.topics if t.status == "active" and t.current_hop < t.max_hops]

    # 4a-4d: process this hop level for all active topics
    # See substages below.

    # 4e: hop-planner decides continue/stop/replan per topic
    for topic in active:
        dispatch hop-planner
        apply decision

    hop_level += 1
```

### 4a. Search (parallel across topics)

For each active topic, choose hop_context based on how the topic got admitted at this iteration:

- **Hop 1 (fresh topic):** dispatch search-agent normally with `topic`, `existing_urls`, `depth`. No hop_context preamble.

- **Hop 2+ following a hop-planner `continue` decision:** the prior hop-planner returned `next_hop`, which Stage 4e persisted to `topic.next_hop`. Read it from state:
  - `pattern`: `topic.next_hop.pattern`
  - `from`: `topic.next_hop.from`
  - `seen_urls`: the topic's seen_urls list

  **Do not clear `next_hop` here.** Stage 4e will overwrite it (or set it to None on stop/replan) when the new hop-planner response lands. Clearing eagerly in 4a would leave the in-flight hop with no routing state if the process crashes between 4a and 4e -- resume could not reconstruct `hop_context`.

- **Hop following a quality-gate replan (Stage 5b or 5c re-admission):** the topic has a stored `replan_hint` instead of a prior `next_hop`. Read it (don't clear):
  - `pattern`: `topic.replan_hint.suggested_pattern`
  - `from`: `topic.replan_hint.suggested_query_focus` (treated as the focus topic/entity for the search)
  - `seen_urls`: the topic's seen_urls list
  - Optional: include the `issue` field as a brief addition to the preamble (e.g., "previous gap: thin sources")

  **Do not clear `replan_hint` here either.** Stage 4e's continue branch is the one place that clears it (via `set_replan_hint(topic, None)`); the stop and replan branches leave it alone or overwrite it. This keeps the hop's routing context durable across crashes.

**Routing precedence for Stage 4a:** when both `next_hop` and `replan_hint` are set (shouldn't happen in normal operation but possible after partial fixes), prefer `replan_hint` -- it's the more recent quality-gate intent. Stage 4e's continue branch handles the cleanup by clearing `replan_hint` once a hop completes successfully, so the precedence collision is short-lived.

The search-agent's prompt template doesn't change. The orchestrator prepends a hop-context preamble for hop 2+:

```
HOP CONTEXT: This is hop {N} of {max_hops} for topic "{topic}". Use the {pattern} pattern, focusing on "{from}". Skip URLs in seen_urls.
{if replan_hint: "Previous gap: " + replan_hint.issue}
```

**Learned-pattern injection (v3.1.0):** if `LEARNED_BY_STAGE["search"]` is non-empty, ALSO load each pattern's full record and append a `## Learned Patterns` block to the search-agent prompt:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" learned-patterns --stage search
```

It prints `[{id, name, body}]` for the run's `search`-stage patterns, the records behind `LEARNED_BY_STAGE["search"]`.

Build a `## Learned Patterns` block from the returned records (4-space indented to show the literal markdown the orchestrator emits):

    ## Learned Patterns (from prior runs, may or may not apply)

    - **{name}** -- {body}

    (repeat per pattern)

Append this block to the search-agent's user prompt under the existing context. Then, for each pattern surfaced, record it in run state:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" record-applied-pattern --pattern-id PATTERN_ID
```

Run once per pattern id.

Batch dispatches at <=5 topics per round.

After dispatch, collect all `selected_urls` arrays from the search agent responses (and merge SearXNG results when `SEARXNG_AVAILABLE`, deduplicating by URL). Build a per-hop `search_context_hop{N}.json` file in `STATE_DIR/` keyed off active topics for this iteration.

### 4b. Fetch

Run `fetch_and_clean.py` once for all active topics' selected URLs at this hop:

```bash
uv run --frozen --project "REPO" "REPO/scripts/fetch_and_clean.py" --input "STATE_DIR/search_context_hop{N}.json" --output "STATE_DIR/fetch_results_hop{N}.json"
```

**Browser pass.** `fetch_results_hop{N}.json` has a `needs_browser` list (and `stats.needs_browser`): `{url, reason, attempts}` entries the scripted fetchers could not get past (bot walls, 403s on government PDFs). It never holds unsafe or private-network URLs; those are in `failed`. If it is empty, skip to the `seen_urls` step below. Otherwise read each entry yourself, before moving on:

- **Browser pane tools available** (`mcp__Claude_Browser__navigate`, `mcp__Claude_Browser__get_page_text`, `mcp__Claude_Browser__javascript_tool`):
  - **HTML page:** `mcp__Claude_Browser__navigate` to the URL, then `mcp__Claude_Browser__get_page_text`. A passive "checking your browser" page may clear by itself; wait a few seconds and read again.
  - **PDF** (the URL ends in `.pdf`, or the page shows a PDF viewer): first `mcp__Claude_Browser__navigate` to a public page on the PDF's own site, then run the pdf.js script below with `mcp__Claude_Browser__javascript_tool`, with `PDF_URL` replaced. It fetches the file in-page with the browser's cookies and returns the text of every page.
  - If the page still shows a CAPTCHA or an interactive "verify you are human" check, do not click it or try to solve it. Record the URL as manual (below) and move on.
  - Otherwise pass the text you read straight to the Write tool as `STATE_DIR/browser_hop{N}_{index}.md` (do not quote or echo it elsewhere; it stays out of your context that way) and record it (TOPIC is the topic whose selected URLs held this URL):

    ```bash
    uv run --frozen --project "REPO" "REPO/scripts/rw.py" add-browser-fetch --vault "VAULT" --hop N --topic "TOPIC" --url "URL" --title "TITLE" --content-file "STATE_DIR/browser_hop{N}_{index}.md"
    ```

    It appends the page to `fetch_results_hop{N}.json` (`fetch_method: "browser"`), removes it from `needs_browser`, caches it and adds it to the topic's `seen_urls`.
- **No browser tools** (plain CLI, or a batch workflow): record every `needs_browser` entry as manual.

Recording a manual URL (REASON is the entry's `reason`, or `captcha`):

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" add-manual-url --vault "VAULT" --url "URL" --reason "REASON" --topic "TOPIC"
```

Stage 10b lists them for the user. Never change the browser's user agent, use stealth tricks, or use a CAPTCHA-solving service.

The pdf.js script for `mcp__Claude_Browser__javascript_tool` (pdf.js 4.0.379, worker `pdf.worker.min.mjs`):

```js
const pdfjs = await import('https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.0.379/pdf.min.mjs');
pdfjs.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.0.379/pdf.worker.min.mjs';
const buf = await (await fetch('PDF_URL', { credentials: 'include' })).arrayBuffer();
const doc = await pdfjs.getDocument({ data: buf }).promise;
const pages = [];
for (let i = 1; i <= doc.numPages; i++) {
  const content = await (await doc.getPage(i)).getTextContent();
  pages.push(content.items.map((item) => item.str).join(' '));
}
pages.join('\n\n');
```

If the script returns little or no text, the PDF is a scan: navigate to the PDF itself and read it from the screen, or record it as manual.

Update each topic's `seen_urls` with the URLs that were successfully fetched, using the `add_seen_urls` state helper. For each topic, pass the list of URLs that ended up in `fetch_results_hop{N}.fetched` (i.e., the URLs that actually returned content, not the failed ones):

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" add-seen-urls --topic "TOPIC" --urls "URL_1" "URL_2"
```

The helper dedupes — calling it again with overlapping URLs (e.g., the same URL appears in multiple hops) is safe.

**If `fetched` is empty and `failed` is non-empty:** evaluate this after the browser pass, so URLs recovered by the browser (now in `fetched`) count as successes; `needs_browser` URLs that were not recovered count as failures. Treat the hop as a failure for the affected topic(s). The hop-planner in 4e will see zero new sources and decide accordingly. If ALL topics had zero successful fetches, abandon the run and stop (no fetched content means downstream stages have nothing to work with).

### 4c. Media

Before invoking `fetch_media.py`, split this hop's `fetch_results_hop{N}.json` into per-article content files. For each entry in `fetched`, write its `content` field to `STATE_DIR/content_hop{N}_{index}.md` (where `{index}` is the 0-based index of the article in `fetched`). Then run `fetch_media.py` per article:

```bash
uv run --frozen --project "REPO" "REPO/scripts/fetch_media.py" --content "STATE_DIR/content_hop{N}_{index}.md" --assets-dir "ASSETS_DIR" --topic "{topic_slug}" --run-id "{RUN_ID}" --output "STATE_DIR/rewritten_hop{N}_{index}.md"
```

Replace the original `content` in `fetch_results_hop{N}` with the rewritten content from the output files.

### 4d. Summarize

Run `summarize.py` over the hop's fetch_results. Write to `summaries_hop{N}.json`.

**If Ollama is available (mid or full tier):**
```bash
uv run --frozen --project "REPO" "REPO/scripts/summarize.py" --input "STATE_DIR/fetch_results_hop{N}.json" --model "RECOMMENDED_MODEL" --output "STATE_DIR/summaries_hop{N}.json"
```

Items that come back `usable: false` with reason `needs visual read` are image-only PDFs with almost no text (the fetch entry has `image_only_pdf: true` and a `pdf_path`). For each, dispatch a Haiku subagent with the base-tier prompt below plus the image-only rule, and replace the item in `summaries_hop{N}.json` with its summary. Image-only PDFs that do carry text are summarized normally and keep `image_only_pdf` / `pdf_path`.

**If Ollama is NOT available (base tier):**
```bash
uv run --frozen --project "REPO" "REPO/scripts/summarize.py" --input "STATE_DIR/fetch_results_hop{N}.json" --prepare-for-claude --output-dir "STATE_DIR/summaries_hop{N}/"
```

Then dispatch a Haiku subagent per article file (same prompt as v2 base-tier path, plus this image-only rule: when the file has an `Image-only PDF:` line, i.e. the fetch entry has `image_only_pdf: true`, Read the PDF at its `pdf_path` with the Read tool, up to 20 pages per call, and summarize from the page images), collect into `summaries_hop{N}.json` with shape `{"topic": "{project name}", "items": [...]}`. Leave each summary's `media_refs` empty -- media assets are already inlined into the rewritten article content as Obsidian embeds during Stage 4c, so the write stage picks them up directly from the per-hop fetch_results without needing a separate manifest.

### 4e. Hop-planner (parallel per-topic)

For each active topic at this hop level, dispatch the hop-planner agent (Sonnet):

- `subagent_type`: `general-purpose`
- `model`: `sonnet`
- `prompt`: The full contents of `agents/hop-planner.md`, followed by `---`, followed by:

```
topic: {topic.topic}
depth: {topic.depth}
current_hop: {hop_level}
max_hops: {topic.max_hops}
confidence_target: {topic.depth's target: quick 0.6, standard 0.7, deep 0.8, exhaustive 0.9}
summaries_so_far: {path to all summaries this topic has collected across all hops}
sources_so_far: {every fetch_results_hop{N}.json path for this topic, one per hop; hop-planner passes each to compute-confidence as a repeated --sources-file}
hop_genealogy: {topic.hop_genealogy as JSON}
seen_urls: {topic.seen_urls}
vault_index_path: {VAULT}/.researcher/vault_index.db
vault_root: {VAULT}
repo: {REPO}
```

**Learned-pattern injection (v3.1.0):** same shape as Stage 4a's learned-pattern injection, but for the hop-planner stage. If `LEARNED_BY_STAGE["hop_planner"]` is non-empty, load the full records with `uv run --frozen --project "REPO" "REPO/scripts/rw.py" learned-patterns --stage hop_planner`, append a `## Learned Patterns` block to the hop-planner's dispatch prompt under the existing context, then run `uv run --frozen --project "REPO" "REPO/scripts/rw.py" record-applied-pattern --pattern-id PATTERN_ID` once per surfaced pattern. See Stage 4a for the exact Bash commands.

Parse each hop-planner response. **Apply the full transition atomically via `apply_hop_decision()`** -- never via the per-field setters in this stage. The atomic helper persists genealogy + current_hop + confidence_history + contradiction_rate + next_hop / replan_hint / status in a single load -> mutate -> save cycle, so a crash partway through the transition cannot leave a topic with mismatched state (hop recorded but quality signals stale, status updated but routing stale, etc.).

Write this topic's hop-planner response to `STATE_DIR/hop_response_{topic_slug}.json`, and the just-completed hop's record (hop number, pattern, queries, sources_kept, ended_at, ...) to `STATE_DIR/hop_data_{topic_slug}.json`, with the Write tool. Then:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" apply-hop-decision --topic "TOPIC" --response-file "STATE_DIR/hop_response_{topic_slug}.json" --hop-data-file "STATE_DIR/hop_data_{topic_slug}.json"
```

It takes `decision`, `confidence_score`, `contradiction_rate`, `next_hop` (used only when decision is `continue`) and `replan_hint` (used only when decision is `replan`) from the response file. `early_terminated` is a special form of stop the hop-planner never emits: when Stage 4a's alternate-pattern attempt also failed, add `--decision early_terminated` to override the response's decision.

What each decision atomically does (all three branches always persist confidence + contradiction_rate from the hop-planner response, in addition to the branch-specific writes):

- `continue`: appends `hop_data` to genealogy, increments `current_hop`, appends `confidence_score` to history, overwrites `contradiction_rate`, sets `next_hop` to the planner's pick, clears `replan_hint`, leaves status `active`.
- `stop`: appends `hop_data`, increments `current_hop`, appends `confidence_score`, overwrites `contradiction_rate`, clears `next_hop`, sets status `complete`.
- `early_terminated` (orchestrator-substituted when Stage 4a's alternate-pattern attempt produced zero usable sources): same as `stop` but sets status `early_terminated`.
- `replan`: appends `hop_data`, increments `current_hop`, appends `confidence_score`, overwrites `contradiction_rate`, clears `next_hop`, sets `replan_hint` to the planner's diagnosis, sets status `replan_pending`. Stage 5 handles re-admission.

Stage 4f no longer exists -- quality signals are now part of the atomic 4e transition.

### 4f. Stage transition

When all topics have status != "active", transition to Stage 5:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" update-stage --stage quality_gate
```

---

## Stage 5: Quality Gate

After all topics have status != "active", compute the run-level quality signals and decide whether to proceed to classify, auto-replan, or prompt the user.

### 5a. Compute per-topic pass/fail

Each topic has its own depth and therefore its own `confidence_target` (quick 0.6, standard 0.7, deep 0.8, exhaustive 0.9). Check per-topic, not run-aggregate:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" quality-gate
```

It prints `{passed, failing_topics, topics, superseded, worst_confidence, replan_count}`. A topic passes when its latest confidence (0.0 if it has none) is at least its depth's `confidence_target` and its `contradiction_rate` is at most 0.3. `failing_topics` lists those that don't, each with `topic`, `depth`, `confidence`, `contradiction_rate` and `replan_hint`.

If `failing_topics` is empty: proceed to Stage 6 (classify).

A topic another one replaced (for example, merged into a sibling after a replan) can be retired instead of failing the gate:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" mark-superseded --topic "TOPIC" --reason "REASON"
```

`quality-gate` then leaves it out of `topics` and `failing_topics` and lists it under `superseded`.

For diagnostic display and the low-confidence note marker, `quality-gate` also returns `worst_confidence`: the lowest latest confidence across all non-superseded topics (0.0 if none).

This is the value used downstream as `OVERALL_CONFIDENCE` in Stage 5c's user-decision payload and 5d's `final_confidence_score`. There is no single "run target" because depths are per-topic, but the worst-topic confidence is the meaningful number to surface in user prompts and frontmatter callouts.

### 5b. Auto-replan (if eligible)

If `failing_topics` is non-empty AND `replan_count < 2`, run an auto-replan cycle:

1. For each failing topic, decide its hint:
   - If the topic has a stored `replan_hint` from a hop-planner `decision: "replan"`: use it directly.
   - Otherwise: synthesize a hint pointing at the gap (e.g., `{"issue": "thin sources", "suggested_pattern": "entity_expansion", "suggested_query_focus": "official agency data"}`).
2. **Re-admit each failing topic into the hop loop.** A topic that exhausted its initial budget has `current_hop == max_hops`, so Stage 4's admission filter (`current_hop < max_hops`) would otherwise skip it. The re-admission needs to set the hint, bump `max_hops` by 1, mark status `active`, increment `replan_count`, and roll the stage marker back to `hop_loop` -- **all in a single atomic state transition**. Use `apply_replan_readmit()`: a crash partway through must not leave the run with topics flagged active but stage stuck at `quality_gate`.
3. Return to Stage 4 (the hop loop runs one more pass; only re-admitted topics dispatch search/fetch/summarize/hop-planner). Stage 4a's search-agent reads `topic.replan_hint` to bias the next query toward the suggested pattern/focus.

Write the specs to `STATE_DIR/replan_specs.json` with the Write tool: one entry per failing topic, carrying either its existing `replan_hint` or a freshly synthesized one. `null` preserves an existing hint without overwriting it.

```json
[{"topic_name": "TOPIC", "replan_hint": {"issue": "thin sources", "suggested_pattern": "entity_expansion", "suggested_query_focus": "official agency data"}}]
```

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" apply-replan-readmit --specs-file "STATE_DIR/replan_specs.json"
```

`apply_replan_readmit` does the full transition (set replan_hint, bump max_hops, mark active, increment replan_count, set stage='hop_loop') in one load->mutate->save cycle. Do not call the per-field setters here -- they're not crash-safe across multiple writes.

### 5c. User prompt (auto-replan exhausted)

If `replan_count >= 2`, present the diagnostic. There are two sub-cases:

- **First time the user sees this prompt** (`replan_count == 2`): the two auto-replan attempts have completed and the user can opt into one more focused cycle (`replan`), accept lower-confidence notes (`continue`), or stop (`abandon`).
- **After the user-driven `replan` already fired** (`replan_count >= 3`): no more replans are allowed -- the gate has now spent the initial budget plus two auto-replans plus one user-approved replan. Present only `continue` and `abandon`.

The branch is `>= 2` (not `== 2`) deliberately: this catches the post-manual-replan re-entry to the gate. Without it, a third quality-gate failure would fall through both 5b's `replan_count < 2` guard and the old `== 2` check, leaving the pipeline stuck.

```
⚠ Quality gate triggered after {replan_count} replan attempts.

Topic-by-topic results:
{for each topic:}
  - {topic.topic}: confidence {topic.confidence_history[-1]:.2f}, contradictions {topic.contradiction_rate:.0%}
{end}

Weakest topics:
{for each weak topic:}
  - "{topic}": {gap description}
{end}

Options:
{if replan_count == 2:}
  - replan: try one more cycle with focused hints (1 extension max)
{end}
  - continue: write notes anyway, with low-confidence flags
  - abandon: stop here, preserve search/fetch results for inspection
```

**After `replan_count >= 3`, the user is offered only `continue` or `abandon` -- no more replans.** Do not present `replan` as an option in that case; reject it if the user types it anyway and re-prompt.

Wait for the user's response.

Record the decision:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" record-user-decision --decision DECISION_VALUE --confidence OVERALL_CONFIDENCE
```

Branch by decision:

- **`replan`** (only valid when `replan_count == 2`): apply the same re-admission recipe as Stage 5b via the atomic helper (`apply_replan_readmit`). For each failing topic, if its `replan_hint` is None, synthesize one (the user may also supply a manual hint via free-text input -- capture it as the `issue` field). Then dispatch the helper once with all specs:

  Write `STATE_DIR/replan_specs.json` as in Stage 5b, then:

  ```bash
  uv run --frozen --project "REPO" "REPO/scripts/rw.py" apply-replan-readmit --specs-file "STATE_DIR/replan_specs.json"
  ```

  This bumps `replan_count` to 3 in the same atomic save that re-admits the topics and rolls `stage` back to `hop_loop`. Return to Stage 4. After this attempt, no more replans -- if it fails again, re-enter Stage 5c with `replan_count == 3` and present only continue / abandon.
- **`continue`**: mark the run with `low_confidence = true` (see 5d). Proceed to Stage 6. The write stage adds body callouts to all written notes.
- **`abandon`**: set `abandoned_at_gate: true` in the run dict, save, then call `abandon_run(STATE_DIR)` (which already exists in state.py and archives via `_archive_run`). Print the archived path so the user can inspect:

  ```bash
  uv run --frozen --project "REPO" "REPO/scripts/rw.py" abandon-run --at-gate
  ```

  The archived files land under `STATE_DIR/history/{run_id}/` (the existing `_archive_run` destination). Print that path to the user.

### 5d. Save low-confidence flag

If decision was `continue`:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" set-low-confidence --confidence OVERALL_CONFIDENCE
```

---

## Stage 6: Classify

Transition stage first:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" update-stage --stage classify
```

### 6a. Aggregate per-hop summaries

Before aggregating, drop from every `summaries_hop{N}.json` any item still `usable: false` with reason `needs visual read` (the visual read in Stage 4d never replaced it).

Before dispatching the classify agent, aggregate all per-hop summary files into a single `summaries.json`:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" aggregate-summaries --project "PROJECT_NAME"
```

It writes `STATE_DIR/summaries.json` as `{"topic": PROJECT_NAME, "items": [...]}`; read that file for 6b.

### 6b. Dispatch classify agent

Read the agent definition: `REPO/agents/classify-agent.md`

Dispatch via the Task tool:
- `subagent_type`: `general-purpose`
- `model`: `haiku`
- `prompt`: The full contents of `agents/classify-agent.md`, followed by a `---` separator, followed by:

```json
{
  "summaries": {the aggregated summaries object from 6a},
  "vault_root": "VAULT",
  "repo": "REPO",
  "shared_context_files": {from the research plan}
}
```

**Learned-pattern injection (v3.1.0):** same shape as Stage 4a's learned-pattern injection, but for the classify stage. If `LEARNED_BY_STAGE["classify"]` is non-empty, load the full records with `uv run --frozen --project "REPO" "REPO/scripts/rw.py" learned-patterns --stage classify`, append a `## Learned Patterns` block to the classify-agent's dispatch prompt under the existing JSON context, then run `uv run --frozen --project "REPO" "REPO/scripts/rw.py" record-applied-pattern --pattern-id PATTERN_ID` once per surfaced pattern. See Stage 4a for the exact Bash commands.

### 6c. Parse classification

The agent returns a single JSON object. Parse it to extract:
- `notes_to_create` -- list of note specs with `title`, `filename`, `folder`, `action`, `type`, `write_model`, `content_summary`, `source_urls`, `tags`, `links`, `stub_links`, `media`, `priority`
- `vault_context` -- `existing_notes_found`, `suggested_moc_update`, `folder_conventions`
- `contradictions_detected` -- list of `{source_a, source_b, claim_a, claim_b, topic}` objects flagging where sources disagree (Phase 5 addition). May be empty.

If `notes_to_create` is empty:
Output: `Classification returned no notes to create. Check fetch results for content quality.`
Complete the run and stop.

### 6d. Save classification

Persist the parsed classify-agent response to `STATE_DIR/classification.json` so Stage 7's resume contract can find it, then advance the stage:

Write the parsed classify-agent response to `STATE_DIR/classify_response.json` with the Write tool, then:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" save-stage-output --name classification --file "STATE_DIR/classify_response.json" --stage write
```

This saves it atomically as `STATE_DIR/classification.json` and advances the stage to `write`.

---

## Stage 7: Write Notes

This is the core stage. **You (the Sonnet orchestrator) write the notes.** For synthesis notes, you escalate to Opus.

### 7.0 OPTIONAL — Delegate the write to the Librarian plugin (feature-flagged, default OFF)

> **This is a "prove the call path" shim, not a full migration.** It demonstrates that researcher *can* hand its note-writing to the standalone Librarian plugin behind a feature flag. The full migration (retiring the inline write path below) is a deliberate fast-follow and is NOT done here. By default this block does nothing and Stage 7 runs exactly as it always has.

**Gate.** Take the Librarian path ONLY if BOTH conditions hold:

1. `config.use_librarian` is `true` (from Stage 0's loaded config; a config missing this key counts as `false`), AND
2. the `librarian` plugin is actually available in this session (its `librarian` skill / `scripts/write_note.py` writer is installed and invokable).

If EITHER condition is false — which is the **default** — skip this entire subsection and proceed to **7a** below. The inline write path (7a–7d) is the unchanged, canonical behavior; nothing about it is modified by this shim, so a run with the flag off is byte-for-byte identical to researcher before Librarian existed.

**If the gate passes**, do the following instead of 7a–7d:

1. **Build Librarian's neutral input contract.** Transform each entry of `classification.notes_to_create[]` into one Librarian note spec. Librarian's writer (`write_note(spec, out_dir)`, invoked via the `librarian` skill) consumes a list of objects shaped:

   `[{title, content, frontmatter_meta, citations, link_hints, priority, action}]`

   plus an optional top-level `vault_context` (pass researcher's `classification.vault_context` through unchanged so Librarian can resolve placement and existing-note context).

   Field mapping, researcher note spec → Librarian neutral contract:

   | researcher note spec field         | Librarian contract field | notes |
   |-------------------------------------------|--------------------------|-------|
   | `title`                                   | `title`                  | direct pass-through |
   | the authored note body (composed per 7c.v, using `content_summary` as the writing guide) | `content` | **you still author the body here**, applying the same frontmatter/callout/wikilink/sources rules from 7c.v; `content` is the finished Markdown body. `content_summary` is *guidance*, not the body — expand it into full prose as today. |
   | `tags` + `type`                           | `frontmatter_meta`       | merge into one metadata object, e.g. `{ "tags": [...], "type": "..." }` |
   | `source_urls`                             | `citations`              | the source URLs backing the note |
   | `links` + `stub_links`                    | `link_hints`             | concatenate; Librarian decides which become `[[wikilinks]]` vs. stubs |
   | `priority`                                | `priority`               | pass-through (`primary` / `secondary` / `scan`) |
   | `action`                                  | `action`                 | pass-through (`create` / `update`) |

   **Dropped fields (do NOT forward):**
   - `write_model` — researcher's own model-routing concern; Librarian does not consume it.
   - `media` — dead v2 field (empty by design in v3; see 7c.v "Media embeds"). Source-inlined `![[path]]` embeds already live inside the `content` body, so they travel with `content`.
   - `filename` / `folder` — Librarian *derives* placement itself; do not pin it.

2. **Delegate the write.** Hand the assembled spec list (plus `vault_context`) to the Librarian plugin's writer via the `librarian` skill. Librarian performs its own classify → write → wikilink for each spec and writes the notes to the vault.

3. **Reconcile.** After Librarian returns the written-note paths, still run researcher's own bookkeeping so the rest of the pipeline is unaffected: record each written note via `append_written_note` (7c.vii), update progress via `update_stage('write', ...)` (7c.viii), and apply the MOC updates in **7d**. Then skip to Stage 8.

If the Librarian writer errors or is unexpectedly unavailable after the gate passed, fall back to the inline path (7a–7d) so the run still completes.

---

### 7a. Aggregate per-hop fetch results

Stage 4 produces one `fetch_results_hop{N}.json` per hop. Before writing notes, merge them into a single url -> content lookup so 7c.iii (full-source-content recovery) doesn't have to re-glob and re-parse per note. **Write atomically via temp+rename** so a crash mid-write cannot leave a corrupt JSON file on disk — the Resume Flow only rebuilds when the file is missing or unparseable, not when it merely looks "present":

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" aggregate-fetch-results
```

The aggregated file has the same `{"fetched": [...]}` shape as the v2 monolithic `fetch_results.json`, plus a convenience `by_url` index for O(1) lookups. Stage 7c.iii reads from this file.

### 7b. Sort notes by priority tier

Order the `notes_to_create` list:
1. `primary` (deep coverage) -- Tier 1 notes first
2. `secondary` (supporting) -- Tier 2
3. `scan` (brief) -- Tier 3

Writing Tier 1 first ensures that Tier 2 and Tier 3 notes can reference them with wikilinks.

### 7c. For each note, in order:

#### i. Check for mtime conflict

Before writing, check if the target note already exists and was modified after the run started:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check-mtime --path "VAULT/FOLDER/FILENAME"
```

It compares the file's mtime with the run's `started_at` and prints `{"conflict": true | false, "mtime": "..." | null}`.

If `conflict` is true:
- Print a warning: `Skipping "{title}": file was modified after run started (mtime: {mtime}). Manual merge needed.`
- Skip this note and continue to the next.

#### ii. Read context files

Read all files listed in `vault_context.existing_notes_found` using the Read tool. Also read any files listed in this note's `links` that correspond to real vault notes. Store their contents for reference.

#### iii. Read full source content

For each URL in this note's `source_urls`, look it up in `STATE_DIR/fetch_results_aggregated.json` (produced by Stage 7a) -- use the `by_url` index for O(1) access, or scan `fetched[]` if you prefer. Get the full `content` field of the matching entry. This is the source material for writing.

If a `source_urls` entry has no match in the aggregated file (e.g., the URL was selected by the search agent but fetch_and_clean.py failed to retrieve it), skip that URL and log a warning. Continue writing the note using the URLs that did fetch successfully.

#### iv. Determine model

Check the note's `write_model` field:
- `sonnet` -- you write it directly (you are Sonnet).
- `opus` -- this note requires deeper synthesis. You still write the note yourself, but add a note in the frontmatter: `write_model: opus`. The content you produce should reflect the synthesis scope -- connect threads across multiple sources, surface strategic implications, build comprehensive overviews.

#### v. Write the note content

Write the complete note following these rules:

**Frontmatter (YAML):**
```yaml
---
title: "{note title}"
tags: [{tags from classification, comma-separated}]
source: ["{source_urls joined}"]
created: {today's date, YYYY-MM-DD}
write_model: {sonnet or opus}
research_run: {RUN_ID}
confidence: {topic.confidence_history[-1] or 1.0 if single-hop}
contradictions_noted: {true if this note's sources appear in contradictions_detected else false}
primary_sources: {count of sources where is_primary == true}
hop_genealogy: [{list of pattern(from) strings for multi-hop runs, omitted for single-hop}]
---
```

**Body callouts (NEW for v3):**

If `run.low_confidence == true`, prepend this callout to the note body BEFORE the H1:

```
> ⚠ **Research confidence: {run.final_confidence_score:.2f}**. Several topics in this run did not reach the standard confidence target. Verify claims before citing.
```

If any contradiction in `classification.contradictions_detected` references a source URL that overlaps with this note's `source_urls`, prepend (or merge with the prior callout) a contradiction callout:

```
> ⚠ **Source contradictions noted.** Two or more sources disagree on aspects of this topic. See `## Sources` section for details.
```

**Wikilinks:**
- Add `[[wikilinks]]` from the classification's `links` list where the linked topic actually appears in the note content.
- Scan the note content for mentions of other vault notes (from the context files you read) and add `[[wikilinks]]` to them.
- For concepts in `stub_links` that do not have vault notes yet, add `[[stub wikilinks]]` on first mention. Do not create empty stub files.
- Use aliases for long titles: `[[Full Note Title|display text]]`.
- Do not wikilink generic terms -- only link specific, notable concepts worthy of their own note.

**Tags:**
- Use the tags from the classification.
- Verify they follow the taxonomy: content-type tag first (`research`, `legislation`, `campaign`, `plan`, `reference`, `tracking`, `decision`, `index`, `resource`, `meta`), then location tags, then domain tags.
- Limit to 2-5 tags per note.

**Sources:**
- Include the full source URL as an inline link at the point where it is first referenced in the body text.
- Add a `## Sources` section at the bottom listing all source URLs.
- In the Sources list, annotate each URL inline with tier and contradiction notes. Match contradictions by URL: any source URL that appears in a `contradictions_detected[].source_a` or `source_b` gets the contradiction annotation:

  ```
  ## Sources

  - https://source-a/ (T1) -- claims X
  - https://source-b/ (T2) -- claims Y (contradicts source-a on Z)
  ```

- Every factual claim from external research must be traceable to its source.

**Format matching:**
- If `action` is `update`, read the existing note first and merge new information into it. Expand sections. Never discard existing content.
- If the target `folder` contains existing notes (from `vault_context`), match their section structure and style.

**Media embeds:**
- Media embeds are already inlined into the source article content during Stage 4c (`fetch_media.py` rewrites the article body to include `![[path/to/asset]]` references in place). When composing the note body from the source content, preserve any `![[path]]` references found there -- do not strip them.
- Do not add new embeds from the classification's `note.media` field; that field is empty by design in v3 (the manifest-based media flow was replaced by inline embeds). The field is retained in the classification schema for backwards compatibility with v2 vault notes only.

**Content:**
- For `create`: Write the complete note from scratch using the fetched source content and `content_summary` as your guide.
- For `update`: Merge new information into the existing note. Preserve and expand sections. Never remove existing content.

#### vi. Save the note

- `create`: Write to `VAULT/{folder}/{filename}` using the Write tool. Create the folder first if needed via Bash: `mkdir -p "VAULT/{folder}"`
- `update`: Write to the existing note path using the Write tool.

#### vii. Track the written note

Run via Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" append-written-note --topic "TOPIC" --path "NOTE_PATH" --model MODEL
```

#### viii. Update progress

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" update-stage --stage write --total TOTAL --completed COMPLETED --current "CURRENT_TITLE"
```

### 7d. Update MOC files

After all notes are written:

1. If `vault_context.suggested_moc_update` is not null, read that MOC file and add/update entries for the notes written in this batch. Match the MOC's existing format exactly.

2. For each folder that received new notes, check if it contains a file starting with `_` or containing `MOC`, `Index`, `Hub`, or `Overview` that was not already processed. If found, update it too.

---

## Stage 8: Wikilink Scan

After all notes and MOCs are written, scan for wikilink opportunities between the new notes and existing project notes. Transition stage first:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" update-stage --stage wikilink_scan
```

### 8a. Refresh vault index

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" update-index
```

This ensures the newly written notes are indexed before the scanner queries the vault.

### 8b. Determine project folder

From the written notes list, extract the common parent folder. For example, if notes were written to `Projects/Activism/BJU/Bob Jones University.md` and `Projects/Activism/BJU/GRACE Report on Bob Jones University.md`, the project folder is `Projects/Activism/BJU`.

### 8c. Dispatch wikilink-scanner agent

Read the agent definition: `REPO/agents/wikilink-scanner.md`

Load the written notes list:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" load-stage-output --name written_notes
```

Dispatch via the Task tool:
- `subagent_type`: `general-purpose`
- `model`: `haiku`
- `prompt`: The full contents of `agents/wikilink-scanner.md`, followed by a `---` separator, followed by:

```json
{
  "new_notes": [{list of {path, title} from written_notes}],
  "project_folder": "{project folder vault-relative path}",
  "vault_root": "VAULT",
  "repo": "REPO"
}
```

### 8d. Parse and apply edits

The agent returns a JSON object with `edits` and `stats`.

For each edit in the `edits` array:
1. Read the target file using the Read tool
2. Find the first occurrence of `edit.find` in the file content, using `edit.context` for disambiguation if needed
3. Replace it with `edit.replace` using the Edit tool
4. If the `find` text is not found (perhaps already wikilinked or content changed), skip it and log a warning

### 8e. Report results

Log the results:
```
Wikilink scan: {stats.total_edits} edits applied
  New notes: +{stats.wikilinks_in_new_notes} wikilinks
  Existing notes: +{stats.wikilinks_in_existing_notes} wikilinks to new notes
```

If no edits were needed, log: `Wikilink scan: no new wikilinks needed.`

### 8f. Update state

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" update-stage --stage discover
```

---

## Stage 9: Discover Threads

### 9a. Dispatch thread-discoverer agent

Read the agent definition: `REPO/agents/thread-discoverer.md`

Load the written notes list:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" load-stage-output --name written_notes
```

Dispatch via the Task tool:
- `subagent_type`: `general-purpose`
- `model`: `haiku`
- `prompt`: The full contents of `agents/thread-discoverer.md`, followed by a `---` separator, followed by:

```json
{
  "project": "{project name}",
  "summaries": {the summaries items from Stage 6},
  "written_notes": {written notes list},
  "vault_root": "VAULT",
  "repo": "REPO"
}
```

### 9b. Parse thread proposals

The agent returns a JSON object with `threads` (sorted by score descending) and `batch_stats`.

### 9c. Present to user

Show the user:
```
Threads discovered from {project}:
{for each thread:}
  {index}. {topic} (score: {score}, {novelty_status})
     {rationale}
{end}

Batch stats: {total_entities_found} entities, {leads_above_threshold} leads above threshold

Research any of these? [1,2,3] / all / none
```

Wait for user response:
- **none:** Proceed to Stage 10.
- **all:** Save all threads as the next batch input.
- **specific numbers:** Save only the selected threads.

If threads are approved, save them to `STATE_DIR/approved_threads.json` for a follow-up `/researcher` invocation. Do NOT start a new pipeline run within this run.

---

## Stage 10: Complete

**Important sequencing:** `complete_run()` archives the state file (moves it under `history/{run_id}/`), so any `load_run()` call AFTER `complete_run()` returns `None`. Stage 10's telemetry, hop genealogy print, and case-record write must all use the dict returned BY `complete_run()` -- not call `load_run()` after the fact.

### 10a. Complete the run (capture final state)

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" complete-run
```

Parse the printed JSON. This is the FINAL run dict (with `completed_at` set). The state file has been moved to history at this point -- do NOT call `load_run` again.

`complete-run` also saves `final` to `VAULT/.researcher/tmp/final_run.json`, which Stage 10c reads.

### 10b. Print summary

Using the `final` dict from 10a, format and print:

```
Research complete: {project}

Created ({count} notes):
{for each:}
  - {path} (confidence {confidence})
{end}

Updated:
{for each updated note/MOC:}
  - {path}
{end}

Hop genealogy:
{for each topic:}
  Topic: {topic} ({hop_count} hops, confidence {confidence}{", " + status if status != "complete"})
  {for each hop in genealogy:}
    Hop {n} ({pattern or "initial"}): {sources_kept} sources kept
  {end}
{end}

Model usage:
  Haiku:   {haiku.calls} calls,  {haiku.in_tokens:,} in / {haiku.out_tokens:,} out
  Sonnet:  {sonnet.calls} calls, {sonnet.in_tokens:,} in / {sonnet.out_tokens:,} out
  Opus:    {opus.calls} call(s), {opus.in_tokens:,} in / {opus.out_tokens:,} out
  Ollama: {ollama.calls} calls (local -- no token cost)

Estimated cost: ${estimated_cost:.2f}

{if low_confidence:}
⚠ Low confidence run (score {final_confidence_score}). Notes marked with low-confidence callouts.
{end}

{if final.manual_urls:}
Needs a manual read ({count}): pages behind a CAPTCHA, or read with no browser available.
{for each in final.manual_urls:}
  - {url} ({reason})
{end}
{end}

{if threads approved:}
Threads queued for follow-up:
  - {topic} (priority: {priority})
  Run /researcher again to execute these.
{end}

Tier: {TIER} | Sources fetched: {total} | Notes written: {count} | Replans: {replan_count}
```

Cost estimation (rough):
- Haiku: $0.25/M input + $1.25/M output
- Sonnet: $3/M input + $15/M output
- Opus: $15/M input + $75/M output

Sum across models for the estimate.

### 10c. Write case record

Write the end-of-run inputs to `VAULT/.researcher/tmp/case_inputs.json` with the Write tool:

```json
{
  "query": "PROJECT_NAME",
  "domain_tags": DERIVED_TAGS,
  "patterns_that_worked": PATTERNS_WORKED,
  "patterns_that_failed": PATTERNS_FAILED,
  "sources_processed": SOURCES_COUNT,
  "notes_created": CREATED_COUNT,
  "notes_updated": UPDATED_COUNT
}
```

Then:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" write-case-record --inputs-file "VAULT/.researcher/tmp/case_inputs.json"
```

`write-case-record` reads `final` from `VAULT/.researcher/tmp/final_run.json` and builds the case record with `state.build_case_record`: `case_id` (the run_id), the strategy, per-depth topic counts, hops executed, per-topic confidence, the max contradiction rate, the run's `applied_patterns` and its user decisions, plus the inputs above. It prints `{"case_path": ...}`. DERIVED_TAGS comes from the most common tags across written notes. PATTERNS_WORKED / PATTERNS_FAILED come from per-hop telemetry (patterns that produced novel notes vs. dead ends). Each is a JSON object of this shape, which the Stage 10d pattern detectors read: `source_tiers` (object mapping source tier to its share of sources), `hop_chain` (array of hop pattern names), `queries` (array of query templates), and optionally `notes` (array or string of free text). Omit keys you have no data for; a bare list or string is wrapped under `notes` as a fallback.

### 10d. Run case analyzer (v3.1.0)

Run via Bash:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" analyze-case --case-path "CASE_PATH"
```

Substitute `CASE_PATH` with the `case_path` printed by `write-case-record`. `analyze-case` holds `acquire_state_lock` on `VAULT/.researcher/` while it runs `case_analyzer.analyze` against `VAULT/.researcher/accumulator.json`, `learned_patterns.md` and `cases/`.

Parse the JSON output.

**Corruption handling (v3.1.0 policy: no automatic recovery, no rebuild UX).**

`analyze()` itself protects state files: if `accumulator.json` or `learned_patterns.md` could not be parsed (or had a schema-version mismatch), the analyzer **does not write back to that file**. So a corrupt file stays as-is on disk -- the user can inspect or recover it. The trade-off is that any in-memory updates (score increments, new candidates, demotion sweep results) that would have touched the corrupted store are discarded for this run.

If `warnings` contains any of these markers:
- `accumulator_corrupted: ...`
- `accumulator_schema_mismatch: ...`
- `learned_patterns_corrupted: ...`
- `learned_patterns_schema_mismatch: ...`

Surface them to the user at Stage 10d output (these flow through to Stage 10's completion summary anyway via state telemetry), with a clear advisory:

```
WARNING: v3.1.0 pattern learning state was not updated this run:
  {warning text(s) from analyzer}

To reset the affected store, delete the file manually:
  {VAULT}/.researcher/accumulator.json
  {VAULT}/.researcher/learned_patterns.md
The next /researcher run will start with an empty store and rebuild from
new cases going forward. Existing case history at
{VAULT}/.researcher/cases/ is unaffected.
```

(There is no in-pipeline rebuild flow in v3.1.0. Corrupt-state recovery is rare; the simpler "user deletes file -> fresh start" path was preferred over carrying the complexity of a rebuild-from-history mechanism. Revisit in v3.1.x if real usage shows this is too coarse.)

**Stage 10e gating.** If any `learned_patterns_*` OR `accumulator_*` warning is present, **skip Stage 10e entirely for this run** -- even if `promotion_candidates` is non-empty. Stage 10e's promote/reject/hold branches would otherwise be unable to safely write the affected store (analyzer refused the write, so the load returns empty + warning, and a save would clobber the recoverable file). Log: "Skipping graduation prompts -- `learned_patterns.md` or `accumulator.json` needs manual repair first."

**Normal flow after warnings handling.**

If `promotion_candidates` is non-empty AND no `learned_patterns_*` or `accumulator_*` warnings, proceed to Stage 10e. Otherwise skip 10e.

If the analyzer fails entirely (script exits non-zero), log to state telemetry and continue silently. The run still completes; the analyzer just didn't update state this round.

### 10e. Graduation prompt (v3.1.0, conditional on Stage 10d output)

For each entry in `promotion_candidates`:

Look up any matching entries in `contradictions` (where `candidate_pattern_id == entry.pattern_id`). If present, include a `Possible contradiction` block in the prompt so the user can weigh whether the new pattern conflicts with an existing graduated one.

Show the user:

```
Learned pattern ready for promotion:

  Name: {name}
  Proposed body: {proposed_promotion_body}

  Evidence:
  {for each row in evidence:}
    - case {case_id}: {signal}
  {end}

  {if contradictions for this entry:}
  WARNING: Possible contradiction with already-graduated patterns
      in the same domain x stage:
  {for each conflicting_name:}
    - {conflicting_name} (id: {conflicting_id})
  {end}
  Promoting both keeps them side-by-side and lets the scoring loop
  sort it out. Rejecting this new pattern preserves the existing rule.
  {end}

Promote / Reject / Hold?
```

Use the user's response:

All three branches below acquire `acquire_state_lock` around the shared-state writes. This is the same lock Stage 10d uses -- without it, concurrent `/researcher` runs (background + foreground) could race on `accumulator.json` and `learned_patterns.md` and lose updates. The lock root is `VAULT/.researcher/`.

**Precondition for all three branches:** Stage 10d already verified that BOTH `learned_patterns.md` AND `accumulator.json` are parseable (no `learned_patterns_*` and no `accumulator_*` warnings) BEFORE letting control reach Stage 10e. If any such warning was present, Stage 10d skipped 10e entirely. So inside each branch we can assume the loaders return a usable file -- but every branch still defends against late corruption by checking warnings before saving (`BRANCH_ABORTED` on mismatch). The Reject and Hold branches would otherwise silently overwrite a recoverable corrupt accumulator with empty content. Each command prints `{"status": "promoted" | "rejected" | "held"}`, or `{"status": "aborted"}` plus a `BRANCH_ABORTED: ...` line on stderr when it refused to save over a store that loaded with warnings.

**Promote:**

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" promote-pattern --pattern-id PATTERN_ID
```

Write order is `learned_patterns.md` first, then `accumulator.json`; a pattern id already in `learned_patterns.md` is not added twice (recovery from a promotion that wrote one file but not the other).

**Reject:**

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" reject-pattern --pattern-id PATTERN_ID
```

**Hold:**

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" hold-pattern --pattern-id PATTERN_ID
```

If the user aborts (Ctrl+C, dismisses, walks away), do nothing. The `promotion_pending` flag remains set on the accumulator entry, and next-run Stage 10e will re-prompt.

---

## Stage 4B: Batch Fan-Out (large web_research batches)

> Reached **only** from Stage 3.5 when the batch routing conditions hold (N > `batch_threshold`, every topic `web_research`, `librarian` available). This **replaces Stages 4–10** for the run: the per-topic hop loop + write span execute inside the `research-batch` Workflow. This stage owns the cost-estimate approval, the dispatch, and the thread-selection gate. Do not run Stages 4–10 on this path.

### 4B.1 Up-front cost estimate + approval

Render an honest estimate from the approved plan. Per-topic agent cost scales with depth (search + fetch-summarize per hop, plus a hop-planner per `continue`):

| depth | max hops | ~agents/topic |
|-------|----------|---------------|
| quick | 1 | ~2 |
| standard | 3 | ~5–7 |
| deep | 4 | ~7–10 |
| exhaustive | 5 | ~9–12 |

Compute:
- `est_agents` = Σ per-depth estimate over topics, + 2 (Librarian classify + write) + 1 (thread-discoverer).
- `est_tokens` ≈ `est_agents × ~50K` (rough per-agent average — the comparable Dossier capstone averaged ~52K/agent).
- `est_minutes` ≈ scaled by `est_agents` and the concurrency cap (~12 parallel).
- `est_cost` from the resolver's `estimated_usage` when present, else from `est_tokens`.

Present and gate (this is the up-front approval; the batch then runs unattended):
```
Large batch: {N} topics ({depth breakdown}). Routing to the parallel batch fan-out.
Estimated: ~{est_agents} agents · ~{est_tokens/1000}K tokens · ~{est_minutes} min · ~${est_cost}
Runs unattended — no mid-run gates; low-confidence topics auto-replan up to 2x then are flagged (not gated).

Proceed? [yes / no / switch to inline]
```
- **yes** → 4B.2.
- **no** → stop.
- **switch to inline** → fall back to Stage 4 (the inline path) for this batch.

### 4B.2 Dispatch the workflow

The batch's `fetch-summarize-runner` runs the fetch/summarize scripts with `uv run --frozen --project` against `repo`, so pass `REPO` in the config. Create the scratch dir `STATE_DIR/batch_scratch` (the runner writes intermediates under unique per-topic-hop subdirs). Build a compact `vaultDigest` of `{title, tags, path}` from the vault index (cap ~1500 most-recent) for novelty resolution.

Record a lightweight **approve** run marker in `STATE_DIR`. Then call the **Workflow tool** (the skill instruction is your opt-in to use it):
```
Workflow({
  name: 'research-batch',
  args: {
    plan: <approved resolver plan: project + topics[{topic, mode, depth}]>,
    config: {
      vault_root: VAULT, repo: REPO,
      ollama_model: RECOMMENDED_MODEL,   // null at base tier
      tier: TIER, work_dir: "STATE_DIR/batch_scratch",
      outputMode: "vault", vaultDigest: <digest>
    },
    runId: RUN_ID
  }
})
```
The workflow's agents draw from the turn's **shared budget pool**, so a `+Nk`/budget directive caps the whole batch. It runs in the background; you are notified on completion.

### 4B.3 Thread-selection gate

The result is `{written_notes, updated_notes, threads, summary}`. If `summary.aborted` / `aborted:true`, report `reason` and stop. Otherwise:
```
Batch complete: {notes_written} written, {notes_updated} updated{, {N} low-confidence flagged}.
Follow-up threads:
  1. {topic} (score {score}, {novelty_status}) — {rationale}
  ...
Run a follow-up batch on selected threads? [numbers / all / none]
```
- **numbers / all** → build a fresh plan from the selected threads (`mode: web_research`; `suggested_priority` → `depth`) and **re-dispatch from 4B.1** (a new cost estimate + approval each round — the don't-compound backstop against runaway escalation).
- **none** → 4B.4.

### 4B.4 Completion summary

Record the **complete** run marker. Present:
```
{project}: {notes_written} written, {notes_updated} updated, {threads_found} threads.
{low-confidence topics flagged, if any} · {failures, if any}
Notes are in the vault.
```
Done. (Stages 4–10 are the inline alternative and are not run on this path.)

---

## Error Handling

Throughout the pipeline, follow these principles:

1. **Failures do not stop the pipeline.** If a single fetch fails, a media download fails, or a summarization fails, log the error and continue. Collect all errors and report them in the final summary at Stage 10.

2. **State checkpoint after every stage.** Always call `update_stage()` before starting the next stage. If the pipeline crashes, the user can resume from the last checkpoint.

3. **Shared context by file path.** Write intermediate results to `STATE_DIR/` and reference them by path in agent prompts. Never duplicate large content blobs in agent prompts.

4. **Mtime checks before writing.** Always check if the target note was modified externally before overwriting. Skip and warn if it was.

5. **Atomic state updates.** Use `save_stage_output()` for all state file writes -- it uses temp file + rename for crash safety.

---

## Resume Flow

When resuming a run (Stage 1 detected an active run and the user chose "Resume"):

1. Read `current_run.json` to find the current `stage` -- one of `triage`, `resolve`, `hop_loop`, `quality_gate`, `classify`, `write`, `wikilink_scan`, `discover`, `complete`.
2. Load any saved stage outputs from `STATE_DIR/`:
   - `research_plan.json` (from Stage 3)
   - `search_context_hop{N}.json` and `fetch_results_hop{N}.json` (per-hop, from Stage 4)
   - `summaries_hop{N}.json` (per-hop, from Stage 4d) and aggregated `summaries.json` (from Stage 6a)
   - `classification.json` (from Stage 6d)
   - `fetch_results_aggregated.json` (from Stage 7a -- regenerate if missing OR if loading it raises `JSONDecodeError` on resume into the write stage)
   - `written_notes.json` (from Stage 7)
3. Skip to the recorded stage. For the `hop_loop` stage, the topics' `current_hop` and `status` fields determine which active topics still need processing -- only un-completed topics dispatch through Stage 4 again. For the `write` stage specifically, check `written_notes.json` to determine which notes are already complete and skip them. If `fetch_results_aggregated.json` is missing OR `json.loads()` on its contents raises `JSONDecodeError` (the prior run crashed mid-write before the temp+rename completed), re-run Stage 7a's `aggregate-fetch-results` -- it's a pure function of the existing per-hop files, so regeneration is safe.
4. Continue the pipeline from that point.
