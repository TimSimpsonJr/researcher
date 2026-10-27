"""
summarize.py — Article Summarization via Ollama or File Output

Purpose: Distills fetched articles to ~500 tokens via Ollama (using map-reduce
for long articles), or produces file-based output for Claude Code to summarize
via Haiku subagents.

Usage:
    # Ollama mode (requires running Ollama instance):
    python summarize.py --input fetch_results.json --model qwen2.5:14b

    # Claude Code file output mode (no Ollama needed):
    python summarize.py --input fetch_results.json --prepare-for-claude --output-dir ./summaries

Dependencies: requests
"""

import argparse
import json
import re
import sys
from pathlib import Path

import requests

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

SCRIPTS_DIR = Path(__file__).parent
PROMPT_PATH = SCRIPTS_DIR / "prompts" / "summarize_fetch.txt"
MERGE_PROMPT_PATH = SCRIPTS_DIR / "prompts" / "summarize_merge.txt"
MAX_CONTENT_CHARS_CLAUDE = 3000    # For --prepare-for-claude file output
CHUNK_SIZE = 20000                 # Chars per chunk for Ollama map phase
CHUNK_OVERLAP = 500                # Overlap between chunks for continuity


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _load_prompt() -> str:
    """Load the summarize_fetch prompt template."""
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def _load_merge_prompt() -> str:
    """Load the summarize_merge prompt template."""
    return MERGE_PROMPT_PATH.read_text(encoding="utf-8").strip()


def _chunk_content(content: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split content into overlapping chunks.

    Returns a list of content chunks. If the content fits in one chunk,
    returns a single-element list (no splitting needed).
    """
    if len(content) <= chunk_size:
        return [content]

    chunks = []
    start = 0
    while start < len(content):
        end = start + chunk_size
        chunk = content[start:end]
        chunks.append(chunk)
        start = end - overlap  # overlap for continuity

    return chunks


from text_utils import slugify as _slugify


# ──────────────────────────────────────────────
# Ollama API call
# ──────────────────────────────────────────────

def _ollama_generate(
    prompt: str,
    model: str,
    ollama_url: str = "http://localhost:11434",
    timeout: int = 120,
) -> str | None:
    """Send a prompt to Ollama and return the raw response text.

    Returns None on any failure.
    """
    try:
        response = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return data["response"]
    except Exception as exc:
        print(f"[summarize] Ollama request failed: {exc}", file=sys.stderr)
        return None


# ──────────────────────────────────────────────
# Ollama summarization (map-reduce)
# ──────────────────────────────────────────────

def _summarize_chunk(
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    title: str,
    url: str,
    model: str,
    ollama_url: str,
) -> dict | None:
    """Summarize a single chunk via Ollama (map phase).

    Returns parsed JSON dict or None on failure.
    """
    prompt_template = _load_prompt()
    context_line = f"[Chunk {chunk_index + 1} of {total_chunks}] " if total_chunks > 1 else ""
    full_prompt = f"{context_line}Title: {title}\nURL: {url}\n\n{chunk}\n\n---\n{prompt_template}"

    raw = _ollama_generate(full_prompt, model, ollama_url)
    if raw is None:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[summarize] Failed to parse chunk {chunk_index + 1} JSON for '{title}': {exc}", file=sys.stderr)
        return None


def _merge_chunk_summaries(
    chunk_results: list[dict],
    title: str,
    url: str,
    model: str,
    ollama_url: str,
) -> dict | None:
    """Merge multiple chunk summaries into one final summary (reduce phase).

    Takes the list of per-chunk summary dicts and asks Ollama to produce
    a single unified summary with deduplicated entities and claims.
    """
    merge_prompt = _load_merge_prompt()

    # Build the input: numbered chunk summaries
    chunks_text = ""
    for i, result in enumerate(chunk_results):
        chunks_text += f"\n--- Chunk {i + 1} ---\n"
        chunks_text += json.dumps(result, ensure_ascii=False, indent=2)

    full_prompt = (
        f"Title: {title}\nURL: {url}\n\n"
        f"The following are partial summaries of different sections of the same article:\n"
        f"{chunks_text}\n\n"
        f"---\n{merge_prompt}"
    )

    raw = _ollama_generate(full_prompt, model, ollama_url, timeout=180)
    if raw is None:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[summarize] Failed to parse merge JSON for '{title}': {exc}", file=sys.stderr)
        return None


def summarize_article(
    content: str,
    title: str,
    url: str,
    model: str,
    ollama_url: str = "http://localhost:11434",
) -> dict | None:
    """Summarize a single article via Ollama, using map-reduce for long articles.

    Short articles (under CHUNK_SIZE chars) are summarized in a single call.
    Long articles are split into overlapping chunks, each summarized separately,
    then merged into a single unified summary via a reduce pass.

    Returns:
        Dict with keys: summary, source_type, key_entities, key_claims.
        None on any failure (network, parse, etc.).
    """
    chunks = _chunk_content(content)

    if len(chunks) == 1:
        # Short article — single pass, no chunking overhead
        return _summarize_chunk(chunks[0], 0, 1, title, url, model, ollama_url)

    # Map phase: summarize each chunk
    print(f"[summarize] '{title}' is {len(content):,} chars — splitting into {len(chunks)} chunks", file=sys.stderr)
    chunk_results = []
    for i, chunk in enumerate(chunks):
        result = _summarize_chunk(chunk, i, len(chunks), title, url, model, ollama_url)
        if result is not None:
            chunk_results.append(result)

    if not chunk_results:
        print(f"[summarize] All chunks failed for '{title}'", file=sys.stderr)
        return None

    if len(chunk_results) == 1:
        # Only one chunk succeeded — use it directly
        return chunk_results[0]

    # Reduce phase: merge chunk summaries
    print(f"[summarize] Merging {len(chunk_results)} chunk summaries for '{title}'", file=sys.stderr)
    return _merge_chunk_summaries(chunk_results, title, url, model, ollama_url)


def summarize_batch(
    fetch_results: dict,
    model: str,
    ollama_url: str = "http://localhost:11434",
) -> list[dict]:
    """Summarize all fetched articles via Ollama.

    Iterates over fetch_results["fetched"], calls summarize_article for each,
    and returns a list of summary dicts with url, title, and summary fields merged.
    Failures (None returns) are filtered out. Image-only PDFs (image_only_pdf)
    have no text for Ollama: they come back usable: false with reason
    "needs visual read" and their pdf_path, for a Claude summarizer to Read.
    """
    summaries: list[dict] = []

    for item in fetch_results.get("fetched", []):
        if item.get("image_only_pdf"):
            summaries.append({
                "url": item["url"],
                "title": item["title"],
                "usable": False,
                "reason": "needs visual read",
                "image_only_pdf": True,
                "pdf_path": item.get("pdf_path"),
            })
            continue
        result = summarize_article(
            content=item["content"],
            title=item["title"],
            url=item["url"],
            model=model,
            ollama_url=ollama_url,
        )
        if result is not None:
            summaries.append({
                "url": item["url"],
                "title": item["title"],
                **result,
            })

    return summaries


# ──────────────────────────────────────────────
# Claude Code file output (no Ollama)
# ──────────────────────────────────────────────

def prepare_for_claude_code(
    fetch_results: dict,
    output_dir: Path,
) -> list[dict]:
    """Write each article to a separate file for Haiku subagent summarization.

    For when Ollama is NOT available. Each file contains title, url, and
    truncated content (first 3000 chars). Files are named {index}-{slug}.md.

    Returns:
        List of {"url", "title", "file"} entries where "file" is the
        filename (relative to output_dir).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []

    for index, item in enumerate(fetch_results.get("fetched", [])):
        slug = _slugify(item.get("title", "untitled"))
        filename = f"{index}-{slug}.md"

        truncated_content = item["content"][:MAX_CONTENT_CHARS_CLAUDE]
        visual = (
            f"Image-only PDF: read the page images at {item.get('pdf_path')}\n\n"
            if item.get("image_only_pdf") else ""
        )

        file_content = (
            f"# {item['title']}\n\n"
            f"Source: {item['url']}\n\n"
            f"{visual}"
            f"---\n\n"
            f"{truncated_content}\n"
        )

        (output_dir / filename).write_text(file_content, encoding="utf-8")

        entry = {"url": item["url"], "title": item["title"], "file": filename}
        if item.get("image_only_pdf"):
            entry["image_only_pdf"] = True
            entry["pdf_path"] = item.get("pdf_path")
        entries.append(entry)

    return entries


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize fetched articles via Ollama or prepare files for Claude Code."
    )
    parser.add_argument("--input", required=True, help="Path to fetch_results.json")
    parser.add_argument("--model", default="qwen2.5:14b", help="Ollama model name")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama API base URL")
    parser.add_argument("--output", help="Output path for summaries JSON (stdout if omitted)")
    parser.add_argument("--prepare-for-claude", action="store_true",
                        help="Write files for Claude Code instead of calling Ollama")
    parser.add_argument("--output-dir", default="./summaries",
                        help="Directory for Claude Code file output (used with --prepare-for-claude)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[summarize] ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    fetch_results: dict = json.loads(input_path.read_text(encoding="utf-8"))

    if args.prepare_for_claude:
        output_dir = Path(args.output_dir)
        entries = prepare_for_claude_code(fetch_results, output_dir)
        result = {"mode": "claude_code", "files": entries}
        print(f"[summarize] Wrote {len(entries)} file(s) to {output_dir}", file=sys.stderr)
    else:
        summaries = summarize_batch(fetch_results, args.model, args.ollama_url)
        # Emit `items` (not `summaries`) so the output shape matches the
        # classify-agent input contract and the per-hop aggregation in
        # SKILL.md Stage 6a (which globs `summaries_hop*.json` and reads
        # `data.get("items", [])`).
        total = len(fetch_results.get("fetched", []))
        usable = [s for s in summaries if s.get("usable", True)]
        result = {
            "mode": "ollama",
            "model": args.model,
            "items": summaries,
            "stats": {
                "total": total,
                "summarized": len(usable),
                "needs_visual_read": len(summaries) - len(usable),
                "failed": total - len(summaries),
            },
        }

    output_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output_json, encoding="utf-8")
        print(f"[summarize] Results written to {output_path}", file=sys.stderr)
    else:
        print(output_json)


if __name__ == "__main__":
    main()
