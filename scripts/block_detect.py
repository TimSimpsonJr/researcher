"""block_detect.py — Is a fetched page real content, a bot wall, gone, or empty?

classify() turns (HTTP status, content type, text, optional raw html) into a Verdict. Every fetcher
(Jina, the direct PDF GET, the local browser, Wayback) returns a FetchResult
carrying one, so the fallback chain in fetch_and_clean routes on the verdict
instead of on exceptions or on length alone.
"""

from dataclasses import dataclass

MIN_USEFUL_CONTENT_CHARS = 200  # below this, content is likely empty/blocked
WALL_MAX_CHARS = 5000  # visible text length; wall pages are short, markers on longer pages are ignored
BLOCKED_STATUSES = frozenset({401, 403, 429, 503})
MISSING_STATUSES = frozenset({404, 410})
PDF_METHODS = ("pdf-local", "pdf-ocr")

# (reason, lowercase needle, in_markup), checked in order; the first hit wins.
# in_markup=True needles are also searched in the raw html; phrase needles
# (False) match visible text only. Login phrases must name a gated action
# ("sign in to continue", "log in to view"), so "sign in to comment" prompts on
# real pages don't count; with html_to_text, link text is visible text too.
# Cloudflare's passive interstitial comes first so the browser waits on it rather
# than giving up. A new wall is one line here plus a fixture in tests/fixtures/pages/.
# "challenge-platform" is deliberately absent: Cloudflare's jsd bot-detection
# loader (/cdn-cgi/challenge-platform/scripts/jsd/main.js) sits on ordinary pages.
MARKERS = [
    ("cloudflare", "just a moment...", False),
    ("cloudflare", "attention required! | cloudflare", False),
    ("cloudflare", "verify you are human", False),
    ("cloudflare", "needs to review the security of your connection", False),
    ("cloudflare", "checking your browser before accessing", False),
    ("cloudflare", "cf-chl", True),
    ("cloudflare", "_cf_chl_opt", True),
    ("captcha", "cf-turnstile", True),
    ("captcha", "h-captcha", True),
    ("captcha", "hcaptcha.com", True),
    ("captcha", "g-recaptcha", True),
    ("captcha", "www.google.com/recaptcha", True),
    ("captcha", "i'm not a robot", False),
    ("denied", "access denied", False),
    ("denied", "request blocked", False),
    ("denied", "you don't have permission to access", False),
    ("login", "sign in to continue", False),
    ("login", "log in to continue", False),
    ("login", "please sign in to view", False),
    ("login", "please log in to view", False),
    ("login", "you must be logged in to view", False),
    ("login", "you must be logged in to access", False),
    ("login", "login required", False),
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

    text is the readable text; html (optional) is the raw markup, scanned only
    for the in_markup needles (script/widget names); phrase needles match text
    alone. Markers only count when the visible text is short, so a long article
    with a reCAPTCHA comment form stays ok. Marker hits beat status codes, so a
    Cloudflare 403 reports "cloudflare", not "http_403".
    """
    if status in MISSING_STATUSES:
        return Verdict("missing", f"http_{status}")
    if "pdf" not in (content_type or "").lower() and len(text) < WALL_MAX_CHARS:
        low_text, low_html = text.lower(), html.lower()
        for reason, needle, in_markup in MARKERS:
            if needle in low_text or (in_markup and needle in low_html):
                return blocked(reason)
    if status in BLOCKED_STATUSES:
        return blocked(f"http_{status}")
    n = len(text.strip())
    if n < MIN_USEFUL_CONTENT_CHARS:
        return Verdict("thin", f"{n}_chars")
    return OK
