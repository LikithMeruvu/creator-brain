import gzip
import json
import threading
import time

import pytest

from cbrain.config import Settings
from cbrain.fetch import runner as R
from cbrain.fetch.errors import Blocked, NoCaptions, Permanent, Retryable
from cbrain.jobs import JobStore
from cbrain.models import Creator, JobState, Segment, Transcript, VideoMeta
from cbrain.paths import VaultPaths

SLUG = "jeremy-ethier"


def vid(i):
    return f"vid{i:08d}"


@pytest.fixture
def vault(tmp_path):
    paths = VaultPaths(tmp_path / "vault")
    paths.ensure_dirs()
    store = JobStore(paths.jobs_db)
    store.upsert_creator(Creator(slug=SLUG, name="Jeremy Ethier", handle="@JeremyEthier", channel_id="UCx"))
    return paths, store


def queue(store, n, views=None):
    metas = [VideoMeta(id=vid(i), title=f"T{i}", views=(views[i] if views else 100 + i)) for i in range(n)]
    store.add_videos(SLUG, metas, {m.id: float(n - i) for i, m in enumerate(metas)})  # vid0 first
    return metas


def settings(**kw):
    base = dict(own_ip_interval_seconds=0, proxy_lane_interval_seconds=0, sponsorblock=False,
                groq_api_key="", local_whisper=False, whisper_upgrade_top=0, lanes=1)
    base.update(kw)
    return Settings(**base)


def ok_transcript(video_id, source="auto_caption"):
    return Transcript(video_id, [Segment(0, 2, f"hello from {video_id}")], source, language="en")


class Pool:
    def __init__(self, proxies):
        self.free = list(proxies)
        self.released = []
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            return self.free.pop(0) if self.free else None

    def release(self, proxy, ok, blocked=False):
        with self.lock:
            self.released.append((proxy, ok, blocked))
            if ok:
                self.free.append(proxy)

    def stats(self):
        return {"healthy": len(self.free)}


def make_runner(vault, cfg=None, pool=None, fetched=None, **kw):
    paths, store = vault
    sink = fetched if fetched is not None else []
    r = R.FetchRunner(store, paths, cfg or settings(), pool, lambda row, t, info: sink.append((row, t, info)),
                      block_backoff_seconds=kw.pop("block_backoff_seconds", 0.2), defer_seconds=0,
                      idle_wait_seconds=0.01, backoff_poll_seconds=0.02, **kw)
    return r, sink


def test_own_lane_happy_path(vault, monkeypatch):
    paths, store = vault
    queue(store, 3)
    order = []

    def fake_fetch(video_id, proxy=None, timeout=15):
        order.append((video_id, proxy))
        return ok_transcript(video_id), {"published": "2026-02-03", "view_count": 4242, "duration": 99,
                                         "chapters": [{"start": 0.0, "title": "Intro"}]}

    monkeypatch.setattr(R, "fetch_captions", fake_fetch)
    r, sink = make_runner(vault)
    stats = r.run()
    assert [o[0] for o in order] == [vid(0), vid(1), vid(2)] and all(o[1] is None for o in order)
    assert stats["fetched"] == 3 and stats["lanes"] == 1 and stats["by_source"] == {"auto_caption": 3}
    assert store.counts(SLUG)["fetched"] == 3
    row = store.get(vid(0))
    assert (row.state, row.transcript_source, row.published, row.views, row.duration) == (
        "fetched", "auto_caption", "2026-02-03", 4242, 99)
    saved = json.load(gzip.open(paths.transcript(vid(0)), "rt", encoding="utf-8"))
    assert saved["segments"][0]["text"] == "hello from vid00000000"
    assert saved["chapters"] == [{"start": 0.0, "title": "Intro"}]
    assert not list(paths.transcripts.glob("*.tmp"))
    cb_row, cb_t, cb_info = sink[0]
    assert cb_row.state == "fetched" and cb_row.published == "2026-02-03" and cb_info["view_count"] == 4242


def test_failures_ladder(vault, monkeypatch):
    paths, store = vault
    queue(store, 4)
    asr_calls = []

    def fake_fetch(video_id, proxy=None, timeout=15):
        n = int(video_id[-1])
        if n == 0:
            raise Permanent("Private video")
        if n == 1:
            raise Retryable("timed out")
        if n == 2:
            raise NoCaptions("none", info={"published": "2025-12-25", "chapters": [{"start": 5.0, "title": "Ch"}]})
        raise NoCaptions("none")

    monkeypatch.setattr(R, "fetch_captions", fake_fetch)
    monkeypatch.setattr(R, "transcribe", lambda vid_, s, proxy=None, language=None: asr_calls.append(vid_) or ok_transcript(vid_, "groq_whisper"))
    r, sink = make_runner(vault, settings(groq_api_key="gsk"))
    stats = r.run()
    assert store.get(vid(0)).state == "parked" and store.get(vid(0)).attempts == 1  # Permanent: parked at once
    assert "Private" in store.get(vid(0)).last_error
    assert store.get(vid(1)).state == "queued" and store.get(vid(1)).attempts == 1  # retry later
    assert store.get(vid(2)).state == "fetched" and store.get(vid(2)).transcript_source == "groq_whisper"
    assert store.get(vid(2)).published == "2025-12-25"
    assert sorted(asr_calls) == [vid(2), vid(3)]
    assert stats["parked"] == 1 and stats["retried"] == 1 and stats["fetched"] == 2
    saved = json.load(gzip.open(paths.transcript(vid(2)), "rt", encoding="utf-8"))
    assert saved["chapters"] == [{"start": 5.0, "title": "Ch"}]  # chapters come from the info even for ASR


def test_no_captions_without_asr_parks(vault, monkeypatch):
    _, store = vault
    queue(store, 1)
    monkeypatch.setattr(R, "fetch_captions", lambda *a, **k: (_ for _ in ()).throw(NoCaptions("none")))
    stats = make_runner(vault)[0].run()
    row = store.get(vid(0))
    assert row.state == "parked" and "no ASR" in row.last_error and stats["parked"] == 1


def test_own_ip_block_defers_without_attempt_and_backs_off(vault, monkeypatch):
    _, store = vault
    queue(store, 1)
    calls = []

    def fake_fetch(video_id, proxy=None, timeout=15):
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise Blocked("Sign in to confirm you're not a bot")
        return ok_transcript(video_id), {}

    monkeypatch.setattr(R, "fetch_captions", fake_fetch)
    stats = make_runner(vault, block_backoff_seconds=0.3)[0].run()
    assert stats["blocked"] == 1 and stats["fetched"] == 1
    assert calls[1] - calls[0] >= 0.28          # lane slept instead of hammering
    assert store.get(vid(0)).attempts == 0       # block never burns an attempt


def test_always_blocked_never_spins_hot(vault, monkeypatch):
    _, store = vault
    queue(store, 2)
    calls = []
    monkeypatch.setattr(R, "fetch_captions", lambda v, p=None, timeout=15: calls.append(v) or (_ for _ in ()).throw(Blocked("429")))
    stop = threading.Event()
    threading.Timer(0.8, stop.set).start()
    stats = make_runner(vault, block_backoff_seconds=0.4)[0].run(stop=stop)
    assert 1 <= len(calls) <= 4 and stats["stopped"]
    assert all(store.get(vid(i)).attempts == 0 for i in range(2))


def test_proxy_lanes_swap_blocked_proxy(vault, monkeypatch):
    _, store = vault
    queue(store, 12)
    seen = []

    def fake_fetch(video_id, proxy=None, timeout=15):
        seen.append((video_id, proxy))
        if proxy == "bad":
            raise Blocked("proxy failure")
        return ok_transcript(video_id), {}

    monkeypatch.setattr(R, "fetch_captions", fake_fetch)
    pool = Pool(["bad", "good1", "good2"])
    r, sink = make_runner(vault, settings(lanes=3), pool=pool)
    stats = r.run()
    assert stats["lanes"] == 3 and stats["fetched"] == 12 and "healthy" in stats["pool"]
    assert ("bad", False, True) in pool.released     # released as blocked
    ids = [s[0] for s in seen if s[1] != "bad"]
    assert sorted(ids) == sorted(vid(i) for i in range(12))  # each video fetched exactly once, nothing lost
    assert {store.get(vid(i)).attempts for i in range(12)} == {0}
    assert len(sink) == 12


def test_no_pool_means_single_lane(vault, monkeypatch):
    _, store = vault
    queue(store, 2)
    monkeypatch.setattr(R, "fetch_captions", lambda v, p=None, timeout=15: (ok_transcript(v), {}))
    stats = make_runner(vault, settings(lanes=12), pool=None)[0].run()
    assert stats["lanes"] == 1 and stats["fetched"] == 2


def test_empty_pool_lane_gives_up_without_spinning(vault, monkeypatch):
    _, store = vault
    queue(store, 2)
    monkeypatch.setattr(R, "fetch_captions", lambda v, p=None, timeout=15: (ok_transcript(v), {}))
    pool = Pool([])
    t0 = time.monotonic()
    stats = make_runner(vault, settings(lanes=3), pool=pool)[0].run()
    assert stats["fetched"] == 2                      # lane 0 did the work alone
    assert time.monotonic() - t0 < 10


def test_pause_flag_stops_claiming(vault, monkeypatch):
    paths, store = vault
    queue(store, 3)
    paths.pause_flag.write_text("1")
    monkeypatch.setattr(R, "fetch_captions", lambda *a, **k: pytest.fail("must not fetch while paused"))
    stats = make_runner(vault)[0].run()
    assert stats["paused"] and stats["fetched"] == 0 and store.counts(SLUG)["queued"] == 3


def test_pause_mid_run(vault, monkeypatch):
    paths, store = vault
    queue(store, 5)

    def fake_fetch(video_id, proxy=None, timeout=15):
        paths.pause_flag.write_text("1")
        return ok_transcript(video_id), {}

    monkeypatch.setattr(R, "fetch_captions", fake_fetch)
    stats = make_runner(vault)[0].run()
    assert stats["fetched"] == 1 and store.counts(SLUG)["queued"] == 4


def test_max_videos(vault, monkeypatch):
    _, store = vault
    queue(store, 6)
    monkeypatch.setattr(R, "fetch_captions", lambda v, p=None, timeout=15: (ok_transcript(v), {}))
    stats = make_runner(vault, settings(lanes=3), pool=Pool(["a", "b"]))[0].run(max_videos=2)
    assert stats["fetched"] == 2 and store.counts(SLUG)["queued"] == 4


def test_stop_event_preset(vault, monkeypatch):
    _, store = vault
    queue(store, 2)
    monkeypatch.setattr(R, "fetch_captions", lambda *a, **k: pytest.fail("stopped"))
    stop = threading.Event()
    stop.set()
    assert make_runner(vault)[0].run(stop=stop)["fetched"] == 0


def test_whisper_upgrade_for_top_videos_only(vault, monkeypatch):
    paths, store = vault
    queue(store, 3, views=[10, 5000, 20])        # vid1 is the top winner
    asr_calls = []
    monkeypatch.setattr(R, "fetch_captions", lambda v, p=None, timeout=15: (ok_transcript(v), {}))
    monkeypatch.setattr(R, "transcribe", lambda v, s, proxy=None, language=None: asr_calls.append(v) or ok_transcript(v, "groq_whisper"))
    r, _ = make_runner(vault, settings(groq_api_key="gsk", whisper_upgrade_top=1))
    stats = r.run()
    assert asr_calls == [vid(1)] and stats["upgraded"] == 1
    assert store.get(vid(1)).transcript_source == "groq_whisper"
    assert store.get(vid(0)).transcript_source == "auto_caption"


def test_whisper_upgrade_failure_keeps_captions(vault, monkeypatch):
    _, store = vault
    queue(store, 1)
    monkeypatch.setattr(R, "fetch_captions", lambda v, p=None, timeout=15: (ok_transcript(v), {}))
    monkeypatch.setattr(R, "transcribe", lambda *a, **k: (_ for _ in ()).throw(Blocked("groq/yt blocked")))
    stats = make_runner(vault, settings(groq_api_key="gsk", whisper_upgrade_top=5))[0].run()
    assert stats["fetched"] == 1 and store.get(vid(0)).transcript_source == "auto_caption"


def test_manual_captions_not_upgraded(vault, monkeypatch):
    _, store = vault
    queue(store, 1)
    monkeypatch.setattr(R, "fetch_captions", lambda v, p=None, timeout=15: (ok_transcript(v, "manual_caption"), {}))
    monkeypatch.setattr(R, "transcribe", lambda *a, **k: pytest.fail("manual captions beat whisper"))
    make_runner(vault, settings(groq_api_key="gsk", whisper_upgrade_top=5))[0].run()
    assert store.get(vid(0)).transcript_source == "manual_caption"


def test_sponsorblock_applied(vault, monkeypatch):
    paths, store = vault
    queue(store, 1)
    t = Transcript(vid(0), [Segment(0, 5, "intro"), Segment(5, 10, "buy my thing"), Segment(10, 15, "real")], "auto_caption")
    monkeypatch.setattr(R, "fetch_captions", lambda v, p=None, timeout=15: (t, {}))
    monkeypatch.setattr(R, "sponsor_spans", lambda v: [(5.0, 10.0)])
    make_runner(vault, settings(sponsorblock=True))[0].run()
    saved = json.load(gzip.open(paths.transcript(vid(0)), "rt", encoding="utf-8"))
    assert [s["text"] for s in saved["segments"]] == ["intro", "real"]
    assert saved["removed_spans"] == [[5.0, 10.0]]


def test_on_fetched_error_does_not_lose_transcript(vault, monkeypatch):
    paths, store = vault
    queue(store, 1)
    monkeypatch.setattr(R, "fetch_captions", lambda v, p=None, timeout=15: (ok_transcript(v), {}))
    r = R.FetchRunner(store, paths, settings(), None, lambda *a: 1 / 0, defer_seconds=0)
    stats = r.run()
    assert stats["fetched"] == 1 and store.get(vid(0)).state == "fetched" and stats["errors"]


def test_unexpected_exception_counts_as_retry(vault, monkeypatch):
    _, store = vault
    queue(store, 1)
    monkeypatch.setattr(R, "fetch_captions", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    stats = make_runner(vault)[0].run()
    assert stats["retried"] == 1 and store.get(vid(0)).state == "queued" and "boom" in store.get(vid(0)).last_error


def test_retries_park_after_max_attempts(vault, monkeypatch):
    _, store = vault
    queue(store, 1)
    monkeypatch.setattr(R, "fetch_captions", lambda *a, **k: (_ for _ in ()).throw(Retryable("net")))
    cfg = settings(max_attempts=2)
    r, _ = make_runner(vault, cfg)
    r.run()
    store.db.execute("UPDATE videos SET next_try_at=0")  # skip the backoff
    r.run()
    assert store.get(vid(0)).state == JobState.PARKED.value
