"""
fetch_and_clean.py — URL Fetch, Clean, and Cache

Purpose: Tier 2 of the research pipeline. Accepts a search_context JSON,
fetches each URL via Jina Reader (with Wayback Machine fallback), caches
results by SHA-256(url), and outputs fetch_results JSON.

Usage:
    python fetch_and_clean.py --input search_context.json
    python fetch_and_clean.py --input search_context.json --output fetch_results.json
    python fetch_and_clean.py --input search_context.json --dry-run

Dependencies: requests
"""

import argparse
import hashlib
import json
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse

import requests

from fetch_playwright import fetch_via_playwright

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

DEFAULT_CACHE_DIR = Path(__file__).parent.parent / ".cache" / "fetch"
DEFAULT_TTL_DAYS = 7
DEFAULT_FETCH_DELAY = 1.0  # seconds between fetches
MAX_CONTENT_CHARS = 50_000
MIN_USEFUL_CONTENT_CHARS = 200  # below this, content is likely empty/blocked
JINA_BASE_URL = "https://r.jina.ai"
WAYBACK_API = "https://archive.org/wayback/available"

# Private/reserved IP ranges that should never be fetched
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}


# ──────────────────────────────────────────────
# URL helpers
# ──────────────────────────────────────────────

def validate_url(url: str) -> None:
    """Reject URLs targeting private/internal networks (SSRF protection).

    Raises ValueError if the URL scheme is not http/https or resolves to
    a private, loopback, reserved, or link-local IP address.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked URL scheme '{parsed.scheme}': {url}")

    hostname = parsed.hostname or ""
    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Blocked hostname '{hostname}': {url}")

    try:
        addr = ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local:
            raise ValueError(f"Blocked private/reserved IP '{hostname}': {url}")
    except ValueError as orig:
        # hostname is not an IP literal — resolve it
        if "Blocked" in str(orig):
            raise
        try:
            resolved = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
            for family, _type, _proto, _canon, sockaddr in resolved:
                addr = ip_address(sockaddr[0])
                if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local:
                    raise ValueError(f"Blocked: '{hostname}' resolves to private IP {addr}: {url}")
        except socket.gaierror:
            pass  # Let the actual fetch handle DNS failures


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
    # Integrity check: verify content hash if present
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
# Fetch helpers
# ──────────────────────────────────────────────

def fetch_via_jina(url: str, api_key: str | None = None) -> tuple[str, str]:
    """
    Fetch URL via Jina Reader API (https://r.jina.ai).
    Returns (content_markdown, title).
    Raises ValueError if the URL targets a private/internal address.
    """
    validate_url(url)
    jina_url = f"{JINA_BASE_URL}/{url}"
    headers = {"Accept": "text/markdown"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = requests.get(jina_url, headers=headers, timeout=30)
    response.raise_for_status()
    content = response.text
    title = ""
    for line in content.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return content, title


def fetch_via_wayback(url: str, api_key: str | None = None) -> tuple[str, str]:
    """
    Fetch URL via Wayback Machine archive.
    Returns (content_markdown, title).
    """
    avail_response = requests.get(WAYBACK_API, params={"url": url}, timeout=15)
    avail_response.raise_for_status()
    data = avail_response.json()
    closest = data.get("archived_snapshots", {}).get("closest", {})
    if not closest or closest.get("status") != "200":
        raise ValueError(f"No Wayback snapshot available for: {url}")
    archive_url = closest["url"]
    return fetch_via_jina(archive_url, api_key)


def fetch_url(url: str, jina_api_key: str | None = None) -> tuple[str, str, str]:
    """
    Fetch URL content with fallback strategy.
    Returns (content, title, method) where method is "jina", "wayback", or "playwright".
    Raises RuntimeError if all methods fail.

    Treats "succeeded but returned thin content" the same as "raised" — falls through
    to the next fetcher.
    """
    try:
        content, title = fetch_via_jina(url, jina_api_key)
        if len(content) >= MIN_USEFUL_CONTENT_CHARS:
            return content, title, "jina"
        print(f"[fetch_and_clean] Jina returned thin content ({len(content)} chars) for {url}; falling through", file=sys.stderr)
    except ValueError:
        raise  # SSRF validation errors should not be retried
    except Exception as exc:
        print(f"[fetch_and_clean] Jina fetch failed for {url}: {exc}", file=sys.stderr)

    try:
        content, title = fetch_via_wayback(url, jina_api_key)
        if len(content) >= MIN_USEFUL_CONTENT_CHARS:
            return content, title, "wayback"
        print(f"[fetch_and_clean] Wayback returned thin content ({len(content)} chars) for {url}; falling through", file=sys.stderr)
    except Exception as exc:
        print(f"[fetch_and_clean] Wayback fetch failed for {url}: {exc}", file=sys.stderr)

    # Final fallback: Playwright (only if installed)
    try:
        content, title = fetch_via_playwright(url)
        if len(content) >= MIN_USEFUL_CONTENT_CHARS:
            return content, title, "playwright"
        raise RuntimeError(f"Playwright returned thin content ({len(content)} chars)")
    except Exception as e:
        raise RuntimeError(f"All fetch methods failed for {url}") from e


# ──────────────────────────────────────────────
# Main processing
# ──────────────────────────────────────────────

def process_urls(
    urls: list[dict],
    cache_dir: Path,
    ttl_days: int,
    jina_api_key: str | None,
    fetch_delay: float = DEFAULT_FETCH_DELAY,
) -> tuple[list[dict], list[dict]]:
    """
    Fetch and cache a list of URL dicts. Each dict must have a "url" key.
    Returns (fetched_list, failed_list).
    Never raises — failures are collected in failed_list.
    """
    fetched: list[dict] = []
    failed: list[dict] = []
    last_fetch_time: float = 0

    for item in urls:
        url = item["url"]
        cache_key = url_cache_key(url)

        # Cache hit?
        cached = load_cache(cache_dir, cache_key)
        if cached is not None and not is_expired(cached, ttl_days):
            truncated = cached["content"][:MAX_CONTENT_CHARS]
            fetched.append({
                "url": url,
                "title": cached.get("title", ""),
                "content": truncated,
                "fetch_method": cached.get("fetch_method", "cached"),
                "cache_hit": True,
                "fetched_at": cached["fetched_at"],
                "word_count": len(truncated.split()),
            })
            continue

        # Cache miss or expired — fetch (with rate limiting)
        elapsed = time.monotonic() - last_fetch_time
        if elapsed < fetch_delay and last_fetch_time > 0:
            time.sleep(fetch_delay - elapsed)
        try:
            content, title, method = fetch_url(url, jina_api_key)
            last_fetch_time = time.monotonic()
            now = datetime.now(timezone.utc).isoformat()

            # Cache the full text; MAX_CONTENT_CHARS caps output only, so a
            # later run with a higher cap still gets the whole document.
            save_cache(cache_dir, cache_key, {
                "url": url,
                "title": title,
                "content": content,
                "fetch_method": method,
                "fetched_at": now,
            })
            content = content[:MAX_CONTENT_CHARS]

            fetched.append({
                "url": url,
                "title": title,
                "content": content,
                "fetch_method": method,
                "cache_hit": False,
                "fetched_at": now,
                "word_count": len(content.split()),
            })

        except Exception as exc:
            print(f"[fetch_and_clean] FAILED {url}: {exc}", file=sys.stderr)
            failed.append({
                "url": url,
                "error": str(exc),
                "attempts": ["jina", "wayback", "playwright"],
            })

    return fetched, failed


# ──────────────────────────────────────────────
# Parallel fetching
# ──────────────────────────────────────────────

def _fetch_single(url_item: dict, cache_dir: Path, ttl_days: int,
                  jina_api_key: str | None) -> tuple[dict | None, dict | None]:
    """Fetch a single URL. Returns (fetched_dict, None) or (None, failed_dict)."""
    url = url_item["url"]
    cache_key = url_cache_key(url)

    cached = load_cache(cache_dir, cache_key)
    if cached is not None and not is_expired(cached, ttl_days):
        truncated = cached["content"][:MAX_CONTENT_CHARS]
        return {
            "url": url, "title": cached.get("title", ""),
            "content": truncated, "fetch_method": cached.get("fetch_method", "cached"),
            "cache_hit": True, "fetched_at": cached["fetched_at"],
            "word_count": len(truncated.split()),
        }, None

    try:
        content, title, method = fetch_url(url, jina_api_key)
        now = datetime.now(timezone.utc).isoformat()
        save_cache(cache_dir, cache_key, {  # full text; truncate output only
            "url": url, "title": title, "content": content,
            "fetch_method": method, "fetched_at": now,
        })
        content = content[:MAX_CONTENT_CHARS]
        return {
            "url": url, "title": title, "content": content,
            "fetch_method": method, "cache_hit": False,
            "fetched_at": now, "word_count": len(content.split()),
        }, None
    except Exception as exc:
        return None, {"url": url, "error": str(exc), "attempts": ["jina", "wayback", "playwright"]}


def process_urls_parallel(
    urls: list[dict], cache_dir: Path, ttl_days: int,
    jina_api_key: str | None, max_workers: int = 3,
) -> tuple[list[dict], list[dict]]:
    """Fetch URLs in parallel using ThreadPoolExecutor."""
    fetched: list[dict] = []
    failed: list[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_fetch_single, item, cache_dir, ttl_days, jina_api_key): item
            for item in urls
        }
        for future in as_completed(futures):
            result, error = future.result()
            if result:
                fetched.append(result)
            if error:
                failed.append(error)

    return fetched, failed


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

    fetched, failed = process_urls(urls, cache_dir, args.ttl_days, jina_api_key)

    result = {
        "topic": search_context.get("topic", ""),
        "search_context": search_context,
        "fetched": fetched,
        "failed": failed,
        "stats": {
            "total_urls": len(urls),
            "fetched": len(fetched),
            "failed": len(failed),
            "cache_hits": sum(1 for f in fetched if f["cache_hit"]),
            "total_words": sum(f["word_count"] for f in fetched),
        },
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
