import json
import urllib.error

import pytest

from cbrain import visual
from cbrain.config import Settings
from cbrain.paths import VaultPaths


@pytest.fixture
def paths(tmp_path):
    p = VaultPaths(tmp_path)
    p.ensure_dirs()
    return p


def http_error(code, headers=None):
    return urllib.error.HTTPError("http://x", code, "err", headers or {}, None)


def info_with(frag_count=4, dur=100.0, rows=5, cols=5):
    return {"formats": [
        {"format_id": "sb1", "format_note": "storyboard", "ext": "mhtml", "rows": 5, "columns": 5,
         "fragments": [{"url": "http://sb1/0", "duration": dur}]},
        {"format_id": "sb0", "format_note": "storyboard", "ext": "mhtml", "rows": rows, "columns": cols,
         "width": 320, "height": 180,
         "fragments": [{"url": f"http://sb0/{i}", "duration": dur} for i in range(frag_count)]},
        {"format_id": "18", "ext": "mp4"},
    ]}


# ---- thumbnail

def test_thumbnail_downloads_and_caches(paths, monkeypatch):
    calls = []

    def fake(url, **kw):
        calls.append(url)
        return b"JPEG"
    monkeypatch.setattr(visual, "_http", fake)
    p = visual.thumbnail("abc", paths)
    assert p == paths.visual / "abc.jpg" and p.read_bytes() == b"JPEG"
    assert "maxresdefault" in calls[0]
    assert visual.thumbnail("abc", paths) == p and len(calls) == 1  # cached
    assert not list(paths.visual.glob("*.tmp"))


def test_thumbnail_falls_back_to_hq(paths, monkeypatch):
    def fake(url, **kw):
        if "maxres" in url:
            raise http_error(404)
        return b"HQ"
    monkeypatch.setattr(visual, "_http", fake)
    assert visual.thumbnail("abc", paths).read_bytes() == b"HQ"


def test_thumbnail_failure_returns_none(paths, monkeypatch):
    def boom(url, **kw):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(visual, "_http", boom)
    assert visual.thumbnail("abc", paths) is None

    def both404(url, **kw):
        raise http_error(404)
    monkeypatch.setattr(visual, "_http", both404)
    assert visual.thumbnail("abc", paths) is None


def test_thumbnail_uses_no_proxy(paths, monkeypatch):
    seen = {}

    def fake(url, **kw):
        seen.update(kw)
        return b"x"
    monkeypatch.setattr(visual, "_http", fake)
    visual.thumbnail("abc", paths)
    assert not seen.get("proxy")


# ---- storyboard

def test_storyboard_whole_mosaics_without_pillow(monkeypatch):
    monkeypatch.setattr(visual, "_pillow", lambda: None)
    got = []

    def fake(url, **kw):
        got.append((url, kw.get("proxy")))
        return url.encode()
    monkeypatch.setattr(visual, "_http", fake)
    monkeypatch.setattr(visual, "_extract_info", lambda *a, **k: pytest.fail("info was provided"))
    frames = visual.storyboard_frames("v", proxy="http://p:1", every_seconds=100, info=info_with())
    assert [t for t, _ in frames] == [0.0, 100.0, 200.0, 300.0]
    assert frames[1][1] == b"http://sb0/1"
    assert all(p == "http://p:1" for _, p in got)


def test_storyboard_skips_fragments_without_samples(monkeypatch):
    monkeypatch.setattr(visual, "_pillow", lambda: None)
    got = []
    monkeypatch.setattr(visual, "_http", lambda url, **kw: got.append(url) or b"m")
    # 4 fragments x 100s, sample every 250s -> t=0,250 -> fragments 0 and 2 only
    frames = visual.storyboard_frames("v", every_seconds=250, info=info_with())
    assert got == ["http://sb0/0", "http://sb0/2"]
    assert [t for t, _ in frames] == [0.0, 200.0]


class FakeImage:
    def __init__(self, size=(1600, 900)):
        self.width, self.height = size
        self.box = None

    def convert(self, mode):
        return self

    def crop(self, box):
        c = FakeImage((box[2] - box[0], box[3] - box[1]))
        c.box = box
        return c

    def save(self, buf, fmt, quality=0):
        buf.write(f"{fmt}:{self.box}".encode())


class FakeImageModule:
    @staticmethod
    def open(fp):
        return FakeImage()


def test_storyboard_crops_tiles_with_pillow(monkeypatch):
    monkeypatch.setattr(visual, "_pillow", lambda: FakeImageModule)
    monkeypatch.setattr(visual, "_http", lambda url, **kw: b"mosaic")
    # one 100s fragment of 5x5 tiles -> 4s/tile; every 30s -> t=0,30,60,90 -> tiles 0,7,15,22
    frames = visual.storyboard_frames("v", every_seconds=30, info=info_with(frag_count=1))
    assert [t for t, _ in frames] == [0.0, 30.0, 60.0, 90.0]
    assert frames[1][1] == b"JPEG:(640, 180, 960, 360)"  # tile 7 = row 1, col 2 (320x180 tiles)


def test_storyboard_caps_images(monkeypatch):
    monkeypatch.setattr(visual, "_pillow", lambda: None)
    monkeypatch.setattr(visual, "_http", lambda url, **kw: b"m")
    frames = visual.storyboard_frames("v", every_seconds=1, info=info_with(frag_count=100))
    assert len(frames) <= visual.MAX_IMAGES


def test_storyboard_byte_cap(monkeypatch):
    monkeypatch.setattr(visual, "_pillow", lambda: None)
    monkeypatch.setattr(visual, "MAX_TOTAL_BYTES", 25)
    monkeypatch.setattr(visual, "_http", lambda url, **kw: b"x" * 10)
    frames = visual.storyboard_frames("v", every_seconds=100, info=info_with())
    assert len(frames) == 2


def test_storyboard_no_storyboard_and_fetch_fallback(monkeypatch):
    assert visual.storyboard_frames("v", info={"formats": [{"format_id": "18"}]}) == []
    monkeypatch.setattr(visual, "_pillow", lambda: None)
    monkeypatch.setattr(visual, "_http", lambda url, **kw: b"m")
    calls = []

    def fake_extract(vid, proxy):
        calls.append((vid, proxy))
        return info_with(frag_count=1)
    monkeypatch.setattr(visual, "_extract_info", fake_extract)
    assert visual.storyboard_frames("v", proxy="http://p:1", every_seconds=500)
    assert calls == [("v", "http://p:1")]


def test_storyboard_fragment_error_is_skipped(monkeypatch):
    monkeypatch.setattr(visual, "_pillow", lambda: None)

    def fake(url, **kw):
        if url.endswith("/1"):
            raise urllib.error.URLError("x")
        return b"m"
    monkeypatch.setattr(visual, "_http", fake)
    frames = visual.storyboard_frames("v", every_seconds=100, info=info_with())
    assert [t for t, _ in frames] == [0.0, 200.0, 300.0]


# ---- describe

def settings(key="gk"):
    return Settings(groq_api_key=key)


def fake_groq(monkeypatch, responder):
    calls = []

    def fake(url, data=None, headers=None, proxy=None, timeout=0):
        body = json.loads(data)
        calls.append({"url": url, "body": body, "headers": headers, "proxy": proxy})
        out = responder(len(calls), body)
        if isinstance(out, Exception):
            raise out
        return json.dumps({"choices": [{"message": {"content": out}}]}).encode()
    monkeypatch.setattr(visual, "_http", fake)
    return calls


def numbered(body):
    n = sum(1 for c in body["messages"][0]["content"] if c["type"] == "image_url")
    return "\n".join(f"{i}: scene {i}" for i in range(1, n + 1))


def test_describe_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(visual, "_http", lambda *a, **k: pytest.fail("no network without key"))
    assert visual.describe([(0.0, b"x")], settings("")) == []
    assert visual.describe([], settings()) == []


def test_describe_batches_of_five(monkeypatch):
    calls = fake_groq(monkeypatch, lambda n, body: numbered(body))
    frames = [(float(i * 30), b"\xff\xd8img") for i in range(12)]
    res = visual.describe(frames, settings())
    assert len(calls) == 3
    counts = [sum(1 for c in k["body"]["messages"][0]["content"] if c["type"] == "image_url") for k in calls]
    assert counts == [5, 5, 2]
    assert len(res) == 12 and res[6] == (180.0, "scene 2")
    c = calls[0]
    assert c["url"] == visual.GROQ_URL and c["proxy"] is None
    assert c["headers"]["Authorization"] == "Bearer gk"
    assert c["body"]["model"] == settings().vision_model
    url = c["body"]["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")


def test_describe_retries_429_with_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(visual.time, "sleep", sleeps.append)

    def responder(n, body):
        return http_error(429, {"retry-after": "7"}) if n == 1 else numbered(body)
    calls = fake_groq(monkeypatch, responder)
    res = visual.describe([(0.0, b"x")], settings())
    assert res == [(0.0, "scene 1")] and len(calls) == 2 and sleeps == [7.0]


def test_describe_gives_up_after_repeated_429(monkeypatch):
    monkeypatch.setattr(visual.time, "sleep", lambda s: None)
    calls = fake_groq(monkeypatch, lambda n, b: http_error(429))
    assert visual.describe([(0.0, b"x")], settings()) == []
    assert len(calls) == visual.MAX_RETRIES


def test_describe_failed_batch_does_not_lose_others(monkeypatch):
    def responder(n, body):
        return http_error(500) if n == 1 else numbered(body)
    fake_groq(monkeypatch, responder)
    res = visual.describe([(float(i), b"x") for i in range(7)], settings())
    assert [t for t, _ in res] == [5.0, 6.0]


def test_describe_unparseable_single_reply(monkeypatch):
    fake_groq(monkeypatch, lambda n, b: "A person doing a squat with a barbell")
    assert visual.describe([(3.0, b"x")], settings()) == [(3.0, "A person doing a squat with a barbell")]


def test_real_http_is_direct_and_sends_headers(monkeypatch):
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    class FakeOpener:
        def open(self, req, timeout=0):
            seen["req"] = req
            return FakeResp()
    monkeypatch.setattr(visual, "_DIRECT", FakeOpener())
    visual._http("https://api.groq.com/x", data=b"{}", headers={"Authorization": "Bearer k"})
    assert seen["req"].get_header("Authorization") == "Bearer k"


# ---- wrapper

def test_visual_for_video_happy_path(paths, monkeypatch):
    monkeypatch.setattr(visual, "_pillow", lambda: None)

    def fake(url, data=None, **kw):
        if data is None:
            return b"IMG"
        return json.dumps({"choices": [{"message": {"content": numbered(json.loads(data))}}]}).encode()
    monkeypatch.setattr(visual, "_http", fake)
    res = visual.visual_for_video("v", paths, settings(), info=info_with(frag_count=1), proxy=None)
    assert res and res[0] == (0.0, "scene 1")
    assert (paths.visual / "v.jpg").exists()


def test_visual_for_video_never_raises(paths, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(visual, "thumbnail", boom)
    assert visual.visual_for_video("v", paths, settings()) == []
    monkeypatch.undo()
    monkeypatch.setattr(visual, "_http", lambda *a, **k: b"x")
    monkeypatch.setattr(visual, "storyboard_frames", boom)
    assert visual.visual_for_video("v", paths, settings()) == []


def test_visual_for_video_no_key_skips_storyboards(paths, monkeypatch):
    monkeypatch.setattr(visual, "_http", lambda *a, **k: b"x")
    monkeypatch.setattr(visual, "storyboard_frames", lambda *a, **k: pytest.fail("not needed"))
    assert visual.visual_for_video("v", paths, settings("")) == []
