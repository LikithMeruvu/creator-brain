import pytest
import yt_dlp

from cbrain import discovery
from cbrain.discovery import channel_base_url, latest, list_channel, select
from cbrain.models import VideoMeta

CID = "UCERm5yFZ1SptUEU4wZ2vJvw"


@pytest.mark.parametrize("raw,expected", [
    ("@JeremyEthier", "https://www.youtube.com/@JeremyEthier"),
    ("JeremyEthier", "https://www.youtube.com/@JeremyEthier"),
    (CID, f"https://www.youtube.com/channel/{CID}"),
    ("https://www.youtube.com/@JeremyEthier/videos", "https://www.youtube.com/@JeremyEthier"),
    ("https://www.youtube.com/@JeremyEthier/", "https://www.youtube.com/@JeremyEthier"),
    (f"https://youtube.com/channel/{CID}/shorts?x=1", f"https://www.youtube.com/channel/{CID}"),
    ("https://m.youtube.com/c/SomeName/videos", "https://www.youtube.com/c/SomeName"),
    ("www.youtube.com/@a", "https://www.youtube.com/@a"),
])
def test_channel_base_url(raw, expected):
    assert channel_base_url(raw) == expected


def test_channel_base_url_empty():
    with pytest.raises(ValueError):
        channel_base_url("  ")


def _entry(i, views=100, **kw):
    return {"id": f"vid{i:08d}", "title": f"Video {i}", "duration": 600, "view_count": views, **kw}


class FakeYDL:
    calls: list = []
    pages = {}

    def __init__(self, params=None):
        self.params = params or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        FakeYDL.calls.append((url, self.params))
        return FakeYDL.pages[url.rsplit("/", 1)[1]]


@pytest.fixture
def fake(monkeypatch):
    FakeYDL.calls = []
    FakeYDL.pages = {
        "videos": {"_type": "playlist", "channel": "Jeremy Ethier", "channel_id": CID, "uploader_id": "@JeremyEthier",
                   "title": "Jeremy Ethier - Videos",
                   "entries": [_entry(1, 5000), _entry(2, 10), {"id": "bad"}, None, _entry(1, 5000)]},
        "shorts": {"channel": "Jeremy Ethier", "entries": [_entry(9, 7, id="short000009")]},
    }
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_list_channel_basic(fake):
    creator, vids = list_channel("@JeremyEthier", limit=5)
    assert (creator.slug, creator.name, creator.handle, creator.channel_id) == (
        "jeremy-ethier", "Jeremy Ethier", "@JeremyEthier", CID)
    assert [v.id for v in vids] == ["vid00000001", "vid00000002"]  # dedup + invalid ids dropped
    assert vids[0].views == 5000 and not vids[0].is_short
    url, params = fake.calls[0]
    assert url == "https://www.youtube.com/@JeremyEthier/videos"
    assert params["extract_flat"] and params["playlistend"] == 5
    assert params["proxy"] == "" and params["retries"] == 0 and params["ignoreconfig"]
    assert params["cookiefile"] is None and params["cookiesfrombrowser"] is None
    assert len(fake.calls) == 1  # no /shorts call by default


def test_list_channel_shorts_flagged(fake):
    _, vids = list_channel("@JeremyEthier", include_shorts=True)
    shorts = [v for v in vids if v.is_short]
    assert [v.id for v in shorts] == ["short000009"]
    assert any(c[0].endswith("/shorts") for c in fake.calls)


def test_list_channel_published_from_upload_date(fake):
    fake.pages["videos"]["entries"] = [_entry(1, upload_date="20260102"), _entry(2, timestamp=1767312000)]
    _, vids = list_channel(CID)
    assert vids[0].published == "2026-01-02" and vids[1].published == "2026-01-02"
    assert fake.calls[0][0] == f"https://www.youtube.com/channel/{CID}/videos"


def test_list_channel_error_is_classified(monkeypatch):
    from cbrain.fetch.errors import Permanent

    class Boom(FakeYDL):
        def extract_info(self, url, download=False):
            raise RuntimeError("ERROR: HTTP Error 404: Not Found; This channel does not exist.")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Boom)
    with pytest.raises(Permanent):
        list_channel("@nobody")


def test_latest(fake):
    vids = latest("@JeremyEthier", n=3)
    assert len(vids) == 2
    assert fake.calls[0][1]["playlistend"] == 3


# ---- select ---------------------------------------------------------------
def make(n, views=None, **kw):
    return [VideoMeta(id=f"v{i:010d}", title=f"t{i}", views=(views(i) if views else None), **kw) for i in range(n)]


def test_select_recent_plus_winners():
    vids = make(100, views=lambda i: 1000 + i)  # oldest videos (high i) have most views
    pri = select(vids, "recent", recent_fraction=0.30, top_winners=10)
    recent_ids = {v.id for v in vids[:30]}
    winner_ids = {v.id for v in vids[-10:]}
    assert set(pri) == recent_ids | winner_ids and len(pri) == 40
    assert min(pri[i] for i in winner_ids) >= 1000
    assert max(pri[i] for i in recent_ids - winner_ids) <= 1.0
    # best winner first, then recency order
    assert pri[vids[-1].id] > pri[vids[-2].id] > pri[vids[-10].id]
    assert pri[vids[0].id] > pri[vids[1].id] > pri[vids[29].id] > 0


def test_select_full_and_flags():
    vids = make(10, views=lambda i: i)
    vids[3].is_short = True
    vids[4].is_live = True
    full = select(vids, "full")
    assert len(full) == 8 and vids[3].id not in full and vids[4].id not in full
    assert vids[3].id in select(vids, "full", include_shorts=True)
    assert vids[4].id in select(vids, "full", include_lives=True)


def test_select_deterministic_and_ties():
    vids = make(20, views=lambda i: 500)  # all equal views
    a = select(vids, "recent", top_winners=3)
    b = select(list(vids), "recent", top_winners=3)
    assert a == b
    winners = sorted((k for k, v in a.items() if v >= 1000), key=lambda k: -a[k])
    assert winners == [vids[0].id, vids[1].id, vids[2].id]  # ties broken by recency


def test_select_sorts_by_date_when_known():
    vids = make(5, views=lambda i: 1)
    for i, v in enumerate(vids):
        v.published = f"2026-01-{i + 1:02d}"  # listing order is oldest first here
    pri = select(vids, "recent", recent_fraction=0.2, top_winners=0)
    assert list(pri) == [vids[4].id]


def test_select_empty():
    assert select([], "recent") == {}
    assert select(make(3, is_short=True), "full") == {}
