import io
import json
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

from cbrain.fetch import sponsorblock
from cbrain.fetch.sponsorblock import cut, sponsor_spans
from cbrain.models import Segment, Transcript


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_sponsor_spans_url_and_parse(monkeypatch):
    seen = {}

    def fake_open(req, timeout=0):
        seen["url"] = req.full_url
        data = [{"segment": [10.0, 20.0], "category": "sponsor", "actionType": "skip"},
                {"segment": [18.0, 30.5], "category": "selfpromo"},
                {"segment": [100, 101], "category": "x", "actionType": "poi"},
                {"segment": [5, 5]}, {"bad": 1}]
        return Resp(json.dumps(data).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    assert sponsor_spans("wI2WVevL_lk") == [(10.0, 30.5)]
    q = parse_qs(urlsplit(seen["url"]).query)
    assert q["videoID"] == ["wI2WVevL_lk"]
    assert json.loads(q["categories"][0]) == ["sponsor", "selfpromo", "interaction"]
    assert "%5B%22sponsor%22" in seen["url"]


def test_sponsor_spans_404_and_errors(monkeypatch):
    def nf(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", nf)
    assert sponsor_spans("x") == []

    def boom(req, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert sponsor_spans("x") == []
    monkeypatch.setattr(urllib.request, "urlopen", lambda r, timeout=0: Resp(b"not json"))
    assert sponsor_spans("x") == []


def _t():
    return Transcript("v", [Segment(0, 5, "intro"), Segment(5, 10, "sponsor read"), Segment(10, 15, "mixed"),
                            Segment(15, 20, "content")], "auto_caption")


def test_cut_drops_mostly_covered_segments():
    out = cut(_t(), [(5, 12)])
    assert [s.text for s in out.segments] == ["intro", "mixed", "content"]  # "mixed" is 20% inside
    assert out.removed_spans == [(5.0, 12.0)]
    assert len(_t().segments) == 4  # original untouched


def test_cut_noop():
    t = _t()
    assert cut(t, []) is t
    assert cut(t, [(100, 200)]) is t
    assert sponsorblock.merge([(5, 8), (1, 2), (7, 9)]) == [(1, 2), (5, 9)]
