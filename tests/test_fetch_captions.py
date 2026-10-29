import json

import pytest
import yt_dlp

from cbrain.fetch import captions
from cbrain.fetch.captions import choose_track, fetch_captions, parse_json3
from cbrain.fetch.errors import Blocked, FetchError, NoCaptions, Permanent, Retryable, classify


# ---- errors ---------------------------------------------------------------
@pytest.mark.parametrize("msg,cls", [
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", Blocked),
    ("HTTP Error 429: Too Many Requests", Blocked),
    ("rate-limited by YouTube", Blocked),
    ("This content isn't available, try again later.", Blocked),
    ("Private video. Sign in if you've been granted access to this video", Permanent),
    ("Video unavailable. This video has been removed by the uploader", Permanent),
    ("Join this channel to get access to members-only content", Permanent),
    ("Sign in to confirm your age. This video may be inappropriate", Permanent),
    ("The read operation timed out", Retryable),
    ("<urlopen error [Errno 11001] getaddrinfo failed>", Retryable),
    ("something weird", Retryable),
])
def test_classify(msg, cls):
    assert type(classify(RuntimeError(msg))) is cls


def test_classify_proxy_failures_blame_the_proxy():
    assert isinstance(classify("ProxyError: Unable to connect to proxy", proxied=True), Blocked)
    assert isinstance(classify("The read operation timed out", proxied=True), Blocked)
    assert isinstance(classify("The read operation timed out", proxied=False), Retryable)
    assert isinstance(classify("Private video", proxied=True), Permanent)


def test_classify_passthrough():
    e = NoCaptions("x")
    assert classify(e) is e


# ---- json3 + track choice -------------------------------------------------
def test_parse_json3():
    payload = {"events": [
        {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Hello "}, {"utf8": "world &amp; co"}]},
        {"tStartMs": 2000, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 2500, "dDurationMs": 1000, "segs": [{"utf8": "second\nline"}]},
        {"tStartMs": 2500, "dDurationMs": 1000, "segs": [{"utf8": "second line"}]},
        {"wsWinStyle": 1},
        {"tStartMs": 4000, "segs": [{"utf8": "last"}]},
    ]}
    segs = parse_json3(payload)
    assert [(s.start, s.end, s.text) for s in segs] == [
        (0.0, 2.0, "Hello world & co"), (2.5, 3.5, "second line"), (4.0, 4.0, "last")]


def _track(url):
    return [{"ext": "vtt", "url": url + "&fmt=vtt"}, {"ext": "json3", "url": url + "&fmt=json3"}]


def test_choose_track_preference():
    info = {"subtitles": {"fr": _track("http://x/fr?a=1"), "en": _track("http://x/en?a=1")},
            "automatic_captions": {"en": _track("http://x/auto-en?a=1"), "en-orig": _track("http://x/orig?a=1")}}
    lang, kind, tr = choose_track(info)
    assert (lang, kind) == ("en", "manual") and "x/en" in tr["url"]
    del info["subtitles"]["en"]
    lang, kind, _ = choose_track(info)
    assert kind == "auto" and lang in ("en", "en-orig")
    info["automatic_captions"] = {"de": _track("http://x/de?a=1"), "es": _track("http://x/es?tlang=de")}
    assert choose_track({"subtitles": {}, "automatic_captions": info["automatic_captions"]})[0] == "de"
    assert choose_track({"subtitles": {"live_chat": _track("http://x/c")}}) is None
    assert choose_track({}) is None


def test_choose_track_other_language_beats_translation():
    info = {"language": "hi", "automatic_captions": {"hi": _track("http://x/hi?a=1"), "en": _track("http://x/en?tlang=en")}}
    assert choose_track(info)[0] == "hi"


# ---- fetch_captions with a fake yt-dlp -----------------------------------
class FakeResp:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.data


class FakeYDL:
    info: dict = {}
    body: bytes = b""
    raise_info: Exception | None = None
    raise_open: Exception | None = None
    last_params: dict = {}
    closed = 0

    def __init__(self, params=None):
        FakeYDL.last_params = params or {}

    def extract_info(self, url, download=False):
        if FakeYDL.raise_info:
            raise FakeYDL.raise_info
        return FakeYDL.info

    def urlopen(self, url):
        if FakeYDL.raise_open:
            raise FakeYDL.raise_open
        return FakeResp(FakeYDL.body)

    def close(self):
        FakeYDL.closed += 1


@pytest.fixture
def ydl(monkeypatch):
    FakeYDL.info, FakeYDL.body = {}, b""
    FakeYDL.raise_info = FakeYDL.raise_open = None
    FakeYDL.closed = 0
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


GOOD = json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 1500, "segs": [{"utf8": "hi there"}]}]}).encode()


def good_info(**kw):
    base = {"title": "T", "upload_date": "20260315", "duration": 600, "view_count": 1234,
            "chapters": [{"start_time": 0, "title": "Intro", "end_time": 10}], "description": "d",
            "channel_id": "UCx", "subtitles": {}, "automatic_captions": {"en": _track("http://x/en?a=1")}}
    base.update(kw)
    return base


def test_fetch_captions_ok(ydl):
    ydl.info, ydl.body = good_info(), GOOD
    t, info = fetch_captions("abc12345678", proxy="http://1.2.3.4:80", timeout=7)
    assert t.source == "auto_caption" and t.language == "en"
    assert [s.text for s in t.segments] == ["hi there"]
    assert t.chapters[0].title == "Intro"
    assert info["published"] == "2026-03-15" and info["view_count"] == 1234 and info["duration"] == 600
    assert info["chapters"] == [{"start": 0.0, "title": "Intro"}] and info["description"] == "d"
    p = ydl.last_params
    assert p["proxy"] == "http://1.2.3.4:80" and p["socket_timeout"] == 7 and p["retries"] == 0
    assert p["skip_download"] and p["cookiefile"] is None and p["ignoreconfig"] and ydl.closed == 1


def test_fetch_captions_manual_and_direct(ydl):
    ydl.info = good_info(subtitles={"en": _track("http://x/m?a=1")})
    ydl.body = GOOD
    t, _ = fetch_captions("abc12345678")
    assert t.source == "manual_caption" and ydl.last_params["proxy"] == ""


def test_fetch_captions_no_tracks_keeps_info(ydl):
    ydl.info = good_info(automatic_captions={})
    with pytest.raises(NoCaptions) as e:
        fetch_captions("abc12345678")
    assert e.value.info["published"] == "2026-03-15"
    assert ydl.closed == 1


def test_fetch_captions_empty_body_is_blocked(ydl):
    ydl.info, ydl.body = good_info(), b"  "
    with pytest.raises(Blocked):
        fetch_captions("abc12345678")


def test_fetch_captions_empty_track(ydl):
    ydl.info, ydl.body = good_info(), json.dumps({"events": []}).encode()
    with pytest.raises(NoCaptions):
        fetch_captions("abc12345678")


def test_fetch_captions_extractor_errors(ydl):
    ydl.raise_info = RuntimeError("ERROR: [youtube] x: Sign in to confirm you're not a bot")
    with pytest.raises(Blocked):
        fetch_captions("abc12345678")
    ydl.raise_info = RuntimeError("Private video")
    with pytest.raises(Permanent):
        fetch_captions("abc12345678")
    ydl.raise_info = RuntimeError("Unable to connect to proxy")
    with pytest.raises(Blocked):
        fetch_captions("abc12345678", proxy="http://p:1")
    with pytest.raises(Retryable):
        ydl.raise_info = TimeoutError("timed out")
        fetch_captions("abc12345678")


def test_fetch_captions_download_429(ydl):
    ydl.info = good_info()
    ydl.raise_open = RuntimeError("HTTP Error 429: Too Many Requests")
    with pytest.raises(Blocked):
        fetch_captions("abc12345678")
    assert ydl.closed == 1


def test_fetch_captions_live_is_permanent(ydl):
    ydl.info = good_info(is_live=True, live_status="is_live")
    with pytest.raises(Permanent):
        fetch_captions("abc12345678")


def test_fetch_captions_bad_json_is_retryable(ydl):
    ydl.info, ydl.body = good_info(), b"<html>nope</html>"
    with pytest.raises(FetchError):
        fetch_captions("abc12345678")


def test_ydl_opts_never_send_cookies():
    o = captions.ydl_opts(None, 9, foo=1)
    assert o["proxy"] == "" and o["cookiefile"] is None and o["cookiesfrombrowser"] is None and o["foo"] == 1
