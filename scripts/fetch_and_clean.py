"""
fetch_and_clean.py — URL Fetch, Clean, and Cache

Purpose: Tier 2 of the research pipeline. Accepts a search_context JSON,
fetches each URL, caches results by SHA-256(url), and outputs fetch_results JSON.

Two phases per run:
  1. Jina Reader for every uncached URL (serial with a delay, or --workers N in
     parallel). URLs that look like PDFs are also fetched directly and
     extracted locally (pdf_extract).
  2. The failures, one at a time: local Chromium (fetch_playwright.BrowserSession)
     and the rate-limited Wayback client (wayback.py), ordered by the first
     verdicts (block_detect.classify). A URL still behind a wall goes to
     needs_browser for the orchestrator's own browser; the rest go to failed.

Usage:
    uv run --frozen --project REPO REPO/scripts/fetch_and_clean.py --input search_context.json --output fetch_results.json
    uv run --frozen --project REPO REPO/scripts/fetch_and_clean.py --input search_context.json --dry-run
"""

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests

import wayback
from block_detect import FetchResult, Verdict, blocked, classify
from fetch_playwright import BrowserSession
from pdf_extract import looks_like_pdf, to_result
from url_safety import UnsafeURLError, validate_url  # tests patch fetch_and_clean.validate_url

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

DEFAULT_CACHE_DIR = Path(__file__).parent.parent / ".cache" / "fetch"
DEFAULT_TTL_DAYS = 7
DEFAULT_FETCH_DELAY = 1.0  # seconds between phase-1 fetches (serial path)
MAX_CONTENT_CHARS = 50_000
JINA_BASE_URL = "https://r.jina.ai"
MAX_REDIRECTS = 5
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
LIVE_METHODS = frozenset({"jina", "direct", "playwright"})  # hit the site itself


# ──────────────────────────────────────────────
# URL helpers
# ──────────────────────────────────────────────

def url_cache_key(url: str) -> str:
    """Return SHA-256 hash of URL as cache filename key."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    """Normalize URL for deduplication: lowercase, strip trailing slash."""
    return url.rstrip("/").lower()


def _normalize_url_items(urls: list) -> list[dict]:
    """Normalize URL entries to dicts with a 'url' key.

    Accepts mixed input:
    - Strings: "https://example.com" -> {"url": "https://example.com"}
    - Dicts with 'url' key: passed through as-is
    """
    result: list[dict] = []
    for item in urls:
        if isinstance(item, str):
            result.append({"url": item})
        elif isinstance(item, dict) and "url" in item:
            result.append(item)
        else:
            print(f"[fetch_and_clean] WARNING: skipping unrecognized URL entry: {item!r}",
                  file=sys.stderr)
    return result


def deduplicate_urls(urls: list[dict]) -> list[dict]:
    """Remove duplicate URLs (case-insensitive, trailing-slash agnostic). Preserves order."""
    seen: set[str] = set()
    result: list[dict] = []
    for item in urls:
        key = normalize_url(item["url"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


# ──────────────────────────────────────────────
# Cache helpers
# ──────────────────────────────────────────────

def _content_hash(content: str) -> str:
    """SHA-256 hash of content for cache integrity verification."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_cache(cache_dir: Path, cache_key: str) -> dict | None:
    """Return cached entry dict, or None if missing, corrupt, or tampered."""
    cache_file = cache_dir / f"{cache_key}.json"
    if not cache_file.exists():
        return None
    try:
        entry = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    stored_hash = entry.get("content_hash")
    if stored_hash and _content_hash(entry.get("content", "")) != stored_hash:
        print(f"[fetch_and_clean] WARNING: cache integrity check failed for {cache_key}", file=sys.stderr)
        return None
    return entry


def is_expired(entry: dict, ttl_days: int) -> bool:
    """Return True if cache entry is older than ttl_days."""
    try:
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched_at > timedelta(days=ttl_days)
    except (KeyError, ValueError, TypeError):
        return True  # Treat unparseable entries as expired


def save_cache(cache_dir: Path, cache_key: str, entry: dict) -> None:
    """Write entry JSON to cache file with integrity hash. Creates cache_dir if needed."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    if "content" in entry:
        entry["content_hash"] = _content_hash(entry["content"])
    cache_file = cache_dir / f"{cache_key}.json"
    cache_file.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")


# ──────────────────────────────────────────────
# Fetchers (each returns a FetchResult)
# ──────────────────────────────────────────────

def fetch_via_jina(url: str, api_key: str | None = None) -> FetchResult:
    """Fetch URL via Jina Reader (https://r.jina.ai) and classify the reply."""
    validate_url(url)
    headers = {"Accept": "text/markdown"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = requests.get(f"{JINA_BASE_URL}/{url}", headers=headers, timeout=30)
    ctype = response.headers.get("Content-Type", "")
    if response.status_code == 429:  # Jina's own throttling, not a site wall
        return FetchResult(blocked("error:jina_429"), method="jina", content_type=ctype)
    content = response.text
    verdict = classify(response.status_code, ctype, content)
    title = next((line[2:].strip() for line in content.splitlines() if line.startswith("# ")), "")
    return FetchResult(verdict, content if verdict.ok else "", title, "jina", ctype)


def _get_validated(url: str):
    """GET following redirects by hand so every hop passes the SSRF guard."""
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(url)
        response = requests.get(url, timeout=(10, 60), allow_redirects=False)
        location = response.headers.get("Location")
        if response.status_code not in _REDIRECT_CODES or not location:
            return response
        url = urljoin(url, location)
    raise RuntimeError(f"too many redirects: {url}")


def fetch_pdf_direct(url: str, cache_dir: Path) -> FetchResult:
    """GET a PDF's bytes ourselves and extract locally (pdf_extract)."""
    response = _get_validated(url)
    ctype = response.headers.get("Content-Type", "")
    if response.status_code != 200:
        return FetchResult(classify(response.status_code, ctype, ""), method="direct", content_type=ctype)
    if not looks_like_pdf("", ctype, response.content[:5]):
        return FetchResult(Verdict("thin", "not_a_pdf"), method="direct", content_type=ctype)
    return to_result(response.content, cache_dir)


# ──────────────────────────────────────────────
# Chain
# ──────────────────────────────────────────────

def _try(attempts: list[dict], method: str, fn) -> FetchResult:
    """Run one fetcher; record {method, verdict, reason}. Never raises."""
    try:
        result = fn()
    except UnsafeURLError as exc:
        result = FetchResult(Verdict("unsafe", "ssrf"), method=method, error=str(exc))
    except Exception as exc:
        result = FetchResult(blocked(f"error:{type(exc).__name__}"), method=method, error=str(exc))
    if result.error:
        print(f"[fetch_and_clean] {method} failed: {result.error}", file=sys.stderr)
    attempts.append({"method": method, "verdict": result.verdict.kind, "reason": result.verdict.reason})
    return result


def first_pass(url: str, jina_api_key: str | None, cache_dir: Path) -> tuple[FetchResult, list[dict]]:
    """Phase 1 for one URL: Jina, plus a direct GET + local extract for PDFs."""
    attempts: list[dict] = []
    result = _try(attempts, "jina", lambda: fetch_via_jina(url, jina_api_key))
    if result.verdict.kind != "unsafe" and looks_like_pdf(url, result.content_type):
        direct = _try(attempts, "direct", lambda: fetch_pdf_direct(url, cache_dir))
        if direct.verdict.ok:
            return direct, attempts
    return result, attempts


def _has_unsafe(attempts: list[dict]) -> bool:
    return any(a["verdict"] == "unsafe" for a in attempts)


def _fallback_order(attempts: list[dict]) -> list[str]:
    kinds = {a["verdict"] for a in attempts}
    if "missing" in kinds and "blocked" not in kinds:
        return ["wayback", "playwright"]
    return ["playwright", "wayback"]


def fallback(url: str, first: FetchResult, attempts: list[dict], session, cache_dir: Path) -> FetchResult | None:
    """Phase 2 for one URL. Returns the first ok result, else None (attempts grows)."""
    if first.verdict.ok:
        return first
    if _has_unsafe(attempts):
        return None
    for step in _fallback_order(attempts):
        if step == "playwright":
            if any(a["reason"] == "captcha" for a in attempts):
                continue  # captcha is terminal for automatic live-site fetchers
            if session is None or not session.available:
                attempts.append({"method": "playwright", "verdict": "skipped", "reason": "playwright_unavailable"})
                continue
            result = _try(attempts, "playwright", lambda: session.fetch(url, cache_dir))
        else:
            result = _try(attempts, "wayback", lambda: wayback.fetch(url, cache_dir))
        if result.verdict.ok:
            return result
        if result.verdict.kind == "unsafe":
            return None  # the URL (or a redirect) reaches an internal host: stop
    return None


def fetch_url(url: str, jina_api_key: str | None = None, cache_dir: Path = DEFAULT_CACHE_DIR,
              session=None) -> tuple[FetchResult | None, list[dict]]:
    """The whole chain for one URL: (ok result or None, attempts)."""
    first, attempts = first_pass(url, jina_api_key, cache_dir)
    return fallback(url, first, attempts, session, cache_dir), attempts


def _live_blocks(attempts: list[dict]) -> list[dict]:
    """Walls the site itself put up (not fetch errors, not Wayback throttling)."""
    return [a for a in attempts
            if a["method"] in LIVE_METHODS and a["verdict"] == "blocked"
            and not a["reason"].startswith("error:")]


def _block_reason(attempts: list[dict]) -> str:
    reasons = [a["reason"] for a in _live_blocks(attempts)]
    return "captcha" if "captcha" in reasons else reasons[-1]


def _failure_summary(attempts: list[dict]) -> str:
    parts = [f"{a['method']}={a['verdict']}" + (f"({a['reason']})" if a["reason"] else "") for a in attempts]
    return "All fetch methods failed: " + ", ".join(parts)


# ──────────────────────────────────────────────
# Records
# ──────────────────────────────────────────────

def _record(entry: dict, cache_hit: bool) -> dict:
    """Output record from a cache entry. MAX_CONTENT_CHARS caps output only."""
    content = entry["content"][:MAX_CONTENT_CHARS]
    record = {
        "url": entry["url"],
        "title": entry.get("title", ""),
        "content": content,
        "fetch_method": entry.get("fetch_method", "cached"),
        "cache_hit": cache_hit,
        "fetched_at": entry["fetched_at"],
        "word_count": len(content.split()),
    }
    if "image_only_pdf" in entry:
        record["image_only_pdf"] = entry["image_only_pdf"]
        record["pdf_path"] = entry.get("pdf_path")
    return record


def _cached_record(cache_dir: Path, url: str, ttl_days: int) -> dict | None:
    cached = load_cache(cache_dir, url_cache_key(url))
    if cached is None or is_expired(cached, ttl_days):
        return None
    return _record(cached, cache_hit=True)


def _store(cache_dir: Path, url: str, result: FetchResult) -> dict:
    """Cache the full text (and PDF fields) and return the output record."""
    entry = {
        "url": url,
        "title": result.title,
        "content": result.content,
        "fetch_method": result.method,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if result.is_pdf:
        entry["image_only_pdf"] = result.image_only_pdf
        entry["pdf_path"] = result.pdf_path
    try:
        save_cache(cache_dir, url_cache_key(url), entry)
    except OSError as exc:
        print(f"[fetch_and_clean] WARNING: cache write failed for {url}: {exc}", file=sys.stderr)
    return _record(entry, cache_hit=False)


def _stats(fetched: list[dict], failed: list[dict], needs_browser: list[dict]) -> dict:
    return {
        "fetched": len(fetched),
        "failed": len(failed),
        "needs_browser": len(needs_browser),
        "cache_hits": sum(1 for f in fetched if f["cache_hit"]),
        "total_words": sum(f["word_count"] for f in fetched),
    }


# ──────────────────────────────────────────────
# Main processing
# ──────────────────────────────────────────────

def _run_first_pass(urls: list[str], jina_api_key: str | None, cache_dir: Path,
                    fetch_delay: float, workers: int) -> list[tuple[FetchResult, list[dict]]]:
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda u: first_pass(u, jina_api_key, cache_dir), urls))
    results = []
    last = None
    for url in urls:
        if last is not None and fetch_delay > 0:
            time.sleep(max(0.0, fetch_delay - (time.monotonic() - last)))
        last = time.monotonic()  # spacing is start to start
        results.append(first_pass(url, jina_api_key, cache_dir))
    return results


def process_urls(
    urls: list[dict],
    cache_dir: Path,
    ttl_days: int,
    jina_api_key: str | None,
    fetch_delay: float = DEFAULT_FETCH_DELAY,
    workers: int = 1,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch and cache URL dicts (each with a "url" key).

    Returns (fetched, failed, needs_browser). Never raises for a fetch failure.
    """
    fetched: list[dict] = []
    misses: list[str] = []
    for item in urls:
        record = _cached_record(cache_dir, item["url"], ttl_days)
        if record is not None:
            fetched.append(record)
        else:
            misses.append(item["url"])

    pending = []
    for url, (result, attempts) in zip(misses, _run_first_pass(misses, jina_api_key, cache_dir, fetch_delay, workers)):
        if result.verdict.ok:
            fetched.append(_store(cache_dir, url, result))
        else:
            pending.append((url, result, attempts))

    failed: list[dict] = []
    needs_browser: list[dict] = []
    if pending:
        with BrowserSession() as session:
            for url, first, attempts in pending:
                result = fallback(url, first, attempts, session, cache_dir)
                if result is not None:
                    fetched.append(_store(cache_dir, url, result))
                elif _live_blocks(attempts) and not _has_unsafe(attempts):
                    needs_browser.append({"url": url, "reason": _block_reason(attempts), "attempts": attempts})
                else:
                    error = _failure_summary(attempts)
                    print(f"[fetch_and_clean] FAILED {url}: {error}", file=sys.stderr)
                    failed.append({"url": url, "error": error, "attempts": attempts})
    return fetched, failed, needs_browser


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and cache URLs for the research pipeline (Tier 2)."
    )
    parser.add_argument("--input", required=True, help="Path to search_context.json")
    parser.add_argument("--output", help="Output path for fetch_results.json (stdout if omitted)")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
    parser.add_argument("--workers", type=int, default=1, help="parallel Jina fetches in phase 1")
    parser.add_argument("--dry-run", action="store_true", help="Print URLs; do not fetch")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[fetch_and_clean] ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    search_context: dict = json.loads(input_path.read_text(encoding="utf-8"))
    raw_urls = search_context.get("selected_urls", [])
    urls = deduplicate_urls(_normalize_url_items(raw_urls))

    if args.dry_run:
        print(f"[fetch_and_clean] Dry run — {len(urls)} URL(s) would be fetched:", file=sys.stderr)
        for item in urls:
            print(f"  {item['url']}", file=sys.stderr)
        return

    jina_api_key: str | None = os.environ.get("JINA_API_KEY")
    cache_dir = Path(args.cache_dir)

    fetched, failed, needs_browser = process_urls(urls, cache_dir, args.ttl_days, jina_api_key,
                                                  workers=args.workers)

    result = {
        "topic": search_context.get("topic", ""),
        "search_context": search_context,
        "fetched": fetched,
        "failed": failed,
        "needs_browser": needs_browser,
        "stats": {"total_urls": len(urls), **_stats(fetched, failed, needs_browser)},
    }

    output_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output_json, encoding="utf-8")
        print(f"[fetch_and_clean] Results written to {output_path}", file=sys.stderr)
    else:
        print(output_json)


if __name__ == "__main__":
    main()
