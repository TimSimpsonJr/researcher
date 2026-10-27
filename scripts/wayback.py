"""wayback.py — Gentle Wayback Machine client.

A process-wide limiter keeps one request in flight with a MIN_GAP_S minimum
gap (per process: parallel batch runners each have their own), and 429/503 replies back off exponentially (honouring a numeric
Retry-After) for at most MAX_TRIES tries. Snapshot lookup goes through the CDX
API (latest statuscode:200 capture); the copy is fetched raw from
web.archive.org/web/<ts>id_/<url>, with no Jina hop. HTML is reduced with
text_utils.html_to_text, PDFs go to pdf_extract.
"""

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests

from block_detect import FetchResult, Verdict, classify
from pdf_extract import looks_like_pdf, to_result
from text_utils import html_to_text
from url_safety import get_validated, validate_url

CDX_API = "https://web.archive.org/cdx/search/cdx"
RAW_URL = "https://web.archive.org/web/{ts}id_/{url}"
MIN_GAP_S = 1.5
MAX_TRIES = 4
BACKOFF_BASE_S = 2.0
MAX_RETRY_AFTER_S = 60.0
RETRY_STATUSES = frozenset({429, 503})

_now = time.monotonic
_sleep = time.sleep


class _Limiter:
    """One request at a time, at least min_gap seconds apart."""

    def __init__(self, min_gap: float):
        self.min_gap = min_gap
        self.last: float | None = None
        self.lock = threading.Lock()

    @contextmanager
    def slot(self):
        with self.lock:
            if self.last is not None:
                wait = self.min_gap - (_now() - self.last)
                if wait > 0:
                    _sleep(wait)
            try:
                yield
            finally:
                self.last = _now()

    def defer(self, seconds: float):
        """Hold every thread's next request for at least `seconds` from now."""
        with self.lock:
            self.last = max(self.last or 0.0, _now() + seconds - self.min_gap)


LIMITER = _Limiter(MIN_GAP_S)


def raw_url(timestamp: str, url: str) -> str:
    return RAW_URL.format(ts=timestamp, url=url)


def _retry_delay(response, attempt: int) -> float:
    value = (response.headers.get("Retry-After") or "").strip()
    if value.isdigit():  # ponytail: an HTTP-date Retry-After falls back to backoff
        return min(float(value), MAX_RETRY_AFTER_S)
    return BACKOFF_BASE_S * 2 ** attempt


def _get(url: str, validated: bool = False, **kwargs):
    """GET through the limiter; retries 429/503. Returns the last response.

    validated=True follows redirects by hand, checking every hop (url_safety).
    """
    for attempt in range(MAX_TRIES):
        with LIMITER.slot():
            if validated:
                response = get_validated(url, timeout=(10, 60))
            else:
                response = requests.get(url, timeout=(10, 60), **kwargs)
        if response.status_code not in RETRY_STATUSES or attempt == MAX_TRIES - 1:
            return response
        LIMITER.defer(_retry_delay(response, attempt))


def fetch(url: str, cache_dir: Path) -> FetchResult:
    """Latest archived copy of url. Never raises for an expected miss or block."""
    validate_url(url)
    cdx = _get(CDX_API, params={
        "url": url, "output": "json", "filter": "statuscode:200",
        "fl": "timestamp", "limit": "-1",
    })
    if cdx.status_code != 200:
        return FetchResult(classify(cdx.status_code, "", ""), method="wayback")
    try:
        rows = cdx.json()
    except ValueError:  # empty or non-JSON reply (e.g. a maintenance page)
        rows = []
    if len(rows) < 2:  # first row is the header
        return FetchResult(Verdict("missing", "no_snapshot"), method="wayback")
    archived = raw_url(rows[-1][0], url)
    validate_url(archived)
    response = _get(archived, validated=True)
    ctype = response.headers.get("Content-Type", "")
    if response.status_code != 200:
        return FetchResult(classify(response.status_code, ctype, ""), method="wayback", content_type=ctype)
    if looks_like_pdf(url, ctype, response.content[:5]):
        return to_result(response.content, cache_dir)
    if "charset" not in ctype.lower():
        response.encoding = "utf-8"  # ponytail: meta-charset non-UTF-8 pages still misdecode; use apparent_encoding if seen
    text, title = html_to_text(response.text)
    verdict = classify(200, ctype, text)
    return FetchResult(verdict, text if verdict.ok else "", title, "wayback", ctype)
