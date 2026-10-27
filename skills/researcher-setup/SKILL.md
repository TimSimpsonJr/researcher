---
name: researcher-setup
description: 'Setup wizard for the research pipeline. Detects infrastructure, configures vault, installs optional tools. Run once per vault, re-run to update.'
---

# Research Setup Wizard

You are an interactive setup wizard. Walk the user through configuring the research pipeline step by step. Use **AskUserQuestion** at every decision point. Never install or modify anything without explicit user confirmation.

Plugin root: `REPO` = the directory two levels above this skill's base directory (Claude Code prints the base directory, ending in `skills/researcher-setup`, when the skill loads). Write that absolute path wherever `REPO` appears below, inside the quotes.

All Python runs as one line through uv: `uv run --frozen --project "REPO" "REPO/scripts/rw.py" <subcommand> [flags]`. Never invoke the Python interpreter directly or write inline Python blocks. `rw.py` prints JSON; on failure it prints `ERROR: ...` on stderr and exits 1. During setup always pass `--vault "VAULT_ROOT"`: the vault has no config yet, so rw.py cannot find it on its own.

---

## Step 0: Check uv and Prepare the Environment

Before starting the wizard, check that uv is installed. Use Bash:
```bash
uv --version
```
If it is not found, stop and tell the user to install uv, then re-run `/researcher-setup`:
```
  Windows (PowerShell):  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  macOS / Linux:         curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then build the plugin's Python environment once:
```bash
uv sync --frozen --project "REPO"
```
This creates `REPO/.venv` from `REPO/uv.lock` (downloading Python 3.12 if needed). If it fails, report the error and attempt to continue — the Python environment may need troubleshooting.

Then check the local browser fallback, which reads pages and PDFs that block scripted fetchers:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check playwright
```
If `status` is not `ok`, use AskUserQuestion: "Download Chromium's headless shell for the local browser fallback (roughly 100–300 MB download, stored in REPO/.cache)? [yes / skip]". On yes:
```bash
PLAYWRIGHT_BROWSERS_PATH="REPO/.cache/ms-playwright" uv run --frozen --project "REPO" playwright install chromium --only-shell
```
Then run the check again and report the result. On skip, note that pages behind bot walls will go to the orchestrator's browser or the manual-read list instead.

---

## Step 1: Vault Path

Ask the user for their Obsidian vault path.

Use **AskUserQuestion**:
```
Where is your Obsidian vault?

Options:
1. Enter the full path to an existing vault
2. Create a new template vault

If entering a path, provide the absolute path (e.g., C:\Users\you\Documents\My Vault or /home/you/vault).
```

**If the user provides a path:**
- Verify it exists using Bash: `test -d "USER_PATH" && echo "EXISTS" || echo "NOT_FOUND"`
- If not found, ask again
- Store as `VAULT_ROOT`

**If the user wants a template vault:**
- Ask where to create it (AskUserQuestion for a parent directory path)
- Create the template structure using Bash:
```bash
mkdir -p "PARENT/Research Vault/Inbox"
mkdir -p "PARENT/Research Vault/Projects"
mkdir -p "PARENT/Research Vault/Sources"
mkdir -p "PARENT/Research Vault/Resources"
mkdir -p "PARENT/Research Vault/assets"
mkdir -p "PARENT/Research Vault/Daily"
```
- Create a starter MOC at `PARENT/Research Vault/_Index.md`:
```markdown
---
title: Vault Index
tags: [index]
---

# Vault Index

## Projects

- [[Inbox]] — unsorted incoming notes

## Sources

Raw source material and ingested content.

## Resources

Reference material and evergreen notes.
```
- Store `PARENT/Research Vault` as `VAULT_ROOT`

**Check for existing config:**

Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" load-config --vault "VAULT_ROOT"
```

It prints the config JSON, or `null` when the vault has none.

If existing config is found, show the user its contents and ask:
```
Found existing configuration for this vault. Would you like to:
1. Update individual settings (re-run specific steps)
2. Start fresh (reconfigure everything)
```

If updating, skip steps where the user is happy with the current value. If starting fresh, proceed through all steps.

---

## Step 2: Scan Vault Conventions

Run a vault scan to detect existing note patterns. Use Bash:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" scan-vault --vault "VAULT_ROOT"
```

It prints `total_notes`, `top_folders`, `tag_format_counts`, `dominant_tag_format`, `frontmatter_fields`, `moc_files` and `moc_pattern` (the first 50 non-hidden notes are sampled).

Present the findings to the user:
```
Vault scan results:
- Total notes: {total_notes}
- Top-level folders: {top_folders}
- Tag format: {dominant_tag_format} (based on {count} sampled notes)
- Common frontmatter fields: {frontmatter_fields}
- MOC/Index notes found: {moc_files}

These conventions will be used to configure the pipeline. Anything look wrong?
```

Wait for acknowledgment via AskUserQuestion before proceeding.

---

## Step 3: Detect Platform

Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check platform
```

Report to user:
```
Platform detected:
- OS: {os}
- Architecture: {arch}
- WSL: {is_wsl}
```

Store `platform_info` for later steps.

---

## Step 4: Ollama

Check if Ollama is installed. Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check ollama
```

**If installed and running:**
- Report: `Ollama is installed and running. Models available: {models}`
- Set `ollama_installed = True`
- Skip to Step 5

**If installed but not running:**
- Report: `Ollama is installed but the server is not running.`
- Ask user: `Would you like to start Ollama? (Run "ollama serve" in a separate terminal, then tell me when it's ready)`
- Set `ollama_installed = True`

**If not installed:**

Use AskUserQuestion:
```
Ollama is not installed. Ollama enables local AI model inference, which allows the mid and full tiers of the research pipeline.

Would you like to install it?
- The base tier works without Ollama (uses Claude API only)
- Mid/full tiers need Ollama for local classification and summarization
```

If the user says yes:
- **Linux or WSL**: Ask for confirmation, then run: `curl -fsSL https://ollama.com/install.sh | sh`
- **macOS**: Ask for confirmation, then run: `curl -fsSL https://ollama.com/install.sh | sh`
- **Windows (not WSL)**: Tell the user: `Download and install Ollama from https://ollama.com/download. Run the installer, then tell me when it's done.`

After installation, re-run the Ollama check to verify.

If the user declines, set `ollama_installed = False` and note that base tier will be used.

---

## Step 5: Hardware Check and Model Recommendation

Gather hardware info. Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" hardware-info
```

Parse the hardware info and get a model recommendation. Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" recommend-model --ram-gb RAM_GB --vram-gb VRAM_GB
```

Replace `RAM_GB` and `VRAM_GB` with the values from the hardware check.

Present to user:
```
Hardware detected:
- RAM: {total_ram_gb} GB
- GPU: {gpu_name} ({gpu_vram_gb} GB VRAM)

Model recommendation: {model} — {reason}
```

**If Ollama is available and a model is recommended:**

Use AskUserQuestion:
```
Would you like to pull the recommended model ({model})?
This will download the model weights (several GB). You can also enter a different model name if you prefer.
```

If yes, pull the model via Bash:
```bash
ollama pull MODEL_NAME
```

After pull completes, offer a quick benchmark:
```
Model pulled successfully. Would you like to run a quick speed test?
This sends a short prompt to the model to measure response time.
```

If yes, run via Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" benchmark-ollama --model MODEL_NAME
```

Report the result and store `ollama_benchmark_ms`.

---

## Step 6: yt-dlp

Check availability. Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check ytdlp
```

**If installed:** Report `yt-dlp is available.` and set `ytdlp_available = True`.

**If not installed:**

Use AskUserQuestion:
```
yt-dlp is not installed. It's used for downloading YouTube videos and extracting audio for transcription.

Would you like to install it? (uv tool install yt-dlp)
```

If yes, run via Bash:
```bash
uv tool install yt-dlp
```
If `check ytdlp` still reports it missing afterwards, uv's tool directory is not on PATH: run `uv tool update-shell` and restart the terminal.

Re-check after install. If the user declines, set `ytdlp_available = False`.

---

## Step 7: Whisper

Check availability. Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check whisper
```

**If installed:** Report `Whisper is available (backend: {backend}).` and set `whisper_available = True`.

**If not installed:** Report:
```
Whisper is not installed. It's used for audio transcription (YouTube videos, podcasts, interviews).

Installation is optional and requires significant disk space and compute resources.
To install later: uv tool install openai-whisper

Skipping for now.
```

Set `whisper_available = False`. Do not offer to auto-install — it's heavy.

---

## Step 7b: Tesseract (optional OCR)

Check availability. Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check tesseract
```

**If `status` is `ok`:** Report `Tesseract is available: scanned PDFs are OCR'd locally.`

**If missing:** Report:
```
Tesseract is not installed. It OCRs scanned (image-only) PDFs. Without it,
those PDFs are saved and Claude reads the page images instead, which costs
more tokens.

To install later:
  Windows:  winget install UB-Mannheim.TesseractOCR
  macOS:    brew install tesseract
  Linux:    sudo apt install tesseract-ocr
```
Do not install it automatically. It does not change the tier.

---

## Step 8: SearXNG

Use AskUserQuestion:
```
Do you have a SearXNG instance running?

SearXNG provides private web search for the research pipeline's full tier.
- If you have one: provide the URL (e.g., http://localhost:8888)
- If not: the base tier will use Claude's built-in WebSearch tool instead

Enter the URL, or type "skip" to skip.
```

**If the user provides a URL:**

Test connectivity via Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" check searxng --url "USER_URL"
```

If reachable: `SearXNG is reachable at {url}.` Set `searxng_url`.
If not reachable: `Could not connect to SearXNG at {url}. Check that the server is running.` Ask user to retry or skip.

**If skip:** Set `searxng_url = None`.

---

## Step 9: Generate Config and Vault Rules

### 9a. Determine tier

Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" detect-tier --searxng-url "SEARXNG_URL"
```

Drop `--searxng-url "SEARXNG_URL"` when SearXNG was skipped. It prints the tier as a JSON string (`"base"`, `"mid"` or `"full"`).

### 9b. Save config

Write the config to `VAULT_ROOT/.researcher/setup_config.json` with the Write tool:

```json
{
  "vault_root": "VAULT_ROOT",
  "inbox": "INBOX_FOLDER",
  "assets": "ASSETS_FOLDER",
  "moc_pattern": "MOC_PATTERN",
  "tag_format": "TAG_FORMAT",
  "date_format": "%Y-%m-%d",
  "frontmatter_fields": FRONTMATTER_FIELDS_LIST,
  "ollama_enabled": OLLAMA_BOOL,
  "ollama_model": OLLAMA_MODEL_OR_NULL,
  "ollama_benchmark_ms": BENCHMARK_OR_NULL,
  "searxng_url": SEARXNG_URL_OR_NULL,
  "whisper_available": WHISPER_BOOL,
  "ytdlp_available": YTDLP_BOOL,
  "tier": "TIER"
}
```

Replace all placeholders with the actual values collected during the wizard. Use the detected values from Step 2 for `inbox` (default "Inbox" if an "Inbox" folder exists, otherwise the first folder that looks like an inbox), `assets`, `moc_pattern`, `tag_format`, and `frontmatter_fields`.

Use JSON values: `true` / `false`, `null` for none, a JSON list for `frontmatter_fields`. Write `vault_root` with forward slashes (or doubled backslashes) so the JSON stays valid. Then save it:

```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" save-config --vault "VAULT_ROOT" --config-file "VAULT_ROOT/.researcher/setup_config.json"
```

It writes `VAULT_ROOT/.researcher/config.json` and prints the saved config. Once saved, `.researcher/setup_config.json` can be deleted.

### 9c. Generate vault_rules.txt

Create `REPO/scripts/prompts/vault_rules.txt` using the Write tool. Base the content on the detected conventions:

```markdown
## Vault Rules

These rules apply to all notes written by the research pipeline.

### Frontmatter

Every note must start with YAML frontmatter:

---
title: Note Title
tags: [{tag_format} format — use brackets for list, bare for string]
source: URL or "local"
created: {date_format}
---

Common fields in this vault: {frontmatter_fields joined by comma}

### Tags

Use {tag_format} format in frontmatter.
Content-type tags (pick one): research, legislation, campaign, plan, reference, tracking, decision, index, resource, meta
Add location tags where relevant (e.g., greenville-sc, sc).
Add purpose prefixes where relevant (strategic-, tactical-).
See docs/TAGGING-REFERENCE.md for the complete taxonomy.

### Wikilinks

- Link to existing vault notes using [[Note Title]] when the topic is mentioned
- Use aliases for long titles: [[Full Title|short name]]
- Only link specific, notable concepts — not generic terms
- For concepts without notes yet, use [[Stub Link]] sparingly and only if the concept deserves its own note

### Sources

- Include source URLs as inline links at first reference
- Add a ## Sources section at the bottom of every research note
- Every factual claim from external research should trace to its source

### MOC Pattern

MOC notes in this vault match: {moc_pattern}
When creating notes in a folder with a MOC, update the MOC to include the new note.
```

Adjust the rules to match what was detected in Step 2. If no vault_rules.txt exists yet, create it. If one already exists, ask the user before overwriting.

---

## Step 10: Build Vault Index

Use Bash:
```bash
uv run --frozen --project "REPO" "REPO/scripts/rw.py" build-index --vault "VAULT_ROOT"
```

It prints `{"db_path": ..., "notes_indexed": N}`.

Report:
```
Vault index built successfully.
- Database: {db_path}
- Notes indexed: {count}
```

---

## Step 11: Optional -- install metadata-hide CSS snippet

The research pipeline writes a few diagnostic frontmatter fields to every note it generates (`hop_genealogy`, `research_run`, `write_model`). They're useful for debugging and telemetry but clutter Obsidian's Properties panel. A small CSS snippet hides them in the UI while leaving them fully searchable in the file.

Use **AskUserQuestion**:
```
Want to hide the diagnostic frontmatter fields (hop_genealogy, research_run, write_model)
in Obsidian's Properties panel? They'll still be searchable, just not displayed.

Install CSS snippet? [yes / no]
```

**If `yes`:**

Copy the snippet into the vault's snippets directory. Use Bash:
```bash
mkdir -p "VAULT_ROOT/.obsidian/snippets"
cp "REPO/assets/research-metadata-hide.css" "VAULT_ROOT/.obsidian/snippets/research-metadata-hide.css"
```

Replace `VAULT_ROOT` with the actual vault path. Then tell the user:
```
Snippet copied. Enable it in Obsidian -> Settings -> Appearance -> CSS snippets -> toggle "research-metadata-hide".
```

**If `no`:** Skip silently and continue to the summary.

---

## Step 12: Summary

Print a clear summary of everything that was configured:

```
Setup complete!

Vault: {vault_root}
Tier: {tier} ({tier description})
  - base: Claude API only (WebSearch + Jina Reader)
  - mid: base + local Ollama models
  - full: mid + SearXNG private search

Platform: {os} ({arch})

Tools:
  - Ollama: {installed/not installed} {model if applicable}
  - yt-dlp: {available/not available}
  - Whisper: {available/not available}
  - Chromium (local browser fallback): {ok/missing}
  - Tesseract (PDF OCR, optional): {ok/missing}
  - SearXNG: {url or "not configured"}

Vault conventions:
  - Tag format: {tag_format}
  - Frontmatter fields: {fields}
  - MOC pattern: {moc_pattern}
  - Notes indexed: {count}

Config saved to: {vault_root}/.researcher/config.json
Vault rules saved to: {REPO}/scripts/prompts/vault_rules.txt

Next steps:
  - Run /researcher "your topic" to start researching
  - Run /researcher path/to/note.md to research an existing note
```

If any issues were encountered (failed installs, missing tools), list them under a "Warnings" section.
