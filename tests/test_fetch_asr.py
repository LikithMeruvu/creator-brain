import io
import json
import urllib.error
from pathlib import Path

import pytest
import yt_dlp

from cbrain.config import Settings
from cbrain.fetch import asr
from cbrain.fetch.errors import Blocked, FetchError, Permanent, Retryable


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeYDL:
    params: dict = {}
    fail: Exception | None = None
    audio = b"x" * 1000

    def __init__(self, params=None):
        FakeYDL.params = params or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def download(self, urls):
        if FakeYDL.fail:
            raise FakeYDL.fail
        out = Path(FakeYDL.params["outtmpl"].replace("%(ext)s", "m4a"))
        out.write_bytes(FakeYDL.audio)


class FakeOpener:
    def __init__(self, script):
        self.script, self.requests = list(script), []

    def open(self, req, timeout=0):
        self.requests.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return Resp(json.dumps(item).encode())


def http_err(code, retry_after=None):
    hdrs = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("https://api.groq.com/x", code, "err", hdrs, io.BytesIO(b'{"error":"x"}'))


@pytest.fixture
def env(monkeypatch):
    FakeYDL.fail = None
    FakeYDL.audio = b"x" * 1000
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    sleeps = []
    monkeypatch.setattr(asr, "_sleep", sleeps.append)
    return sleeps


def settings(**kw):
    return Settings(**{"groq_api_key": "gsk_test", **kw})


VERBOSE = {"language": "en", "text": "hello world", "segments": [
    {"start": 0.0, "end": 1.5, "text": " hello"}, {"start": 1.5, "end": 3.0, "text": " world "}, {"start": 3, "end": 4, "text": "  "}]}


def test_transcribe_groq_ok_and_key_not_via_proxy(env, monkeypatch):
    op = FakeOpener([VERBOSE])
    monkeypatch.setattr(asr, "_direct", op)
    t = asr.transcribe("abc12345678", settings(), proxy="http://9.9.9.9:80")
    assert t.source == "groq_whisper" and t.language == "en"
    assert [(s.start, s.end, s.text) for s in t.segments] == [(0.0, 1.5, "hello"), (1.5, 3.0, "world")]
    # proxy used for the audio download only
    assert FakeYDL.params["proxy"] == "http://9.9.9.9:80"
    req = op.requests[0]
    assert req.full_url == asr.GROQ_URL and req.get_header("Authorization") == "Bearer gsk_test"
    assert b"whisper-large-v3-turbo" in req.data and b"verbose_json" in req.data
    assert req.get_header("Content-type").startswith("multipart/form-data; boundary=")


def test_direct_opener_ignores_proxies(monkeypatch):
    import urllib.request
    monkeypatch.setenv("HTTPS_PROXY", "http://evil:8080")
    default = urllib.request.build_opener()
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in default.handlers)
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    assert not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in direct.handlers)
    assert not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in asr._direct.handlers)


def test_groq_429_retry_after_then_success(env, monkeypatch):
    sleeps = env
    monkeypatch.setattr(asr, "_direct", FakeOpener([http_err(429, 7), VERBOSE]))
    t = asr.transcribe("abc12345678", settings())
    assert len(t.segments) == 2 and sleeps == [7.5]


def test_groq_429_long_wait_is_retryable(env, monkeypatch):
    monkeypatch.setattr(asr, "_direct", FakeOpener([http_err(429, 3600)]))
    with pytest.raises(Retryable):
        asr.transcribe("abc12345678", settings())
    assert env == []


def test_groq_bad_key_is_not_retryable_parking(env, monkeypatch):
    monkeypatch.setattr(asr, "_direct", FakeOpener([http_err(401)]))
    with pytest.raises(FetchError) as e:
        asr.transcribe("abc12345678", settings())
    assert not isinstance(e.value, (Permanent, Blocked))


def test_groq_5xx_retries(env, monkeypatch):
    monkeypatch.setattr(asr, "_direct", FakeOpener([http_err(503), http_err(502), VERBOSE]))
    assert asr.transcribe("abc12345678", settings()).segments
    assert len(env) == 2


def test_no_asr_configured(env):
    with pytest.raises(FetchError):
        asr.transcribe("abc12345678", Settings(groq_api_key="", local_whisper=False))


def test_audio_download_errors_are_classified(env):
    FakeYDL.fail = RuntimeError("Sign in to confirm you're not a bot")
    with pytest.raises(Blocked):
        asr.transcribe("abc12345678", settings())
    FakeYDL.fail = RuntimeError("Private video")
    with pytest.raises(Permanent):
        asr.transcribe("abc12345678", settings())


def test_large_audio_without_ffmpeg_is_permanent(env, monkeypatch):
    FakeYDL.audio = b"x" * (asr.MAX_UPLOAD_BYTES + 10)
    monkeypatch.setattr(asr.shutil, "which", lambda name: None)
    with pytest.raises(Permanent):
        asr.transcribe("abc12345678", settings())


def test_large_audio_split_offsets(env, monkeypatch, tmp_path):
    FakeYDL.audio = b"x" * (asr.MAX_UPLOAD_BYTES + 10)
    monkeypatch.setattr(asr.shutil, "which", lambda name: "ffmpeg")

    def fake_run(cmd, **kw):
        d = Path(cmd[-1]).parent
        for i in range(2):
            (d / f"chunk_{i:04d}.m4a").write_bytes(b"c")

    monkeypatch.setattr(asr.subprocess, "run", fake_run)
    one = {"language": "en", "segments": [{"start": 1.0, "end": 2.0, "text": "a"}]}
    monkeypatch.setattr(asr, "_direct", FakeOpener([one, one]))
    t = asr.transcribe("abc12345678", settings())
    assert [s.start for s in t.segments] == [1.0, 601.0]


def test_groq_failure_falls_back_to_local(env, monkeypatch):
    monkeypatch.setattr(asr, "_direct", FakeOpener([http_err(429, 3600)]))
    from cbrain.models import Segment
    monkeypatch.setattr(asr, "local_transcribe_file", lambda p, language=None: ([Segment(0, 1, "local")], "en"))
    t = asr.transcribe("abc12345678", settings(local_whisper=True))
    assert t.source == "local_whisper" and t.segments[0].text == "local"


def test_local_only_when_no_key(env, monkeypatch):
    from cbrain.models import Segment
    monkeypatch.setattr(asr, "local_transcribe_file", lambda p, language=None: ([Segment(0, 1, "x")], None))
    t = asr.transcribe("abc12345678", Settings(groq_api_key="", local_whisper=True))
    assert t.source == "local_whisper"


def test_local_whisper_missing_package(monkeypatch, tmp_path):
    import sys
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # forces ImportError
    with pytest.raises(FetchError):
        asr.local_transcribe_file(tmp_path / "a.m4a")


def test_whisper_is_told_the_language():
    """Whisper must never guess the language: it once wrote fast English as Hindi."""
    s = Settings(groq_api_key="k", default_language="en")
    assert asr.whisper_language(None, s) == "en"
    assert asr.whisper_language("en-US", s) == "en"
    assert asr.whisper_language("es", s) == "es"
    assert asr.whisper_language("??", s) == "en"
