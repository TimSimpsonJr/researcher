"""block_detect.py — Is a fetched page real content, a bot wall, gone, or empty?

classify() turns (HTTP status, content type, text) into a Verdict. Every fetcher
(Jina, the direct PDF GET, the local browser, Wayback) returns a FetchResult
carrying one, so the fallback chain in fetch_and_clean routes on the verdict
instead of on exceptions or on length alone.
"""

from dataclasses import dataclass

MIN_USEFUL_CONTENT_CHARS = 200  # below this, content is likely empty/blocked
WALL_MAX_CHARS = 5000  # wall pages are short; markers in longer text are ignored
BLOCKED_STATUSES = frozenset({401, 403, 429, 503})
MISSING_STATUSES = frozenset({404, 410})
PDF_METHODS = ("pdf-local", "pdf-ocr")

# (reason, lowercase needle), checked in order; the first hit wins. Cloudflare's
# passive interstitial comes first so the browser waits on it rather than giving
# up. A new wall is one line here plus a fixture in tests/fixtures/pages/.
MARKERS = [
    ("cloudflare", "just a moment..."),
    ("cloudflare", "attention required! | cloudflare"),
    ("cloudflare", "verify you are human"),
    ("cloudflare", "needs to review the security of your connection"),
    ("cloudflare", "checking your browser before accessing"),
    ("cloudflare", "cf-chl"),
    ("cloudflare", "_cf_chl_opt"),
    ("cloudflare", "challenge-platform"),
    ("captcha", "cf-turnstile"),
    ("captcha", "h-captcha"),
    ("captcha", "hcaptcha.com"),
    ("captcha", "g-recaptcha"),
    ("captcha", "www.google.com/recaptcha"),
    ("captcha", "i'm not a robot"),
    ("denied", "access denied"),
    ("denied", "request blocked"),
    ("denied", "you don't have permission to access"),
    ("login", "sign in to continue"),
    ("login", "log in to continue"),
    ("login", "please sign in"),
    ("login", "please log in"),
    ("login", "you must be logged in"),
    ("login", "login required"),
]


@dataclass(frozen=True)
class Verdict:
    kind: str  # "ok" | "blocked" | "missing" | "thin" ("unsafe" from fetch_and_clean)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


OK = Verdict("ok")


def blocked(reason: str) -> Verdict:
    return Verdict("blocked", reason)


@dataclass
class FetchResult:
    """One fetch attempt. content is empty unless the verdict is ok."""

    verdict: Verdict
    content: str = ""
    title: str = ""
    method: str = ""
    content_type: str = ""
    image_only_pdf: bool = False
    pdf_path: str | None = None
    error: str = ""

    @property
    def is_pdf(self) -> bool:
        return self.method in PDF_METHODS


def classify(status: int | None, content_type: str, text: str, html: str = "") -> Verdict:
    """Verdict for one response.

    text is the readable text; html (optional) is the raw markup, scanned for
    widget markers alongside text. Markers only count when the visible text is
    short, so a long article with a reCAPTCHA comment form stays ok. Marker hits
    beat status codes, so a Cloudflare 403 reports "cloudflare", not "http_403".
    """
    if status in MISSING_STATUSES:
        return Verdict("missing", f"http_{status}")
    if "pdf" not in (content_type or "").lower() and len(text) < WALL_MAX_CHARS:
        hay = f"{text}\n{html}".lower()
        for reason, needle in MARKERS:
            if needle in hay:
                return blocked(reason)
    if status in BLOCKED_STATUSES:
        return blocked(f"http_{status}")
    n = len(text.strip())
    if n < MIN_USEFUL_CONTENT_CHARS:
        return Verdict("thin", f"{n}_chars")
    return OK
