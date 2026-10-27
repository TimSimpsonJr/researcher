"""Tests for wayback.py: CDX lookup, raw id_ fetch, limiter and backoff.
requests and the clock are faked; nothing touches the network."""

import pytest

import wayback
from block_detect import FetchResult, Verdict

ARTICLE_HTML = (
    "<html><head><title>Agenda</title></head><body><p>"
    + "Item 4: license plate reader contract renewal. " * 10
    + "</p></body></html>"
)


class Resp:
    def __init__(self, status=200, json_data=None, text="", content=b"", headers=None):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._json


def cdx_ok():
    return Resp(200, [["timestamp"], ["20250101120000"]], text='[["timestamp"],["20250101120000"]]')


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 100.0, "sleeps": []}

    def sleep(seconds):
        state["sleeps"].append(round(seconds, 3))
        state["t"] += seconds

    monkeypatch.setattr(wayback, "_now", lambda: state["t"])
    monkeypatch.setattr(wayback, "_sleep", sleep)
    monkeypatch.setattr(wayback, "LIMITER", wayback._Limiter(wayback.MIN_GAP_S))
    return state


@pytest.fixture
def net(monkeypatch):
    queue, calls, validated = [], [], []

    def get(url, timeout=60, **kwargs):
        calls.append((url, kwargs.get("params")))
        return queue.pop(0)

    monkeypatch.setattr(wayback.requests, "get", get)
    monkeypatch.setattr(wayback, "validate_url", validated.append)
    return queue, calls, validated


def test_raw_url_uses_id_suffix():
    assert (wayback.raw_url("20250101120000", "https://x.gov/a.pdf")
            == "https://web.archive.org/web/20250101120000id_/https://x.gov/a.pdf")


def test_fetch_html_snapshot(clock, net, tmp_path):
    queue, calls, validated = net
    queue += [cdx_ok(), Resp(200, text=ARTICLE_HTML, headers={"Content-Type": "text/html"})]
    result = wayback.fetch("https://x.gov/agenda", tmp_path)
    assert result.verdict.ok and result.method == "wayback" and result.title == "Agenda"
    assert "license plate reader" in result.content
    cdx_url, params = calls[0]
    assert cdx_url == wayback.CDX_API
    assert params["url"] == "https://x.gov/agenda" and params["filter"] == "statuscode:200"
    assert calls[1][0] == "https://web.archive.org/web/20250101120000id_/https://x.gov/agenda"
    assert validated == ["https://x.gov/agenda", calls[1][0]]
    assert clock["sleeps"] == [1.5]


def test_no_snapshot_is_missing(clock, net, tmp_path):
    queue, calls, _ = net
    queue.append(Resp(200, [], text="[]"))
    assert wayback.fetch("https://x.gov/gone", tmp_path).verdict == Verdict("missing", "no_snapshot")
    assert len(calls) == 1


def test_429_backoff_honours_retry_after(clock, net, tmp_path):
    queue, calls, _ = net
    queue += [Resp(429, headers={"Retry-After": "7"}), Resp(429), cdx_ok(),
              Resp(200, text=ARTICLE_HTML, headers={"Content-Type": "text/html"})]
    assert wayback.fetch("https://x.gov/agenda", tmp_path).verdict.ok
    assert clock["sleeps"] == [7.0, 4.0, 1.5]
    assert len(calls) == 4


def test_gives_up_after_max_tries(clock, net, tmp_path):
    queue, calls, _ = net
    queue += [Resp(429) for _ in range(wayback.MAX_TRIES)]
    assert wayback.fetch("https://x.gov/agenda", tmp_path).verdict == Verdict("blocked", "http_429")
    assert len(calls) == wayback.MAX_TRIES
    assert clock["sleeps"] == [2.0, 4.0, 8.0]


def test_limiter_spaces_requests(clock, net):
    queue, _, _ = net
    queue += [Resp(200), Resp(200)]
    wayback._get("https://web.archive.org/a")
    wayback._get("https://web.archive.org/b")
    assert clock["sleeps"] == [1.5]


def test_pdf_snapshot_goes_to_pdf_extract(clock, net, tmp_path, monkeypatch):
    queue, _, _ = net
    queue += [cdx_ok(), Resp(200, content=b"%PDF-1.7 data", headers={"Content-Type": "application/pdf"})]
    seen = []
    monkeypatch.setattr(wayback, "to_result",
                        lambda body, cache_dir: seen.append(body) or FetchResult(Verdict("ok"), "pdf text", "", "pdf-local"))
    result = wayback.fetch("https://x.gov/packet.pdf", tmp_path)
    assert result.method == "pdf-local" and seen == [b"%PDF-1.7 data"]
